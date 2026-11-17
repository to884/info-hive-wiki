"""
wiki_pages.py — wiki のページの集合と、wikilink の抜き出しと解決の規則。

lint、検索（とそれを使う MCP の索引）、グラフ、統計、判定レイヤーが共通で使う。のちに閲覧（browse）の
中核も使う。規則を 1 か所に置くのは、スクリプトごとに規則を書くと食い違うからである（ADR-0010 の決定の 1）。
標準ライブラリ、wiki_text、同梱した markdown-it-py（_vendor/）だけで書き、単独では実行しない。

- ページは、wiki のルートからの相対パスが `.md`（大文字と小文字は問わない）で終わり、最上位の SCHEMA.md、
  index.md、log.md、README.md と、最上位の indexes/、graph/、raw/、assets/、.evolution/、.wiki-cache/ に
  当たらず、どの要素の名前も `.` で始まらないファイルである。
- wiki の側の記事（is_article_path）は、ページと、最上位の index.md、log.md、SCHEMA.md と、indexes の下の .md である。
  記事と記事までの道のリンクは、読まず、たどらず、Walk.links と article_path() の結果で知らせる（info-hive-dev の
  ADR-0029）。リンクの判定は wiki_links.link_kind() の 1 か所にある。
- wikilink は、閲覧が使う markdown-it（commonmark、html は無効、表は有効）が本文の文字とするところだけから
  拾う。コードブロックの中、インラインのコードの中、参照リンクの定義の中のものは数えない。
- パスとリンクの行き先は NFC にそろえて比べ、大文字と小文字を区別する。
- wiki をたどる経路は walk_markdown() の 1 つで、ページ（walk_pages）と索引のファイル（walk_index_files）は、
  起点を変えて同じたどり方を使う。最上位の除く名前の項目と、名前が `.` で始まる項目には、どの OS の呼び出しもしない。
  OS の呼び出しの失敗は、read_os() の 1 か所で「ない」「分からない」「そのほか（送出）」に分ける。設計は info-hive-dev の
  `docs/plans/wiki-walk-excluded-dirs-2026-11-11.md`（7 版目）と、リンクを拒む形に変えた
  `docs/plans/wiki-links-2026-11-16.md`（ADR-0029 の実装の設計）にある。
"""

from __future__ import annotations

import atexit
import errno
import functools
import hashlib
import io
import os
import re
import stat
import sys
import threading
import unicodedata
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from wiki_text import canonical_kind, canonical_type, nfc, posix_rel
from wiki_links import link_kind

SKIP_TOP_LEVEL_FILES = frozenset({"SCHEMA.md", "index.md", "log.md", "README.md"})
SKIP_TOP_LEVEL_DIRS = frozenset({"indexes", "graph", "raw", "assets", ".evolution", ".wiki-cache"})

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# WIKILINK_RE と同じものに当たり、別名も取り出す（1 つ目が行き先、2 つ目が別名）。閲覧が wikilink を描くときに使う。
# 当たる範囲が WIKILINK_RE と同じなので、描くものと extract_wikilinks が拾うものは一致する。
WIKILINK_FULL_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


# ---------------------------------------------------------------------------
# ページの集合
# ---------------------------------------------------------------------------

def page_text(data: bytes) -> str:
    """先頭の BOM 1個と改行の違いを除いたページ全文。"""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline=None).read()


def normalized_text_sha256(text: str) -> str:
    """既に正規化した文字列を、そのままハッシュにする。BOM を再び除かない。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_text_sha256(data: bytes) -> str:
    """BOM と改行の違いを除いたページ全文の SHA-256。読めなければ UnicodeDecodeError。"""
    return normalized_text_sha256(page_text(data))


def _relative_text(rel, native: bool) -> str:
    """コマンドの区切りはそろえる。OS から得た名前は、その OS の文字のまま扱う。"""
    text = str(rel)
    return text if native and not NT else text.replace("\\", "/")


def is_page_path(rel: "str | PurePosixPath", *, native: bool = False) -> bool:
    """wiki のルートからの相対パス（/ 区切り）がページかを返す。"""
    parts = PurePosixPath(_relative_text(rel, native)).parts
    # 拡張子は大文字と小文字を区別しない。Windows の rglob("*.md") は Upper.MD も返し、0.8.0 までもページだった。
    if not parts or not parts[-1].lower().endswith(".md") or any(part.startswith(".") for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] not in SKIP_TOP_LEVEL_FILES
    return parts[0] not in SKIP_TOP_LEVEL_DIRS


# ---------------------------------------------------------------------------
# wiki をたどる
# ---------------------------------------------------------------------------
#
# 走査が OS を呼ぶ口は、os.scandir()（開く、項目を 1 つ取り出す、閉じる）、項目の stat(follow_symlinks=False)、
# POSIX でリンクの項目の is_dir()、os.lstat() だけである。どれも、呼ぶたびに os から引いて呼ぶ
# （試験が差し替えられるように）。pathlib の入出力、os.walk、os.path.realpath、os.path.isfile は使わない。内側で
# いくつもの呼び出しをして、失敗を版ごとに違う形で読み替えるからである。口の各行の内側の扱いと、それが結果を
# 変えない理由は、設計の「決めること」の 1 の表にある。

NT = os.name == "nt"


# ---------------------------------------------------------------------------
# 拒んだことの知らせ（info-hive-dev の ADR-0029 の I4）
# ---------------------------------------------------------------------------
#
# 走査と記事を読む口は、リンクなので読まなかったもの（種類は wiki_links.KINDS）と、調べられなかったもの（種類は
# "unknown"、"unlistable"）を、拒んだその場で受け皿に入れる。知らせを出すのは使う側の仕事にしない。使う側は
# 10 か所を超え、1 か所の付け忘れで「ない」に変わるからである（実装の 2 回目のレビュー）。
# - MCP のサーバーは、ツールの呼び出しごとに capture_notices() で受け皿を開き、結果か誤りの文に付ける。
# - CLI（スクリプトのディレクトリにあるスクリプトを直接動かしたとき）は、プロセスの終わりに標準エラーへまとめて出す。
# 相対パスは、走査や読む口に渡した起点からのものである。

NOTICE_UNVERIFIED = ("unknown", "unlistable")
_SINK = threading.local()
# CLI のときだけ、プロセスの終わりに出す知らせを貯める（重ねない。MCP のサーバーや試験では貯めない）
_PROCESS_NOTICES: dict = {}
_CLI = False


def _remember(item: dict) -> None:
    if _CLI:
        _PROCESS_NOTICES.setdefault((item["path"], item["kind"]), item)


def _notice(path: str, kind: str) -> None:
    item = {"path": path, "kind": kind}
    sink = getattr(_SINK, "items", None)
    if sink is not None:
        sink.append(item)
    else:
        _remember(item)


class capture_notices:
    """with の間に、この実行の筋で走査と読む口が入れた知らせを集める（入れ子にできる）。items に、重ねない並びを返す。"""

    def __enter__(self):
        self._outer = getattr(_SINK, "items", None)
        _SINK.items = []
        return self

    def __exit__(self, *exc):
        found = _SINK.items
        _SINK.items = self._outer
        if self._outer is not None:
            self._outer.extend(found)
        else:
            for item in found:  # CLI では、受け皿で集めたものも、終わりに標準エラーへ出す
                _remember(item)
        self.items = dedupe_notices(found)
        return False


def dedupe_notices(items) -> list:
    seen, out = set(), []
    for item in items or ():
        key = (item["path"], item["kind"])
        if key not in seen:
            seen.add(key)
            out.append(dict(item))
    return out


def notice_lines(items) -> list:
    """知らせを、利用者に見せる 1 行ずつの文にする。"""
    out = []
    for item in dedupe_notices(items):
        if item["kind"] in NOTICE_UNVERIFIED:
            out.append(f"調べられない: {item['path']}（{'一覧を作れない' if item['kind'] == 'unlistable' else '属性を取れない'}）")
        else:
            out.append(f"リンクなので読まない: {item['path']}（{item['kind']}）")
    return out


def _print_process_notices() -> None:
    for line in notice_lines(list(_PROCESS_NOTICES.values())):
        print(line, file=sys.stderr)


# スクリプトのディレクトリのスクリプトを直接動かしたときだけ、終わりに出す（試験や、ほかのプログラムが読み込んだときは
# 出さない）。MCP のサーバーは、呼び出しごとに受け皿を開くので、ここには残らない。
def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.samefile(os.path.dirname(os.path.abspath(a)), os.path.dirname(os.path.abspath(b)))
    except OSError:
        return os.path.normcase(os.path.dirname(os.path.abspath(a))) == os.path.normcase(os.path.dirname(os.path.abspath(b)))


if sys.argv and sys.argv[0] and _same_dir(sys.argv[0], __file__) \
        and os.path.basename(sys.argv[0]).lower() not in ("wiki_mcp_server.py", "wiki_browse.py"):
    # 常駐するもの（MCP のサーバー、閲覧のサーバー）は貯めない。呼び出しごとに受け皿を開く
    _CLI = True
    atexit.register(_print_process_notices)


class MdFile(NamedTuple):
    """名前が .md で終わる、ふつうのファイル（リンクの先がふつうのファイルのものを含む）。"""
    rel: str        # wiki のルートからの相対パス（/ 区切り、ディスク上の綴り）
    size: int       # ファイルかどうかを調べた os.stat() の値
    mtime_ns: int


class Walk(NamedTuple):
    """走査の結果。知らせ（unlistable と unknown）がどちらも空なら、たどる間に変わらなかったものは欠けていない。

    links は、ADR-0029 の規則に反した配置で、読まなかったものである。files に入らないが、黙って消えたのではない。
    """
    files: list            # MdFile の並び
    unlistable: list       # 一覧を作れなかったディレクトリ（相対パス）
    unknown: list          # 種類を調べられなかった項目（相対パス）
    links: list = ()       # リンクなので読まず、降りなかった記事と、記事までの道（(相対パス, 種類) の並び。ADR-0029）


class NoFileId(OSError):
    """ファイルシステムが、識別子（FileIdInfo）を返さない。"""


# 失敗の分け方（設計の「決めること」の 2）。今の実装の扱い（3.12 と 3.13 の pathlib の _ignore_error は ENOENT、ENOTDIR、
# EBADF、ELOOP と Windows のエラー 21、123、1921 を無視する。3.14 の glob と os.path.isfile は、どの OSError も無視する）と
# 並べて決めた。木の状態として、障害なしに必ず起きる失敗（解けないリンク）は、送出しない。
_ABSENT = (FileNotFoundError, NotADirectoryError)
_UNRESOLVABLE_ERRNOS = (errno.ELOOP,)       # 解けないリンク。ページにならない
_UNRESOLVABLE_WINERRORS = (1921, 123)       # ERROR_CANT_RESOLVE_FILENAME、ERROR_INVALID_NAME
_UNKNOWN_ERRNOS = (errno.EBADF,)            # 木の状態としては起きないはずだが、今の実装は無視する。送出はしない
_UNKNOWN_WINERRORS = (21,)                  # ERROR_NOT_READY。準備のできていないドライブ。中身があるかが分からない


def read_os(call, *args, **kwargs):
    """OS の呼び出しの結果を (状態, 値) で返す。走査の失敗の読み替えは、ここだけで行う。

    状態は "ok"、"absent"（ない）、"denied"（分からない）。そのほかの OSError は送出する。
    """
    try:
        return "ok", call(*args, **kwargs)
    except _ABSENT:
        return "absent", None
    except (PermissionError, NoFileId):
        return "denied", None
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror in _UNRESOLVABLE_WINERRORS or (winerror is None and exc.errno in _UNRESOLVABLE_ERRNOS):
            return "absent", None
        if winerror in _UNKNOWN_WINERRORS or (winerror is None and exc.errno in _UNKNOWN_ERRNOS):
            return "denied", None
        raise


# Windows の識別子（ctypes。標準ライブラリだけ）。os.stat() の識別子は使わない。Python 3.12 以降の os.stat() は、
# FileIdInfo の取得の失敗を捨てて、st_dev を 64 ビットではなく 32 ビットの値にするからである。

_UNSUPPORTED_WINERRORS = (1, 50, 87)  # ERROR_INVALID_FUNCTION、ERROR_NOT_SUPPORTED、ERROR_INVALID_PARAMETER
_KERNEL32 = None


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

        class FileIdInfo(ctypes.Structure):
            _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", ctypes.c_ubyte * 16)]
        _KERNEL32 = (ctypes, k, FileIdInfo)
    return _KERNEL32


def win_file_id(path) -> "tuple[int, bytes]":
    """(VolumeSerialNumber, FileId) を返す（Windows だけ）。リンクの先をたどる。どの失敗も OSError で送出し、読み替えない。

    ないパスは FileNotFoundError、拒まれたパスは PermissionError（OSError の第 4 引数に Windows のエラーの番号を渡すと、
    Python が分ける）。ファイルシステムが FileIdInfo を返さないときは NoFileId。
    """
    ctypes, k, FileIdInfo = _kernel32()
    path = os.fspath(path)

    def error():
        code = ctypes.get_last_error()
        return OSError(None, ctypes.FormatError(code).strip(), path, code)
    # FILE_READ_ATTRIBUTES、読み書きと削除の共有、OPEN_EXISTING、FILE_FLAG_BACKUP_SEMANTICS（ディレクトリを開く）
    handle = k.CreateFileW(path, 0x80, 0x7, None, 3, 0x02000000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise error()
    try:
        info = FileIdInfo()
        if not k.GetFileInformationByHandleEx(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):  # 18 は FileIdInfo
            code = ctypes.get_last_error()
            if code in _UNSUPPORTED_WINERRORS:
                raise NoFileId(None, ctypes.FormatError(code).strip(), path, code)
            raise error()
        return info.VolumeSerialNumber, bytes(info.FileId)
    finally:
        if not k.CloseHandle(handle):
            raise error()


def _sort_key(rel: str) -> "tuple[str, str]":
    """並びの鍵。今の iter_page_files() の鍵（posix_rel）に、同じ鍵のときの決め方（ディスク上の綴り）を足したもの。"""
    return (posix_rel(rel), rel)


def _stat_entry(path: str):
    """(状態, stat の結果)。.md の項目と、最上位の記事（index.md、log.md、SCHEMA.md）を調べる os.lstat()。

    リンクをたどらない。リンクかどうか（wiki_links.link_kind）と、ふつうのファイルかどうかと、ハードリンクの
    リンクの数（st_nlink。Windows の DirEntry.stat() ではいつも 0 なので、この呼び出しでしか分からない）を、
    1 回の呼び出しで決める。

    Windows の os.lstat() は、属性の取得がエラー 1、50、87 で失敗すると、S_IFBLK の成功に読み替える。一覧に載る項目が
    ボリュームや物理ディスクであることはないので、Windows で S_IFBLK が返ったら「分からない」にする。POSIX の S_IFBLK は
    本物のデバイスのファイルなので、ふつうのファイルでない（今と同じ）。
    """
    state, st = read_os(os.lstat, path)
    if state == "ok" and NT and stat.S_ISBLK(st.st_mode):
        return "denied", None
    return state, st


def _list_dir(directory: str) -> list:
    # 項目を最後まで取り出してから閉じる。閉じるときの失敗は、CPython が捨てる（ScandirIterator_closedir が FindClose と
    # closedir の戻り値を見ない）が、取り出し終えた一覧は変わらない。
    with os.scandir(directory) as it:
        return list(it)


def _entry_lstat(entry):
    """(状態, stat の結果)。一覧の項目の、リンクをたどらない属性。Windows では一覧の情報（再解析のタグと属性）から
    答え、OS を呼ばない。POSIX では lstat を 1 回呼ぶ。st_nlink は使わない（Windows では 0 になる）。"""
    return read_os(entry.stat, follow_symlinks=False)


def _link_is_dir(entry, est):
    """(状態, ディレクトリへのリンクか)。Windows では属性の FILE_ATTRIBUTE_DIRECTORY で決め、OS を呼ばない（ジャンクションと
    ディレクトリのシンボリックリンクに立つ）。POSIX では、リンクの先をたどる is_dir() を呼ぶ。"""
    if NT:
        return "ok", bool((getattr(est, "st_file_attributes", 0) or 0) & 0x10)
    return read_os(entry.is_dir)


def walk_markdown(start, prefix: str = "", skip_top: bool = False, visited: "list | None" = None) -> Walk:
    """start の下をたどり、名前が .md で終わるふつうのファイルと、拒んだリンクと、知らせを返す（ADR-0029）。

    - 起点には、リンクであっても降りる（根と祖先のリンクは許す）。
    - 起点の下の、名前が . で始まる項目には、どの深さでも、どの呼び出しもしない（記事でない）。
    - skip_top が真なら、起点の直下の、名前が SKIP_TOP_LEVEL_DIRS に入っている項目には、どの呼び出しもしない。
    - リンク（wiki_links.link_kind が種類を返すもの）には降りず、読まない。ディレクトリへのリンクと、名前が .md で
      終わるもののリンクは、links に (相対パス, 種類) で入れる。名前が .md で終わらないファイルのリンクは、記事で
      ないので入れない。ハードリンク（リンクの数が 2 以上）の .md も、読まずに links に入れる。
    - prefix は、返す相対パスの先頭に付ける。並びは posix_rel の順、同じなら綴りの順である。
    - visited にリストを渡すと、一覧を作れたディレクトリの相対パス（起点は prefix から末尾の / を除いたもの。
      prefix が空なら ""）を足す。この一覧が降りる場所かを、規則を写さずに確かめるためのもの（YouTube の字幕の
      照合が、用意したページの場所を確かめるのに使う）。
    """
    files, unlistable, unknown, links = [], [], [], []
    start = os.fspath(start)
    stack = [(start, prefix)]
    while stack:
        directory, pre = stack.pop()
        if pre != prefix:
            # 一覧で見たあと、降りる前に、ディレクトリがリンクに替わっていないかを確かめる（起点は、リンクでも降りる）
            state, dst = _stat_entry(directory)
            if state == "absent":
                continue
            if state == "denied":
                unknown.append(pre.rstrip("/"))
                continue
            kind = link_kind(dst)
            if kind is not None:
                links.append((pre.rstrip("/"), kind))
                continue
            if not stat.S_ISDIR(dst.st_mode):
                continue
        state, entries = read_os(_list_dir, directory)
        if state == "absent":
            continue
        if state == "denied":
            unlistable.append(pre.rstrip("/") or ".")
            continue
        if visited is not None:
            visited.append(pre.rstrip("/"))
        for entry in entries:
            name = entry.name
            rel = pre + name
            if skip_top and pre == prefix and name in SKIP_TOP_LEVEL_DIRS:
                continue  # 名前だけで決める。リンクかどうかも、ディレクトリかどうかも調べない
            if name.startswith("."):
                continue  # 名前が . で始まる項目は、どの深さでも記事でない。どの呼び出しもしない
            state, est = _entry_lstat(entry)
            if state == "absent":
                continue
            if state == "denied":
                unknown.append(rel)
                continue
            kind = link_kind(est, nlink_known=False)
            if kind is not None:
                # 名前が .md で終わるリンクは、記事のリンクとして入れる。ただし起点の直下の README.md は記事でない
                # ので、ディレクトリへのリンク（記事までの道）のときだけ入れる（下の判定に回す）
                if name.lower().endswith(".md") and not (skip_top and pre == prefix and name == "README.md"):
                    links.append((rel, kind))
                    continue
                state, is_dir = _link_is_dir(entry, est)
                if state == "denied":
                    unknown.append(rel)
                elif state == "ok" and is_dir:
                    links.append((rel, kind))  # ディレクトリへのリンク。降りない
                continue  # .md でないファイルのリンクと、先のないリンクは、記事でない
            if stat.S_ISDIR(est.st_mode):
                stack.append((entry.path, rel + "/"))
                continue
            if name.lower().endswith(".md"):
                state, st = _stat_entry(entry.path)
                if state == "absent":
                    continue
                if state == "denied":
                    unknown.append(rel)
                    continue
                kind = link_kind(st)
                if kind is not None:
                    if kind == "hardlink" and skip_top and pre == prefix and name == "README.md":
                        continue  # 起点の直下の README.md は記事でない（ふつうのファイルのハードリンクは道でもない）
                    links.append((rel, kind))  # ハードリンク（一覧と lstat の間に置き換えられたリンクも）
                elif stat.S_ISREG(st.st_mode):
                    files.append(MdFile(rel, st.st_size, st.st_mtime_ns))
    files.sort(key=lambda f: _sort_key(f.rel))
    links.sort(key=lambda item: _sort_key(item[0]))
    for rel, kind in links:
        _notice(rel, kind)
    for rel in unknown:
        _notice(rel, "unknown")
    for rel in unlistable:
        _notice(rel, "unlistable")
    return Walk(files, sorted(unlistable, key=_sort_key), sorted(unknown, key=_sort_key), links)


def walk_pages(wiki_root, visited: "list | None" = None) -> Walk:
    """ページのファイルと、知らせ。知らせは、たどった全体のものをそのまま返す。visited は walk_markdown と同じ。

    links は、走査が拒んだものをそのまま返す（最上位の index.md、log.md、SCHEMA.md のリンクも入る。どの記事の
    リンクを入れるかは walk_markdown の 1 か所で決め、使う側で絞らない）。
    """
    found = walk_markdown(wiki_root, skip_top=True, visited=visited)
    return Walk([f for f in found.files if is_page_path(f.rel, native=True)], found.unlistable, found.unknown, found.links)


def walk_index_files(wiki_root) -> Walk:
    """索引のファイル（最上位の index.md と、indexes を起点にした .md）と、拒んだリンクと、知らせ。

    最上位の index.md と indexes そのものがリンクなら、読まず、降りず、links に入れる（ADR-0029。indexes は記事までの道）。
    """
    root = os.fspath(wiki_root)
    files, unknown, links = [], [], []
    state, st = _stat_entry(os.path.join(root, "index.md"))
    if state == "denied":
        unknown.append("index.md")
        _notice("index.md", "unknown")
    elif state == "ok":
        kind = link_kind(st)
        if kind is not None:
            links.append(("index.md", kind))
            _notice("index.md", kind)
        elif stat.S_ISREG(st.st_mode):
            files.append(MdFile("index.md", st.st_size, st.st_mtime_ns))
    state, ist = _stat_entry(os.path.join(root, "indexes"))
    if state == "denied":
        _notice("indexes", "unknown")
        return Walk(files, [], unknown + ["indexes"], links)
    if state == "absent":
        return Walk(files, [], unknown, links)
    kind = link_kind(ist)
    if kind is not None and kind != "hardlink":  # ふつうのファイルのハードリンクは、記事でも記事までの道でもない
        _notice("indexes", kind)
        return Walk(files, [], unknown, links + [("indexes", kind)])
    below = walk_markdown(os.path.join(root, "indexes"), prefix="indexes/")
    return Walk(files + below.files, below.unlistable, unknown + below.unknown, links + below.links)


TOP_ARTICLES = ("index.md", "log.md", "SCHEMA.md")


def is_article_path(rel: "str | PurePosixPath", *, native: bool = False) -> bool:
    """wiki のルートからの相対パス（/ 区切り）が、wiki の側の記事かを、名前の規則だけで返す（ADR-0029 の作者の決定の 6）。

    記事は、ページ（is_page_path）と、最上位の index.md、log.md、SCHEMA.md と、indexes の下の .md（どの要素も名前が
    . で始まらないもの）である。リンクかどうかは定義に入れない。raw の記事は、source ページの raw: で決まる。
    """
    parts = PurePosixPath(_relative_text(rel, native)).parts
    if not parts or any(part.startswith(".") for part in parts):
        return False
    if len(parts) == 1 and parts[0] in TOP_ARTICLES:
        return True
    if parts[0] == "indexes" and len(parts) > 1:
        return parts[-1].lower().endswith(".md")
    return is_page_path(rel, native=native)


class ArticlePath(NamedTuple):
    """記事を読む口の結果。state は "ok"、"absent"（ない、ふつうのファイルでない）、"denied"（分からない）、
    "link"（記事か記事までの道がリンク。link に (相対パス, 種類)）、"not_article"（名前の規則で記事でない）。"""
    state: str
    path: "Path | None" = None
    link: "tuple | None" = None


def article_path(wiki_root, rel, *, directory: bool = False, native: bool = False) -> ArticlePath:
    """記事を読む口（ADR-0029 の I2、I4）。根から rel までの道と rel そのものに、走査と同じ判定を当てる。

    根そのものと、その祖先のリンクは許す。rel の要素ごとに os.lstat() を呼び、リンクに当たれば読まずに "link" を返す。
    directory が真なら、記事を含みうるディレクトリまでの道を検査する。
    native が真なら、POSIX のファイル名に含まれるバックスラッシュを区切りに変えない。
    呼ぶ側は、"ok" のときだけ path を読むか降りる。拒んだことを「ない」に変えない。
    """
    rel = _relative_text(rel, native)
    if directory and not rel:
        return ArticlePath("ok", Path(wiki_root))  # 起点そのもののリンクは許す
    parts = rel.split("/")
    # 根の外へ出る名前（絶対パス、ドライブ、..、空の要素）は、記事の名前ではない
    if rel.startswith("/") or any(part in ("", ".", "..") or (NT and ":" in part) for part in parts):
        return ArticlePath("not_article")
    if not is_article_path(rel + "/article.md" if directory else rel, native=native):
        return ArticlePath("not_article")
    current = os.fspath(wiki_root)
    st = None
    for i, part in enumerate(parts):
        current = os.path.join(current, part)
        state, st = _stat_entry(current)
        if state == "denied":
            _notice("/".join(parts[:i + 1]), "unknown")
        if state != "ok":
            return ArticlePath(state)
        kind = link_kind(st)
        if kind is not None:
            _notice("/".join(parts[:i + 1]), kind)
            return ArticlePath("link", link=("/".join(parts[:i + 1]), kind))
    if not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
        return ArticlePath("absent")
    return ArticlePath("ok", Path(current))


def resolve_raw(meta, page_path, wiki_root, wiki_root_given=None, allowed_roots=None, *, raw_roots=()):
    """raw の共通の読む口。リンクの検査と版の照合は wiki_raw にまとめる。"""
    from wiki_raw import resolve_raw as resolve
    return resolve(meta, page_path, wiki_root, wiki_root_given, allowed_roots, raw_roots=raw_roots)


def absolute_user_path(path) -> Path:
    """利用者のパスを、意味を変えずに絶対化する。途中の .. は解決する前に拒む（ADR-0029）。"""
    if ".." in Path(path).parts:
        raise ValueError(f".. を含むパスの所属は決められない。.. を含まないパスを指定する: {path}")
    return Path(os.path.abspath(os.fspath(path)))


def user_file_path(path, wiki_root=None, *, directory: bool = False) -> Path:
    """記事と記事までの道を検査し、同じパスを返す。記事でない入力は規則の対象外。"""
    path = Path(path)
    absolute_user_path(path)  # 所属を調べない場合も、検査後と読み取り時で意味が変わる名前を受け取らない
    if wiki_root is not None:
        found = _article_rel(path, wiki_root)
        if found is not None:
            root, rel = found
            if directory and not rel:
                return Path(root)
            if not is_article_path(rel + "/article.md" if directory else rel, native=True):
                return path
            opened = article_path(root, rel, directory=directory, native=True)
            if opened.state == "link":
                raise ValueError(f"リンクなので読まない: {opened.link[0]}（{opened.link[1]}）")
            if opened.state == "denied":
                raise ValueError(f"調べられない: {rel}")
            if opened.state == "ok":
                return opened.path
            if opened.state == "absent":
                raise FileNotFoundError(f"記事がないか、通常のファイルでない: {rel}")
            # 記事の定義に入る名前を、読む口の入力検証の失敗で対象外に落とさない
            _notice(rel, "unknown")
            raise ValueError(f"記事として検査できないので読まない: {rel}")
    return path


def user_directory_path(path, wiki_root=None) -> Path:
    """記事を含みうるディレクトリを、降りる前に検査する。"""
    return user_file_path(path, wiki_root, directory=True)


def read_user_file(path, wiki_root=None, encoding: str = "utf-8-sig") -> str:
    """記事かどうかを判定し、記事ならリンクを拒んでから本文を読む。"""
    return user_file_path(path, wiki_root).read_text(encoding=encoding)


def read_user_bytes(path, wiki_root=None) -> bytes:
    """read_user_file と同じ規則で、改行や BOM を変えずに読む。"""
    return user_file_path(path, wiki_root).read_bytes()


def _article_rel(path, wiki_root) -> "tuple[str, str] | None":
    """path が wiki の中なら (比べた根, 根からの / 区切りの相対パス)。外なら None。

    根の別名を通る入力は、祖先を上から解決し、既知の根に届いたところで止める。根より下の要素は解決しない。
    外のファイルの別名も展開する。根より下のファイルのリンクは、記事を読む口で検査する。
    大小文字の比較と、相対パスの要素の切り出しを分ける。
    """
    given = os.fspath(absolute_user_path(path))
    roots = [os.fspath(absolute_user_path(wiki_root))]
    try:
        roots.append(os.path.realpath(roots[0]))
    except OSError:
        pass
    def relative(candidate, root):
        parts, base = Path(candidate).parts, Path(root).parts
        if len(parts) < len(base) or any(os.path.normcase(a) != os.path.normcase(b)
                                         for a, b in zip(parts, base)):
            return None
        return "/".join(parts[len(base):])

    for root in roots:
        rel = relative(given, root)
        if rel is not None:
            return root, rel
    # 外の別名は、リンクの行き先を 1 つずつ開く。realpath で連鎖をまとめて
    # 解くと、wiki の中から外へ出るリンクまで解き、wiki の所属を失ってしまう。
    pending = list(Path(given).parts)
    current = Path(pending.pop(0))
    followed = 0

    def found(root):
        if ".." in pending:
            raise ValueError("リンクの行き先に wiki 内の .. があるので、所属を決められない")
        return root, "/".join(pending)

    while True:
        for root in roots:
            if relative(str(current), root) == "":
                return found(root)
            # current は、起点か、リンクを展開し終えた通常の祖先である。
            try:
                same_root = os.path.samefile(current, root)
            except OSError:
                same_root = False
            if same_root:
                return found(root)
        if not pending:
            return None
        name = pending.pop(0)
        if name == ".":
            continue
        if name == "..":
            current = current.parent
            continue
        current = current / name
        # 明示された根そのもののリンクは許す。根より下には進まない。
        for root in roots:
            if relative(str(current), root) == "":
                return found(root)
        state, st = _stat_entry(os.fspath(current))
        if state == "absent":
            return None
        if state == "denied":
            raise ValueError(f"パスの所属を調べられない: {current}")
        if link_kind(st, nlink_known=False) is not None:
            followed += 1
            if followed > 64:
                raise ValueError(f"リンクが循環しているか、多すぎるので所属を決められない: {path}")
            try:
                target = Path(os.readlink(current))
            except (OSError, ValueError) as exc:
                raise ValueError(f"リンクの行き先を調べられない: {current}") from exc
            if target.is_absolute():
                target_parts = list(target.parts)
                current = Path(target_parts.pop(0))
            else:
                target_parts = list(target.parts)
                current = current.parent
            pending = target_parts + pending
            continue


def source_wiki_root(path, wiki_root=None):
    """部分木は元の wiki の記事規則を引き継ぐ。wiki 外のコーパスだけを独立した根として扱う。"""
    if wiki_root is not None and _article_rel(path, wiki_root) is not None:
        return wiki_root
    return path


def link_warnings(*walks) -> list:
    """走査の結果の links を、結果の欄 link_warnings の形（{"path", "kind"} の並び）にする。重ねて出さない。"""
    seen, out = set(), []
    for walk in walks:
        for rel, kind in getattr(walk, "links", ()) or ():
            if rel not in seen:
                seen.add(rel)
                out.append({"path": rel, "kind": kind})
    return out


ASSETS_DIR = "assets"


def _is_figure_name(name: str) -> bool:
    """閲覧が図として読む svg の名前か（ADR-0030）。"""
    return name.lower().endswith(".svg") and not name.startswith(".")


def walk_assets(wiki_root) -> Walk:
    """wiki/assets/ の直下の svg（閲覧が図として出すもの。ADR-0030）と、知らせ。

    - assets そのものがシンボリックリンクかジャンクションなら、たどらない（何も返さない）。wiki の外の実体を、
      閲覧の出力に写さないためである。ファイルのシンボリックリンクも返さない。ハードリンクは見分けない。
    - 名前の末尾が .svg（大文字と小文字を区別しない）で、. で始まらない、ふつうのファイルだけを返す。
      下位のディレクトリには降りない。
    - 守るのは、走査の時点で木に置かれたリンクである。読む間だけリンクに替えて戻す同時の書き手は対象にしない
      （図の描画の設計の 6.1）。
    OS の呼び出しは、walk_markdown と同じ口（_list_dir、_stat_entry）だけを使う。
    リンクの種類は wiki_links.link_kind で決め、記事と違ってハードリンクは拒まない。
    """
    root = os.fspath(wiki_root)
    files, unlistable, unknown = [], [], []
    state, entries = read_os(_list_dir, root)
    if state == "denied":
        return Walk([], ["."], [])
    top = next((e for e in entries or [] if e.name == ASSETS_DIR), None)
    if top is None:
        return Walk([], [], [])
    state, st = _stat_entry(top.path)
    if state == "absent":
        return Walk([], [], [])
    if state == "denied":
        return Walk([], [], [ASSETS_DIR])
    if link_kind(st, nlink_known=False) is not None:
        return Walk([], [], [])            # assets がリンクかジャンクション。たどらない
    if not stat.S_ISDIR(st.st_mode):
        return Walk([], [], [])            # assets がディレクトリでない
    state, children = read_os(_list_dir, top.path)
    if state == "absent":
        return Walk([], [], [])
    if state == "denied":
        return Walk([], [ASSETS_DIR], [])
    for entry in children:
        name = entry.name
        if not _is_figure_name(name):
            continue
        rel = f"{ASSETS_DIR}/{name}"
        state, st = _stat_entry(entry.path)
        if state == "absent":
            continue
        if state == "denied":
            unknown.append(rel)
            continue
        if link_kind(st, nlink_known=False) is not None:
            continue                       # ファイルのリンク。ハードリンクは許す
        if stat.S_ISREG(st.st_mode):
            files.append(MdFile(rel, st.st_size, st.st_mtime_ns))
    files.sort(key=lambda f: _sort_key(f.rel))
    return Walk(files, unlistable, sorted(unknown, key=_sort_key))


def iter_page_files(wiki_root: Path) -> list[Path]:
    """ページのファイルを、NFC の / 区切りの相対パスの順に返す（知らせは捨てる。知らせを見るなら walk_pages）。"""
    wiki_root = Path(wiki_root)
    return [wiki_root / f.rel for f in walk_pages(wiki_root).files]


# ---------------------------------------------------------------------------
# 本文の解析（同梱した markdown-it-py）
# ---------------------------------------------------------------------------
#
# 本文のどこがコードで、どこが見出しかは、閲覧が使う markdown-it-py（commonmark、html は無効、表は有効）で
# 決める。規則を手で写すと、まれな形で閲覧と食い違うので、解析器そのものを _vendor/ に同梱して使う
# （lint と統計は、何も入れずに動く）。

MARKDOWN_IT_VERSION = "3.0.0"
MDURL_VERSION = "0.1.2"
VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"
# 検索のパースキャッシュに記録する解析器の名前。解析器が変わったら、キャッシュを作り直す。


def _vendor_digest() -> str:
    """同梱した解析器の .py の中身のハッシュ。版の番号を変えずに直したときも、パースキャッシュを作り直すため。"""
    digest = hashlib.sha256()
    for path in sorted(VENDOR_DIR.rglob("*.py"), key=lambda p: p.relative_to(VENDOR_DIR).as_posix()):
        if "__pycache__" not in path.parts:
            digest.update(path.relative_to(VENDOR_DIR).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:12]


PARSER_ID = f"markdown-it-py {MARKDOWN_IT_VERSION} / mdurl {MDURL_VERSION} / {_vendor_digest()}"
# セクションの分け目になる見出しの行（検索の split_sections の HEADING_RE と同じく、行頭の # のあとに空白と文字）。
# 中身のない見出し（# だけの行）では分けない。
_SECTION_HEADING_RE = re.compile(r"#{1,6}[ 	]+\S")


def _load_vendored():
    """同梱した markdown_it と mdurl を読み込む。別の場所のものが先に読み込まれていても、同梱したものに替える。"""
    names = ("markdown_it", "mdurl")
    vendor = VENDOR_DIR.resolve()

    def vendored(mod) -> bool:
        # 文字列の前方一致ではなく、パスとして _vendor/ の中にあるかで見る（_vendor_old/ は同梱ではない）。
        file = getattr(mod, "__file__", None)
        return bool(file) and vendor in Path(file).resolve().parents

    foreign = [k for k, mod in list(sys.modules.items()) if k.split(".")[0] in names and not vendored(mod)]
    for k in foreign:
        del sys.modules[k]
    if str(VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(VENDOR_DIR))
    else:
        sys.path.remove(str(VENDOR_DIR))
        sys.path.insert(0, str(VENDOR_DIR))
    import markdown_it as mdit
    import mdurl as url
    for mod, version in ((mdit, MARKDOWN_IT_VERSION), (url, MDURL_VERSION)):
        file = Path(mod.__file__).resolve()
        if getattr(mod, "__version__", None) != version or VENDOR_DIR.resolve() not in file.parents:
            raise ImportError(
                f"同梱した {mod.__name__} {version} ではなく、{getattr(mod, '__version__', '?')}（{file}）が"
                f"読み込まれた。閲覧と同じ規則で本文を読めないので止める。")
    return mdit


markdown_it = _load_vendored()
import markdown_it.common.utils as _md_utils  # noqa: E402  （同梱のものを読み込んだあと）


_SRC_PARSER = None


def destination_src(raw: str) -> str:
    """本文に書いた画像とリンクの参照先から、markdown-it がトークンの src（href）に入れる文字を作る。

    markdown-it と同じ手順（Markdown のエスケープと文字参照を解く unescapeAll のあと、normalizeLink で前後の
    空白を落としてパーセントエンコードする）を、同じ関数で通す。lint の画像の参照先の検査と閲覧が、作りの上で
    同じ参照先を見るために使う（図の描画の設計の 6.4）。呼ぶ側は、このあとで # と ? から後を落とし、unquote する。
    """
    global _SRC_PARSER
    if _SRC_PARSER is None:
        _SRC_PARSER = new_parser()
    return _SRC_PARSER.normalizeLink(_md_utils.unescapeAll(raw))


def new_parser():
    """本文を読むパーサーを新しく作る（同梱の markdown-it-py、commonmark、生の HTML は無効、表は有効）。

    lint などがリンクを拾うパーサーも、閲覧が描くパーサーも、これで作る。設定が同じなので、同じ本文から
    同じトークンができる。閲覧は、描くための規則を、自分で作ったパーサーにだけ足す。
    """
    return markdown_it.MarkdownIt("commonmark", {"html": False}).enable("table")


_MD = new_parser()
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def split_lines(body: str) -> list[str]:
    """Markdown と同じく、\\r\\n、\\r、\\n だけで行に分ける（str.splitlines は U+2028 などでも分ける）。"""
    return _LINE_SPLIT_RE.split(body)


def _splits_section(tok, lines: "tuple[str, ...]") -> bool:
    """見出しのトークンが、セクションの分け目になるか。行頭（字下げなし、引用やリストの外）の ATX の見出しだけを
    分け目にする（検索のセクションの分け方を、0.8.0 までと同じく行頭の # に限る）。"""
    return (tok.markup.startswith("#") and tok.map is not None and tok.level == 0
            and _SECTION_HEADING_RE.match(lines[tok.map[0]]) is not None)


@functools.lru_cache(maxsize=32)
def _analyze(lines: "tuple[str, ...]") -> "tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]":
    """本文の行を 1 度だけ解析し、(行ごとの種類, 行ごとの wikilink) を返す。

    種類は "code"（コードブロックの行）、"heading"（セクションの分け目になる見出しの行）、"other" のどれか。
    wikilink は、markdown-it が本文の文字とするところ（inline の中の text）から拾い、その inline が始まる行に
    置く。セクションの分け目（行頭の見出し）が段落や表の行の途中に来ることはないので、セクションごとのリンクは、
    セクションの行に置いたリンクを集めれば本文全体の解析と同じになる（参照リンクの定義が別のセクションに
    あっても同じ）。同じ本文を、リンクの抜き出しとセクションの分割で 2 度解析しないよう、結果を覚えておく。
    """
    kinds = ["other"] * len(lines)
    links: list[list[str]] = [[] for _ in lines]
    heading_splits = False
    for tok in _MD.parse("\n".join(lines)):
        if tok.type in ("fence", "code_block") and tok.map is not None:
            start, end = tok.map
            for i in range(start, min(end, len(lines))):
                kinds[i] = "code"
        elif tok.type == "heading_open":
            heading_splits = _splits_section(tok, lines)
            if heading_splits:
                kinds[tok.map[0]] = "heading"
        elif tok.type == "heading_close":
            heading_splits = False
        elif tok.type == "inline" and tok.map is not None:
            found = [nfc(m.group(1).strip())
                     for child in tok.children or [] if child.type == "text" and "[[" in child.content
                     for m in WIKILINK_RE.finditer(child.content)]
            if found:
                links[tok.map[0]].extend(found)
    return tuple(kinds), tuple(tuple(x) for x in links)


def extract_wikilinks(body: str, headings: bool = True) -> list[str]:
    """本文の wikilink の行き先（別名を除いたもの）を、現れた順に返す。NFC にそろえ、前後の空白を除く。

    閲覧と同じく、markdown-it が本文の文字（inline の中の text）とするところだけから拾う。コードブロック、
    インラインのコード、参照リンクの定義、リンクの行き先とタイトルの中のものは数えない。escape と文字参照は、
    元の文字に戻ったあとで探す（`\\[\\[x]]` は [[x]] と、`[[R&amp;D]]` は [[R&D]] と書いたのと同じになる）。
    headings が偽なら、セクションの分け目になる見出し（heading_line_mask）の中のリンクを除く。
    """
    kinds, links = _analyze(tuple(split_lines(body)))
    return [link for kind, line in zip(kinds, links) if headings or kind != "heading" for link in line]


def links_by_line(lines: list[str]) -> list[list[str]]:
    """行ごとの wikilink を返す。段落や表の行などのリンクは、それが始まる行に置く。"""
    return [list(x) for x in _analyze(tuple(lines))[1]]


def code_line_mask(lines: list[str]) -> list[bool]:
    """行ごとに、コードブロック（フェンスと字下げ）の行なら True を返す。"""
    return [kind == "code" for kind in _analyze(tuple(lines))[0]]


def heading_line_mask(lines: list[str]) -> list[bool]:
    """行ごとに、セクションの分け目になる見出し（行頭の ATX の見出し）の行なら True を返す。

    コードブロックの中の `# ` や、`#　見出し`（# のあとが全角空白）は、閲覧でも見出しにならないので含まない。
    """
    return [kind == "heading" for kind in _analyze(tuple(lines))[0]]


# ---------------------------------------------------------------------------
# リンクの解決
# ---------------------------------------------------------------------------

def clean_link_target(link: str) -> str:
    """wikilink から別名、アンカー、.md を取り除き、パスだけにする。NFC にそろえる。

    全角の「｜」「＃」は区切りとして扱わない（Obsidian と同じ）。
    """
    target = nfc(link).split("|", 1)[0]   # 表示用の別名があれば取り除く
    target = target.split("#", 1)[0]      # 見出しのアンカーを取り除く
    target = target.replace("\\", "/").strip().strip("/")
    if target.lower().endswith(".md"):
        target = target[:-3]
    return nfc(target.strip())


def build_link_index(pages: list[dict]) -> tuple[dict, set]:
    """wikilink を解決するための (パスからスラッグへの対応, {スラッグ}) を返す。

    pages の各項目は rel_path（wiki からの相対パス）と slug を持つ。
    """
    by_path = {}
    for p in pages:
        rel = posix_rel(p["rel_path"])
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        by_path[rel] = p["slug"]
    return by_path, {p["slug"] for p in pages}


def link_lookup(link: str) -> "tuple[str, str] | None":
    """wikilink を、何で引くかに分ける。("path", .md を除いた相対パス) か ("slug", スラッグ) を返す。

    パスのない形（[[kalman-filter]]）はスラッグで、パスを付けた形（[[entities/kalman-filter]]）は
    そのパスで引く。行き先が空なら None を返す。
    """
    target = clean_link_target(link)
    if not target:
        return None
    return ("path", target) if "/" in target else ("slug", target)


def link_candidates(link: str, pages_by_path: dict, pages_by_slug: dict) -> list:
    """wikilink の規則（resolve_link と同じ）に当たるページを、すべて返す。

    pages_by_path は .md を除いた相対パスから、pages_by_slug はスラッグから、ページ（を表す値）のリストを
    引く。1 つならそのページ、2 つ以上なら曖昧、空なら未解決である。resolve_link はスラッグを返すので、
    同じスラッグのページのどれかを区別できない。ページを 1 つに決めたいときはこちらを使う。
    """
    lookup = link_lookup(link)
    if lookup is None:
        return []
    kind, target = lookup
    return list((pages_by_path if kind == "path" else pages_by_slug).get(target) or [])


_FM_WIKILINK_RE = re.compile(r"^\[\[(.+)\]\]$")


def ref_values(value) -> list[str]:
    """frontmatter のリストの値（文字列 1 つでもよい）を、空でない文字列のリストにする。"""
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        value = [value]
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def text_value(value) -> str:
    """frontmatter の 1 つの文字を入れる欄（title など）の値を、文字にする。

    パーサーは書かれた形のまま返すので、`title:` は空の配列、`title: [WIP]` は 1 要素の配列になる。
    文字はそのまま（前後の空白も変えずに）返す。それ以外は ref_values() でそろえ、要素を「, 」でつなぐ。
    値がないか、空でない要素がなければ "" を返す。呼ぶ側は、"" ならスラッグなどを使う。
    """
    if isinstance(value, str):
        return value
    return ", ".join(ref_values(value))


def scalar_value(value):
    """frontmatter の 1 つの値を入れる欄（updated、created など）の値を、文字か None にする。

    文字と None は、そのまま返す（"" も変えない）。それ以外は text_value() と同じく、空でない要素を「, 」でつなぎ、
    空なら None を返す。`updated: [2026-03-01]` は "2026-03-01"、`updated:`（空の配列）は None になる。
    """
    if value is None or isinstance(value, str):
        return value
    return text_value(value) or None


def type_value(value):
    """frontmatter の type の値を、文字か None にする。scalar_value() と同じ規則で、配列の要素の日本語の別名
    （概念など）も英語に直す。文字の type は、読み込みのとき（canonical_meta）に直してあるので、そのまま返す。
    """
    if value is None or isinstance(value, str):
        return value
    return ", ".join(canonical_type(v) for v in ref_values(value)) or None


def kind_value(value):
    """frontmatter の kind の値を、type_value() と同じ規則で、文字か None にする。"""
    if value is None or isinstance(value, str):
        return value
    return ", ".join(canonical_kind(v) for v in ref_values(value)) or None


def ref_link(ref: str) -> str:
    """frontmatter のページへの参照（`sources:` の値など）を、wikilink の中身にする。

    `paper` と `sources/paper` はそのまま、`[[sources/paper]]` は角括弧を外す。
    """
    m = _FM_WIKILINK_RE.match(ref)
    return m.group(1) if m else ref


def build_page_index(pages: list[dict], value=lambda p: p) -> "tuple[dict, dict]":
    """link_candidates に渡す (パスからの対応, スラッグからの対応) を作る。値は value(p) のリストである。

    パスは rel_path（NFC）から .md を除いたもの、スラッグは p["slug"] である。
    """
    pages_by_path: dict = defaultdict(list)
    pages_by_slug: dict = defaultdict(list)
    for p in pages:
        rel = posix_rel(p["rel_path"])
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        pages_by_path[rel].append(value(p))
        pages_by_slug[p["slug"]].append(value(p))
    return pages_by_path, pages_by_slug


def classify_source_ref(ref: str, pages_by_path: dict, pages_by_slug: dict, page_type,
                        *, link_warnings=None) -> "tuple[str, list, object]":
    """`sources:` の値 1 つを分ける。(状態, 当たるページ, source でない type) を返す。

    値は ref_link で外側の [[ ]] を外し、wikilink と同じ規則（link_candidates）でページを探す。状態は次のどれかである。
    - "missing": 当たるページがない。
    - "not_source": 当たるページのうち type のあるものが 1 つ以上あり、どれも source でない。3 つ目の値は最初の type。
    - "ok": それ以外。
    - "refused": link_warnings が渡され、リンクなので読まなかった記事を指す。
    lint の unresolved_sources（missing と not_source）と、グラフの抽出の sourced_from が、この関数を共有する。
    当たるページが 2 つ以上かどうか（曖昧さ）は、状態に含めない。呼び出す側が、当たるページの数で見る。
    page_type は、ページ（pages_by_* の値）から type を返す。
    """
    if link_warnings and refused_link(ref_link(ref), link_warnings) is not None:
        return "refused", [], None
    found = link_candidates(ref_link(ref), pages_by_path, pages_by_slug)
    if not found:
        return "missing", [], None
    types = [t for t in (page_type(x) for x in found) if t not in (None, "", [])]
    if types and "source" not in types:
        return "not_source", found, types[0]
    return "ok", found, None


def resolve_link(link: str, by_path: dict, by_slug) -> "str | None":
    """wikilink を、それが指すページのスラッグに解決する。解決できなければ None を返す。

    パスのない形（[[kalman-filter]]）はスラッグで、パスを付けた形（[[entities/kalman-filter]]）は
    そのパスがちょうどあるときだけ解決する。ディレクトリの接頭辞は飾りではなく制約なので、
    [[raw/foo]] が sources/foo.md に黙って結び付くことはない。照合は大文字と小文字を区別する。
    誤った「リンク切れ」の報告は目に見えて害がないが、誤った解決は気づかれない。
    """
    lookup = link_lookup(link)
    if lookup is None:
        return None
    kind, target = lookup
    if kind == "path":
        return by_path.get(target)
    return target if target in by_slug else None


def refused_link(link: str, warnings: list, *, articles: bool = False) -> "dict | None":
    """読まなかった記事か、その途中の道を指す wikilink なら、その知らせを返す。

    降りなかったディレクトリの下のスラッグは推測しない。パスを指定したときだけ、
    要素の境界で照合する。lint と閲覧が、拒否をリンク切れに読み替えないために共用する。
    articles は Index の行用で、パスで指す索引の記事も含める。本文と sources はページだけを照合する。
    """
    lookup = link_lookup(link)
    if lookup is None:
        return None
    kind, target = lookup
    accepts = is_article_path if articles and kind == "path" else is_page_path
    for item in warnings:
        if item.get("scope") == "raw":
            continue  # raw の同名のファイルは、wiki のページの拒否ではない
        # OS 上の読み取りのパスは変えず、wikilink の照合だけをページの索引と同じ鍵にする。
        rel = posix_rel(item["path"])
        if kind == "path" and accepts(target + ".md", native=True) and target.startswith(rel + "/"):
            return item
        if rel.lower().endswith(".md") and accepts(rel, native=True):
            stem = rel[:-3]
            expected = stem if kind == "path" else PurePosixPath(stem).name
            if target == expected:
                return item
    return None


# ---------------------------------------------------------------------------
# 大文字と小文字だけが違うパス
# ---------------------------------------------------------------------------

def case_key(text: str) -> str:
    """大文字と小文字を同一視して比べるためのキー。casefold の前後で NFC にそろえる。"""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def case_only_duplicates(rel_paths: list[str]) -> list[dict]:
    """大文字と小文字だけが違うパスの組を返す。

    rel_paths はページの wiki からの相対パス。ページのパスと、その途中のディレクトリのパスを NFC に
    そろえ、case_key で同じになるのに綴りが違うものを {"kind", "paths"} で返す。kind は、ページの
    ファイルどうしなら "file"、ディレクトリを含むなら "dir" である。NFC にそろえると同じになるだけの
    もの（正規化の違い）は、ここでは挙げない（lint の duplicate_slugs が挙げる）。
    """
    # ファイルはファイルどうし、ディレクトリはディレクトリどうしで比べる。Foo.md と foo.MD/ は、閲覧の書き出しで
    # Foo.html と foo.MD/ になり、同じ場所を取らない。
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rel in rel_paths:
        parts = posix_rel(rel).split("/")
        for i in range(1, len(parts) + 1):
            spelled = "/".join(parts[:i])
            kind = "file" if i == len(parts) else "dir"
            groups[(kind, case_key(spelled))].add(spelled)
    found = []
    for (kind, _key), spellings in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
        if len(spellings) > 1:
            found.append({"kind": kind, "paths": sorted(spellings)})
    return found
