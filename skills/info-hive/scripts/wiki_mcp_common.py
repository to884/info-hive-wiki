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
import json
import os
import re
import sys
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
