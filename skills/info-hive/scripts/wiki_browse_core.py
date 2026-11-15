"""
wiki_browse_core.py — 閲覧（browse）の中核。

Wiki を読んで写し（Snapshot）を作り、そこからページ、リンク、被リンク、関係表、ナビゲーション、診断を組み立て
（Core）、ページの HTML を描く。入出力は呼ばない（読むのは wiki_browse_fs.py の口を通す）。静的の書き出しと
サーバーは、どちらもこのモジュールの同じ関数でページを描く（守ること 6）。設計は
`docs/plans/wiki-browse-stage1-2026-11-09.md` の 2〜5 と、`docs/plans/wiki-browse-structure-2026-11-11.md` の
4、5、8 にある。

ページの集合、リンクの解決、`sources:` の解決は、lint と同じ wiki_pages の関数で決める（守ること 2）。
関係表だけが PyYAML（グラフの抽出）を使う。PyYAML がなければ、関係表は「使えない」になる。

写しは、内容（入力ごとの種類、相対パス、文字か読めない理由、世代）だけを持ち、Path と、機械の絶対パス（Wiki の
ルート、ホーム、data_dir）を持たない。Core は、内容だけを受け取る（設計の 8）。
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import string
import sys
import threading
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wiki_browse_fs as fs  # noqa: E402
import wiki_lint  # noqa: E402
import wiki_pages  # noqa: E402
from wiki_text import canonical_meta, nfc, posix_rel  # noqa: E402
from markdown_it.token import Token  # noqa: E402  （wiki_pages が同梱のものを読み込んだあと）

try:
    import yaml  # noqa: F401
    import wiki_graph_extract  # noqa: E402
except (ImportError, SystemExit):
    wiki_graph_extract = None

PROTOCOL_VERSION = 2   # 探しやすさの設計の 2.3、6（nav.js の形とグループの並びが変わった）
CORE_VERSION = "browse-core-2"
NAME_LIMIT = 200
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'")
RAW_MODES = ("text", "link", "basename", "none")
# 画面の部品。fluent2.css は Fluent 2 の再現の CSS の同梱の写し（Fluent 2 の設計の 2）、theme.js は配色を描く前に決める
ASSETS = ("assets/app.js", "assets/style.css", "assets/fluent2.css", "assets/theme.js")
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browse_assets")
DATA_FILES = ("nav.js", "data/manifest.js", "data/diagnostics.js", "data/search.js")
EXTERNAL_SCHEMES = ("http", "https", "mailto")
ONTOLOGY_REL = "graph/ontology.yaml"

# wiki のスコープの表示。スコープは、wiki を選んだ側（/wiki:browse）が --scope で指定する。
SCOPE_KINDS = ("user", "project")
SCOPE_LABELS = {"user": "ユーザースコープ", "project": "プロジェクト"}
SCOPE_HEADINGS = {"user": "ユーザースコープの wiki", "project": "プロジェクトの wiki"}


def normalize_scope(value) -> "str | None":
    """スコープの値を、描くもの（SCOPE_KINDS のどれか）か None（出さない）にそろえる。

    中核、サーバー、使い回すサーバーの答えの読み取りは、どれもこの 1 つの規則を使う。
    """
    return value if isinstance(value, str) and value in SCOPE_KINDS else None


_DISPLAY = wiki_pages.new_parser()


class NameCollision(Exception):
    """出力の名前が、接尾辞の桁を増やしても重なった。"""


# ---------------------------------------------------------------------------
# 写し（設計の 2。構造の直しの設計の 4、8）
# ---------------------------------------------------------------------------

@dataclass
class InputFile:
    """内容の 1 つの入力。Path と絶対パスを持たない（設計の 8）。"""
    kind: str            # "page"、"index"、"ontology"
    disk_rel: str        # ディスク上の綴りの / 区切りの相対パス
    rel: str             # NFC にそろえた / 区切りの相対パス
    text: "str | None" = None
    error: "str | None" = None   # "os"（読めない）か "decode"（UTF-8 でない）
    sha: "str | None" = None

    @property
    def stem(self) -> str:
        return PurePosixPath(self.disk_rel).stem


class InputsUnknown(Exception):
    """入力の名前の集合が分からない（走査が送出した、一覧を作れない場所、種類を調べられない項目）。

    そのときは全体を採らない（設計の 4.1）。サーバーは前の中核を「古い」と示して使い、書き出しは失敗する。
    """


class InputsChanged(Exception):
    """読む前と読んだあとの走査が違った（読んでいるあいだに Wiki が変わった。設計の 4.2）。"""


def input_list(root) -> tuple:
    """入力の一覧（設計の 4）。(種類, ディスク上の相対パス, 大きさ, 更新時刻) の並び。分からなければ InputsUnknown。

    写しが読むものも、変化を知るための印も、比べる 2 回の走査も、この 1 つの一覧から作る。
    """
    entries, problem = fs.walk_inputs(root)
    if problem is not None:
        raise InputsUnknown(problem)
    return tuple(entries)


def signature_of(entries) -> str:
    """入力の印。入力の一覧の、種類、相対パス、大きさ、更新時刻から作る。"""
    h = hashlib.sha256()
    for kind, rel, size, mtime in entries:
        h.update(f"{kind}\0{rel}\0{size}\0{mtime}\n".encode("utf-8"))
    return h.hexdigest()


def signature(root) -> str:
    """今の入力の印。名前の集合が分からなければ InputsUnknown。"""
    return signature_of(input_list(root))


def read_text_like_lint(data: bytes) -> str:
    """lint の read_text(encoding="utf-8-sig") と同じ文字にする。BOM を除き、改行を LF にそろえる。"""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline=None).read()


@dataclass
class Snapshot:
    """写し。inputs と generation が内容、signature と unreadable が読み取りの情報である（設計の 8）。"""
    signature: str
    inputs: list
    generation: str
    unreadable: list = field(default_factory=list)   # 中身を読めなかった入力の (種類, 相対パス)（UTF-8 でないものを含む）
    entries: tuple = ()                              # 入力の一覧（読み取りの情報。公開の前の検査が使う）
    transient: list = field(default_factory=list)    # そのうち、開けなかった入力（ファイルを変えずに読めるようになりうる）

    def by_kind(self, kind: str) -> list:
        return [f for f in self.inputs if f.kind == kind]


def take_snapshot(root, entries=None) -> Snapshot:
    """入力の一覧を作り、入力を読み、もう 1 度一覧を作る。2 回が同じときだけ、写しを採る（設計の 4.2）。

    違えば InputsChanged、名前の集合が分からなければ InputsUnknown。中身を読めない入力は、その入力がない Wiki と
    同じに描き、読み取りの印を足す（設計の 4.1）。
    """
    first = input_list(root) if entries is None else tuple(entries)
    inputs = []
    unreadable = []
    transient = []
    gen = hashlib.sha256(CORE_VERSION.encode("utf-8"))
    for kind, rel_disk, size, _mtime in first:
        f = InputFile(kind=kind, disk_rel=rel_disk, rel=posix_rel(rel_disk))
        data = None
        if size is not None:
            try:
                data = fs.read_input(root, rel_disk)
            except (fs.Unknown, fs.Absent):
                data = None
        if data is None:
            f.error = "os"
        else:
            f.sha = hashlib.sha256(data).hexdigest()
            try:
                f.text = read_text_like_lint(data)
            except UnicodeDecodeError:
                f.error = "decode"
        if f.error is not None:
            unreadable.append((kind, f.rel))
        if f.error == "os":
            transient.append((kind, f.rel))
        gen.update(f"{kind}\0{rel_disk}\0{f.sha or '!' + str(f.error)}\n".encode("utf-8"))
        inputs.append(f)
    second = input_list(root)
    if second != first:
        raise InputsChanged("読んでいるあいだに Wiki が変わった")
    return Snapshot(signature=signature_of(second), inputs=inputs, generation=gen.hexdigest()[:20],
                    unreadable=unreadable, entries=second, transient=transient)


# ---------------------------------------------------------------------------
# 出力の名前（設計の 3.2）
# ---------------------------------------------------------------------------

WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                              *(f"COM{c}" for c in "0123456789¹²³"),
                              *(f"LPT{c}" for c in "0123456789¹²³")})
WINDOWS_BAD = set('<>:"\\|?*')
SUFFIX_RE = re.compile(r"~[0-9a-f]{8,64}$")
RESERVED_TOP = frozenset({"index.html", "nav.js", "data", "assets"})
CASE_SUFFIX_ROOM = 17   # 大文字と小文字だけが違う名前に足す接尾辞（~ と最大 16 桁）の分


def short_hash(text: str, digits: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:digits]


def is_reserved_windows(name: str) -> bool:
    return name.split(".")[0].rstrip(" .").upper() in WINDOWS_RESERVED


def looks_like_short_name(name: str) -> bool:
    """name が、`~` を含む 8.3 の形（基の部分が 1〜8 文字、拡張子が 3 文字以内、点は 1 つまで）なら真。

    Windows が付ける短い名前は、どれも `~` を含む 8.3 の形である。この形でない名前は、ほかの項目の短い名前と
    同じ綴りにならないので、短い名前で別の項目に届くことがない。
    """
    if "~" not in name or name.count(".") > 1:
        return False
    base, _dot, ext = name.partition(".")
    return 1 <= len(base) <= 8 and len(ext) <= 3


def safe_component(part: str, key: str, limit: int, is_dir: bool, top: bool) -> str:
    """1 つの名前の要素を、どの OS でも作れる名前にする。変えたときだけ ~<key の SHA-256 の先頭 8 桁> を付ける。"""
    changed = False
    out = []
    for ch in part:
        if ch in WINDOWS_BAD or ord(ch) < 0x20:
            out.append("_")
            changed = True
        else:
            out.append(ch)
    name = "".join(out)
    if name.rstrip(" .") != name:
        name = name.rstrip(" .")
        changed = True
    if not name:
        # 空の要素（Linux の `\tmp\a.md` は、/ にそろえると先頭が空になる）、`.`、`..` は名前にしない。
        # そのまま残すと、出力の名前が絶対パスや、書き出し先の外を指すパスになる。
        name = "_"
        changed = True
    if is_reserved_windows(name):
        # 接尾辞は末尾に付くので、最初の . より前が予約名のままになる。先頭に _ を付ける。
        name = "_" + name
        changed = True
    if SUFFIX_RE.search(name):
        changed = True
    if is_dir and name.lower().endswith(".html"):
        changed = True
    if is_dir and looks_like_short_name(name):
        # Windows の 8.3 の短い名前と同じ形のディレクトリの名前は、ほかの出力のディレクトリに付く短い名前で、同じ
        # ディレクトリに届きうる（実装の 6 回目のレビューの重大 1）。接尾辞を付けて、8.3 の形でなくする。ファイルの
        # 名前は拡張子が .html（4 文字）なので、8.3 の形にならない
        changed = True
    if name.lower().startswith(".browse-"):   # Windows と macOS では大文字の名前も目印と同じ場所になる
        changed = True
    if top and (name if is_dir else name + ".html").casefold() in RESERVED_TOP:
        changed = True
    room = 0 if is_dir else CASE_SUFFIX_ROOM
    budget = limit - len(".html") - 9 - room
    if len(name.encode("utf-8")) > budget:
        name = name.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        changed = True
    if changed:
        name = f"{name}~{short_hash(key)}"
    return name


def export_name(page_id: str, limit: int = NAME_LIMIT) -> str:
    """ページの識別子から、出力のパスを決める。返すのは、空の要素と `.`、`..` を持たない相対パスである。"""
    parts = page_id.split("/")
    out = [safe_component(p, "/".join(parts[:i + 1]) + ("/" if i < len(parts) - 1 else ""), limit,
                          is_dir=i < len(parts) - 1, top=i == 0)
           for i, p in enumerate(parts)]
    name = "/".join(out) + ".html"
    if not is_safe_relative(name):
        raise NameCollision(f"出力の名前を相対パスにできない: {page_id!r}")
    return name


def is_safe_relative(name: str) -> bool:
    """/ 区切りの相対パスで、空の要素、`.`、`..`、`\\`、NUL、ドライブ名を持たないなら真。"""
    parts = name.split("/")
    return bool(name) and all(p not in ("", ".", "..") and "\\" not in p and "\0" not in p and ":" not in p
                              for p in parts)


def _fixed_files() -> list:
    return ["index.html", *DATA_FILES, *ASSETS]


def _clashes(names: dict) -> set:
    """大文字と小文字を同一視して重なる出力の、ページの識別子の集合。生成器のファイルとの重なりと、ファイルとディレクトリの重なりも数える。"""
    files = defaultdict(list)
    for fixed in _fixed_files():
        files[fixed.casefold()].append(None)
    dirs = defaultdict(list)
    for fixed in _fixed_files():
        parts = fixed.casefold().split("/")
        for i in range(1, len(parts)):
            dirs["/".join(parts[:i])].append(None)
    for pid, name in names.items():
        files[name.casefold()].append(pid)
        parts = name.casefold().split("/")
        for i in range(1, len(parts)):
            dirs["/".join(parts[:i])].append(pid)
    bad = set()
    for key, owners in files.items():
        if len(owners) > 1:
            bad.update(o for o in owners if o is not None)
        if key in dirs:
            bad.update(o for o in owners if o is not None)
            bad.update(o for o in dirs[key] if o is not None)
    return bad


def unify_dir_case(names: dict) -> dict:
    """出力の名前のディレクトリの部分を、大文字と小文字を同一視して 1 つの綴りにそろえる。

    大文字と小文字を区別する Wiki の `A/x.md` と `a/y.md` を、区別しない書き出し先に書くと、ディレクトリは 1 つに
    なり、一覧（`A` と `a`）と書き出し先の綴りが合わず、次の書き出しの門で止まる（実装の 1 回目のレビューの重大 2）。
    ファイルどうしの重なりは `_clashes` が数えるので、ここではディレクトリだけをそろえる。綴りは、識別子の順で
    最初に現れたものにする。
    """
    canonical = {}
    out = {}
    for pid in sorted(names):
        parts = names[pid].split("/")
        dirs = []
        for part in parts[:-1]:
            key = "/".join(dirs + [part]).casefold()
            dirs.append(canonical.setdefault(key, part))
        out[pid] = "/".join(dirs + [parts[-1]])
    return out


def assign_names(page_ids, limit: int = NAME_LIMIT) -> dict:
    """全ページの出力の名前を決める。重なった名前には接尾辞を足し、まだ重なれば桁を 4 桁ずつ増やす。"""
    base = unify_dir_case({pid: export_name(pid, limit) for pid in page_ids})
    names = dict(base)
    digits = {}
    bad = _clashes(names)
    while bad:
        for pid in bad:
            d = digits.get(pid, 4) + 4
            if d > 64:
                raise NameCollision(f"出力の名前が重なる: {sorted(bad)[:5]}")
            digits[pid] = d
            names[pid] = base[pid][:-len(".html")] + f"~{short_hash(pid, d)}.html"
        bad = _clashes(names)
    return names


def rel_href(from_path: str, to_path: str) -> str:
    """出力のパスどうしの相対の href。名前の要素ごとにパーセントエンコードする。"""
    src = PurePosixPath(from_path).parent.parts
    dst = PurePosixPath(to_path).parts
    common = 0
    while common < min(len(src), len(dst) - 1) and src[common] == dst[common]:
        common += 1
    up = [".."] * (len(src) - common)
    rest = list(dst[common:])
    return "/".join(p if p == ".." else urllib.parse.quote(p, safe="") for p in up + rest) or "."


def root_prefix(path: str) -> str:
    return "../" * (len(PurePosixPath(path).parts) - 1)


# ---------------------------------------------------------------------------
# 見出し（設計の 3.4）
# ---------------------------------------------------------------------------

_ASCII_PUNCT_NOT_HYPHEN = "".join(c for c in string.punctuation if c != "-")
_PUNCT_RE = re.compile("[" + re.escape(_ASCII_PUNCT_NOT_HYPHEN) + "]")


def heading_key(text: str) -> str:
    key = nfc(text).strip().lower()
    key = re.sub(r"\s+", "-", key)
    key = _PUNCT_RE.sub("", key)
    return key or "section"


def display_href(href: str) -> str:
    """リンクにしない Markdown のリンクの行き先を、表示の文字にする（描画と見出しの鍵で同じものを使う）。"""
    return _DISPLAY.normalizeLinkText(href)


def _keeps_link(children, i: int) -> bool:
    """children[i] の link_open が、外部へのリンクとして残るなら真（描画の _transform_inline と同じ判定）。"""
    href = children[i].attrs.get("href", "")
    j = i + 1
    while j < len(children) and children[j].type != "link_close":
        if children[j].type == "text" and wiki_pages.WIKILINK_FULL_RE.search(children[j].content):
            return False
        j += 1
    try:
        scheme = urllib.parse.urlparse(href).scheme.lower()
    except ValueError:
        return False
    return scheme in EXTERNAL_SCHEMES


def inline_text(children) -> str:
    """inline の子を、表示される文字にする（wikilink は表示の文字、画像は代替テキスト、リンクにしない
    Markdown のリンクは行き先を括弧に入れて添える。描画と同じ文字になる）。"""
    out = []
    children = list(children or [])
    pending = []
    for i, c in enumerate(children):
        if c.type == "link_open":
            keep = _keeps_link(children, i) or c.markup == "autolink"
            pending.append(None if keep else display_href(c.attrs.get("href", "")))
            continue
        if c.type == "link_close":
            suffix = pending.pop() if pending else None
            if suffix is not None:
                out.append(f"（{suffix}）")
            continue
        if c.type == "text":
            out.append(wiki_pages.WIKILINK_FULL_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), c.content))
        elif c.type == "code_inline":
            out.append(c.content)
        elif c.type == "image":
            out.append(c.content)
        elif c.type in ("softbreak", "hardbreak"):
            out.append(" ")
    return "".join(out)


def assign_heading_ids(tokens) -> list:
    """見出しに ID を付け、(鍵, ID, 表示の文字) の並びを返す。ID は鍵に、重なれば連番を付けたもの。"""
    used = set()
    found = []
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        text = inline_text(tokens[i + 1].children) if i + 1 < len(tokens) else ""
        key = heading_key(text)
        candidate, n = key, 0
        while candidate in used:
            n += 1
            candidate = f"{key}-{n}"
        used.add(candidate)
        tok.attrSet("id", candidate)
        found.append((key, candidate, text))
    return found


# ---------------------------------------------------------------------------
# raw:（設計の 3.8）
# ---------------------------------------------------------------------------

def classify_raw(value: str) -> dict:
    value = str(value)
    if wiki_lint.DRIVE_PATH_RE.match(value) or value.startswith(("/", "\\", "~")):
        return {"kind": "absolute", "value": value}
    if wiki_lint.URL_SCHEME_RE.match(value):
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError:
            # `http://[` のように解釈できない URL。描くときに例外にせず、URL として文字で出す。
            return {"kind": "url", "value": value}
        if parsed.scheme.lower() == "file":
            path = urllib.parse.unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:[\\/]", path):
                path = path[1:]
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                path = f"//{parsed.netloc}{path}"
            return {"kind": "absolute", "value": path}
        return {"kind": "url", "value": value}
    return {"kind": "relative", "value": value}


def file_url(path: str) -> str:
    """絶対パスの file: の URL。~ はホームに展開しない（呼ぶ側が文字で出す。構造の直しの設計の 8）。"""
    p = path.replace(BS, "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "file:///" + urllib.parse.quote(p, safe="/:")
    return "file://" + urllib.parse.quote(p, safe="/:")


# ---------------------------------------------------------------------------
# 中核
# ---------------------------------------------------------------------------

def _page_id(rel: str) -> str:
    return rel[:-3] if rel.lower().endswith(".md") else rel


# MessageBar のアイコン（Fluent UI System Icons の WarningFilled と DiamondDismissFilled、20px。MIT）
ICON_WARNING = ('<svg class="fui-Icon" width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" '
                'focusable="false"><path d="M7.37 3.56a3 3 0 0 1 5.26 0l5.5 10A3 3 0 0 1 15.5 18h-11a3 3 0 0 1-2.63-4.44z'
                'M10 12.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5m0-6.25a.5.5 0 0 0-.5.5v4a.5.5 0 0 0 1 0V7a.5.5 0 0 0-.5-.5"/></svg>')
ICON_ERROR = ('<svg class="fui-Icon" width="20" height="20" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" '
              'focusable="false"><path d="M7.88 1.88a3 3 0 0 1 4.24 0l6 6a3 3 0 0 1 0 4.24l-6 6a3 3 0 '
              '0 1-4.24 0l-6-6a3 3 0 0 1 0-4.24zM13.1 6.9a.5.5 0 0 0-.7 0L10 9.3 7.6 6.9a.5.5 0 0 0-.7.7L9.3 10l-2.4 2.4a.5.5 0 0 0 .7.7l2.4-2.4 2.4 2.4a.5.5 0 0 0 .7-.7L10.7 10l2.4-2.4a.5.5 0 0 0 0-.7"/></svg>')

# ---------------------------------------------------------------------------
# ナビゲーションの木の補助（探しやすさの設計の 2）
# ---------------------------------------------------------------------------

TYPE_LABELS = {"source": "ソース", "entity": "エンティティ", "concept": "コンセプト", "synthesis": "シンセシス"}
SUMMARY_SEPARATORS = ":：—–-"
GROUP_ID_RE = re.compile(r"_g[0-9a-z]+\Z")
TYPE_CLASS_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
LEAD_LIMIT = 120


class NavInvariantError(Exception):
    """組み終えた木が、設計の 2.1 の末尾の性質を満たさない。組み立てを失敗にする。"""


def type_label(value: str) -> str:
    """種類の名前（N7）。知っている 4 つの値だけを日本語にし、ほかはそのまま、値がなければ「種類なし」。"""
    return TYPE_LABELS[value] if value in TYPE_LABELS else (value or "種類なし")


def type_class(value: str) -> str:
    """種類の印のクラス。英小文字の識別子でなければ t-other。"""
    return "t-" + value if TYPE_CLASS_RE.match(value or "") else "t-other"


def _walk(groups):
    for g in groups:
        yield g
        yield from _walk(g["children"])


def _page_ids(groups) -> set:
    return {it["id"] for g in _walk(groups) for it in g["items"] if it["state"] == "resolved"}


def _after_first_link(inline_tok) -> str:
    """段落の最初の wikilink より後ろの表示の文字（4.2 の 2）。先頭の区切りの記号と空白を除く。"""
    children = inline_tok.children or []
    for i, child in enumerate(children):
        if child.type == "text" and "[[" in child.content:
            m = wiki_pages.WIKILINK_FULL_RE.search(child.content)
            if not m:
                continue
            rest = child.copy()
            rest.content = child.content[m.end():]
            text = inline_text([rest] + list(children[i + 1:]))
            return nfc(text.strip().lstrip(SUMMARY_SEPARATORS).strip())
    return ""


def lead_text(tokens) -> str:
    """本文の冒頭（4.2 の 3）。見出し、リスト、引用、表の中を除いた最初の段落の、最初の「。」まで（なければ 120 字まで）。"""
    depth = 0
    for i, tok in enumerate(tokens or []):
        if tok.type in ("bullet_list_open", "ordered_list_open", "blockquote_open", "table_open"):
            depth += 1
        elif tok.type in ("bullet_list_close", "ordered_list_close", "blockquote_close", "table_close"):
            depth -= 1
        elif tok.type == "paragraph_open" and depth == 0 and i + 1 < len(tokens) and tokens[i + 1].type == "inline":
            text = nfc(inline_text(tokens[i + 1].children or []).strip())
            if not text:
                continue
            end = text.find("。")
            return text[:end + 1] if end >= 0 else text[:LEAD_LIMIT]
    return ""


def _check_tree(top: list) -> None:
    """設計の 2.1 の末尾の性質を確かめる。成り立たなければ NavInvariantError。"""
    ids = [g["id"] for g in _walk(top)]
    if len(ids) != len(set(ids)):
        raise NavInvariantError("グループの ID が重なる")
    if not all(GROUP_ID_RE.match(i) for i in ids):
        raise NavInvariantError("グループの ID の形が違う")
    shown = set(ids)
    titles = {g["id"]: g["title"] for g in _walk(top)}
    for g in _walk(top):
        for it in g["items"]:
            if it["state"] == "group" and (it["group"] not in shown or it["label"] != titles[it["group"]]):
                raise NavInvariantError("木にないグループを指すか、名前が行き先のグループの名前と違う")


class Core:
    """1 つの写しから組み立てた中核。描く関数は静的の書き出しとサーバーで同じものを使う。"""

    def __init__(self, snapshot: Snapshot, name_limit: int = NAME_LIMIT, scope: "str | None" = None):
        self.snapshot = snapshot
        # wiki のスコープ（user か project）。呼び出し元の指定で、中核は決めない。None なら出さない。
        self.scope = normalize_scope(scope)
        self.generation = snapshot.generation
        self.parser = wiki_pages.new_parser()
        self.diagnostics = []
        self._body_cache = {}
        # 描けなかったページの識別子（設計の 7.2。そのページに「描けない」と出して続ける）
        self.render_failures = []
        # 解釈できなかった入力の (種類, 相対パス)。読めない入力と同じに扱い、「不完全」の印を描く
        self.parse_failures = []
        # 読めなかった入力（読み取りの情報）。読み取りの印（「不完全」）を描くのに使う（構造の直しの設計の 4.1）
        unreadable = list(snapshot.unreadable)
        self.unreadable_pages = sorted(rel for kind, rel in unreadable if kind == "page")
        self.unreadable_indexes = sorted(rel for kind, rel in unreadable if kind == "index")
        self.unreadable_ontology = [rel for kind, rel in unreadable if kind == "ontology"]
        self._render_lock = threading.Lock()
        self._build_pages()
        self.out_name = assign_names(sorted(self.drawn), name_limit)
        self.name_to_id = {name: pid for pid, name in self.out_name.items()}
        for pid in sorted(self.drawn):
            if self.out_name[pid] != pid + ".html":
                self.diagnostics.append({"kind": "renamed", "page": pid, "output": self.out_name[pid]})
        self._prepass()
        self._backlinks()
        kept = len(self.diagnostics)
        try:
            self._relations()
        except Exception:  # noqa: BLE001  関係表を作れないときは、関係表だけを使えないにする（構造の直しの設計の 7.2）
            self.relations = {pid: {"out": [], "in": []} for pid in self.drawn}
            self.relations_state = "error"
            del self.diagnostics[kept:]          # 途中まで足した関係表の診断を取り除く
            self.diagnostics.append({"kind": "relations_error", "reason": "unexpected"})
        self._nav()
        # 解釈できなかった入力も、読めない入力と同じに「不完全」の印を描く
        for kind, rel in self.parse_failures:
            target = {"page": self.unreadable_pages, "index": self.unreadable_indexes}.get(kind)
            if target is not None and rel not in target:
                target.append(rel)
                target.sort()

    # --- ページ ---

    def _build_pages(self) -> None:
        self.pages = {}          # disk_rel -> ページ（lint の形）
        self.page_files = {}     # disk_rel -> InputFile
        by_id = defaultdict(list)
        for f in self.snapshot.by_kind("page"):
            pid = _page_id(f.rel)
            if f.text is None:
                self.diagnostics.append({"kind": "read_error", "page": pid, "reason": f.error})
                continue
            try:
                raw_meta, _fm, body, malformed = wiki_lint.parse_frontmatter_raw(f.text)
                meta, _conflicts = canonical_meta(raw_meta)
            except Exception:  # noqa: BLE001  1 ページの解釈の失敗で、全体を止めない（構造の直しの設計の 7.2）
                self.diagnostics.append({"kind": "read_error", "page": pid, "reason": "parse"})
                self.parse_failures.append((f.kind, f.rel))
                continue
            p = {"rel_path": f.rel, "disk_rel_path": f.disk_rel, "slug": nfc(f.stem), "meta": meta,
                 "body": body, "malformed_fm": malformed, "id": pid, "text": f.text}
            self.pages[f.disk_rel] = p
            self.page_files[f.disk_rel] = f
            by_id[pid].append(f.disk_rel)
        self.drawn = {}          # 識別子 -> disk_rel（描くページ）
        for pid, disks in by_id.items():
            if len(disks) == 1:
                self.drawn[pid] = disks[0]
            else:
                self.diagnostics.append({"kind": "id_collision", "page": pid, "files": sorted(disks)})
        self.pages_by_path, self.pages_by_slug = wiki_pages.build_page_index(
            list(self.pages.values()), lambda p: p["disk_rel_path"])

    def page(self, pid: str) -> dict:
        return self.pages[self.drawn[pid]]

    def title(self, pid: str) -> str:
        p = self.page(pid)
        return str(p["meta"].get("title") or p["slug"])

    def resolve_disks(self, disks: list) -> "str | None":
        """当たるページの並びから、描かれているページ 1 つに決まればその識別子を返す。"""
        if len(disks) != 1:
            return None
        pid = self.pages[disks[0]]["id"]
        return pid if self.drawn.get(pid) == disks[0] else None

    def resolve(self, target: str) -> dict:
        """wikilink の行き先を解決する（設計の 3.3）。"""
        disks = wiki_pages.link_candidates(target, self.pages_by_path, self.pages_by_slug)
        if not disks:
            return {"state": "unresolved", "target": target}
        pid = self.resolve_disks(disks)
        if pid is None:
            return {"state": "ambiguous", "target": target,
                    "candidates": sorted(self.pages[d]["id"] for d in disks)}
        result = {"state": "resolved", "target": target, "id": pid}
        if "#" in target:
            anchor = nfc(target.split("#", 1)[1]).strip()
            key = heading_key(anchor)
            match = next((hid for k, hid, _t in self.headings.get(pid, []) if k == key), None)
            if match is None:
                result["missing_heading"] = anchor
            else:
                result["anchor"] = match
        return result

    # --- 本文の下ごしらえ ---

    def _prepass(self) -> None:
        self.tokens = {}
        self.headings = {}
        unparsed = set()
        for pid, disk in self.drawn.items():
            try:
                tokens = self.parser.parse(self.pages[disk]["body"])
                headings = assign_heading_ids(tokens)
            except Exception:  # noqa: BLE001  1 ページの解釈の失敗で、全体を止めない（構造の直しの設計の 7.2）
                unparsed.add(disk)
                tokens, headings = None, []
            self.tokens[pid] = tokens
            self.headings[pid] = headings
        self.links = {}
        # 描かないページ（id_collision）のリンクも、lint と同じく数える。描かないので、診断は全体にだけ載せる。
        self.hidden_links = {}
        for disk, p in sorted(self.pages.items()):
            pid = p["id"]
            drawn = self.drawn.get(pid) == disk
            if drawn:
                tokens = self.tokens[pid]
            elif disk not in unparsed:
                try:
                    tokens = self.parser.parse(p["body"])
                except Exception:  # noqa: BLE001  描かないページの解釈の失敗も、全体を止めない
                    unparsed.add(disk)
                    tokens = None
            found = []
            if tokens is None:
                # 本文を解釈できない。そのページのリンクは分からないので、「不完全」の印を描く。ページは「描けない」になる
                self.diagnostics.append({"kind": "read_error", "page": pid if drawn else None,
                                         "source_page": None if drawn else pid, "reason": "parse"})
                self.parse_failures.append(("page", p["rel_path"]))
                tokens = []
            for tok in tokens:
                if tok.type == "inline":
                    for child in tok.children or []:
                        if child.type == "text" and "[[" in child.content:
                            for m in wiki_pages.WIKILINK_FULL_RE.finditer(child.content):
                                target = nfc(m.group(1).strip())
                                r = self.resolve(target)
                                r["label"] = (m.group(2) or m.group(1)).strip()
                                found.append(r)
            if drawn:
                self.links[pid] = found
            else:
                self.hidden_links[disk] = found
            for r in found:
                where = {"page": pid} if drawn else {"page": None, "source_page": pid}
                if r["state"] != "resolved":
                    self.diagnostics.append({"kind": r["state"], **where, "target": r["target"]})
                elif r.get("missing_heading"):
                    self.diagnostics.append({"kind": "missing_heading", **where, "target": r["target"]})

    def link_targets(self, pid: str) -> list:
        """そのページの本文の wikilink の行き先（lint の extract_wikilinks と同じものになる）。"""
        return [r["target"] for r in self.links[pid]]

    def unresolved(self) -> list:
        """未解決のリンクの (ページの相対パス, 行き先)。lint の broken_links と同じ集合になる。"""
        shown = [(self.page(pid)["rel_path"], r) for pid in self.drawn for r in self.links[pid]]
        hidden = [(self.pages[d]["rel_path"], r) for d, rs in self.hidden_links.items() for r in rs]
        return sorted((rel, r["target"]) for rel, r in shown + hidden if r["state"] == "unresolved")

    def _backlinks(self) -> None:
        inbound = defaultdict(set)
        for pid in self.drawn:
            for r in self.links[pid]:
                if r["state"] == "resolved" and r["id"] != pid:
                    inbound[r["id"]].add(pid)
        self.backlinks = {pid: sorted(inbound.get(pid, ())) for pid in self.drawn}

    # --- 関係表（設計の 3.6） ---

    def _relations(self) -> None:
        self.relations = {pid: {"out": [], "in": []} for pid in self.drawn}
        self.relations_state = "none"
        onto = self.snapshot.by_kind("ontology")
        if not onto:
            return
        f = onto[0]
        if f.text is None or wiki_graph_extract is None:
            self.relations_state = "error"
            self.diagnostics.append({"kind": "relations_error",
                                     "reason": "unavailable" if wiki_graph_extract is None else f.error})
            return
        try:
            ontology = wiki_graph_extract.parse_ontology(f.text)
        except Exception:  # noqa: BLE001  OntologyError のほか、思わない例外でも関係表だけを使えないにする
            self.relations_state = "error"
            self.diagnostics.append({"kind": "relations_error", "reason": "invalid"})
            return
        gpages = []
        diag = []
        for disk, p in sorted(self.pages.items()):
            # グラフの抽出と同じ読み方（wiki 0.9.5 の read_frontmatter）。最上位の欄は lint の値、graph だけを
            # PyYAML から取る（設計の 2）。PyYAML が組み立てられない値は、関係表の診断になる。
            try:
                meta, _body, problems = wiki_graph_extract.read_frontmatter(p["text"])
            except Exception:  # noqa: BLE001  そのページの関係を使えないにして、ほかのページは続ける
                meta, problems = dict(p["meta"]), [{"kind": "unreadable_graph", "reason": "unexpected"}]
                meta.pop("graph", None)
            try:
                links = wiki_pages.extract_wikilinks(p["body"])
            except Exception:  # noqa: BLE001  本文のリンクを取り出せないページは、リンクなしで続ける
                links = []
                problems = list(problems) + [{"kind": "unreadable_graph", "reason": "unexpected"}]
            gpages.append({"path": p["rel_path"], "rel_path": p["rel_path"],
                           "disk_rel_path": disk, "slug": p["slug"], "meta": meta, "body": p["body"],
                           "links": links, "fm_problems": problems})
        try:
            _nodes, page_node_ids, _aliases = wiki_graph_extract.build_nodes(gpages, ontology, diag)
            edges = wiki_graph_extract.build_edges(gpages, page_node_ids, diag)
        except Exception:  # noqa: BLE001  オントロジーの中の形が誤っている（node_types が配列など）
            self.relations_state = "error"
            self.diagnostics.append({"kind": "relations_error", "reason": "invalid"})
            return
        self.relations_state = "ok"
        node_pages = defaultdict(list)
        for disk, node in page_node_ids.items():
            node_pages[node].append(disk)
        rel_to_disk = {p["rel_path"]: d for d, p in self.pages.items()}
        for e in edges:
            if e["predicate"] in ("mentions", "summarizes_raw"):
                continue
            src_pid = self._drawn_by_rel(e["page"], rel_to_disk)
            obj_disks = node_pages.get(e["object"], [])
            obj_pid = self.resolve_disks(obj_disks) if len(obj_disks) == 1 else None
            row = {"predicate": e["predicate"], "status": e.get("status", ""),
                   "valid_from": (e.get("extras") or {}).get("valid_from", ""),
                   "valid_to": (e.get("extras") or {}).get("valid_to", ""),
                   "confidence": e.get("confidence", ""), "source": e.get("source", ""),
                   "evidence": e.get("evidence", "")}
            if src_pid is not None:
                self.relations[src_pid]["out"].append({**row, "other_id": obj_pid, "other_text": e["object"]})
            if obj_pid is not None and len(obj_disks) == 1:
                self.relations[obj_pid]["in"].append({**row, "other_id": src_pid,
                                                      "other_text": _page_id(e["page"])})
        # グラフの抽出の診断を、それを書いたページに載せる（設計の 3.6）。duplicate_node_id は page を持たず、
        # pages（ディスク上の相対パスの並び）を持つので、そのそれぞれのページに載せる。
        for d in diag:
            entry = {k: v for k, v in d.items() if k not in ("page", "pages")}
            if "pages" in d:
                entry["pages"] = sorted(self.pages[x]["id"] for x in d["pages"] if x in self.pages)
            if d.get("kind") == "duplicate_node_id":
                placed = [self.pages[x]["id"] for x in d.get("pages", [])
                          if x in self.pages and self.drawn.get(self.pages[x]["id"]) == x]
                for pid in placed:
                    self.diagnostics.append({**entry, "page": pid})
                if not placed:
                    self.diagnostics.append({**entry, "page": None})
            else:
                self.diagnostics.append({**entry, "page": self._drawn_by_rel(d.get("page", ""), rel_to_disk),
                                         "source_page": _page_id(d.get("page", ""))})

    def _drawn_by_rel(self, rel: str, rel_to_disk: dict) -> "str | None":
        disk = rel_to_disk.get(rel)
        if disk is None:
            return None
        pid = self.pages[disk]["id"]
        return pid if self.drawn.get(pid) == disk else None

    # --- ナビゲーション（設計の 3.7） ---

    def _nav(self) -> None:
        """ナビゲーションの木。1 つ目の段（N1）で索引のファイルごとのグループを組み、2 つ目の段で畳んで並べる。"""
        self.nav_groups = []
        file_groups = []     # (索引のファイル, そのグループ)。解釈できたファイルだけ
        listed = set()
        index_files = self.snapshot.by_kind("index")
        # 索引を先に解釈する。解釈できない索引は、読めない索引と同じに扱う（構造の直しの設計の 7.2）
        parsed = {}
        for f in index_files:
            if f.text is None:
                continue
            try:
                raw_meta, _fm, body, _malformed = wiki_lint.parse_frontmatter_raw(f.text)
                meta, _c = canonical_meta(raw_meta)
                parsed[f.disk_rel] = (meta, self.parser.parse(body))
            except Exception:  # noqa: BLE001  1 つの索引の解釈の失敗で、全体を止めない
                self.parse_failures.append(("index", f.rel))
        # 索引のファイルのグループの ID は、`_gf` と写しの索引のファイルの並びの番号（探しやすさの設計の 2.2）。
        # 読めないファイルも数える。ハッシュの先頭の桁は別の名前でも重なるので使わない。
        file_gid = {f.disk_rel: f"_gf{k}" for k, f in enumerate(index_files)}
        group_of_file = {}
        for f in index_files:
            if f.disk_rel not in parsed:
                continue   # 読めない索引のグループは作らないので、そこへのリンクも作らない
            group_of_file.setdefault(_page_id(f.rel), file_gid[f.disk_rel])
        counter = [0]

        def new_group(title, gid=None):
            counter[0] += 1
            return {"id": gid or f"_g{counter[0]}", "title": title, "items": [], "children": []}

        for f in index_files:
            is_top = f.rel == "index.md"
            if f.disk_rel not in parsed:
                self.diagnostics.append({"kind": "read_error", "page": _page_id(f.rel),
                                         "reason": f.error or "parse"})
                continue
            meta, tokens = parsed[f.disk_rel]
            first_heading = next((inline_text(tokens[i + 1].children) for i, t in enumerate(tokens)
                                  if t.type == "heading_open" and i + 1 < len(tokens)), None)
            gid = file_gid[f.disk_rel]
            if is_top:
                top = new_group("", gid)
            else:
                top = new_group(str(meta.get("title") or first_heading or f.stem), gid)
            stack = [(0, top)]
            pending = []
            for i, tok in enumerate(tokens):
                if tok.type == "heading_open":
                    level = int(tok.tag[1])
                    text = inline_text(tokens[i + 1].children)
                    while len(stack) > 1 and stack[-1][0] >= level:
                        stack.pop()
                    g = new_group(text)
                    stack[-1][1]["children"].append(g)
                    stack.append((level, g))
                elif tok.type == "list_item_open":
                    pending.append(True)
                elif tok.type == "list_item_close":
                    if pending:
                        pending.pop()
                elif tok.type == "inline" and pending and pending[-1]:
                    pending[-1] = False
                    item = self._nav_item(tok, group_of_file)
                    if item is not None:
                        stack[-1][1]["items"].append(item)
                        if item.get("id"):
                            listed.add(item["id"])
            file_groups.append((f, top))
        rest = defaultdict(list)
        for pid in sorted(self.drawn):
            if pid not in listed:
                rest[str(self.page(pid)["meta"].get("type") or "")].append(pid)
        rest_groups = []
        if rest:
            g = new_group("索引にないページ")
            for t in sorted(rest):
                # N7: 名前だけを日本語にする。ID と並び（type の値の順）は 1 つ目の段のまま
                sub = new_group(type_label(t))
                sub["items"] = [{"state": "resolved", "id": pid, "label": self.title(pid)} for pid in rest[t]]
                g["children"].append(sub)
            rest_groups.append(g)
        self.nav_groups = self._nav_stage_two(file_groups, rest_groups)
        self._page_listing()

    def _nav_stage_two(self, file_groups: list, rest_groups: list) -> list:
        """木の 2 つ目の段（探しやすさの設計の 2.1 の N2〜N8）。新しい ID は作らない。"""
        listed_before = _page_ids([g for _f, g in file_groups])
        by_id = {g["id"]: g for _f, g in file_groups}
        index_g = next((g for f, g in file_groups if f.rel == "index.md"), None)
        for _f, g in file_groups:
            origin = "index" if g is index_g else "indexes"
            for x in _walk([g]):
                x["origin"] = origin
                x.setdefault("summary", "")
            if g is index_g:
                g["title"] = "索引"                                  # N2
            if not g["items"] and len(g["children"]) == 1:           # N3: 1 度だけ畳む
                child = g["children"][0]
                g["items"], g["children"] = child["items"], child["children"]
        for _f, g in file_groups:                                    # N6: 行き先のグループの名前にする
            for x in _walk([g]):
                for it in x["items"]:
                    if it["state"] == "group":
                        it["label"] = by_id[it["group"]]["title"]
        top = []
        if index_g is not None:                                      # N4
            if index_g["items"]:
                top.append({**index_g, "title": "索引", "children": []})
            top.extend(index_g["children"])
        order, summaries = [], {}                                    # N5: 順と要約はグループの ID で決める
        if index_g is not None:
            for x in _walk([index_g]):
                for it in x["items"]:
                    if it["state"] == "group" and it["group"] not in summaries:
                        order.append(it["group"])
                        summaries[it["group"]] = it.get("after", "")
        for _f, g in file_groups:
            if g is not index_g and g["id"] not in summaries:
                order.append(g["id"])
        for gid in order:
            by_id[gid]["summary"] = summaries.get(gid, "")
            top.append(by_id[gid])

        def only_group_items(g):
            return all(it["state"] == "group" for x in _walk([g]) for it in x["items"])

        def prune(groups):                                           # N6: index.md から来たグループだけを外せる
            kept = []
            for g in groups:
                if g.get("origin") == "index" and only_group_items(g):
                    continue
                g["children"] = prune(g["children"])
                kept.append(g)
            return kept

        top = prune(top)
        if _page_ids(top) != listed_before:
            raise NavInvariantError("2 つ目の段で、索引に載ったページが変わった")
        for g in rest_groups:                                        # N7
            for x in _walk([g]):
                x["origin"], x["summary"] = "", ""
            top.append(g)

        def count(g):                                                # N8
            ids = {it["id"] for it in g["items"] if it["state"] == "resolved"}
            for ch in g["children"]:
                ids |= count(ch)
            g["count"] = len(ids)
            return ids

        for g in top:
            count(g)
        _check_tree(top)
        return top

    def _page_listing(self) -> None:
        """索引のページの一覧と検索の索引に使う、ページごとの並び、グループ、階層、索引の行の要約（4.1、4.2）。"""
        self.page_order = []          # 索引の順（木を深さ優先でたどり、ページが最初に現れた順）
        self.page_groups = {}         # ページ -> そのページを項目に持つグループと祖先のグループの ID
        self.page_path = {}           # ページ -> 最初に現れたグループの階層（名前のないグループは飛ばす）
        self.index_summary = {}       # ページ -> 索引の行の要約（4.2 の 2）
        self.index_unresolved = []    # (階層, 項目)。ページに決まらない索引の行（4.4）
        # 本文の冒頭（4.2 の 3）。描くときに本文のトークンを書き換えるので、組み立てのときに取っておく
        self.lead = {pid: lead_text(self.tokens.get(pid)) for pid in self.drawn}

        def rec(g, trail):
            trail = trail + [g]
            for it in g["items"]:
                if it["state"] == "resolved":
                    pid = it["id"]
                    if pid not in self.page_groups:
                        self.page_order.append(pid)
                        self.page_path[pid] = [x["title"] for x in trail if x["title"]]
                        self.page_groups[pid] = set()
                    self.page_groups[pid].update(x["id"] for x in trail)
                    if pid not in self.index_summary and it.get("after"):
                        self.index_summary[pid] = it["after"]
                elif it["state"] in ("unresolved", "ambiguous"):
                    self.index_unresolved.append(([x["title"] for x in trail if x["title"]], it))
            for ch in g["children"]:
                rec(ch, trail)

        for g in self.nav_groups:
            rec(g, [])

    def _nav_item(self, inline_tok, group_of_file) -> "dict | None":
        for child in inline_tok.children or []:
            if child.type == "text" and "[[" in child.content:
                m = wiki_pages.WIKILINK_FULL_RE.search(child.content)
                if not m:
                    continue
                target = nfc(m.group(1).strip())
                label = (m.group(2) or m.group(1)).strip()
                clean = wiki_pages.clean_link_target(target)
                after = _after_first_link(inline_tok)
                if clean in group_of_file and clean.startswith("indexes/"):
                    return {"state": "group", "group": group_of_file[clean], "label": label, "after": after}
                r = self.resolve(target)
                if r["state"] == "resolved":
                    return {"state": "resolved", "id": r["id"], "label": self.title(r["id"]),
                            "anchor": r.get("anchor"), "after": after}
                return {"state": r["state"], "label": label, "after": after}
        return None

    # --- 描画 ---

    def page_names(self) -> list:
        return sorted(self.out_name.values())

    def knows(self, name: str) -> bool:
        return name in self.name_to_id or name in ("index.html", *DATA_FILES)

    def _link_html(self, path: str, r: dict) -> str:
        label = html.escape(r["label"])
        if r["state"] == "resolved":
            href = rel_href(path, self.out_name[r["id"]])
            if r.get("anchor"):
                href += "#" + urllib.parse.quote(r["anchor"], safe="")
            cls = "wl" + (" wl-missing-heading" if r.get("missing_heading") else "")
            return f'<a class="{cls}" href="{html.escape(href)}">{label}</a>'
        if r["state"] == "ambiguous":
            return f'<span class="wl wl-ambiguous" title="行き先を 1 つに決められない">{label}</span>'
        return f'<span class="wl wl-unresolved" title="ページがない">{label}</span>'

    def _transform_inline(self, children, path: str, counter: list, file: str) -> list:
        out = []
        i = 0
        while i < len(children):
            c = children[i]
            if c.type == "link_open":
                j = i + 1
                depth = 1
                while j < len(children):
                    if children[j].type == "link_open":
                        depth += 1
                    elif children[j].type == "link_close":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                inner = children[i + 1:j]
                href = c.attrs.get("href", "")
                try:
                    scheme = urllib.parse.urlparse(href).scheme.lower()
                except ValueError:
                    scheme = ""
                has_wl = any(t.type == "text" and wiki_pages.WIKILINK_FULL_RE.search(t.content) for t in inner)
                if scheme in EXTERNAL_SCHEMES and not has_wl:
                    c.attrSet("rel", "noopener noreferrer")
                    c.attrSet("class", "ext")
                    out.append(c)
                    out.extend(self._transform_inline(inner, path, counter, file))
                    if j < len(children):
                        out.append(children[j])
                else:
                    out.extend(self._transform_inline(inner, path, counter, file))
                    if c.markup != "autolink":
                        out.append(Token("text", "", 0, content=f"（{display_href(href)}）"))
                i = j + 1
                continue
            if c.type == "image":
                out.append(Token("text", "", 0, content=c.content))
                i += 1
                continue
            if c.type == "text" and "[[" in c.content and wiki_pages.WIKILINK_FULL_RE.search(c.content):
                pos = 0
                for m in wiki_pages.WIKILINK_FULL_RE.finditer(c.content):
                    if m.start() > pos:
                        out.append(Token("text", "", 0, content=c.content[pos:m.start()]))
                    r = counter[1][counter[0]]
                    counter[0] += 1
                    out.append(Token("html_inline", "", 0, content=self._link_html(path, r)))
                    pos = m.end()
                if pos < len(c.content):
                    out.append(Token("text", "", 0, content=c.content[pos:]))
                i += 1
                continue
            out.append(c)
            i += 1
        return out

    def body_html(self, pid: str) -> str:
        if pid in self._body_cache:
            return self._body_cache[pid]
        path = self.out_name[pid]
        # 下ごしらえの字句を 1 度だけ使う。描くときに字句を書き換えるので、描けずに終わったあとは、本文から
        # 解析し直して描く（設計の 7.2。描画は中核を変えないので、失敗のあとも同じ中核からもう一度描ける）。
        tokens = self.tokens.pop(pid, None)
        if tokens is None:
            tokens = self.parser.parse(self.page(pid)["body"])
            assign_heading_ids(tokens)
        counter = [0, self.links[pid]]
        for tok in tokens:
            if tok.type in ("th_open", "td_open") and "style" in (tok.attrs or {}):
                style = tok.attrs.pop("style")
                m = re.search(r"text-align:(left|center|right)", style)
                if m:
                    tok.attrSet("class", f"ta-{m.group(1)}")
            if tok.type == "inline" and tok.children:
                tok.children = self._transform_inline(tok.children, path, counter, path)
        rendered = self.parser.renderer.render(tokens, self.parser.options, {})
        self._body_cache[pid] = rendered
        return rendered

    def _meta_rows(self, pid: str, path: str, raw_mode: str) -> str:
        meta = self.page(pid)["meta"]
        rows = []
        for key, label in (("type", "種類"), ("tags", "タグ"), ("summary", "要約"),
                           ("created", "作成"), ("updated", "更新")):
            value = meta.get(key)
            if value in (None, "", []):
                continue
            text = "、".join(map(str, value)) if isinstance(value, list) else str(value)
            rows.append(f"<tr><th>{label}</th><td>{html.escape(text)}</td></tr>")
        sources = wiki_pages.ref_values(meta.get("sources"))
        if sources:
            cells = []
            for ref in sources:
                _status, found, _t = wiki_pages.classify_source_ref(
                    ref, self.pages_by_path, self.pages_by_slug, lambda d: self.pages[d]["meta"].get("type"))
                sp = self.resolve_disks(found)
                if sp is not None:
                    cells.append(f'<a href="{html.escape(rel_href(path, self.out_name[sp]))}">'
                                 f"{html.escape(self.title(sp))}</a>")
                else:
                    cells.append(f'<span class="wl wl-unresolved">{html.escape(ref)}</span>')
            rows.append(f"<tr><th>出典</th><td>{'、'.join(cells)}</td></tr>")
        raws = wiki_pages.ref_values(meta.get("raw"))
        if raws and raw_mode != "none":
            sha = str(meta.get("raw_sha256") or "")
            cells = [self._raw_html(classify_raw(v), sha, raw_mode, path) for v in raws]
            rows.append(f"<tr><th>原本</th><td>{'<br>'.join(cells)}</td></tr>")
        return "".join(rows)

    def _raw_html(self, raw: dict, sha: str, mode: str, path: str) -> str:
        value = raw["value"]
        if raw["kind"] != "absolute" or mode == "text":
            return f"<code>{html.escape(value)}</code>"
        if mode == "link":
            if value.startswith("~"):
                # ~ はホームに展開しない。生成器が機械の情報（ホームの絶対パス）を出力に足さないため、文字で出す
                # （構造の直しの設計の 8）。
                return f"<code>{html.escape(value)}</code>"
            return f'<a class="raw" href="{html.escape(file_url(value))}">{html.escape(value)}</a>'
        base = re.split(r"[\\/]", value)[-1]
        tail = f"（SHA-256 {html.escape(sha[:12])}…）" if sha else ""
        return f"<code>{html.escape(base)}</code>{tail}"

    def _relations_html(self, pid: str, path: str) -> str:
        if self.relations_state == "none":
            return ""
        if self.relations_state == "error":
            return '<section id="_relations"><h2>関係</h2><p class="state-error">オントロジーを読めない</p></section>\n'
        rel = self.relations[pid]
        if not rel["out"] and not rel["in"]:
            return ('<section id="_relations"><h2>関係</h2>' + self._incomplete_html()
                    + '<p class="state-empty">関係はない</p></section>\n')
        rows = []
        for direction, label in (("out", "→"), ("in", "←")):
            for r in rel[direction]:
                if r["other_id"] is not None:
                    other = (f'<a href="{html.escape(rel_href(path, self.out_name[r["other_id"]]))}">'
                             f'{html.escape(self.title(r["other_id"]))}</a>')
                else:
                    other = html.escape(str(r["other_text"]))
                cells = [label, html.escape(str(r["predicate"])), other]
                for key in ("status", "valid_from", "valid_to", "confidence", "source", "evidence"):
                    value = str(r.get(key) or "")
                    cell = html.escape(value)
                    if key == "source" and value:
                        found = self.resolve(nfc(value))
                        if found["state"] == "resolved":
                            cell = (f'<a href="{html.escape(rel_href(path, self.out_name[found["id"]]))}">'
                                    f"{html.escape(self.title(found['id']))}</a>")
                    cells.append(cell)
                rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        head = "".join(f"<th>{h}</th>" for h in ("向き", "述語", "相手", "状態", "開始", "終了", "確からしさ",
                                                 "出典", "証拠"))
        return (f'<section id="_relations"><h2>関係</h2>{self._incomplete_html()}'
                f'<table class="relations"><thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(rows)}</tbody></table></section>\n")

    def page_diagnostics(self, pid: str) -> list:
        return [d for d in self.diagnostics if d.get("page") == pid]

    def _diagnostics_html(self, items: list, path: str) -> str:
        if not items:
            return ""
        lis = []
        for d in items:
            parts = [d["kind"]]
            for key in ("target", "reason", "output", "node_id", "predicate", "object", "source", "source_page"):
                if d.get(key):
                    parts.append(f"{key}: {d[key]}")
            lis.append(f"<li>{html.escape(' / '.join(map(str, parts)))}</li>")
        return f'<section id="_diagnostics"><h2>診断</h2><ul>{"".join(lis)}</ul></section>\n'

    def _scope_html(self) -> str:
        """サイドバーの先頭に置く、スコープの表示。スクリプトなしでも見える。

        Fluent の Badge（tint）。ユーザースコープは brand、プロジェクトは中立の色で、文字でも見分ける（Fluent 2 の設計の 4）。
        """
        if not self.scope:
            return ""
        color = "brand" if self.scope == "user" else "informative"
        return (f'<p id="_scope" class="scope scope-{self.scope}"><span class="fui-Badge" data-appearance="tint" '
                f'data-color="{color}" data-size="large" data-shape="rounded">{SCOPE_HEADINGS[self.scope]}</span></p>')

    def _message_bar(self, mid: str, intent: str, title: str, body: str, extra_class: str = "") -> str:
        """Fluent の MessageBar（multiline）。アイコンはインライン SVG（Fluent 2 の設計の 4）。body は文字で渡す。"""
        cls = f"{extra_class} fui-MessageBar" if extra_class else "fui-MessageBar"
        return (f'<div id="{mid}" class="{cls}" role="group" aria-labelledby="{mid}_title" data-intent="{intent}" '
                f'data-layout="multiline"><div class="fui-MessageBar__icon" aria-hidden="true">'
                f'{ICON_ERROR if intent == "error" else ICON_WARNING}</div>'
                f'<div class="fui-MessageBarBody"><span class="fui-MessageBarTitle" id="{mid}_title">'
                f'{html.escape(title)}</span>{html.escape(body)}</div>'
                f'<div class="fui-MessageBarActions"></div><div class="fui-MessageBar__bottomReflowSpacer"></div></div>\n')

    def _head(self, path: str, title: str, stale: "str | None") -> str:
        prefix = root_prefix(path)
        banner = ""
        if stale:
            banner = self._message_bar("_stale", "warning", "前の内容を表示している",
                                       f"Wiki の最新の内容を表示できていない（{stale}）。", "stale")
        full_title = f"{title} · {SCOPE_LABELS[self.scope]}" if self.scope else title
        kind = "page-index" if path == "index.html" else "page-article"
        # data-theme="auto" は OS の配色に従う。theme.js が、選んだ配色を描く前に当てる（Fluent 2 の設計の 3）
        return (f'<!doctype html>\n<html lang="ja" data-theme="auto" data-root="{prefix}" '
                f'data-generation="{self.generation}">\n'
                f'<head>\n<meta charset="utf-8">\n'
                f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
                f'<meta name="referrer" content="no-referrer">\n'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f"<title>{html.escape(full_title)}</title>\n"
                f'<link rel="stylesheet" href="{prefix}assets/fluent2.css">\n'
                f'<link rel="stylesheet" href="{prefix}assets/style.css">\n'
                f'<script src="{prefix}assets/theme.js"></script>\n</head>\n'
                f'<body class="fui-FluentProvider {kind}">\n'
                f'<nav id="_nav" class="fui-NavDrawer" aria-label="wiki のナビゲーション">'
                f'<div class="fui-NavDrawerHeader nav-head">{self._scope_html()}'
                f'<a class="fui-NavItem nav-home" href="{prefix}index.html">索引</a></div>'
                f'<div class="fui-NavDrawerBody" id="_navbody">'
                f'<noscript><a class="fui-Link" href="{prefix}index.html">索引へ</a></noscript></div></nav>\n'
                f'<main id="_main">\n{banner}')

    def _tail(self, path: str, extra_data: tuple = ()) -> str:
        prefix = root_prefix(path)
        scripts = "".join(f'<script src="{prefix}{name}"></script>\n'
                          for name in ("nav.js", "data/manifest.js", *extra_data, "assets/app.js"))
        return f"</main>\n{scripts}</body>\n</html>\n"

    def render_page(self, pid: str, raw_mode: str = "text", stale: "str | None" = None) -> str:
        path = self.out_name[pid]
        title = self.title(pid)
        back = "".join(f'<li><a href="{html.escape(rel_href(path, self.out_name[b]))}">'
                       f"{html.escape(self.title(b))}</a></li>" for b in self.backlinks[pid])
        diags = list(self.page_diagnostics(pid))
        if stale:
            diags.insert(0, {"kind": "stale", "reason": stale})
        return (self._head(path, title, stale)
                + f'<article id="_article" data-page-id="{html.escape(pid)}">\n'
                + f'<table id="_meta" class="meta">{self._meta_rows(pid, path, raw_mode)}</table>\n'
                + f'<div id="_body">\n{self.body_html(pid)}</div>\n</article>\n'
                + f'<section id="_backlinks"><h2>被リンク</h2>{self._incomplete_html()}<ul>{back}</ul></section>\n'
                + self._relations_html(pid, path)
                + self._diagnostics_html(diags, path)
                + self._tail(path))

    def _incomplete_html(self) -> str:
        """中身に依る一覧（未解決リンク、被リンク、関係）の「不完全」の印（構造の直しの設計の 4.1）。"""
        if not self.unreadable_pages:
            return ""
        return ('<p class="notice incomplete">読めないページがあるので、この一覧は不完全である'
                f"（{len(self.unreadable_pages)} ページ）</p>")

    def _broken_page(self, pid: str, error: str, stale: "str | None") -> str:
        """描けないページの代わり。その場所に「このページを描けない」と出す（設計の 7.2）。"""
        path = self.out_name[pid]
        title = self.title(pid)
        return (self._head(path, title, stale)
                + f'<article id="_article" data-page-id="{html.escape(pid)}">\n'
                + self._message_bar("_render_error", "error", "このページを描けない",
                                    f"{error}。lint で確かめる。", "notice notice-error render-error")
                + "</article>\n"
                + self._tail(path))

    # --- 索引のページの一覧と検索（探しやすさの設計の 4、5） ---

    def page_summary(self, pid: str) -> tuple:
        """ページの要約と、その出どころ（4.2）。(文字, "frontmatter" | "index" | "lead" | "none")。"""
        own = wiki_pages.scalar_value(self.page(pid)["meta"].get("summary"))
        if own and str(own).strip():
            return nfc(str(own).strip()), "frontmatter"
        if self.index_summary.get(pid):
            return self.index_summary[pid], "index"
        lead = self.lead.get(pid, "")
        if lead:
            return lead, "lead"
        return "", "none"

    def page_type(self, pid: str) -> str:
        return str(wiki_pages.type_value(self.page(pid)["meta"].get("type")) or "")

    def page_updated(self, pid: str) -> str:
        return str(wiki_pages.scalar_value(self.page(pid)["meta"].get("updated")) or "")

    def page_tags(self, pid: str) -> list:
        return [nfc(t) for t in wiki_pages.ref_values(self.page(pid)["meta"].get("tags"))]

    def _type_badge(self, value: str) -> str:
        return (f'<span class="badge fui-Badge {type_class(value)}" data-appearance="tint" data-size="small" '
                f'data-shape="rounded">{html.escape(type_label(value))}</span>')

    def _index_row(self, pid: str, order: int, path: str) -> str:
        title = self.title(pid)
        t = self.page_type(pid)
        updated = self.page_updated(pid)
        summary, _from = self.page_summary(pid)
        tags = self.page_tags(pid)
        gpath = " › ".join(self.page_path.get(pid, []))
        href = rel_href(path, self.out_name[pid])
        # data-id は、検索の索引（data/search.js）の行と結ぶためのページの識別子
        parts = [f'<li class="fx-row" data-id="{html.escape(pid)}" data-type="{html.escape(t)}" '
                 f'data-updated="{html.escape(updated)}" '
                 f'data-title="{html.escape(title)}" data-order="{order}" '
                 f'data-groups="{" ".join(sorted(self.page_groups.get(pid, ())))}">']
        if gpath:
            parts.append(f'<div class="fx-path fui-Caption1">{html.escape(gpath)}</div>')
        parts.append(f'<div class="ix-line"><a class="ix-title" href="{html.escape(href)}">{html.escape(title)}</a>'
                     f"{self._type_badge(t)}")
        if updated:
            parts.append(f'<time class="fui-Caption1" datetime="{html.escape(updated)}">{html.escape(updated)}</time>')
        parts.append("</div>")
        if summary:
            parts.append(f'<p class="ix-summary">{html.escape(summary)}</p>')
        if tags:
            parts.append('<div class="ix-tags">' + "".join(
                f'<span class="tag fui-Tag" data-appearance="outline" data-size="extra-small">'
                f'<span class="fui-Tag__primaryText">{html.escape(t)}</span></span>' for t in tags) + "</div>")
        parts.append("</li>")
        return "".join(parts)

    def _index_unresolved_html(self) -> str:
        if not self.index_unresolved:
            return ""
        lis = []
        for trail, it in self.index_unresolved:
            title = "ページがない" if it["state"] == "unresolved" else "行き先を 1 つに決められない"
            where = f'<span class="fui-Caption1 state">（{html.escape(" › ".join(trail))}）</span>' if trail else ""
            lis.append(f'<li><span class="wl wl-{it["state"]}" title="{title}">{html.escape(it["label"])}</span>'
                       f"{where}</li>")
        return ('<section id="_index_unresolved"><h2 class="fui-Subtitle2">ページに決まらない索引の行</h2>'
                f'<ul>{"".join(lis)}</ul></section>\n')

    def render_index(self, stale: "str | None" = None) -> str:
        path = "index.html"
        diags = list(self.diagnostics)
        if stale:
            diags.insert(0, {"kind": "stale", "reason": stale})
        nav_note = ""
        if self.unreadable_indexes:
            nav_note = (self._message_bar("_nav_incomplete", "warning", "ナビゲーションは不完全",
                                          f"読めない索引のファイルがある（{len(self.unreadable_indexes)} 件）。",
                                          "notice incomplete"))
        diag_note = self._incomplete_html()
        total = len(self.page_order)
        rows = "".join(self._index_row(pid, k + 1, path) for k, pid in enumerate(self.page_order))
        return (self._head(path, "索引", stale)
                + f'<header id="_ixhead"><h1 class="fui-Title2">索引</h1>'
                  f'<p class="ix-stats fui-Caption1">{total} ページ</p></header>\n{nav_note}'
                + '<div id="_filters" class="fx-bar"><noscript><p class="state fui-Caption1">絞り込みと検索には '
                  'JavaScript が要る。一覧は索引の順に並んでいる。</p></noscript></div>\n'
                + f'<p id="_count" class="fx-count fui-Caption1" aria-live="polite">{total} 件</p>\n'
                + f'<ul id="_index" class="fx-list">{rows}</ul>\n'
                + self._index_unresolved_html()
                + (f'<section id="_incomplete">{diag_note}</section>\n' if diag_note else "")
                + self._diagnostics_html(diags, path)
                + self._tail(path, ("data/diagnostics.js",)))

    def search_json(self) -> dict:
        """data/search.js（5.1）。描かれているページ 1 つに 1 行、識別子の順。"""
        rows = []
        for pid in sorted(self.drawn):
            summary, origin = self.page_summary(pid)
            headings = [[hid, nfc(text)] for _k, hid, text in self.headings.get(pid, [])]
            rows.append([self.out_name[pid], pid, nfc(self.title(pid)), summary, origin, self.page_type(pid),
                         self.page_tags(pid), headings, self.page_updated(pid),
                         [nfc(x) for x in self.page_path.get(pid, [])]])
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation,
                "incomplete": bool(self.unreadable_pages),
                "fields": ["path", "id", "title", "summary", "summary_from", "type", "tags", "headings", "updated",
                           "group_path"],
                "rows": rows}

    # --- データのファイル ---

    def _nav_json(self) -> dict:
        def conv(groups):
            out = []
            for g in groups:
                items = []
                for it in g["items"]:
                    item = {"state": it["state"], "label": it["label"]}
                    if it["state"] == "resolved":
                        item["id"] = it["id"]
                        item["path"] = self.out_name[it["id"]]
                        if it.get("anchor"):
                            item["anchor"] = it["anchor"]
                    elif it["state"] == "group":
                        item["group"] = it["group"]
                    items.append(item)
                out.append({"id": g["id"], "title": g["title"], "summary": g.get("summary", ""),
                            "count": g.get("count", 0), "items": items, "children": conv(g["children"])})
            return out
        nav = {"protocol_version": PROTOCOL_VERSION, "generation": self.generation, "groups": conv(self.nav_groups)}
        if self.unreadable_indexes:
            nav["incomplete"] = True
        return nav

    def manifest(self, generated_at: str = "") -> dict:
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation, "generated_at": generated_at,
                "capabilities": {"search": "embedded", "relations": self.relations_state, "rag": False,
                                 "multi_wiki": False, "live_reload": False},
                "pages": len(self.drawn),
                "incomplete": {"unreadable_pages": len(self.unreadable_pages),
                               "unreadable_indexes": len(self.unreadable_indexes),
                               "unreadable_ontology": bool(self.unreadable_ontology)}}

    def diagnostics_json(self) -> dict:
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation, "items": self.diagnostics}

    def render_data(self, name: str, generated_at: str = "", stale: "str | None" = None) -> str:
        if name == "nav.js":
            return js_data("WIKI_NAV", self._nav_json())
        if name == "data/manifest.js":
            m = self.manifest(generated_at)
            if stale:
                m["stale"] = stale
            return js_data("WIKI_MANIFEST", m)
        if name == "data/search.js":
            return js_data("WIKI_SEARCH", self.search_json())
        if name == "data/diagnostics.js":
            d = self.diagnostics_json()
            if stale:
                d["items"] = [{"kind": "stale", "reason": stale}] + d["items"]
            return js_data("WIKI_DIAGNOSTICS", d)
        raise KeyError(name)

    def render(self, name: str, raw_mode: str = "text", generated_at: str = "",
               stale: "str | None" = None) -> str:
        """出力の名前 1 つを描く。中核が知らない名前なら KeyError。ほかの要求と同時に呼んでよい。"""
        with self._render_lock:
            return self._render(name, raw_mode, generated_at, stale)

    def _render(self, name: str, raw_mode: str, generated_at: str, stale: "str | None") -> str:
        if name == "index.html":
            return self.render_index(stale)
        if name in DATA_FILES:
            return self.render_data(name, generated_at, stale)
        pid = self.name_to_id[name]
        try:
            return self.render_page(pid, raw_mode, stale)
        except Exception as e:  # noqa: BLE001  1 ページを描けなくても、その場所に「描けない」と出して続ける（設計の 7.2）
            if pid not in self.render_failures:
                self.render_failures.append(pid)
            return self._broken_page(pid, type(e).__name__, stale)

    def render_all(self, raw_mode: str, generated_at: str = "") -> dict:
        out = {}
        for name in self.page_names():
            out[name] = self.render(name, raw_mode)
        out["index.html"] = self.render_index()
        for name in DATA_FILES:
            out[name] = self.render_data(name, generated_at)
        return out


def js_data(name: str, value) -> str:
    """<script src> で読むデータ。</、U+2028、U+2029 をエスケープする。"""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    text = text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"window.{name}={text};\n"


def load_assets() -> dict:
    """同梱の画面の部品（app.js、style.css）を読む。serve と build の始めに 1 度だけ呼ぶ（設計の 3.3）。

    読めなければ fs.Unknown か fs.Absent を送出する（呼ぶ側は起動しない）。
    """
    return {name: fs.read_file(os.path.join(ASSET_DIR, PurePosixPath(name).name)) for name in ASSETS}


# ---------------------------------------------------------------------------
# 公開の前の検査（構造の直しの設計の 5、8）
# ---------------------------------------------------------------------------

def path_forms(path: str) -> list:
    """絶対パスを、出力に現れうる形にする。"""
    posix = str(path).replace(BS, "/").rstrip("/")
    win = posix.replace("/", BS)
    forms = {posix, win, win.replace(BS, BS * 2), posix.replace("/", BS + "/")}
    forms |= {html.escape(f) for f in list(forms)}
    forms.add(urllib.parse.quote(posix, safe=""))
    forms.add(urllib.parse.quote(posix, safe="/:"))
    return sorted(f for f in forms if len(f) >= 4)


def scan_machine_info(files: dict, needles: list, tokens: list) -> list:
    """作った世代から、機械の情報（絶対パスと、サーバーの記録のトークン）を探す。見つけた (ファイル名, 種類) の並び。

    生成器は機械の情報を出力に足さないので、見つかるのは入力の実体の中身にあるときだけである。だれがその中身を
    書いたかは問わない。見つかれば、呼ぶ側が警告を出す。書き出しは止めない（構造の直しの設計の 8）。
    """
    fold = os.name == "nt" or sys.platform == "darwin"
    norm = (lambda s: s.lower()) if fold else (lambda s: s)
    forms = [norm(f) for needle in needles for f in path_forms(needle)]
    found = []
    for name in sorted(files):
        data = files[name]
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        folded = norm(text)
        if any(f in folded for f in forms):
            found.append((name, "path"))
        if any(t in text for t in tokens):
            found.append((name, "token"))
    return found


BS = chr(92)
