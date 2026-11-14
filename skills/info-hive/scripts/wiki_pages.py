"""
wiki_pages.py — wiki のページの集合と、wikilink の抜き出しと解決の規則。

lint、検索（とそれを使う MCP の索引）、グラフ、統計、判定レイヤーが共通で使う。のちに閲覧（browse）の
中核も使う。規則を 1 か所に置くのは、スクリプトごとに規則を書くと食い違うからである（ADR-0010 の決定の 1）。
標準ライブラリ、wiki_text、同梱した markdown-it-py（_vendor/）だけで書き、単独では実行しない。

- ページは、wiki のルートからの相対パスが `.md`（大文字と小文字は問わない）で終わり、最上位の SCHEMA.md、
  index.md、log.md、README.md と、最上位の indexes/、graph/、raw/、assets/、.evolution/、.wiki-cache/ に
  当たらず、ファイル名が `.` で始まらないファイルである。
- wikilink は、閲覧が使う markdown-it（commonmark、html は無効、表は有効）が本文の文字とするところだけから
  拾う。コードブロックの中、インラインのコードの中、参照リンクの定義の中のものは数えない。
- パスとリンクの行き先は NFC にそろえて比べ、大文字と小文字を区別する。
- wiki をたどる経路は walk_markdown() の 1 つで、ページ（walk_pages）と索引のファイル（walk_index_files）は、
  起点を変えて同じたどり方を使う。最上位の除く名前の項目には、どの OS の呼び出しもしない。OS の呼び出しの失敗は、
  read_os() の 1 か所で「ない」「分からない」「そのほか（送出）」に分ける。設計は info-hive-dev の
  `docs/plans/wiki-walk-excluded-dirs-2026-11-11.md`（7 版目）にある。
"""

from __future__ import annotations

import errno
import functools
import hashlib
import os
import re
import stat
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from wiki_text import canonical_kind, canonical_type, nfc, posix_rel

SKIP_TOP_LEVEL_FILES = frozenset({"SCHEMA.md", "index.md", "log.md", "README.md"})
SKIP_TOP_LEVEL_DIRS = frozenset({"indexes", "graph", "raw", "assets", ".evolution", ".wiki-cache"})

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# WIKILINK_RE と同じものに当たり、別名も取り出す（1 つ目が行き先、2 つ目が別名）。閲覧が wikilink を描くときに使う。
# 当たる範囲が WIKILINK_RE と同じなので、描くものと extract_wikilinks が拾うものは一致する。
WIKILINK_FULL_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


# ---------------------------------------------------------------------------
# ページの集合
# ---------------------------------------------------------------------------

def is_page_path(rel: "str | PurePosixPath") -> bool:
    """wiki のルートからの相対パス（/ 区切り）がページかを返す。"""
    parts = PurePosixPath(str(rel).replace("\\", "/")).parts
    # 拡張子は大文字と小文字を区別しない。Windows の rglob("*.md") は Upper.MD も返し、0.8.0 までもページだった。
    if not parts or not parts[-1].lower().endswith(".md") or parts[-1].startswith("."):
        return False
    if len(parts) == 1:
        return parts[0] not in SKIP_TOP_LEVEL_FILES
    return parts[0] not in SKIP_TOP_LEVEL_DIRS


# ---------------------------------------------------------------------------
# wiki をたどる
# ---------------------------------------------------------------------------
#
# 走査が OS を呼ぶ口は、os.scandir()（開く、項目を 1 つ取り出す、閉じる）、項目の is_symlink()、is_dir()、
# is_junction()、os.stat()、Windows の win_file_id() だけである。どれも、呼ぶたびに os から引いて呼ぶ
# （試験が差し替えられるように）。pathlib の入出力、os.walk、os.path.realpath、os.path.isfile は使わない。内側で
# いくつもの呼び出しをして、失敗を版ごとに違う形で読み替えるからである。口の各行の内側の扱いと、それが結果を
# 変えない理由は、設計の「決めること」の 1 の表にある。

NT = os.name == "nt"


class MdFile(NamedTuple):
    """名前が .md で終わる、ふつうのファイル（リンクの先がふつうのファイルのものを含む）。"""
    rel: str        # wiki のルートからの相対パス（/ 区切り、ディスク上の綴り）
    size: int       # ファイルかどうかを調べた os.stat() の値
    mtime_ns: int


class Walk(NamedTuple):
    """走査の結果。知らせ（unlistable と unknown）がどちらも空なら、たどる間に変わらなかったものは欠けていない。"""
    files: list            # MdFile の並び
    unlistable: list       # 一覧を作れなかったディレクトリ（相対パス）
    unknown: list          # 種類か、同じ場所を回っていないかを、調べられなかった項目（相対パス）


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
    """(状態, stat の結果)。.md の項目と最上位の index.md がふつうのファイルかどうかを調べる os.stat()。

    Windows の os.stat() は、属性の取得がエラー 1、50、87 で失敗すると、S_IFBLK の成功に読み替える。一覧に載る項目が
    ボリュームや物理ディスクであることはないので、Windows で S_IFBLK が返ったら「分からない」にする。POSIX の S_IFBLK は
    本物のデバイスのファイルなので、ふつうのファイルでない（今と同じ）。
    """
    state, st = read_os(os.stat, path)
    if state == "ok" and NT and stat.S_ISBLK(st.st_mode):
        return "denied", None
    return state, st


def _list_dir(directory: str) -> list:
    # 項目を最後まで取り出してから閉じる。閉じるときの失敗は、CPython が捨てる（ScandirIterator_closedir が FindClose と
    # closedir の戻り値を見ない）が、取り出し終えた一覧は変わらない。
    with os.scandir(directory) as it:
        return list(it)


def _identity(path: str):
    """(状態, 識別子)。識別子は win_file_id() の値。FileId がすべて 0 なら、分からない。Windows でだけ呼ばれる。"""
    state, value = read_os(win_file_id, path)
    if state != "ok":
        return state, None
    if not any(value[1]):
        return "denied", None
    return "ok", value


def _chain_identities(chain: tuple):
    """(状態, 識別子の並び)。chain の各ディレクトリの識別子を、起点から順に取る。"""
    found = []
    for path in chain:
        state, one = _identity(path)
        if state != "ok":
            return state, None
        found.append(one)
    return "ok", tuple(found)


def _is_junction(entry):
    """(状態, ジャンクションか)。DirEntry.is_junction()（3.12 から）は、Windows では一覧の情報（再解析のタグ）から答え、
    OS を呼ばない。Windows のほかでは、いつも偽を返す。"""
    return read_os(entry.is_junction)


def walk_markdown(start, prefix: str = "", skip_top: bool = False) -> Walk:
    """start の下をたどり、名前が .md で終わるふつうのファイルと、知らせを返す。

    - 起点には、リンクであっても降りる。途中のディレクトリのシンボリックリンクには降りない。ジャンクションには降りる
      （どちらも 0.10 までの rglob と同じ）。
    - skip_top が真なら、起点の直下の、名前が SKIP_TOP_LEVEL_DIRS に入っている項目には、どの呼び出しもしない。
    - ジャンクションを 1 つでも通った道筋では、入ろうとするディレクトリの識別子が、起点からそこまでに通った
      ディレクトリのどれかと同じなら、降りない（同じ場所を回る道筋）。識別子を取れなければ、unknown に入れて降りない。
    - prefix は、返す相対パスの先頭に付ける。並びは posix_rel の順、同じなら綴りの順である。
    """
    files, unlistable, unknown = [], [], []
    start = os.fspath(start)
    # スタックの項目: (ディレクトリのパス, 相対パスの接頭辞, 起点からここまでのパスの並び, 識別子の並びか None)
    stack = [(start, prefix, (start,), None)]
    while stack:
        directory, pre, chain, ids = stack.pop()
        state, entries = read_os(_list_dir, directory)
        if state == "absent":
            continue
        if state == "denied":
            unlistable.append(pre.rstrip("/") or ".")
            continue
        for entry in entries:
            name = entry.name
            rel = pre + name
            if skip_top and pre == prefix and name in SKIP_TOP_LEVEL_DIRS:
                continue  # 名前だけで決める。リンクかどうかも、ディレクトリかどうかも調べない
            is_link = is_dir = junction = False
            state, is_link = read_os(entry.is_symlink)
            if state == "ok":
                state, is_dir = read_os(entry.is_dir)  # リンクの先で判定する
            if state == "ok" and is_dir and not is_link:
                state, junction = _is_junction(entry)
            if state == "absent":
                continue
            if state == "denied":
                unknown.append(rel)
                continue
            if is_dir:
                if is_link:
                    continue  # ディレクトリのシンボリックリンク。降りず、ファイルでもない
                child_ids = None
                if junction or ids is not None:
                    state, child = _identity(entry.path)
                    path_ids = ids
                    if state == "ok" and path_ids is None:
                        # このジャンクションで、道筋に初めて識別子が要る。起点からここまでの識別子を、
                        # このジャンクションのためだけに取る。兄弟の項目には引き継がない。
                        state, path_ids = _chain_identities(chain)
                    if state == "absent":
                        continue
                    if state == "denied":
                        unknown.append(rel)  # 同じ場所を回っていないかを、調べられなかった
                        continue
                    if child in path_ids:
                        continue  # 道筋の上のディレクトリに戻る。降りない
                    child_ids = path_ids + (child,)
                stack.append((entry.path, rel + "/", chain + (entry.path,), child_ids))
                continue
            if name.lower().endswith(".md"):
                state, st = _stat_entry(entry.path)
                if state == "absent":
                    continue  # 先のないリンク
                if state == "denied":
                    unknown.append(rel)
                    continue
                if stat.S_ISREG(st.st_mode):
                    files.append(MdFile(rel, st.st_size, st.st_mtime_ns))
    files.sort(key=lambda f: _sort_key(f.rel))
    return Walk(files, sorted(unlistable, key=_sort_key), sorted(unknown, key=_sort_key))


def walk_pages(wiki_root) -> Walk:
    """ページのファイルと、知らせ。知らせは、たどった全体のものをそのまま返す。"""
    found = walk_markdown(wiki_root, skip_top=True)
    return Walk([f for f in found.files if is_page_path(f.rel)], found.unlistable, found.unknown)


def walk_index_files(wiki_root) -> Walk:
    """索引のファイル（最上位の index.md と、indexes を起点にした .md）と、知らせ。

    indexes は、リンクであっても起点として降りる。ページの側とは別の呼び出しでたどるので、indexes がページの
    ディレクトリを指していても、ページと索引の両方がそろう。
    """
    root = os.fspath(wiki_root)
    files, unknown = [], []
    state, st = _stat_entry(os.path.join(root, "index.md"))
    if state == "denied":
        unknown.append("index.md")
    elif state == "ok" and stat.S_ISREG(st.st_mode):
        files.append(MdFile("index.md", st.st_size, st.st_mtime_ns))
    below = walk_markdown(os.path.join(root, "indexes"), prefix="indexes/")
    return Walk(files + below.files, below.unlistable, unknown + below.unknown)


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


def classify_source_ref(ref: str, pages_by_path: dict, pages_by_slug: dict, page_type) -> "tuple[str, list, object]":
    """`sources:` の値 1 つを分ける。(状態, 当たるページ, source でない type) を返す。

    値は ref_link で外側の [[ ]] を外し、wikilink と同じ規則（link_candidates）でページを探す。状態は次のどれかである。
    - "missing": 当たるページがない。
    - "not_source": 当たるページのうち type のあるものが 1 つ以上あり、どれも source でない。3 つ目の値は最初の type。
    - "ok": それ以外。
    lint の unresolved_sources（missing と not_source）と、グラフの抽出の sourced_from が、この関数を共有する。
    当たるページが 2 つ以上かどうか（曖昧さ）は、状態に含めない。呼び出す側が、当たるページの数で見る。
    page_type は、ページ（pages_by_* の値）から type を返す。
    """
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
