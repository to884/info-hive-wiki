"""wiki の MCP サーバーと stdio の中継が共有する、置き場所と識別子の決まり（ADR-0012）。

標準ライブラリだけで書く。中継は重い依存を持たないので、ここから numpy などを import しない。

置き場所:
    Windows        %LOCALAPPDATA%/info-hive/
    macOS、Linux   $XDG_DATA_HOME/info-hive/（未設定なら ~/.local/share/info-hive/）
    環境変数 INFO_HIVE_DATA_DIR があれば、それを使う（テストと検証用）。

その下に次を置く。
    server.json        動いているサーバーのポート、トークン、pid、コードの版
    server.lock        サーバーの起動を 1 つにするためのロック
    index/<wiki のハッシュ>/       wiki ごとの索引
    index/collections/<名前>/     index_chunks で受け取るチャンクの索引
    models/            埋め込みモデルのファイル
    logs/              サーバーのログ
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

DATA_DIR_ENV = "INFO_HIVE_DATA_DIR"
# 要求がない状態がこの秒数続いたら、サーバーは自分で止まる。
IDLE_SECONDS_ENV = "INFO_HIVE_MCP_IDLE_SECONDS"
DEFAULT_IDLE_SECONDS = 900
# 中継とサーバーのあいだの取り決めの版。server.json の形やツールの呼び方を変えたら上げる。
PROTOCOL = 1
SCRIPTS = Path(__file__).resolve().parent
# サーバーの版を決めるファイル。中身が変わったら、中継は古いサーバーを止めて起動し直す。
SERVER_FILES = ("wiki_mcp_common.py", "wiki_mcp_server.py", "wiki_index.py", "wiki_search.py", "wiki_text.py",
                "wiki_lint.py", "wiki_stats.py", "wiki_workspace.py", "wiki_graph_common.py", "wiki_graph_query.py",
                "wiki_graph_lint.py", "wiki_graph_extract.py")
COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "info-hive"


def server_json_path() -> Path:
    return data_dir() / "server.json"


def server_lock_path() -> Path:
    return data_dir() / "server.lock"


def logs_dir() -> Path:
    return data_dir() / "logs"


def models_dir() -> Path:
    return data_dir() / "models"


def normalize_wiki_root(wiki: str | os.PathLike) -> Path:
    """wiki のルートを、ハッシュの元にする形にそろえる。存在しなければ FileNotFoundError。"""
    path = Path(unicodedata.normalize("NFC", os.fspath(wiki))).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise NotADirectoryError(f"wiki のルートがディレクトリではない: {path}")
    return path


def wiki_key(root: Path) -> str:
    """wiki ごとの索引のディレクトリ名。ユーザースコープとプロジェクトスコープの wiki をパスで分ける。"""
    text = unicodedata.normalize("NFC", root.as_posix())
    if os.name == "nt" or sys.platform == "darwin":
        # Windows と macOS の既定のファイルシステムは、大文字と小文字を区別しない。
        text = text.casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def wiki_index_dir(root: Path) -> Path:
    return data_dir() / "index" / wiki_key(root)


def collection_dir(name: str) -> Path:
    if not COLLECTION_RE.match(name or ""):
        raise ValueError("collection の名前は、英小文字、数字、_、- の 1〜64 文字で、英小文字か数字で始める")
    return data_dir() / "index" / "collections" / name


def code_version() -> str:
    """サーバーのコードの版。サーバーの起動に関わるファイルの中身のハッシュ。"""
    digest = hashlib.sha256(f"protocol={PROTOCOL}".encode())
    for name in SERVER_FILES:
        try:
            digest.update(name.encode() + b"\0" + (SCRIPTS / name).read_bytes())
        except OSError:
            digest.update(name.encode() + b"\0missing")
    return digest.hexdigest()[:16]


def uv_executable() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        raise RuntimeError("uv が見つからない。wiki の MCP サーバーの起動には uv が要る")
    return found


# uv の環境が壊れているとき、中継とサーバーはこの終了コードで止まる（EX_TEMPFAIL）。
# 起動した側（入口、中継）は、この終了コードを見て環境を入れ直し、1 回だけ起動し直す。
BROKEN_ENV_EXIT = 75
# 中継とサーバーが起動の最初に import する外部の依存（require_env）。共有ライブラリが読めないことも見つける。
RELAY_MODULES = ("anyio", "httpx", "filelock", "psutil", "mcp", "mcp.types", "mcp.server.lowlevel",
                 "mcp.server.stdio", "mcp.client.session", "mcp.client.streamable_http")
SERVER_MODULES = ("anyio", "uvicorn", "numpy", "usearch.index", "tantivy", "yaml", "mcp.server.fastmcp",
                  "mcp.server.fastmcp.exceptions", "mcp.server.transport_security")
CHECK_IMPORTS_ARG = "--check-imports"


def _listed_files(root: Path) -> dict[str, os.DirEntry]:
    """root の下の通常のファイル。鍵は root からの相対パス（/ 区切り、大文字と小文字はそのまま）。

    os.scandir でディレクトリを 1 回ずつなめる。Windows では、大きさをディレクトリの一覧から読めるので、
    ファイルを 1 つずつ開かない。ファイルごとに stat() すると、サーバーの環境（約 3,800 ファイル）では
    ウイルス対策の検査も重なって数分かかることがあった。シンボリックリンクは載せず（ディレクトリもたどらない）、
    __pycache__ もなめない。載らなかったファイルは、照らすときに 1 つずつ確かめる。
    """
    found: dict[str, os.DirEntry] = {}
    stack = [(os.fspath(root), "")]
    while stack:
        folder, prefix = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name != "__pycache__":
                                stack.append((entry.path, f"{prefix}{entry.name}/"))
                        elif entry.is_file(follow_symlinks=False):
                            found[prefix + entry.name] = entry
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _listed_size(listed: dict[str, os.DirEntry], name: str) -> int | None:
    """RECORD のパス name の大きさを、ディレクトリの一覧から読む。一覧で決められなければ None。

    .. や . を含むパス（site-packages の外の Scripts/ など）は、シンボリックリンクをはさむと文字列の上の
    正規化と実際の場所が食い違うので、一覧を使わない。
    """
    parts = name.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    entry = listed.get("/".join(parts))
    if entry is None:
        return None
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None


def env_problems(limit: int = 10) -> list[str]:
    """この python の環境で、dist-info の RECORD に載っているのに欠けているか、大きさの違うファイル。

    依存の中には、実行中に初めて import するモジュール（anyio のバックエンド、uvicorn の lifespan など）が
    あるので、決まったモジュールを import するだけでは欠けを見逃す。入れたファイルの一覧（RECORD）と照らす。
    中身のハッシュは比べない（時間がかかる）。RECORD がない dist-info も壊れたものとみなす。

    ファイルの大きさは、site-packages を os.scandir で 1 回なめた一覧から読む（_listed_files）。一覧で決められない
    ファイル（site-packages の外の Scripts/ など、シンボリックリンク、大文字と小文字の違う名前）と、一覧の大きさが
    RECORD と違うファイルだけを、1 つずつ stat() で確かめ直す。欠けたファイルは、従来どおりすべて見つかる。
    Windows の一覧の大きさは、そのファイルを別のハードリンクから書き換えると古いまま残ることがある。
    uv は環境のファイルをその場で書き換えない（入れ直すときは消してから置く）ので、この確かめでは扱わない。
    """
    import csv
    import sysconfig
    paths = sysconfig.get_paths()
    problems: list[str] = []
    for root in sorted({Path(paths["purelib"]), Path(paths["platlib"])}):
        on_disk = _listed_files(root)
        for dist in sorted(root.glob("*.dist-info")):
            record = dist / "RECORD"
            try:
                rows = list(csv.reader(record.read_text(encoding="utf-8", errors="replace").splitlines()))
            except OSError:
                problems.append(f"{dist.name}/RECORD がない")
                rows = []
            else:
                # 正しい RECORD は、自分自身と METADATA の行を必ず含む。ない RECORD は、書く途中で止まったものとみなす。
                listed = {row[0].replace("\\", "/") for row in rows if row}
                if f"{dist.name}/RECORD" not in listed or f"{dist.name}/METADATA" not in listed:
                    problems.append(f"{dist.name}/RECORD が途中で切れている")
            for row in rows:
                if not row or not row[0] or row[0].endswith(".pyc"):
                    continue
                expected = int(row[2]) if len(row) > 2 and row[2].isdigit() else None
                size = _listed_size(on_disk, row[0])
                if size is None or (expected is not None and size != expected):
                    try:
                        size = (root / row[0]).stat().st_size
                    except OSError:
                        problems.append(f"{row[0]} がない")
                        size = None
                if size is not None and expected is not None and size != expected:
                    problems.append(f"{row[0]} の大きさ（{size}）が RECORD（{row[2]}）と違う")
                if len(problems) >= limit:
                    return problems
    return problems


def require_env(modules) -> None:
    """uv の環境がそろっているかを確かめる。壊れていれば理由を標準エラーに書き、BROKEN_ENV_EXIT で止まる。

    uv が依存を入れている途中で止められると、環境が作りかけのまま残る。dist-info の METADATA が欠けると
    `uv sync` が失敗するが、モジュールの本体だけが欠けると `uv sync` は成功してしまう。そのため、
    標準入力を読む前に、RECORD と照らし（env_problems）、modules を import して確かめる。
    import の失敗のうち BROKEN_ENV_EXIT にするのは ImportError だけである（メモリ不足などは入れ直しても直らない）。
    """
    import importlib
    problems = env_problems()
    if problems:
        print(f"[wiki] uv の環境が壊れているかもしれない（{sys.prefix}）: " + "、".join(problems[:5]),
              file=sys.stderr, flush=True)
        sys.exit(BROKEN_ENV_EXIT)
    for name in modules:
        try:
            module = importlib.import_module(name)
            # パッケージの __init__.py が欠けると、名前空間パッケージとして import できてしまう（__file__ がない）。
            if getattr(module, "__file__", None) is None:
                raise ImportError(f"{name} の本体（__init__.py）がない")
        except ImportError as exc:  # 共有ライブラリを読めないときも ImportError になる
            print(f"[wiki] 依存の {name} を import できない（{type(exc).__name__}: {exc}）。uv の環境が壊れているかもしれない",
                  file=sys.stderr, flush=True)
            sys.exit(BROKEN_ENV_EXIT)


class FileLock:
    """標準ライブラリだけのファイルのロック（Windows は msvcrt、POSIX は fcntl）。"""

    def __init__(self, path: Path, timeout: float):
        self.path, self.timeout, self._handle = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")  # noqa: SIM115 - __exit__ で閉じる
        ends = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > ends:
                    self._handle.close()
                    raise RuntimeError(f"{self.path.name} のロックを {self.timeout:.0f} 秒以内に取れなかった") from None
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()


def env_lock_path(script: Path) -> Path:
    """script の uv の環境を入れ直すときのロック。同じ環境を使うセッションが同じロックを使うよう、環境の
    置き場所である uv のキャッシュ（`uv cache dir`。UV_CACHE_DIR も反映される）の下に置く。一時ディレクトリは
    ホストやセッションごとに違うことがあり、Linux では利用者のあいだで共有されることもあるので使わない。"""
    found = subprocess.run([uv_executable(), "cache", "dir"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=120, env={**os.environ, "PYTHONUTF8": "1"})
    lines = found.stdout.decode("utf-8", "replace").strip().splitlines()
    if found.returncode != 0 or not lines:
        detail = found.stderr.decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"uv のキャッシュの場所が分からない（uv cache dir の終了コード {found.returncode}）: {detail}")
    key = os.path.normcase(str(Path(script).resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(lines[-1]) / "info-hive-locks" / f"env-{Path(script).stem}-{digest}.lock"


# uv に OS の証明書ストアを使わせる環境変数。UV_NATIVE_TLS は古い名前で、古い uv はこちらしか読まない。
# どちらかがあれば利用者が決めたものとみなす。
SYSTEM_CERTS_ENV = "UV_SYSTEM_CERTS"
NATIVE_TLS_ENV = "UV_NATIVE_TLS"
_TLS_USER_ENVS = (SYSTEM_CERTS_ENV, NATIVE_TLS_ENV)
# uv が TLS 証明書を検証できなかったときの出力の句（小文字で比べる）。uv の版で文言が変わりうるので、いくつかを見る。
# `--system-certs` のような短い断片は、パスなどに含まれて誤判定しうるので使わない。誤判定すると、壊れた環境を
# 入れ直さずに止まってしまう。
_TLS_ERROR_MARKERS = ("invalid peer certificate", "certificate verify failed", "use of system tls certificates",
                      "use of native tls")


class UvTlsError(RuntimeError):
    """uv が TLS 証明書を検証できず、依存を取得できなかった。環境を入れ直しても直らない。"""


def is_tls_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TLS_ERROR_MARKERS)


def tls_error_message(script: Path, user_setting: str | None) -> str:
    tried = (f"{user_setting} が設定されているので、そのまま止まる。" if user_setting else
             "OS の証明書ストアを使ってやり直しても失敗した。")
    return (f"uv が {script.name} の依存を取得するときに、TLS 証明書を検証できなかった。社内ネットワークなどで、"
            "通信を検査するプロキシが OS の証明書ストアにだけある CA で証明書を差し替えていると起きる。"
            f"{tried}対処: 環境変数 {SYSTEM_CERTS_ENV}=true（古い uv では {NATIVE_TLS_ENV}=true）を設定するか、"
            "uv の設定ファイル（Windows は "
            "%APPDATA%\\uv\\uv.toml、macOS と Linux は ~/.config/uv/uv.toml。XDG_CONFIG_HOME があれば "
            "$XDG_CONFIG_HOME/uv/uv.toml）に system-certs = true（古い uv では native-tls = true）を書く。"
            "CA が OS の証明書ストアにもないときは、環境変数 SSL_CERT_FILE に CA の証明書のファイルを指定する。"
            "設定したら、ホスト（Claude Code など）を起動し直す。")


def _write_output(output, data: bytes) -> None:
    """取り込んだ uv の出力を output に書く。output は標準エラー（テキスト）か、ログのファイル（バイナリ）。"""
    if not data or output is None:
        return
    if isinstance(output, io.TextIOBase):
        output.write(data.decode("utf-8", "replace"))
    else:
        output.write(data)
    output.flush()


def record_failure(message: str) -> None:
    """失敗の理由を logs/launch.log に残す。ホストは標準エラーを利用者に見せないことがある（Claude Code は
    CONNECTION_CLOSED とだけ表示する）ので、利用者が後から読める場所に書く。書けなくても止まらない。"""
    try:
        logs_dir().mkdir(parents=True, exist_ok=True)
        with open(logs_dir() / "launch.log", "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _run_uv(command: list[str], env: dict, output, timeout: float) -> tuple[int, str]:
    try:
        out = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        # 待ちきれなかったときも、それまでの出力は残す。
        _write_output(output, exc.stdout or exc.output or b"")
        raise
    _write_output(output, out.stdout)
    return out.returncode, out.stdout.decode("utf-8", "replace")


def system_certs_env(timeout: float = 60) -> str:
    """この uv が OS の証明書ストアを使う設定として読む環境変数の名前。

    新しい uv は UV_SYSTEM_CERTS を読み、UV_NATIVE_TLS には非推奨の警告を出す。古い uv は UV_SYSTEM_CERTS を
    知らずに黙って無視し、UV_NATIVE_TLS だけを読む。`uv sync --help` が UV_SYSTEM_CERTS を挙げるかで決める。
    """
    try:
        found = subprocess.run([uv_executable(), "sync", "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"})
    except (OSError, subprocess.SubprocessError):
        return SYSTEM_CERTS_ENV
    text = found.stdout.decode("utf-8", "replace")
    return NATIVE_TLS_ENV if found.returncode == 0 and SYSTEM_CERTS_ENV not in text and NATIVE_TLS_ENV in text         else SYSTEM_CERTS_ENV


def _uv_sync(script: Path, *, reinstall: bool, output, timeout: float, log=None) -> int:
    """`uv sync --script` の終了コードを返す。TLS 証明書を検証できずに失敗したときは、利用者が
    UV_SYSTEM_CERTS（または UV_NATIVE_TLS）を決めていなければ、OS の証明書ストアを使って 1 回だけやり直す。
    環境変数の名前は uv の版で選ぶ（system_certs_env）。

    uv は既定で同梱の証明書を使い、OS の証明書ストアを見ない。TLS を検査する社内ネットワークでは、PyPI への
    通信の証明書が OS にだけある CA で署名されているので、既定のままでは必ず失敗する。OS の証明書ストアを
    いつも使わないのは、uv がそちらを遅いとしている（特に macOS）ためである。やり直しで直ったら、この
    プロセスの環境変数にも入れ、以後の uv の呼び出しと子のプロセス（中継が用意するサーバーの環境）で使う。
    それでも証明書の検証に失敗したら UvTlsError にする。環境を入れ直しても直らないので、入れ直しに進まない。
    """
    command = [uv_executable(), "sync", "--quiet", *(["--reinstall"] if reinstall else []), "--script", str(script)]
    env = {**os.environ, "PYTHONUTF8": "1"}
    code, text = _run_uv(command, env, output, timeout)
    if code == 0 or not is_tls_error(text):
        return code
    user_setting = next((f"{name}={os.environ[name]}" for name in _TLS_USER_ENVS
                         if os.environ.get(name, "").strip()), None)
    if user_setting is None:
        name = system_certs_env()
        if log:
            log(f"uv が TLS 証明書を検証できなかったので、OS の証明書ストアを使ってやり直す（{name}=true）")
        code, text = _run_uv(command, {**env, name: "true"}, output, timeout)
        if code == 0 or not is_tls_error(text):
            os.environ[name] = "true"
            return code
    message = tls_error_message(script, user_setting)
    # 中継がサーバーの環境を用意するときも、利用者が読む場所（launch.log）に残す。
    record_failure(f"UvTlsError: {message}")
    raise UvTlsError(message)


def _imports_ok(python: str, script: Path, output, timeout: float) -> bool:
    """環境がそろっていれば真、壊れていれば（BROKEN_ENV_EXIT）偽。それ以外の失敗は例外にする。

    メモリ不足などで確かめのプロセスが落ちたときに入れ直すと、先にほかのセッションが直して使っている
    環境まで入れ直してしまう。入れ直して直るのは、壊れた環境だけである。
    """
    out = subprocess.run([python, "-X", "utf8", str(script), CHECK_IMPORTS_ARG], stdin=subprocess.DEVNULL,
                         stdout=output, stderr=output, timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"})
    if out.returncode in (0, BROKEN_ENV_EXIT):
        return out.returncode == 0
    raise RuntimeError(f"{script.name} の環境を確かめられなかった（{CHECK_IMPORTS_ARG} の終了コード {out.returncode}）")


def prepare_script(script: Path, *, output, timeout: float, log, force: bool = False) -> str:
    """script の inline の依存を入れた uv の環境を用意し、その python のパスを返す。uv の出力は output に書く。

    uv が環境に依存を入れている途中で止められると（ホストがセッションの終わりにプロセスを止めるなど）、
    環境が作りかけのまま残る。dist-info の METADATA が欠けると `uv sync` が失敗し続け（uv 0.12）、
    モジュールの本体だけが欠けると `uv sync` は成功するのに import に失敗する。

    - ふだん（force が偽）: `uv sync --script` が成功すれば、そのまま返す。速い。
    - `uv sync` が失敗したとき、または force が真（起動したスクリプトが BROKEN_ENV_EXIT で止まった）のとき:
      スクリプトごとのロックを取り、ほかのセッションが先に直していないかを `uv sync` と import の確認
      （`<script> --check-imports`。RECORD との照合と import）で確かめる。直っていなければ `--reinstall` で入れ直し、もう一度確かめる。
      ロックの中で確かめ直すので、2 つのセッションが同時に入れ直して、動いている環境を壊すことはない。
    - uv が TLS 証明書を検証できなかったとき（_uv_sync）: 環境は壊れていないので入れ直さず、UvTlsError を送り出す。
    """
    if not force and _uv_sync(script, reinstall=False, output=output, timeout=timeout, log=log) == 0:
        return script_python(script)
    with FileLock(env_lock_path(script), timeout=timeout):
        if _uv_sync(script, reinstall=False, output=output, timeout=timeout, log=log) == 0:
            python = script_python(script)
            if _imports_ok(python, script, output, timeout):
                return python
        log(f"{script.name} の環境が作りかけで壊れているかもしれないので、入れ直す")
        code = _uv_sync(script, reinstall=True, output=output, timeout=timeout, log=log)
        if code != 0:
            raise RuntimeError(f"{script.name} の環境を入れ直せなかった（uv sync --reinstall の終了コード {code}）")
        python = script_python(script)
        if not _imports_ok(python, script, output, timeout):
            raise RuntimeError(f"{script.name} の環境を入れ直しても、依存を import できない")
        return python


def script_python(script: Path, timeout: float = 120) -> str:
    """prepare_script で用意した環境の python のパス。"""
    found = subprocess.run([uv_executable(), "python", "find", "--script", str(script)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"})
    if found.returncode != 0:
        detail = found.stderr.decode("utf-8", "replace").strip()[-800:]
        raise RuntimeError(f"{script.name} の環境の python を見つけられなかった（uv python find の終了コード "
                           f"{found.returncode}）: {detail}")
    lines = found.stdout.decode("utf-8", "replace").strip().splitlines()
    path = lines[-1] if lines else ""
    if not path or not Path(path).is_file():
        raise RuntimeError(f"{script.name} の環境の python が見つからない: {path!r}")
    return path


def idle_seconds() -> float:
    try:
        value = float(os.environ.get(IDLE_SECONDS_ENV, "") or DEFAULT_IDLE_SECONDS)
    except ValueError:
        return DEFAULT_IDLE_SECONDS
    return value if value > 0 else DEFAULT_IDLE_SECONDS


def read_server_info() -> dict | None:
    try:
        info = json.loads(server_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not all(k in info for k in ("port", "token", "pid")):
        return None
    return info


def write_server_info(info: dict) -> None:
    """server.json を一時ファイルに書いてから置き換える。トークンを含むので、POSIX では本人だけが読めるようにする。"""
    path = server_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"server.json.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(info, handle, ensure_ascii=False)
    os.replace(temporary, path)


def remove_server_info(pid: int) -> None:
    """自分の pid が書かれているときだけ server.json を消す。後から起動したサーバーの分は消さない。"""
    info = read_server_info()
    if info and info.get("pid") == pid:
        try:
            server_json_path().unlink()
        except OSError:
            pass
