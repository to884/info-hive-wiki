"""wiki_initialized.py — 初期化された wiki かを決める（info-hive-dev の ADR-0019）。

初期化された wiki は、ルートがディレクトリで、その直下の `SCHEMA.md` が通常のファイル（シンボリックリンクなら、
リンクの先が通常のファイル）であるものとする。`SCHEMA.md` は `init_wiki.py` が初期化で必ず作り、上書きしない。
中身は読まない。

wiki のパスを受け取る入口（CLI、MCP のツール、閲覧、Web ページの取得、経験の記録、アップグレード）は、どれも
ここで確かめる。ライブラリの関数（lint や索引の関数）は確かめない。

標準ライブラリだけを使う。読み込む入口は、先に `wiki_text` を読み込む（Python の版の確かめ）。

    import wiki_initialized
    wiki = wiki_initialized.require_or_exit(args.wiki)   # CLI: 理由と案内を標準エラーに書き、終了コード 1
    wiki_initialized.require(path)                       # 未初期化なら NotInitialized（ValueError）を送出する
    wiki_initialized.problem(path)                       # 未初期化の理由の文。初期化済みなら None
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

MARKER = "SCHEMA.md"
INIT_HINT = ("/wiki:init で初期化する（ユーザースコープの wiki は /wiki:init --user。スクリプトだけで使うときは "
             "init_wiki.py）。初期化した wiki が別の場所にあるなら、そのパスを指定する。")


class NotInitialized(ValueError):
    """初期化された wiki ではない。文は理由と、次の行の案内からなる。"""


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def problem(root: str | os.PathLike) -> str | None:
    """root が初期化された wiki でなければ理由の文を、初期化済みなら None を返す。

    調べ方は os.stat()（リンクをたどる）の 1 回ずつで、FileNotFoundError と NotADirectoryError を「ない」、
    それ以外の OSError を「調べられない」とする。Path.is_file() は失敗の扱いが Python の版で違うので使わない。
    """
    path = Path(root)
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return f"wiki のディレクトリが見つからない: {path}"
    except OSError as exc:
        return f"wiki のディレクトリを確かめられない: {path}（{_reason(exc)}）"
    if not stat.S_ISDIR(st.st_mode):
        return f"wiki のルートがディレクトリではない: {path}"
    try:
        st = os.stat(path / MARKER)
    except (FileNotFoundError, NotADirectoryError):
        return f"初期化された wiki ではない（{MARKER} がない）: {path}"
    except OSError as exc:
        return f"初期化された wiki か確かめられない（{MARKER} を調べられない: {_reason(exc)}）: {path}"
    if not stat.S_ISREG(st.st_mode):
        return f"初期化された wiki ではない（{MARKER} が通常のファイルではない）: {path}"
    return None


def require(root: str | os.PathLike) -> Path:
    """初期化された wiki なら root を Path にして返す。そうでなければ NotInitialized を送出する。"""
    found = problem(root)
    if found is not None:
        raise NotInitialized(f"{found}\n{INIT_HINT}")
    return Path(root)


def require_or_exit(root: str | os.PathLike) -> Path:
    """require() と同じ。未初期化なら理由と案内を標準エラーに書き、終了コード 1 で止まる。"""
    try:
        return require(root)
    except NotInitialized as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
