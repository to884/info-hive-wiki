"""wiki_links.py — wiki と raw の記事にリンクを置かない規則（info-hive-dev の ADR-0029）の、リンクの判定。

何をリンクとみなすかを、この 1 か所で決める。走査（wiki_pages）、記事を読む口、lint、取り込みのあとの確かめが、
どれもここを使う。判定は、項目ごとの os.lstat() の結果だけで決める。種類の名前を数え上げず、OS が返す値の条件で書く。

| 種類 | 条件 |
| --- | --- |
| symlink | POSIX では S_ISLNK。Windows では再解析のタグが IO_REPARSE_TAG_SYMLINK |
| junction | Windows で、再解析のタグが IO_REPARSE_TAG_MOUNT_POINT |
| reparse | Windows で、再解析点の属性があり、タグの「名前の代理」のビットが立つ、上の 2 つ以外のもの |
| hardlink | ふつうのファイルで、リンクの数（st_nlink）が 2 以上。ディレクトリのリンクの数は見ない |

Windows の DirEntry.stat() の st_nlink はいつも 0 なので、ハードリンクは os.lstat() の結果でしか分からない。
「名前の代理」のビットは、Microsoft の文書が「システムの中の別の名前の実体を表す」と定義するもので、知らない
タグにも同じ規則が当たる。クラウドのファイルの置き換え（OneDrive など）は、このビットが立たないので、リンクではない。

標準ライブラリだけで書き、何も書かない。CLI:

    python wiki_links.py check <パス>...   リンクも調べられなかったものもなければ終了コード 0、ほかは 1。結果は JSON
"""

from __future__ import annotations

import json
import os
import stat
import sys

KINDS = ("symlink", "junction", "reparse", "hardlink")

IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
REPARSE_TAG_NAME_SURROGATE = 0x20000000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def link_kind(st, nlink_known: bool = True) -> "str | None":
    """リンクをたどらない stat の結果（os.lstat()、DirEntry.stat(follow_symlinks=False)）から、リンクの種類を返す。

    リンクでなければ None。nlink_known が偽なら（Windows の DirEntry.stat() のように st_nlink が分からない結果）、
    ハードリンクは判定しない。
    """
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    attributes = getattr(st, "st_file_attributes", 0) or 0
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", 0) or 0
        if tag == IO_REPARSE_TAG_SYMLINK:
            return "symlink"
        if tag == IO_REPARSE_TAG_MOUNT_POINT:
            return "junction"
        if tag & REPARSE_TAG_NAME_SURROGATE:
            return "reparse"
    if nlink_known and stat.S_ISREG(st.st_mode) and st.st_nlink >= 2:
        return "hardlink"
    return None


def is_link(path) -> "tuple[str, str | None]":
    """(状態, 種類)。状態は wiki_pages.read_os() と同じ "ok"、"absent"、"denied"。種類はリンクでなければ None。"""
    from wiki_pages import _stat_entry  # wiki_pages がこのモジュールを読み込むので、使うときに読み込む
    # 走査と同じ読み替え（Windows で属性の取得に失敗したときの S_IFBLK を「分からない」にする）を使う
    state, st = _stat_entry(os.fspath(path))
    if state != "ok":
        return state, None
    return "ok", link_kind(st)


def _within(path: str, origin: str) -> bool:
    """path が origin そのものか、その祖先か（文字のうえで比べる。解決しない）。"""
    a = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    b = os.path.normcase(os.path.abspath(origin)).rstrip("\\/")
    return b == a or b.startswith(a + os.sep)


def find_links(path, origin=None, articles_only: bool = False) -> dict:
    """path がリンクか、path がディレクトリならその下のすべての項目のうち、リンクであるものと、調べられなかったものを返す。

    戻り値は {"links": [(相対パス, 種類)], "unchecked": [相対パス]}。相対パスは path からの / 区切りで、path そのものは "."。
    - origin を渡すと、path が origin そのものかその祖先なら、path そのものは調べない（根と祖先のリンクは許す）。
    - リンクには降りない。一覧できないディレクトリと、os.lstat() が失敗した項目は unchecked に入れる。
    - articles_only が真なら、名前が "." で始まる項目とその下を見ない（raw のディレクトリの記事に絞る）。
    """
    from wiki_pages import _list_dir, _stat_entry, read_os
    path = os.fspath(path)
    links, unchecked = [], []
    allowed = origin is not None and _within(path, os.fspath(origin))
    if not allowed:
        state, kind = is_link(path)
        if state == "denied":
            return {"links": [], "unchecked": ["."]}
        if state == "absent":
            return {"links": [], "unchecked": []}
        if kind is not None:
            return {"links": [(".", kind)], "unchecked": []}
    # 許した根（起点そのものか祖先）は、リンクでも、その先がディレクトリなら下を調べる
    state, st = read_os(os.stat if allowed else os.lstat, path)
    if state != "ok":
        return {"links": [], "unchecked": ["."] if state == "denied" else []}
    if not stat.S_ISDIR(st.st_mode):
        return {"links": [], "unchecked": []}
    stack = [(path, "")]
    while stack:
        directory, pre = stack.pop()
        state, entries = read_os(_list_dir, directory)
        if state == "absent":
            continue
        if state == "denied":
            unchecked.append(pre.rstrip("/") or ".")
            continue
        for entry in entries:
            if articles_only and entry.name.startswith("."):
                continue
            rel = pre + entry.name
            state, est = _stat_entry(entry.path)
            if state == "absent":
                continue
            if state == "denied":
                unchecked.append(rel)
                continue
            kind = link_kind(est)
            if kind is not None:
                links.append((rel, kind))
            elif stat.S_ISDIR(est.st_mode):
                stack.append((entry.path, rel + "/"))
    links.sort()
    unchecked.sort()
    return {"links": links, "unchecked": unchecked}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="wiki と raw の記事にリンクがないかを調べる（何も書かない）。")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="パスと、その下のリンクを JSON で出す")
    check.add_argument("paths", nargs="+")
    check.add_argument("--articles-only", action="store_true", help="名前が . で始まる項目とその下を見ない")
    args = parser.parse_args(argv)
    results, ok = {}, True
    for p in args.paths:
        found = find_links(p, articles_only=args.articles_only)
        results[p] = {"links": [{"path": r, "kind": k} for r, k in found["links"]], "unchecked": found["unchecked"]}
        ok = ok and not found["links"] and not found["unchecked"]
    json.dump({"results": results, "ok": ok}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
