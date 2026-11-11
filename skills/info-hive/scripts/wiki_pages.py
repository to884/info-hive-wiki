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
"""

from __future__ import annotations

import functools
import hashlib
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path, PurePosixPath

from wiki_text import nfc, posix_rel

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


def iter_page_files(wiki_root: Path) -> list[Path]:
    """ページのファイルを、NFC の / 区切りの相対パスの順に返す。"""
    found = []
    # 拡張子の大文字と小文字を問わずに集める（Linux の rglob("*.md") は Upper.MD を返さない）。
    for md_path in wiki_root.rglob("*.[mM][dD]"):
        rel = md_path.relative_to(wiki_root).as_posix()
        if is_page_path(rel) and md_path.is_file():
            found.append((posix_rel(rel), md_path))
    found.sort(key=lambda item: item[0])
    return [path for _rel, path in found]


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
