"""wiki_workspace.py — info-hive の作業領域の設定（`.info-hive/workspace.json`）を読む。

標準ライブラリだけを使う。規則は info-hive-dev の ADR-0008 に従う。log と rag にも同じものを置き、
同じ入力で同じ結果になることを info-hive-dev の `tests/workspace/` で確かめる。

- 見つけ方: 起点のディレクトリから親へさかのぼり、各階層で案内のファイル `.info-hive.json` と
  作業領域の設定 `.info-hive/workspace.json` を探す。同じ階層に両方あれば案内のファイルを先に見る。
  Git のリポジトリやサブモジュールの `.git` では止まらない。
- 案内のファイルが指す作業領域に設定がなければ、WorkspaceError を送出する（黙ってほかの場所を使わない）。
- 設定のパスは、作業領域の中の相対パスだけを受け付ける。`..` と絶対パスは拒否する。
- 未知のキーは警告にするだけで消さない。書き出すときも残し、キーの順序を変えない。
- ユーザースコープの作業領域は、環境変数 `INFO_HIVE_HOME`、なければ `~/info-hive/` にある。
- パスの中の `{...}` の語は、決まったものだけを受け付ける。

wiki の各スクリプトは `default_wiki_dir()` で `--wiki` の既定を決める。
作業領域の設定があれば `dirs.wiki`、なければ今までどおり `./wiki` を使う。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

GUIDE_NAME = ".info-hive.json"
CONFIG_DIR = ".info-hive"
CONFIG_NAME = "workspace.json"
HOME_ENV = "INFO_HIVE_HOME"
FORMAT_VERSION = 1

DEFAULT_DIRS: dict[str, str] = {
    "inbox": "00_Inbox",
    "assets": "10_Assets",
    "daily": "20_Daily",
    "weekly": "30_Weekly",
    "notes": "50_Notes",
    "output": "60_Output",
    "adr": "70_ADR",
    "wiki": "wiki",
    "raw": "raw",
    "rag": "92_RAG",
    "archive": "A0_Archive",
}
# ユーザースコープでは ADR のディレクトリを作らない。
DEFAULT_DIRS_USER_NULL = {"adr"}

KNOWN_TOP_KEYS = ("$schema", "version", "kind", "id", "dirs", "log", "wiki", "rag")
KNOWN_GUIDE_KEYS = ("version", "workspace", "log_scope")
KINDS = ("user", "project")
LOG_SCOPES = ("user", "project")

PLACEHOLDERS = ("year", "month", "day", "end_month", "end_day", "stem", "seq")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


class WorkspaceError(Exception):
    """作業領域の設定か案内のファイルが壊れている。"""


@dataclass
class Workspace:
    root: Path
    config_path: Path
    config: dict
    guide_path: Path | None = None
    guide: dict | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.config["kind"]

    def dir(self, role: str) -> Path | None:
        """役割のディレクトリの絶対パス。`null` にした役割は None。"""
        return resolve_dir(self.root, self.config, role)


# ---------------------------------------------------------------------------
# 読み込みと検査
# ---------------------------------------------------------------------------


def read_json(path: Path) -> object:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"UTF-8 として読めない: {path}: {exc}") from exc
    except OSError as exc:
        raise WorkspaceError(f"読めない: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise WorkspaceError(f"JSON として読めない: {path}: {exc}") from exc


def check_relative(value: object, what: str) -> str:
    """作業領域の中の相対パスであることを確かめ、`/` 区切りにして返す。"""
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"{what} は空でない文字列にする: {value!r}")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute() \
            or PureWindowsPath(value).drive or value.startswith(("/", "\\")):
        raise WorkspaceError(f"{what} に絶対パスは使えない: {value}")
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        raise WorkspaceError(f"{what} に .. は使えない: {value}")
    normalized = "/".join(p for p in parts if p not in ("", "."))
    if not normalized:
        raise WorkspaceError(f"{what} が作業領域の根そのものを指している: {value}")
    return normalized


def check_config(config: object, path: Path) -> list[str]:
    """設定を検査する。壊れていれば WorkspaceError、未知のキーは警告の文のリストで返す。"""
    if not isinstance(config, dict):
        raise WorkspaceError(f"作業領域の設定の最上位はオブジェクトにする: {path}")
    warnings = [f"未知のキー {key!r} がある（消さずに残す）: {path}"
                for key in config if key not in KNOWN_TOP_KEYS]
    version = config.get("version", FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise WorkspaceError(f"対応していない設定の版 {version!r}（対応は {FORMAT_VERSION}）: {path}")
    kind = config.get("kind")
    if kind not in KINDS:
        raise WorkspaceError(f"kind は {' か '.join(KINDS)} にする（必須）: {path}")
    if kind == "project" and not (isinstance(config.get("id"), str) and config["id"].strip()):
        raise WorkspaceError(f"プロジェクトの作業領域には id が要る: {path}")
    dirs = config.get("dirs", {})
    if not isinstance(dirs, dict):
        raise WorkspaceError(f"dirs はオブジェクトにする: {path}")
    for role, value in dirs.items():
        if role not in DEFAULT_DIRS:
            # 新しい版のプラグインが足した役割かもしれないので、null も受け付けて警告だけにする。
            warnings.append(f"未知の役割 dirs.{role} がある（消さずに残す）: {path}")
            if value is None:
                continue
        elif value is None and role == "adr":
            continue
        check_relative(value, f"dirs.{role}")
    return warnings


def check_guide(guide: object, path: Path) -> list[str]:
    if not isinstance(guide, dict):
        raise WorkspaceError(f"案内のファイルの最上位はオブジェクトにする: {path}")
    warnings = [f"未知のキー {key!r} がある（消さずに残す）: {path}"
                for key in guide if key not in KNOWN_GUIDE_KEYS]
    version = guide.get("version", FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise WorkspaceError(f"対応していない案内のファイルの版 {version!r}: {path}")
    if "workspace" not in guide:
        raise WorkspaceError(f"案内のファイルに workspace がない: {path}")
    if guide["workspace"] not in (".", "./"):
        check_relative(guide["workspace"], "workspace")
    scope = guide.get("log_scope", "user")
    if scope not in LOG_SCOPES:
        raise WorkspaceError(f"log_scope は {' か '.join(LOG_SCOPES)} にする: {path}")
    return warnings


def load_workspace(root: Path, guide_path: Path | None = None, guide: dict | None = None,
                   warnings: list[str] | None = None) -> Workspace:
    """root を作業領域の根として設定を読む。設定がなければ WorkspaceError。"""
    root = Path(root)
    config_path = root / CONFIG_DIR / CONFIG_NAME
    if not config_path.is_file():
        raise WorkspaceError(f"作業領域の設定が見つからない: {config_path}")
    config = read_json(config_path)
    found = list(warnings or [])
    found += check_config(config, config_path)
    return Workspace(root=root, config_path=config_path, config=config,
                     guide_path=guide_path, guide=guide, warnings=found)


def find_workspace(start: Path | None = None) -> Workspace | None:
    """start から親へさかのぼって作業領域を探す。見つからなければ None。"""
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for directory in (here, *here.parents):
        guide_path = directory / GUIDE_NAME
        if guide_path.is_file():
            guide = read_json(guide_path)
            warnings = check_guide(guide, guide_path)
            target = guide["workspace"]
            root = directory if target in (".", "./") else directory / check_relative(target, "workspace")
            if not (root / CONFIG_DIR / CONFIG_NAME).is_file():
                raise WorkspaceError(
                    f"案内のファイル {guide_path} が指す作業領域に設定がない: "
                    f"{root / CONFIG_DIR / CONFIG_NAME}"
                )
            return load_workspace(root, guide_path, guide, warnings)
        if (directory / CONFIG_DIR / CONFIG_NAME).is_file():
            return load_workspace(directory)
    return None


def user_workspace_root(environ: dict | None = None) -> Path:
    """ユーザースコープの作業領域の根。INFO_HIVE_HOME、なければ ~/info-hive。"""
    env = os.environ if environ is None else environ
    value = env.get(HOME_ENV)
    if value:
        return Path(os.path.expanduser(value))
    return Path.home() / "info-hive"


def resolve_dir(root: Path, config: dict, role: str) -> Path | None:
    """役割のディレクトリ。書かなかった役割は既定値を使い、`null` は None を返す。"""
    dirs = config.get("dirs") or {}
    if role in dirs:
        value = dirs[role]
        if value is None:
            return None
    elif role not in DEFAULT_DIRS:
        raise WorkspaceError(f"未知の役割: {role}")
    elif config.get("kind") == "user" and role in DEFAULT_DIRS_USER_NULL:
        return None
    else:
        value = DEFAULT_DIRS[role]
    return Path(root) / check_relative(value, f"dirs.{role}")


# ---------------------------------------------------------------------------
# パスの中の {...} の語と、書き出し
# ---------------------------------------------------------------------------


def expand_template(template: str, values: dict) -> str:
    """`{year}` などの語を埋める。決まった語以外や、値のない語があれば WorkspaceError。"""
    def replace(match: re.Match) -> str:
        word = match.group(1)
        if word not in PLACEHOLDERS:
            raise WorkspaceError(f"パスに使えない語 {{{word}}} がある: {template}")
        if word not in values:
            raise WorkspaceError(f"語 {{{word}}} の値がない: {template}")
        return str(values[word])
    return PLACEHOLDER_RE.sub(replace, template)


def format_seq(n: int) -> str:
    """01 から始まる 2 桁の連番。99 を超えたら 3 桁にする。"""
    if n < 1:
        raise WorkspaceError(f"連番は 1 から始める: {n}")
    return f"{n:02d}" if n <= 99 else f"{n:03d}"


def dump_config(config: dict) -> str:
    """UTF-8、LF、2 文字の字下げで、キーの順序と未知のキーを保ったまま文字列にする。"""
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def update_section(config: dict, plugin: str, section: dict) -> dict:
    """プラグインが自分の名前の部分だけを書き換えた設定を返す。ほかのキーと順序は変えない。"""
    updated = dict(config)
    updated[plugin] = section
    return updated


# ---------------------------------------------------------------------------
# wiki のスクリプトから使う
# ---------------------------------------------------------------------------


def default_wiki_dir(start: Path | None = None) -> Path:
    """`--wiki` を省いたときの wiki のディレクトリ。

    作業領域の設定があれば `dirs.wiki`（書かなければ `wiki`）、なければ `./wiki`。
    壊れた設定は WorkspaceError を送出する。
    """
    workspace = find_workspace(start)
    if workspace is None:
        return Path("wiki")
    wiki = workspace.dir("wiki")
    if wiki is None:
        raise WorkspaceError(f"dirs.wiki を null にはできない: {workspace.config_path}")
    return wiki


def default_wiki_or_exit(start: Path | None = None) -> Path:
    """default_wiki_dir と同じ。壊れた設定なら理由を書いて終了コード 1 で止まる。警告は標準エラーに書く。"""
    try:
        workspace = find_workspace(start)
    except WorkspaceError as exc:
        print(f"作業領域の設定を読めない: {exc}", file=sys.stderr)
        sys.exit(1)
    if workspace is None:
        return Path("wiki")
    for warning in workspace.warnings:
        print(f"警告: {warning}", file=sys.stderr)
    try:
        wiki = workspace.dir("wiki")
    except WorkspaceError as exc:
        print(f"作業領域の設定を読めない: {exc}", file=sys.stderr)
        sys.exit(1)
    if wiki is None:
        print(f"作業領域の設定を読めない: dirs.wiki を null にはできない: {workspace.config_path}",
              file=sys.stderr)
        sys.exit(1)
    return wiki


WIKI_DEFAULT_HELP = "wiki のディレクトリ（既定: 作業領域の設定の dirs.wiki。設定がなければ ./wiki）"


def _main(argv: list[str] | None = None) -> int:
    """`python wiki_workspace.py [--start DIR]` で、見つかった作業領域と wiki の場所を JSON で出力する。"""
    import argparse
    parser = argparse.ArgumentParser(description="作業領域の設定を探し、wiki の既定の場所を出力する。")
    parser.add_argument("--start", type=Path, default=None, help="探し始めるディレクトリ（既定: カレントディレクトリ）")
    args = parser.parse_args(argv)
    try:
        workspace = find_workspace(args.start)
        wiki = default_wiki_dir(args.start)
    except WorkspaceError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({
        "status": "found" if workspace else "not_found",
        "root": str(workspace.root) if workspace else None,
        "kind": workspace.kind if workspace else None,
        "wiki": str(wiki),
        "warnings": workspace.warnings if workspace else [],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(_main())
