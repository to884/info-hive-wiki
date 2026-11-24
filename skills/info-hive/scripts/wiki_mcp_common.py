"""wiki の MCP サーバーと stdio の中継が共有する、置き場所と識別子の決まり（ADR-0012）。

標準ライブラリだけで書く。中継は重い依存を持たないので、ここから numpy などを import しない。

置き場所:
    Windows        %LOCALAPPDATA%/info-hive/
    macOS、Linux   $XDG_DATA_HOME/info-hive/（未設定なら ~/.local/share/info-hive/）
    環境変数 INFO_HIVE_DATA_DIR があれば、それを使う（テストと検証用）。

その下に次を置く。
    server.json        動いているサーバーのポート、トークン、pid、コードの版
    server.lock        サーバーの起動を 1 つにするためのロック
    server-info.lock   サーバーの記録の読み取り、公開、削除を直列化するロック
    server-start.json  起動を許可された試行の識別子（中継が起動を待つ間だけ使う）
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
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

DATA_DIR_ENV = "INFO_HIVE_DATA_DIR"
START_ID_ENV = "INFO_HIVE_MCP_START_ID"
# 要求がない状態がこの秒数続いたら、サーバーは自分で止まる。
IDLE_SECONDS_ENV = "INFO_HIVE_MCP_IDLE_SECONDS"
DEFAULT_IDLE_SECONDS = 900
# 中継とサーバーのあいだの取り決めの版。server.json の形やツールの呼び方を変えたら上げる。
PROTOCOL = 1
SCRIPTS = Path(__file__).resolve().parent
# サーバーの版を決めるファイル。中身が変わったら、中継は古いサーバーを止めて起動し直す。
SERVER_FILES = ("wiki_mcp_common.py", "wiki_mcp_server.py", "wiki_index.py", "wiki_search.py", "wiki_tokens.py",
                "wiki_text.py",
                "wiki_pages.py", "wiki_lint.py", "wiki_stats.py", "wiki_workspace.py", "wiki_initialized.py",
                "wiki_graph_common.py", "wiki_graph_query.py",
                "wiki_graph_lint.py", "wiki_graph_extract.py",
                # ADR-0029 の実装で足した。サーバーが関数の中で遅れて読み込むものも入れる（MCP の lint が wiki_captions を、
                # それが閲覧の中核を、検索が判定のレイヤーを読み込む）。足りているかは tests/wiki/test_wiki_links.py が確かめる
                "wiki_overview.py", "wiki_links.py", "wiki_raw.py", "wiki_decide.py", "wiki_captions.py", "wiki_browse_core.py", "wiki_browse_fs.py",
                "wiki_browse_highlight.py",
                "wiki_fetch.py", "wiki_rawcheck.py", "wiki_youtube_url.py", "wiki_fsguard.py", "wiki_raw_roots.py",
                "wiki_apply.py", "wiki_live_apply.py", "wiki_public_save.py", "wiki_public_projection.py",
                "wiki_semantic_format.py", "wiki_semantic_model.py",
                # P3 の形式の遷移の照合。公開の書く段が遅れて読み込む。MCP から意味の書込みは公開しない
                "wiki_semantic_apply.py", "wiki_semantic_definitions.py", "wiki_semantic_store.py",
                "wiki_semantic_adopt.py", "wiki_semantic_graph.py", "wiki_semantic_projection.py",
                "wiki_semantic_migrate.py", "wiki_ossie_contract.py",
                "wiki_live_return.py", "wiki_republish.py",
                "wiki_restore.py", "wiki_restore_content.py", "wiki_restore_raw.py")
COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def data_dir(environ=None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = env.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else _data_home(environ) / "AppData" / "Local"
    else:
        base = env.get("XDG_DATA_HOME", "").strip()
        root = Path(base) if base else _data_home(environ) / ".local" / "share"
    return root / "info-hive"


def _data_home(environ) -> Path:
    if environ is None:
        return Path.home()
    if os.name == "nt":
        home = environ.get("USERPROFILE")
        if home is None and "HOMEPATH" in environ:
            home = os.path.join(environ.get("HOMEDRIVE", ""), environ["HOMEPATH"])
    else:
        home = environ.get("HOME")
    if not home:
        raise ValueError("プロセスのホームディレクトリを環境から確認できない")
    return Path(home)


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
    # 同梱した Markdown の解析器（_vendor/）の中身も含める。markdown-it-py や mdurl だけを入れ替えても、
    # 動いているサーバーが古い解析器のまま使われないようにする。
    vendor = SCRIPTS / "_vendor"
    if vendor.is_dir():
        for path in sorted(vendor.rglob("*"), key=lambda p: p.relative_to(vendor).as_posix()):
            if path.is_file() and "__pycache__" not in path.parts:
                try:
                    digest.update(b"_vendor/" + path.relative_to(vendor).as_posix().encode() + b"\0" + path.read_bytes())
                except OSError:
                    digest.update(b"_vendor/" + path.relative_to(vendor).as_posix().encode() + b"\0missing")
    return digest.hexdigest()[:16]


def uv_executable() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        raise RuntimeError("uv が見つからない。wiki の MCP サーバーの起動には uv が要る")
    return found


# Windows で子プロセスを起動するときの旗（info-hive-wiki#36）。MCP の経路（入口、中継、サーバー）の子プロセスの
# 起動は、すべて次の 2 つのどちらかを creationflags に渡す（tests/wiki/test_mcp_spawn_flags.py）。
# コンソールを持たないプロセスがコンソール用のプログラムを起動すると、新しいコンソールが作られ、画面（既定の
# ターミナル）が出る。CREATE_NO_WINDOW なら、子は窓のないコンソールを持ち、その子孫はそれを引き継ぐ。
# ただし、子が親と別のコンソールに入ると、親から受け取ったコンソールの標準ハンドルに書けない。

def has_console() -> bool:
    """このプロセスがコンソールを持つか（窓のないコンソールも含む）。Windows 以外では偽。"""
    if os.name != "nt":
        return False
    import ctypes
    processes = (ctypes.c_uint32 * 1)()
    return ctypes.WinDLL("kernel32").GetConsoleProcessList(processes, 1) > 0


def child_creationflags() -> int:
    """標準入出力を受け渡す子プロセスの旗。コンソールがあれば引き継ぎ（0）、なければ窓のないコンソールを作る。"""
    if os.name != "nt" or has_console():
        return 0
    return subprocess.CREATE_NO_WINDOW


def server_creationflags() -> int:
    """サーバーの旗。中継とは別の、窓のない自分のコンソールと新しいプロセスグループで起動する。

    中継やホストのコンソールを閉じたことも、そこでの Ctrl+C も、サーバーには届かない。DETACHED_PROCESS は使わない。
    コンソールのない venv の python.exe が本体の python.exe を起動すると、本体に新しいコンソールが作られ、画面が
    出る。DETACHED_PROCESS と併せると CREATE_NO_WINDOW は無視される。サーバーの標準入出力はファイルにつなぐ。
    """
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


# uv の環境が壊れているとき、中継とサーバーはこの終了コードで止まる（EX_TEMPFAIL）。
# 起動した側（入口、中継）は、この終了コードを見て環境を入れ直し、1 回だけ起動し直す。
BROKEN_ENV_EXIT = 75
# Windows で、依存の共有ライブラリ（DLL）を読めないとき、中継とサーバーはこの終了コードで止まる。
# 素の Windows にない Visual C++ 再頒布可能パッケージが原因で、環境を入れ直しても直らない。
# 起動した側は、BROKEN_ENV_EXIT と違い、入れ直さずに利用者へ対処を伝えて止まる。
RUNTIME_LIB_EXIT = 76
VC_REDIST_COMMAND = "winget install --id Microsoft.VCRedist.2015+.x64 -e --source winget"
# 中継とサーバーが起動の最初に import する外部の依存（require_env）。共有ライブラリが読めないことも見つける。
RELAY_MODULES = ("anyio", "httpx", "psutil", "mcp", "mcp.types", "mcp.server.lowlevel",
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


def is_dll_load_error(exc: BaseException, windows: bool | None = None) -> bool:
    """Windows で、依存の DLL を読めなかった ImportError か。CPython はこの文言を OS の言語によらず英語で付ける
    （`DLL load failed while importing <module>: <OS のメッセージ>`）。"""
    if not (os.name == "nt" if windows is None else windows):
        return False
    return isinstance(exc, ImportError) and "DLL load failed" in str(exc)


VC_RUNTIME_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")


def missing_vc_runtime_dlls(system_dir: Path | None = None) -> list[str]:
    """システムのディレクトリ（System32）にない Visual C++ ランタイムの DLL の名前。

    `DLL load failed` は、拡張ファイルの形式が不正なときなど、ランタイム不足以外でも出る。案内を出して
    入れ直しをやめるのは、不足を実際に確かめられたときだけにする。Windows 以外では空を返す。
    """
    if system_dir is None:
        if os.name != "nt":
            return []
        system_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return [name for name in VC_RUNTIME_DLLS if not (system_dir / name).is_file()]


def vc_runtime_message(missing: list[str] | None = None) -> str:
    names = "、".join(missing) if missing else "、".join(VC_RUNTIME_DLLS)
    return (f"Visual C++ 再頒布可能パッケージ（{names}）が必要である。"
            f"次を実行して入れ、Claude Code を再起動する: {VC_REDIST_COMMAND}")


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
            missing = missing_vc_runtime_dlls() if is_dll_load_error(exc) else []
            if missing:
                # ランタイム不足を確かめられた。入れ直しても直らない。BROKEN_ENV_EXIT にすると、起動した側が
                # 無駄に入れ直す。確かめられない DLL の失敗は、従来どおり壊れた環境として入れ直しに任せる。
                print(f"[wiki] 依存の {name} の DLL を読み込めない（{type(exc).__name__}: {exc}）。"
                      + vc_runtime_message(missing), file=sys.stderr, flush=True)
                sys.exit(RUNTIME_LIB_EXIT)
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
                           timeout=120, env={**os.environ, "PYTHONUTF8": "1"}, creationflags=child_creationflags())
    lines = found.stdout.decode("utf-8", "replace").strip().splitlines()
    if found.returncode != 0 or not lines:
        detail = found.stderr.decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"uv のキャッシュの場所が分からない（uv cache dir の終了コード {found.returncode}）: {detail}")
    key = os.path.normcase(str(Path(script).resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(lines[-1]) / "info-hive-locks" / f"env-{Path(script).stem}-{digest}.lock"


# uv に OS の証明書ストアを使わせる環境変数。UV_NATIVE_TLS は古い名前で、新しい uv も読むが、古い uv は
# こちらしか読まない。利用者が決めたものとみなすのは、この uv が読む設定だけである（_user_tls_setting）。
SYSTEM_CERTS_ENV = "UV_SYSTEM_CERTS"
NATIVE_TLS_ENV = "UV_NATIVE_TLS"
# uv が真と読む値（clap の BoolishValueParser と同じ。大文字と小文字は区別しない）。
_TRUTHY_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
# uv が TLS 証明書を検証できなかったときの出力の句（小文字で比べる）。uv の版で文言が変わりうるので、いくつかを見る。
# `--system-certs` のような短い断片は、パスなどに含まれて誤判定しうるので使わない。誤判定すると、壊れた環境を
# 入れ直さずに止まってしまう。
_TLS_ERROR_MARKERS = ("invalid peer certificate", "certificate verify failed", "use of system tls certificates",
                      "use of native tls")


# uv が依存を解決できなかったときの、誤りの行の書き出し（行の頭で、小文字で比べる）。使う Python に合う配布物
# （wheel）がない、版の固定どうしが食い違う、など。環境は作られていないので、入れ直しても直らない。行の頭で
# 比べるのは、パスなどに同じ句が含まれても誤判定しないためである。誤判定すると、入れ直せば直る壊れた環境を
# 入れ直さずに止まってしまう。uv の誤りの書き方は版で違う。0.12 までは木の形（`× 見出し` と `╰─▶ 理由`）、
# 0.13 からは `error: 見出し` と `cause: 理由` である（uv 0.7.22、0.12.2、0.13.8 の実際の出力で確かめた）。
_RESOLVE_ERROR_PREFIXES = ("error: no solution found when resolving", "× no solution found when resolving")
# uv の出力の要約に使う行の書き出し。どちらの書き方の見出し、理由、手がかりも拾う。
_UV_DIAGNOSTIC_PREFIXES = ("error:", "cause:", "hint:", "×", "╰─▶", "├─▶", "help:")


class UvSetupError(RuntimeError):
    """uv が環境を用意できなかった。環境を入れ直しても直らない失敗で、_uv_sync が launch.log に記録済み。"""


class UvTlsError(UvSetupError):
    """uv が TLS 証明書を検証できず、依存を取得できなかった。環境を入れ直しても直らない。"""


class UvResolveError(UvSetupError):
    """uv が依存を解決できなかった（使う Python に合う配布物がない、など）。環境を入れ直しても直らない。"""


class VcRuntimeError(UvSetupError):
    """Windows に Visual C++ 再頒布可能パッケージがなく、依存の DLL を読めない。環境を入れ直しても直らない。"""


def raise_vc_runtime_error(prefix: str = "") -> None:
    """VcRuntimeError を launch.log に記録して送り出す。理由と対処（vc_runtime_message）を含める。"""
    message = prefix + vc_runtime_message()
    record_failure(f"VcRuntimeError: {message}")
    raise VcRuntimeError(message)


def is_tls_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TLS_ERROR_MARKERS)


def is_resolve_error(text: str) -> bool:
    return any(line.strip().lower().startswith(_RESOLVE_ERROR_PREFIXES) for line in text.splitlines())


def uv_error_summary(text: str, limit: int = 600) -> str:
    """uv の出力のうち、誤りの見出しと理由と手がかりを、出た順に並べた要約。木の形の出力では、理由の文が
    字下げした次の行に折り返されるので、続きの行もつなぐ。そうした行がなければ、最後の空でない行。
    launch.log に、終了コードだけでなく理由を残すために使う。"""
    entries: list[str] = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            current = None
        elif line.lower().startswith(_UV_DIAGNOSTIC_PREFIXES):
            entries.append(line)
            current = len(entries) - 1
        elif current is not None and raw[:1].isspace():
            entries[current] += " " + line
        else:
            current = None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = " / ".join(entries[:6] or lines[-1:])
    return summary if len(summary) <= limit else summary[:limit] + "…"


def resolve_error_message(script: Path, text: str) -> str:
    detail = uv_error_summary(text)
    return (f"uv が {script.name} の依存を解決できなかった。使う Python の版に合う配布物（wheel）がない、"
            "版の固定どうしが食い違う、などで起きる。環境を入れ直しても直らないので、入れ直さずに止まる。"
            + (f"uv の出力: {detail} " if detail else "")
            + "対処: プラグインを新しい版に更新する。直らなければ、uv の出力をそえて報告する。")


def tls_error_message(script: Path, user_setting: str | None, ignored_setting: str | None = None,
                      reads_system_certs=True) -> str:
    """user_setting は、uv が使う利用者の設定。ignored_setting は、利用者が設定したが uv に届いていない
    設定（古い uv の UV_SYSTEM_CERTS=true）。reads_system_certs は uv_reads_system_certs の結果（None は
    確かめられなかった）。"""
    if user_setting:
        tried = f"{user_setting} が設定されているので、そのまま止まる。"
    elif ignored_setting and reads_system_certs is False:
        tried = (f"{ignored_setting} が設定されているが、この uv は古く、{SYSTEM_CERTS_ENV} を読まない。"
                 f"{NATIVE_TLS_ENV}=true で OS の証明書ストアを使ってやり直しても失敗した。uv を新しくする"
                 f"（uv self update）と、{SYSTEM_CERTS_ENV} を読むようになる。")
    elif reads_system_certs is None:
        tried = ((f"{ignored_setting} が設定されているが、" if ignored_setting else "")
                 + f"この uv が {SYSTEM_CERTS_ENV} を読むかを確かめられなかった（uv sync --help が動かなかったか、"
                 "どちらの名前も挙げなかった）。"
                 f"{SYSTEM_CERTS_ENV}=true と {NATIVE_TLS_ENV}=true で OS の証明書ストアを使ってやり直しても"
                 "失敗した。")
    else:
        tried = "OS の証明書ストアを使ってやり直しても失敗した。"
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
        files = ServerFiles()
        with files.open_log("launch.log") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n".encode("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[wiki] 起動ログを書けない。{exc}", file=sys.stderr, flush=True)


def _run_uv(command: list[str], env: dict, output, timeout: float) -> tuple[int, str]:
    try:
        out = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, env=env, creationflags=child_creationflags())
    except subprocess.TimeoutExpired as exc:
        # 待ちきれなかったときも、それまでの出力は残す。launch.log に添えるため、例外の注記にも要約を入れる。
        partial = exc.stdout or exc.output or b""
        _write_output(output, partial)
        summary = uv_error_summary(uv_text(partial))
        if summary:
            exc.add_note(f"uv の出力: {summary}")
        raise
    _write_output(output, out.stdout)
    return out.returncode, uv_text(out.stdout)


# 端末の色や太字の指定（ANSI のエスケープ）。uv は FORCE_COLOR などで、パイプにも色を付けることがある。
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def uv_text(data: bytes | str) -> str:
    """uv の出力を、見分けと要約に使う文字列にする。色の指定を除く（`error:` が行の頭に来るように）。"""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return _ANSI_ESCAPE.sub("", text)


def uv_reads_system_certs(timeout: float = 60) -> bool | None:
    """この uv が UV_SYSTEM_CERTS を読むなら真、読まない古い uv なら偽、確かめられなければ None。

    新しい uv は UV_SYSTEM_CERTS を読み、UV_NATIVE_TLS には非推奨の警告を出す。古い uv は UV_SYSTEM_CERTS を
    知らずに黙って無視し、UV_NATIVE_TLS だけを読む。`uv sync --help` がどちらの名前を挙げるかで決める。
    `uv sync --help` が動かない、または終了コードが 0 でないときは、版を推し量らずに None を返す。
    どちらかの版とみなすと、外れたときに、やり直しを止めるか、効かない名前でやり直してしまう。
    """
    try:
        found = subprocess.run([uv_executable(), "sync", "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"},
                               creationflags=child_creationflags())
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0:
        return None
    text = found.stdout.decode("utf-8", "replace")
    if SYSTEM_CERTS_ENV in text:
        return True
    if NATIVE_TLS_ENV in text:
        return False
    return None


def system_certs_envs(reads_system_certs: bool | None) -> tuple[str, ...]:
    """OS の証明書ストアを使わせるために、真にする環境変数の名前。版が分からなければ両方にする
    （新しい uv は UV_SYSTEM_CERTS を、古い uv は UV_NATIVE_TLS を読み、もう片方は効かないだけである）。"""
    if reads_system_certs is True:
        return (SYSTEM_CERTS_ENV,)
    if reads_system_certs is False:
        return (NATIVE_TLS_ENV,)
    return (SYSTEM_CERTS_ENV, NATIVE_TLS_ENV)


# _user_tls_setting が uv の版を確かめなかったことを表す。
_UNCHECKED = object()


def _user_tls_setting():
    """利用者の証明書の設定を (この uv が使う設定, この uv に届いていない設定, uv の版) で返す。
    設定は "NAME=値" の文。uv の版は uv_reads_system_certs の結果で、確かめなかったときは _UNCHECKED。

    利用者が決めたとみなすのは、uv が実際に使う設定だけである。uv の読み方は次のとおり。
    - 新しい uv: UV_SYSTEM_CERTS を読む。UV_NATIVE_TLS も読むが、両方あるときの扱いは版で違う（0.11、0.12 は
      どちらかが真なら OS の証明書ストアを使い、のちの版は UV_SYSTEM_CERTS を優先する）。
    - 古い uv: UV_SYSTEM_CERTS を黙って無視し、UV_NATIVE_TLS だけを使う。
    - 真偽の値は、大文字と小文字を区別せずに読み、前後の空白は除かない（clap の BoolishValueParser と同じ）。
    そのため、uv の版を確かめずに決まる場合がほとんどである。
    - 両方ある: どの uv でも、利用者の設定のどれかを使う。決定である。
    - UV_NATIVE_TLS だけ: どちらの uv も使う。決定である。
    - UV_SYSTEM_CERTS だけで、真でない値: 新しい uv では決定、古い uv では無視されるが、uv の既定（OS の
      証明書ストアを使わない）と同じなので、利用者の意図どおりである。どちらにしても決定とみなす。
    - UV_SYSTEM_CERTS だけで、真の値: 新しい uv では決定である。古い uv では、利用者の意図（OS の証明書
      ストアを使う）が uv に届いていない。ここだけ uv の版を確かめ、新しい uv と分かったときだけ決定とみなす。
      確かめられなければ、届いていないものとして扱う（やり直しは、両方の名前を真にして行う）。
    """
    native = os.environ.get(NATIVE_TLS_ENV, "")
    system = os.environ.get(SYSTEM_CERTS_ENV, "")
    has_native, has_system = bool(native.strip()), bool(system.strip())
    if has_native and has_system:
        return (f"{SYSTEM_CERTS_ENV}={system} と {NATIVE_TLS_ENV}={native}（古い uv は {NATIVE_TLS_ENV} だけを使い、"
                f"新しい uv は版によって {SYSTEM_CERTS_ENV} を優先するか、両方を見る）"), None, _UNCHECKED
    if has_native:
        return f"{NATIVE_TLS_ENV}={native}", None, _UNCHECKED
    if not has_system:
        return None, None, _UNCHECKED
    setting = f"{SYSTEM_CERTS_ENV}={system}"
    if system.lower() not in _TRUTHY_VALUES:
        return setting, None, _UNCHECKED
    reads = uv_reads_system_certs()
    if reads is True:
        return setting, None, reads
    return None, setting, reads


def _unless_unresolvable(script: Path, code: int, text: str) -> int:
    """uv が依存を解決できずに失敗していれば、launch.log に記録して UvResolveError にする。それ以外は code を返す。

    解決できないときは環境が作られていないので、壊れた環境とみなして入れ直しても直らない。入れ直すと、
    「環境が作りかけで壊れているかもしれない」と誤った理由を残し、同じ失敗を繰り返す。
    """
    if code != 0 and is_resolve_error(text):
        message = resolve_error_message(script, text)
        record_failure(f"UvResolveError: {message}")
        raise UvResolveError(message)
    return code


def _uv_sync(script: Path, *, reinstall: bool, output, timeout: float, log=None) -> int:
    """`uv sync --script` の終了コードを返す（_uv_sync_result）。"""
    return _uv_sync_result(script, reinstall=reinstall, output=output, timeout=timeout, log=log)[0]


def _uv_sync_result(script: Path, *, reinstall: bool, output, timeout: float, log=None) -> tuple[int, str]:
    """`uv sync --script` の終了コードと、最後に動かした uv の出力を返す。出力は、失敗の理由を launch.log に
    残すために使う。TLS 証明書を検証できずに失敗したときは、利用者が
    この uv が読む証明書の設定を決めていなければ、OS の証明書ストアを使って 1 回だけやり直す（_user_tls_setting）。
    環境変数の名前は uv の版で選ぶ（uv_reads_system_certs、system_certs_envs）。

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
        return _unless_unresolvable(script, code, text), text
    user_setting, ignored_setting, reads = _user_tls_setting()
    if user_setting is None:
        if reads is _UNCHECKED:
            reads = uv_reads_system_certs()
        names = system_certs_envs(reads)
        retry = {name: "true" for name in names}
        if log:
            if ignored_setting and reads is False:
                reason = f"（{ignored_setting} は、この古い uv が読まない名前である）"
            elif reads is None:
                reason = "（この uv がどちらの名前を読むかを確かめられなかったので、両方を設定する）"
            else:
                reason = ""
            log("uv が TLS 証明書を検証できなかったので、OS の証明書ストアを使ってやり直す（"
                + "、".join(f"{name}=true" for name in names) + f"）{reason}")
        code, text = _run_uv(command, {**env, **retry}, output, timeout)
        if code == 0 or not is_tls_error(text):
            os.environ.update(retry)
            return _unless_unresolvable(script, code, text), text
    message = tls_error_message(script, user_setting, ignored_setting, reads)
    # 中継がサーバーの環境を用意するときも、利用者が読む場所（launch.log）に残す。
    record_failure(f"UvTlsError: {message}")
    raise UvTlsError(message)


def _imports_ok(python: str, script: Path, output, timeout: float) -> bool:
    """環境がそろっていれば真、壊れていれば（BROKEN_ENV_EXIT）偽。それ以外の失敗は例外にする。

    メモリ不足などで確かめのプロセスが落ちたときに入れ直すと、先にほかのセッションが直して使っている
    環境まで入れ直してしまう。入れ直して直るのは、壊れた環境だけである。
    """
    out = subprocess.run([python, "-X", "utf8", str(script), CHECK_IMPORTS_ARG], stdin=subprocess.DEVNULL,
                         stdout=output, stderr=output, timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"},
                         creationflags=child_creationflags())
    if out.returncode == RUNTIME_LIB_EXIT:
        raise_vc_runtime_error(f"{script.name} の依存の DLL を読み込めない。")  # 入れ直しても直らない
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
    - uv が TLS 証明書を検証できなかったとき、依存を解決できなかったとき（_uv_sync）: 環境は壊れていないので
      入れ直さず、UvTlsError か UvResolveError（どちらも UvSetupError）を送り出す。
    - 依存の DLL を読めなかったとき（`--check-imports` が RUNTIME_LIB_EXIT で止まる）: Visual C++ 再頒布可能
      パッケージがないので、入れ直さず VcRuntimeError（UvSetupError）を送り出す。

    失敗は、どれも利用者が読む logs/launch.log に残す。ホストは標準エラーを見せないことがあり、中継が
    サーバーの環境を用意するときは、uv の出力は server.log にしか書かれないためである。
    """
    try:
        return _prepare_script(script, output=output, timeout=timeout, log=log, force=force)
    except UvSetupError:
        raise  # _uv_sync が記録済み
    except Exception as exc:
        notes = " ".join(getattr(exc, "__notes__", []))  # 待ちきれなかったときの uv の出力の要約（_run_uv）
        record_failure(f"{script.name} の環境を用意できなかった: {type(exc).__name__}: {exc}"
                       + (f" {notes}" if notes else ""))
        raise


def _prepare_script(script: Path, *, output, timeout: float, log, force: bool) -> str:
    if not force and _uv_sync(script, reinstall=False, output=output, timeout=timeout, log=log) == 0:
        return script_python(script)
    with FileLock(env_lock_path(script), timeout=timeout):
        if _uv_sync(script, reinstall=False, output=output, timeout=timeout, log=log) == 0:
            python = script_python(script)
            if _imports_ok(python, script, output, timeout):
                return python
        log(f"{script.name} の環境が作りかけで壊れているかもしれないので、入れ直す")
        code, text = _uv_sync_result(script, reinstall=True, output=output, timeout=timeout, log=log)
        if code != 0:
            raise RuntimeError(f"{script.name} の環境を入れ直せなかった（uv sync --reinstall の終了コード {code}）。"
                               f"uv の出力: {uv_error_summary(text)}")
        python = script_python(script)
        if not _imports_ok(python, script, output, timeout):
            raise RuntimeError(f"{script.name} の環境を入れ直しても、依存を import できない")
        return python


def script_python(script: Path, timeout: float = 120) -> str:
    """prepare_script で用意した環境の python のパス。"""
    found = subprocess.run([uv_executable(), "python", "find", "--script", str(script)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"},
                           creationflags=child_creationflags())
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


class ServerFiles:
    """設定されたデータの根にある、MCP 起動用のロックと記録。"""

    def __init__(self):
        from wiki_fsguard import WriteGuard
        from wiki_pages import absolute_user_path

        self.root = absolute_user_path(data_dir())
        self.root.mkdir(parents=True, exist_ok=True)
        self.guard = WriteGuard(self.root)
        self.record = self.root / "server.json"
        self.pending = self.root / "server-start.json"

    def open_log(self, name: str):
        logs = self.root / "logs"
        self.guard.ensure_directory(logs)
        return self.guard.open_append(logs / name)

    def startup_lock(self, *, timeout: float):
        return self.guard.hold_lock(self.root / "server.lock", timeout=timeout)

    def _record_lock(self):
        # 中継が旧サーバーの終了や新サーバーの公開を待つ間も、サーバーは記録を操作できる。
        return self.guard.hold_lock(self.root / "server-info.lock", timeout=30)

    def _read_info(self) -> dict | None:
        content = self.guard.read_bytes(self.record)
        if content is None:
            return None
        try:
            info = json.loads(content.decode("utf-8"))
        except ValueError:
            return None
        if not isinstance(info, dict) or not all(k in info for k in ("port", "token", "pid")):
            return None
        if (type(info["pid"]) is not int or info["pid"] <= 0
                or type(info["port"]) is not int or not 0 < info["port"] <= 65535
                or not isinstance(info["token"], str) or not info["token"]):
            return None
        return info

    def read_info(self) -> dict | None:
        with self._record_lock():
            return self._read_info()

    def write_info(self, info: dict, *, start_id: str | None = None) -> None:
        """トークンを含むので、POSIX では一時ファイルの作成時から本人だけが読めるようにする。"""
        if start_id is not None:
            info = {**info, "start_id": start_id}
        data = json.dumps(info, ensure_ascii=False).encode("utf-8")
        with self._record_lock():
            expected = self._start_marker(start_id) if start_id is not None else None
            if self.guard.read_bytes(self.pending) != expected:
                raise OSError(f"起動の許可が失われた。中継からやり直す: {self.pending}")
            if start_id is None and self.guard.file_info(self.record) is not None:
                raise OSError(f"サーバーの記録がすでにある。中継から起動する: {self.record}")
            self.guard.write_bytes(self.record, data, replace=True, prefix=".server-json-", mode=0o600)

    @staticmethod
    def _start_marker(start_id: str) -> bytes:
        return json.dumps(start_id).encode("utf-8")

    def reserve_start(self, previous: dict | None) -> str:
        """中継が起動ロックを保持している間に、残った記録を消し、子の公開を許可する。"""
        start_id = secrets.token_hex(16)
        with self._record_lock():
            self.guard.file_info(self.pending)
            current = self._read_info()
            if current is not None and current != previous:
                raise RuntimeError(f"サーバーの確認中に記録が変わった。もう一度やり直す: {self.record}")
            self.guard.remove_file(self.record)
            self.guard.write_bytes(self.pending, self._start_marker(start_id), replace=True, mode=0o600)
        return start_id

    def release_start(self, start_id: str) -> None:
        with self._record_lock():
            if self.guard.read_bytes(self.pending) == self._start_marker(start_id):
                self.guard.remove_file(self.pending)

    def remove_own_info(self, pid: int, token: str) -> None:
        """自分が公開した記録を消す。照合から削除まで記録のロックを保持する。"""
        with self._record_lock():
            info = self._read_info()
            if info is not None and info["pid"] == pid and info["token"] == token:
                self.guard.remove_file(self.record)
