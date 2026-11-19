#!/usr/bin/env python3
"""
wiki_lint.py — LLM Wiki の構造の健全性をチェックする。

孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の問題、更新の止まったページ、
重複したスラッグと、（--suggest-pages を付けた場合は）多くのページに出てくるのに専用のページがない語を報告する。
日本語の wiki で起きやすい書き損じとして、英語のキーと日本語の別名の重複、全角の「：」を使った
frontmatter のキー、「、」で区切ったリストの値、全角の「｜」「＃」を使った wikilink も報告する。
frontmatter の文字列の値（配列の要素を含む）が二重引用符で囲まれていないページも報告する。
索引（index.md と indexes/）の見出しと、その下に並ぶページの type の食い違い、`sources:` が
source ページを指していないもの、source ページの `raw:` の原本がないもの、（--allowed-tags を
渡した場合は）タグの体系にないタグも報告する。
大文字と小文字だけが違うページとディレクトリも報告する。
YouTube の動画のソースの、字幕の訂正と留保の印と定義、confidence を raw と照らした食い違いも報告する
（caption_corrections、info-hive-dev の ADR-0027。照合は wiki_captions.py が行う）。
本文の画像（`![説明](../assets/x.svg)`）の参照先がないものも報告する。参照先はページのファイルが
あるディレクトリを基準に解決する。ページの大きさは、コードフェンス（mermaid を含む）の中身と
画像だけの行を除いて数える。

ファイル名、パス、スラッグ、wikilink の行き先は NFC にそろえてから比べる。frontmatter の
日本語のキー（タイトル、種類など）と type・kind の日本語の値は、英語に直してから検査する。

保守的な設計で、検出結果を報告するだけで編集はしない。

使い方:
    python wiki_lint.py [<wiki-dir>] [options]

<wiki-dir> を省くと、作業領域の設定（`.info-hive/workspace.json`）の dirs.wiki を使う。
設定がなければ ./wiki を使う。設定が壊れているときは理由を書いて終了コード 1 で止まる。

オプション:
    --soft-cap N        ページサイズのソフト上限の行数（既定: 100）
    --hard-cap N        ページサイズのハード上限の行数（既定: 200）
    --soft-char-cap N   ページサイズのソフト上限の文字数。空白を除いて数える（既定: 10000）
    --hard-char-cap N   ページサイズのハード上限の文字数。空白を除いて数える（既定: 25000）
    --required-fm a,b   必須の frontmatter のフィールド（既定: type,title,tags,created,updated）
    --suggest-pages     多くのページに出てくるのに専用のページがない語を挙げる
    --suggest-min N     --suggest-pages で挙げる最小の出現ページ数（既定: 5）
    --allowed-tags a,b  タグの体系（SCHEMA.md の「タグの体系」に並ぶタグ）。渡すと、ここにないタグを
                        報告する。渡さないか空にすると、タグの検査はしない
    --json              テキストの代わりに JSON を出力する

例:
    python wiki_lint.py wiki/
    python wiki_lint.py wiki/ --suggest-pages
    python wiki_lint.py wiki/ --json > lint.json
    python wiki_lint.py wiki/ --allowed-tags "知識表現,情報検索,手法"
"""

import argparse
import json
import os
import re
import urllib.parse
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path, PurePosixPath

# 同じディレクトリの wiki_text を import する。スクリプトとして実行したときも、
# テストから import したときも見つかるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import (  # noqa: E402
    TYPE_ALIASES,
    canonical_meta,
    char_count,
    fullwidth_colon_keys,
    has_fullwidth_link_syntax,
    ideographic_comma_values,
    is_iso_date,
    CODE_SPAN_RE,
    IMAGE_RE,
    image_target,
    lines_outside_fences,
    localize_argparse,
    nfc,
    nfkc,
    normalize_tags,
    parse_frontmatter_text,
    posix_rel,
    strip_fences_and_images,
    unquoted_string_keys,
)
import wiki_initialized  # noqa: E402
import wiki_workspace  # noqa: E402
# ページの集合とリンクの規則は wiki_pages の 1 か所に置く（ADR-0010 の決定の 1）。ほかのスクリプトと試験が
# wiki_lint.resolve_link などの名前で使うので、同じ名前で読み込んでおく。
from wiki_pages import (  # noqa: E402
    destination_src,
    SKIP_TOP_LEVEL_DIRS,
    SKIP_TOP_LEVEL_FILES,
    WIKILINK_RE,
    case_only_duplicates,
    clean_link_target,
    refused_link,
    extract_wikilinks,
    build_page_index,
    classify_source_ref,
    iter_page_files,
    ref_values,
    resolve_link,
    article_path,
    is_page_path,
    walk_pages,
    scalar_value,
    text_value,
    type_value,
    walk_index_files,
    resolve_raw,
)


URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# Windows のドライブから始まる絶対パス（C:/Books/x.pdf、C:\Books\x.pdf）。URL_SCHEME_RE にも合うので先に見る。
DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
# frontmatter の値に書かれた wikilink（`sources: ["[[消費税]]"]`）。
# 索引の見出しとリストの項目。
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
# 見出しの末尾の件数（「ソース（9）」「Sources (9)」）。
HEADING_COUNT_RE = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# 日本語のページの候補。カタカナ語は 3 文字以上、漢字の複合語は 3 文字以上の漢字の並びとする。
# 2 文字の漢字の並びは「方法」「場合」のような一般語が多く、候補として役に立たない。
KATAKANA_TERM_RE = re.compile(r"[\u30a1-\u30fa\u30fc]{3,}")
KANJI_COMPOUND_RE = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{3,}")
CAPITALIZED_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3})\b")


# 分割した索引（indexes/<type>.md、indexes/entities-people.md）のファイル名の `-` より前が表す type。
INDEX_FILE_TYPES = {
    "source": "source", "sources": "source",
    "entity": "entity", "entities": "entity",
    "concept": "concept", "concepts": "concept",
    "synthesis": "synthesis", "syntheses": "synthesis",
}
# 索引の見出しが表すページの type。日本語は type の値の別名と同じ語を使い、英語は単数と複数を読む。
# 比べるときは NFKC にそろえて小文字にする。
INDEX_HEADING_TYPES = {nfkc(k).lower(): v for k, v in TYPE_ALIASES.items()}
INDEX_HEADING_TYPES.update(INDEX_FILE_TYPES)


def heading_type(text: str) -> "str | None":
    """索引の見出しの文字列から、そこに並ぶページの type を返す。種類の見出しでなければ None を返す。"""
    key = HEADING_COUNT_RE.sub("", nfkc(text).strip()).strip().lower()
    return INDEX_HEADING_TYPES.get(key)


def index_files(wiki_root: Path) -> list[Path]:
    """索引のファイル（index.md と、indexes/ の下の Markdown）を、読む順に返す。

    lint の索引の検査と、閲覧のナビゲーションが、同じ集め方をする。集め方は wiki_pages.walk_index_files で、
    ふつうのファイルだけを返す（0.10.1 までは、rglob("*.md") が返す名前を、種類を確かめずに返していた）。
    並びは今までどおり、index.md、そのあと indexes の下を Path の順にする。知らせは捨てる。
    """
    wiki_root = Path(wiki_root)
    rels = [f.rel for f in walk_index_files(wiki_root).files]
    top = [wiki_root / rel for rel in rels if rel == "index.md"]
    return top + sorted(wiki_root / rel for rel in rels if rel != "index.md")


def collect_index_entries(wiki_root: Path) -> list[dict]:
    """index.md と indexes/ の各ファイルから、種類の見出しの下に並ぶ項目を集める。

    index.md は「## ソース」「## シンセシス」のような種類の見出しの下だけを読む。indexes/ のファイルは、
    ファイル名（`sources.md`、`entities-people.md` の `-` より前）が種類を表すので、ファイル全体を
    その種類として読み、種類の見出しがあればそちらを優先する。種類の見出しより深い見出し（### 人物）は
    上の種類を引き継ぎ、同じか浅い見出しで種類でないもの（## 使い方）が来たら、種類の指定を外す。

    読むのはリストの項目（`- `、`* `、`1. `）の最初の wikilink だけである。冒頭の説明文に書いた
    `[[消費税]]` のような例示や、項目の要約に出てくるリンクは数えない。コードの中も読まない。
    """
    top = wiki_root / "index.md"
    files = index_files(wiki_root)
    entries = []
    for path in files:
        rel = posix_rel(str(path.relative_to(wiki_root)))
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        default = None
        if path != top:
            default = INDEX_FILE_TYPES.get(nfkc(path.stem).lower().split("-", 1)[0])
        current, level, heading = default, 0, None
        for line in lines_outside_fences(text):
            m = HEADING_RE.match(line)
            if m:
                depth = len(m.group(1))
                kind = heading_type(m.group(2))
                if kind:
                    current, level, heading = kind, depth, m.group(2).strip()
                elif depth <= level:
                    current, level, heading = default, 0, None
                continue
            if current is None:
                continue
            item = LIST_ITEM_RE.match(line)
            if not item:
                continue
            # コードを空白に置き換えてから探す。消すと左右がつながり、もとにないリンクができることがある。
            link = WIKILINK_RE.search(CODE_SPAN_RE.sub(lambda c: " " * len(c.group(0)), item.group(1)))
            if link:
                entries.append({"index": rel, "heading": heading, "expected": current,
                                "link": nfc(link.group(1).strip())})
    return entries


# frontmatter の参照の読み方は、グラフの抽出と同じく wiki_pages に置く。
_ref_values = ref_values


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def missing_raw(meta: dict, md_path: Path, wiki_root: Path) -> list[dict]:
    """共通の raw 解決で missing になった値だけを返す。拒否や曖昧さを「ない」に変えない。"""
    return [{"raw": item.raw, "absolute": bool(DRIVE_PATH_RE.match(item.raw)
             or item.raw.startswith(("/", "\\", "~")))}
            for item in resolve_raw(meta, md_path, wiki_root) if item.state == "missing"]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _encode_md_path(path: str) -> str:
    """Markdown の参照先に書けるよう、意味を持つ文字（% # ? 空白 括弧 山括弧）をパーセントエンコードする。"""
    table = {"%": "%25", "#": "%23", "?": "%3F", " ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E"}
    return "".join(table.get(ch, ch) for ch in path)


def image_ref_path(target: str) -> str:
    """画像の参照先を、ファイルのパスの文字にする。

    markdown-it が画像の src を作るのと同じ手順（Markdown のエスケープと文字参照を解き、URL を正規化する）を
    destination_src で通してから、# と ? から後を落とし、パーセントエンコードを戻す。閲覧（markdown-it の src を
    解く figure_target）と、作りの上で同じ文字を解く（図の描画の設計の 6.4）。文字参照の # を区切りと取り違えない
    ように、切る前に解く。
    """
    src = destination_src(target)
    return urllib.parse.unquote(src.split("#", 1)[0].split("?", 1)[0])


def missing_assets(body: str, md_path: Path, wiki_root: Path) -> list[dict]:
    """本文の画像の参照のうち、ファイルがないもの（または wiki の外を指すもの）を返す。

    参照先は、Markdown の決まりどおり、ページのファイルがあるディレクトリを基準に解決する。
    wiki/concepts/x.md から wiki/assets/x-01.svg を指すなら ../assets/x-01.svg と書く。
    wiki のルートを基準にすると見つかるときは、正しい相対パスを suggest に入れる。
    URL、data:、絶対パスは見ない。コードフェンスとインラインのコードの中は見ない。
    """
    found: list[dict] = []
    root = wiki_root.resolve()
    for line in lines_outside_fences(body):
        # コードを空白に置き換える。消すと左右がつながり、もとにない画像の記法ができることがある。
        for m in IMAGE_RE.finditer(CODE_SPAN_RE.sub(lambda c: " " * len(c.group(0)), line)):
            target = image_target(m).strip()
            if not target or URL_SCHEME_RE.match(target) or target.startswith(("/", "#", "\\")):
                continue
            rel = image_ref_path(target)
            if not rel:
                continue
            # %00 のように、ファイルのパスにできない参照先。lint を止めずに報告する。
            # NUL は自分で確かめる。Path.resolve() が NUL で ValueError を出すのは、Windows では 3.12 までである。
            if "\0" in rel:
                found.append({"target": target, "invalid": True})
                continue
            try:
                resolved = (md_path.parent / rel).resolve()
            except (ValueError, OSError):
                found.append({"target": target, "invalid": True})
                continue
            inside = _inside(resolved, root)
            if inside and resolved.is_file():
                continue
            item = {"target": target}
            if not inside:
                item["outside"] = True
            # wiki のルートを基準にすると wiki の中のファイルが見つかるときだけ、正しい相対パスを示す。
            try:
                from_root = (root / rel).resolve()
            except (ValueError, OSError):
                from_root = None
            if from_root is not None and from_root.is_file() and _inside(from_root, root):
                suggest = Path(os.path.relpath(from_root, md_path.parent.resolve())).as_posix()
                item["suggest"] = _encode_md_path(suggest)
            found.append(item)
    return found


def parse_frontmatter(text: str) -> tuple[dict, str, bool]:
    """(metadata, body, malformed) を返す。frontmatter が書かれているのにパースできなかった場合は malformed=True になる。

    日本語のキーと type・kind の日本語の値は英語に直して返す。
    """
    raw_meta, _fm_text, body, malformed = parse_frontmatter_raw(text)
    meta, _conflicts = canonical_meta(raw_meta)
    return meta, body, malformed


def parse_frontmatter_raw(text: str) -> "tuple[dict, str | None, str, bool]":
    """(書かれたままのキーの metadata, frontmatter の本文, body, malformed) を返す。

    キーは英数字に限らず、日本語のキーも読む。英語に直す前の形を返すので、別名の衝突や
    全角の「：」の検査にも使える。frontmatter がなければ、frontmatter の本文は None になる。
    """
    if not text.startswith("---"):
        return {}, None, text, False
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, None, text, True
    fm_text = m.group(1)
    body = text[m.end():]
    return parse_frontmatter_text(fm_text), fm_text, body, False


def link_report(wiki_root: Path) -> dict:
    """wiki の側の記事と記事までの道のリンク（info-hive-dev の ADR-0029）。何も書かず、直さない。

    走査（walk_pages、walk_index_files）が拒んだ項目と、最上位の SCHEMA.md と log.md を読む口（article_path）が
    拒んだものを、{"path", "kind"} で返す。走査の知らせ（一覧を作れなかったディレクトリと、種類を調べられなかった
    項目）は unchecked に入れる。raw の側（source ページの raw: が指すもの）は、この版では調べない。
    """
    root = Path(wiki_root)
    pages, index = walk_pages(root), walk_index_files(root)
    seen, links = set(), []
    for walk in (pages, index):
        for rel, kind in walk.links:
            if rel not in seen:
                seen.add(rel)
                links.append({"path": rel, "kind": kind})
    unchecked = set(pages.unlistable) | set(pages.unknown) | set(index.unlistable) | set(index.unknown)
    for name in ("SCHEMA.md", "log.md"):
        found = article_path(root, name)
        if found.state == "link" and found.link[0] not in seen:
            seen.add(found.link[0])
            links.append({"path": found.link[0], "kind": found.link[1]})
        elif found.state == "denied":
            unchecked.add(name)
    links.sort(key=lambda item: (posix_rel(item["path"]), item["path"]))
    return {"links": links, "unchecked": [{"path": rel} for rel in sorted(unchecked, key=lambda r: (posix_rel(r), r))]}


class LintPages(list):
    """原本をまだ読まず、raw の解決の基準だけを集計まで保つ。"""
    def __init__(self, wiki_root):
        super().__init__()
        self.wiki_root = wiki_root


def collect_pages(wiki_root: Path) -> list[dict]:
    pages = LintPages(wiki_root)
    for md_path in iter_page_files(wiki_root):
        rel = md_path.relative_to(wiki_root)
        # 比べるときは NFC にそろえた / 区切りのパスを使う。ディスク上の名前のままのパスも残し、
        # 正規化すると同じ名前になる2つのファイルを、重複の報告で見分けられるようにする。
        rel_path = posix_rel(str(rel))
        disk_rel_path = rel.as_posix()
        slug = nfc(md_path.stem)
        try:
            # Windows のメモ帳などが付ける BOM は読み飛ばす。
            text = md_path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as e:
            pages.append({
                "path": str(md_path),
                "rel_path": rel_path,
                "disk_rel_path": disk_rel_path,
                "slug": slug,
                "read_error": str(e),
            })
            continue
        raw_meta, fm_text, body, malformed = parse_frontmatter_raw(text)
        meta, alias_conflicts = canonical_meta(raw_meta)
        # 大きさは、コードフェンスの中身と画像だけの行を除いて数える（ADR-0005）。
        sized = strip_fences_and_images(text)
        line_count = sized.count("\n") + 1
        # コードフェンスとインラインのコードの中の [[...]] は数えない（ADR-0010 の決定の 1）。
        links = extract_wikilinks(body)
        pages.append({
            "path": str(md_path),
            "rel_path": rel_path,
            "disk_rel_path": disk_rel_path,
            "slug": slug,
            "meta": meta,
            "body": body,
            "line_count": line_count,
            "char_count": char_count(sized),
            "links": links,
            "malformed_fm": malformed,
            "alias_conflicts": alias_conflicts,
            "fullwidth_colon_keys": fullwidth_colon_keys(fm_text) if fm_text else [],
            "ideographic_comma_keys": ideographic_comma_values(raw_meta),
            "unquoted_keys": unquoted_string_keys(fm_text) if fm_text else [],
            "missing_assets": missing_assets(body, md_path, wiki_root),
        })
    return pages


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ISO 形式（YYYY-MM-DD）を求める frontmatter の日付のフィールド。
DATE_FIELDS = ("created", "updated", "ingested")
# 必須の frontmatter のフィールドの既定値（--required-fm と MCP の lint ツールが使う）。
DEFAULT_REQUIRED_FM = ("type", "title", "tags", "created", "updated")


def lint(pages: list[dict], soft_cap: int, hard_cap: int, required_fm: list[str], suggest_pages: bool, suggest_min: int,
         soft_char_cap: int = 10000, hard_char_cap: int = 25000, index_entries: "list[dict] | None" = None,
         allowed_tags: "list[str] | None" = None, wiki_root: "Path | None" = None,
         raw_root: "Path | None" = None, wiki_root_given: "Path | None" = None) -> dict:
    """検出結果を返す。

    wiki_root を渡すと、YouTube の字幕の訂正と留保の印と定義、confidence を raw と照らす
    （caption_corrections、ADR-0027）。None なら照らさず、summary の caption_corrections を None にする。

    index_entries は collect_index_entries の結果で、None なら索引の検査（index_type_mismatch）をしない。
    allowed_tags はタグの体系で、None か空ならタグの検査（unknown_tags）をせず、summary の
    unknown_tags を None にする。タグは NFKC にそろえてから比べる。
    """
    allowed = None
    if allowed_tags is not None:
        allowed = set(normalize_tags(allowed_tags)) - {""}
        if not allowed:
            allowed = None
    raw_wiki_root = wiki_root if wiki_root is not None else getattr(pages, "wiki_root", None)
    findings = {
        "orphans": [],
        "broken_links": [],
        "oversized_hard": [],
        "oversized_soft": [],
        "missing_frontmatter": [],
        "malformed_frontmatter": [],
        "duplicate_slugs": [],
        "case_only_duplicates": [],
        "stale_pages": [],
        "read_errors": [],
        "frontmatter_alias_conflicts": [],
        "fullwidth_colon_keys": [],
        "ideographic_comma_lists": [],
        "fullwidth_link_syntax": [],
        "non_iso_dates": [],
        "unquoted_strings": [],
        "missing_assets": [],
        "index_type_mismatch": [],
        "unresolved_sources": [],
        "missing_raw": [],
        "unknown_tags": [],
        "caption_corrections": [],
        "overview_stale": None,
        "overview_problems": None,
        "links": [],
        "links_unchecked": [],
        "raw_ambiguous": [],
        "raw_unknown": [],
        "raw_version_unverified": [],
        "raw_paths_unchecked": [],
        "raw_outside": [],
        "link_warnings": [],
        "suggested_pages": [],
        "summary": {},
    }

    # 記事と記事までの道のリンク（ADR-0029）。走査はリンクの記事を読まないので、ページの集合に入らない。
    # そのページへの wikilink は、リンク切れではなく、ここで挙げる（拒んだことを「ない」に変えない。I4）。
    if wiki_root is not None:
        report = link_report(Path(wiki_root))
        findings["links"] = report["links"]
        findings["links_unchecked"] = report["unchecked"]
        # どの読む側の結果にも、同じ名前の欄で知らせる（MCP の query、get_page などと同じ形）
        findings["link_warnings"] = list(report["links"])

    raw_roots = [raw_root] if raw_root is not None else []
    recorded_raw_roots = []
    if raw_wiki_root is not None:
        from wiki_raw_roots import read_raw_roots
        recorded_raw_roots, unchecked = read_raw_roots(Path(raw_wiki_root))
        findings["links_unchecked"].extend({**item, "scope": "raw_roots"} for item in unchecked)

    # YouTube の字幕の訂正と留保（ADR-0027）。ページは照合のモジュールが読み直す（印の行の番号をファイルの
    # 行で示すため）。動画の raw を持たない wiki では、印のあるページだけを見る。
    if wiki_root is not None:
        import wiki_captions
        result = wiki_captions.check_wiki(Path(wiki_root), raw_roots=raw_roots, recorded_raw_roots=recorded_raw_roots,
                                          wiki_root_given=wiki_root_given)
        findings["caption_corrections"] = [f.as_dict() for f in result.findings]
        import wiki_overview
        overviews = wiki_overview.inspect_wiki(Path(wiki_root))
        findings["overview_stale"] = overviews["overview_stale"]
        findings["overview_problems"] = overviews["overview_problems"]

    # 読み込みのエラー
    for p in pages:
        if "read_error" in p:
            findings["read_errors"].append({"path": p["rel_path"], "error": p["read_error"]})

    pages = [p for p in pages if "read_error" not in p]

    # スラッグ。NFC にそろえたスラッグで比べるので、macOS の NFD のファイル名と NFC の
    # ファイル名が同じスラッグになる場合も重複として見つかる。報告にはディスク上の名前のままの
    # パスを並べる。NFC のパスでは、同じディレクトリの2つのファイルが同じ文字列になってしまう。
    slug_to_pages = defaultdict(list)
    for p in pages:
        slug_to_pages[p["slug"]].append(p)
    for slug, group in slug_to_pages.items():
        if len(group) > 1:
            finding = {"slug": slug, "paths": [p.get("disk_rel_path", p["rel_path"]) for p in group]}
            nfc_paths = [p["rel_path"] for p in group]
            if len(set(nfc_paths)) < len(nfc_paths):
                # Unicode の正規化の違いだけで名前が異なるファイルがある。
                finding["normalization_collision"] = True
            findings["duplicate_slugs"].append(finding)

    # 大文字と小文字だけが違うページとディレクトリ。NTFS と APFS の既定では、静的な書き出しで同じ場所を
    # 取るので、閲覧の書き出しが接尾辞を足すか止まる（ADR-0010 の決定の 3）。前もって知らせる。
    findings["case_only_duplicates"] = case_only_duplicates([p["rel_path"] for p in pages])

    # 被リンクの対応表
    inbound = defaultdict(set)
    all_slugs = set(slug_to_pages.keys())
    # 解決用のインデックス。パスを付けたリンクには wiki からの完全な相対パス（.md なし）を、
    # パスのないリンクにはステムだけを使う。
    by_slug = all_slugs
    def resolve_available(link):
        if refused_link(link, findings["links"]) is not None:
            return None
        return resolve_link(link, by_path, by_slug)

    by_path = {}
    for p in pages:
        rel = p["rel_path"].replace("\\", "/")
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        by_path[rel] = p["slug"]

    for p in pages:
        for link in p["links"]:
            resolved = resolve_available(link)
            if resolved:
                inbound[resolved].add(p["slug"])

    # sources: の値を、ページを単位に引くためのインデックス。
    src_by_path, src_by_slug = build_page_index(pages)

    def page_type(q: dict):
        return type_value(q["meta"].get("type"))

    def types_of(slug: str) -> list:
        """スラッグのページの type（空のものは除く）。スラッグが重複していれば、それぞれの type を返す。"""
        types = [type_value(g["meta"].get("type")) for g in slug_to_pages[slug]]
        return [t for t in types if t not in (None, "")]

    # 索引の見出しとページの type の食い違い。リンク切れの項目と type のないページは見ない
    # （分割した索引の index.md は indexes/ のファイルを指し、type の欠落は missing_frontmatter で挙がる）。
    for entry in index_entries or []:
        resolved = resolve_available(entry["link"])
        if resolved is None:
            continue
        types = types_of(resolved)
        if not types or entry["expected"] in types:
            continue
        group = slug_to_pages[resolved]
        findings["index_type_mismatch"].append({
            "index": entry["index"],
            "heading": entry["heading"],
            "expected": entry["expected"],
            "link": entry["link"],
            "path": group[0]["rel_path"],
            "type": types[0],
        })

    # 孤立ページ、リンク切れ、サイズ超過、frontmatter、更新の停滞
    for p in pages:
        # 孤立ページ
        if not inbound.get(p["slug"]):
            findings["orphans"].append({"slug": p["slug"], "path": p["rel_path"]})

        # リンク切れ。リンクなので読まなかったページを指すものは、links で挙げるので、ここでは数えない
        for link in p["links"]:
            if refused_link(link, findings["links"]) is None and resolve_available(link) is None:
                findings["broken_links"].append({
                    "from": p["slug"],
                    "from_path": p["rel_path"],
                    "to": link,
                })

        # 全角の「｜」「＃」を使った wikilink。区切りとしては扱わないので、多くはリンク切れにもなる。
        for link in p["links"]:
            if has_fullwidth_link_syntax(link):
                findings["fullwidth_link_syntax"].append({"from_path": p["rel_path"], "link": link})

        # サイズ超過。行数と文字数（空白を除く）の、どちらかが上限を超えたら指摘する（計画の方針 I）。
        # 日本語のページは 1 行が長いので、行数だけでは大きすぎるページを見落とす。
        lines, chars = p["line_count"], p["char_count"]
        size = {"path": p["rel_path"], "lines": lines, "chars": chars}
        if lines > hard_cap or chars > hard_char_cap:
            findings["oversized_hard"].append(size)
        elif lines > soft_cap or chars > soft_char_cap:
            findings["oversized_soft"].append(size)

        # frontmatter
        if p["malformed_fm"]:
            findings["malformed_frontmatter"].append({"path": p["rel_path"]})
        else:
            missing = [field for field in required_fm if field not in p["meta"] or p["meta"].get(field) in ("", None, [])]
            if missing:
                findings["missing_frontmatter"].append({"path": p["rel_path"], "missing": missing})

        # 日本語の frontmatter の書き損じ
        for conflict in p.get("alias_conflicts", []):
            findings["frontmatter_alias_conflicts"].append({
                "path": p["rel_path"],
                "key": conflict["key"],
                "keys": conflict["keys"],
            })
        if p.get("fullwidth_colon_keys"):
            findings["fullwidth_colon_keys"].append({"path": p["rel_path"], "keys": p["fullwidth_colon_keys"]})
        if p.get("ideographic_comma_keys"):
            findings["ideographic_comma_lists"].append({"path": p["rel_path"], "keys": p["ideographic_comma_keys"]})
        # 文字列の値は二重引用符で囲む。日付と数値は囲まない。
        if p.get("unquoted_keys"):
            findings["unquoted_strings"].append({"path": p["rel_path"], "keys": p["unquoted_keys"]})

        # 本文の画像の参照先がない。
        for item in p.get("missing_assets", []):
            findings["missing_assets"].append({"from_path": p["rel_path"], **item})

        # sources: は source ページを指す。ページがないか、source でないページを指すものを挙げる。
        # 分け方はグラフの抽出（sourced_from）と同じ wiki_pages.classify_source_ref に任せる。
        for ref in _ref_values(p["meta"].get("sources")):
            status, _found, wrong_type = classify_source_ref(ref, src_by_path, src_by_slug, page_type,
                                                           link_warnings=findings["links"])
            if status == "missing":
                findings["unresolved_sources"].append({"path": p["rel_path"], "source": ref, "reason": "missing"})
            elif status == "not_source":
                findings["unresolved_sources"].append({"path": p["rel_path"], "source": ref, "reason": "not_source",
                                                       "type": wrong_type})

        # source ページの raw: の原本がない。
        if raw_wiki_root is not None:
            raw_results = resolve_raw(p["meta"], p["path"], raw_wiki_root, wiki_root_given,
                                      raw_roots=raw_roots, recorded_raw_roots=recorded_raw_roots)
            for item in raw_results:
                location = {"path": p["rel_path"], "raw": item.raw}
                if item.state == "missing":
                    notice = {**location, "absolute": bool(DRIVE_PATH_RE.match(item.raw)
                              or item.raw.startswith(("/", "\\", "~")))}
                    if item.mismatches:
                        notice["mismatches"] = item.mismatches
                    findings["missing_raw"].append(notice)
                elif item.state in ("ambiguous", "unknown"):
                    key = "raw_ambiguous" if item.state == "ambiguous" else "raw_unknown"
                    findings[key].append({**location, "candidates": item.candidates, "reason": item.reason})
                elif item.state == "outside" and item.candidates:
                    findings["raw_outside"].append({**location, "candidates": item.candidates, "reason": item.reason,
                                                    "mismatches": item.mismatches})
                if item.version_unverified and item.state == "resolved" and not item.external:
                    findings["raw_version_unverified"].append(location)
                for path in item.parents_unchecked:
                    findings["raw_paths_unchecked"].append({**location, "candidate": path})
                if item.state == "resolved" and item.scope_unverified:
                    findings["raw_paths_unchecked"].append({**location, "candidate": str(item.path),
                        "reason": "raw の根が未指定のため、外の原本かは未確認", "scope_unverified": True})
                for link in item.links:
                    notice = {**link, "scope": "raw", "source": p["rel_path"], "raw": item.raw}
                    findings["links"].append(notice)
                    findings["link_warnings"].append(notice)
                for unchecked in item.unchecked:
                    findings["links_unchecked"].append({**unchecked, "scope": "raw", "source": p["rel_path"], "raw": item.raw})
        else:
            for item in p.get("missing_raw", []):
                findings["missing_raw"].append({"path": p["rel_path"], **item})

        # タグの体系にないタグ。
        if allowed is not None:
            unknown = []
            for tag in normalize_tags(p["meta"].get("tags")):
                if tag and tag not in allowed and tag not in unknown:
                    unknown.append(tag)
            if unknown:
                findings["unknown_tags"].append({"path": p["rel_path"], "tags": unknown})

        # 日付は ISO 形式（YYYY-MM-DD）にする。「2026年11月3日」や「2026/11/03」は、更新の停滞の
        # 判定や --since の絞り込みで読めない。
        for field in DATE_FIELDS:
            value = p["meta"].get(field)
            if value not in (None, "", []) and not is_iso_date(value):
                findings["non_iso_dates"].append({"path": p["rel_path"], "key": field, "value": value})

        # 更新の停滞: ヒューリスティック。90 日間更新されておらず、かつ最近の取り込みで触れられたページ。
        # 近似: 最終更新が 90 日より前で、よくリンクされている（ハブである）ページを指摘する。
        # 指摘の updated は、判定に使った値（配列なら要素をつないだ文字）を出す。書き方の指摘（non_iso_dates）だけが、
        # 書かれた形のまま出す。
        updated_value = scalar_value(p["meta"].get("updated"))
        updated = parse_date(updated_value)
        if updated:
            age_days = (date.today() - updated).days
            if age_days > 90 and len(inbound.get(p["slug"], [])) >= 3:
                findings["stale_pages"].append({
                    "path": p["rel_path"],
                    "updated": updated_value,
                    "age_days": age_days,
                    "inbound_count": len(inbound.get(p["slug"], [])),
                })

    # ページの提案: 多くのページに出てくるのに専用のページがない、大文字で始まる複数語の語句
    if suggest_pages:
        phrase_pages = defaultdict(set)
        for p in pages:
            seen = set()
            # 図のソースと画像の alt は語の候補にしない。
            prose = IMAGE_RE.sub("", strip_fences_and_images(p["body"]))
            for m in CAPITALIZED_PHRASE_RE.finditer(prose):
                phrase = m.group(1).strip()
                seen.add(phrase)
            # 半角のカタカナも全角にそろえてから探す。
            body = nfkc(prose)
            for regex in (KATAKANA_TERM_RE, KANJI_COMPOUND_RE):
                seen.update(m.group() for m in regex.finditer(body))
            for phrase in seen:
                phrase_pages[phrase].add(p["slug"])

        # 絞り込みに使うタイトルの集合
        existing_titles = {nfkc(text_value(p["meta"].get("title"))).lower() for p in pages}
        existing_slugs_normalized = {nfkc(s).lower().replace("-", " ") for s in all_slugs}

        candidates = []
        for phrase, page_set in phrase_pages.items():
            if len(page_set) < suggest_min:
                continue
            if nfkc(phrase).lower() in existing_titles:
                continue
            if nfkc(phrase).lower() in existing_slugs_normalized:
                continue
            # セクションの見出しの断片を除く
            if phrase.split()[0] in {"Section", "Where", "Sources", "Tags", "Type", "Title"}:
                continue
            candidates.append({"phrase": phrase, "page_count": len(page_set), "pages": sorted(page_set)[:5]})
        candidates.sort(key=lambda x: -x["page_count"])
        findings["suggested_pages"] = candidates[:30]

    findings["summary"] = {
        "total_pages": len(pages),
        "orphans": len(findings["orphans"]),
        "broken_links": len(findings["broken_links"]),
        "oversized_hard": len(findings["oversized_hard"]),
        "oversized_soft": len(findings["oversized_soft"]),
        "missing_frontmatter": len(findings["missing_frontmatter"]),
        "malformed_frontmatter": len(findings["malformed_frontmatter"]),
        "duplicate_slugs": len(findings["duplicate_slugs"]),
        "case_only_duplicates": len(findings["case_only_duplicates"]),
        "stale_pages": len(findings["stale_pages"]),
        "read_errors": len(findings["read_errors"]),
        "frontmatter_alias_conflicts": len(findings["frontmatter_alias_conflicts"]),
        "fullwidth_colon_keys": len(findings["fullwidth_colon_keys"]),
        "ideographic_comma_lists": len(findings["ideographic_comma_lists"]),
        "fullwidth_link_syntax": len(findings["fullwidth_link_syntax"]),
        "non_iso_dates": len(findings["non_iso_dates"]),
        "unquoted_strings": len(findings["unquoted_strings"]),
        "missing_assets": len(findings["missing_assets"]),
        "index_type_mismatch": len(findings["index_type_mismatch"]),
        "unresolved_sources": len(findings["unresolved_sources"]),
        "missing_raw": len(findings["missing_raw"]),
        # タグの体系を渡さなかったときは、検査していないことを 0 と区別できるよう None にする。
        "unknown_tags": len(findings["unknown_tags"]) if allowed is not None else None,
        "caption_corrections": len(findings["caption_corrections"]) if wiki_root is not None else None,
        "overview_stale": len(findings["overview_stale"]) if findings["overview_stale"] is not None else None,
        "overview_problems": len(findings["overview_problems"]) if findings["overview_problems"] is not None else None,
        # wiki の根を渡さなかったときは、リンクを調べていない。
        "links": len(findings["links"]) if raw_wiki_root is not None else None,
        "links_wiki": sum(f.get("scope") != "raw" for f in findings["links"]) if wiki_root is not None else None,
        "links_unchecked": len(findings["links_unchecked"]) if raw_wiki_root is not None else None,
        "links_unchecked_wiki": sum(f.get("scope") not in ("raw", "raw_roots") for f in findings["links_unchecked"]) if wiki_root is not None else None,
        "links_unchecked_raw": sum(f.get("scope") in ("raw", "raw_roots") for f in findings["links_unchecked"]) if raw_wiki_root is not None else None,
        "links_raw": sum(f.get("scope") == "raw" for f in findings["links"]) if raw_wiki_root is not None else None,
        "raw_ambiguous": len(findings["raw_ambiguous"]) if raw_wiki_root is not None else None,
        "raw_unknown": len(findings["raw_unknown"]) if raw_wiki_root is not None else None,
        "raw_version_unverified": len(findings["raw_version_unverified"]) if raw_wiki_root is not None else None,
        "raw_paths_unchecked": len(findings["raw_paths_unchecked"]) if raw_wiki_root is not None else None,
        "raw_outside": len(findings["raw_outside"]) if raw_wiki_root is not None else None,
        "suggested_pages": len(findings["suggested_pages"]),
    }
    return findings


LINK_KIND_LABELS = {"symlink": "シンボリックリンク", "junction": "ジャンクション", "reparse": "名前の代理のリパースポイント",
                    "hardlink": "ハードリンク"}


def size_text(f: dict) -> str:
    return f"  - {f['path']}  （{f['lines']} 行、{f['chars']:,} 文字）"


def render_text(findings: dict) -> str:
    out = []
    s = findings["summary"]
    out.append("=" * 60)
    out.append("wiki の lint の報告")
    out.append("=" * 60)
    out.append(f"調べたページ数: {s['total_pages']}")
    out.append("")

    sections = [
        ("orphans", "孤立ページ（被リンクがない）", lambda f: f"  - {f['slug']}  （{f['path']}）"),
        ("broken_links", "リンク切れの wikilink", lambda f: f"  - [[{f['to']}]]  （{f['from_path']} から）"),
        ("oversized_hard", "大きすぎるページ（ハード上限を超えている。分割する）", size_text),
        ("oversized_soft", "大きいページ（ソフト上限を超えている。分割を検討する）", size_text),
        ("missing_frontmatter", "frontmatter に足りないフィールドがある", lambda f: f"  - {f['path']}  不足: {', '.join(f['missing'])}"),
        ("malformed_frontmatter", "frontmatter を読めない", lambda f: f"  - {f['path']}"),
        ("duplicate_slugs", "重複したスラッグ", lambda f: f"  - {f['slug']}: {', '.join(f['paths'])}"
         + ("  （Unicode の正規化の違いだけで名前が異なる）" if f.get("normalization_collision") else "")),
        ("case_only_duplicates", "大文字と小文字だけが違うページかディレクトリ（閲覧の書き出しで同じ場所を取る）",
         lambda f: f"  - {'、'.join(f['paths'])}" + ("  （ディレクトリ）" if f["kind"] == "dir" else "")),
        ("stale_pages", "更新の止まったページ（よくリンクされているのに 90 日以上更新されていない）",
         lambda f: f"  - {f['path']}  （更新 {f['updated']}、{f['age_days']} 日前、被リンク {f['inbound_count']}）"),
        ("read_errors", "読み込めないファイル", lambda f: f"  - {f['path']}: {f['error']}"),
        ("frontmatter_alias_conflicts", "英語のキーと日本語の別名を両方書いている", lambda f: f"  - {f['path']}  {f['key']}: {', '.join(f['keys'])}"),
        ("fullwidth_colon_keys", "frontmatter のキーのあとが全角の「：」になっている", lambda f: f"  - {f['path']}  キー: {', '.join(f['keys'])}"),
        ("ideographic_comma_lists", "リストの値を「、」で区切っている", lambda f: f"  - {f['path']}  キー: {', '.join(f['keys'])}"),
        ("fullwidth_link_syntax", "wikilink に全角の「｜」か「＃」がある", lambda f: f"  - [[{f['link']}]]  （{f['from_path']}）"),
        ("non_iso_dates", "日付が YYYY-MM-DD の形になっていない", lambda f: f"  - {f['path']}  {f['key']}: {f['value']}"),
        ("unquoted_strings", "文字列の値が二重引用符で囲まれていない", lambda f: f"  - {f['path']}  キー: {', '.join(f['keys'])}"),
        ("missing_assets", "画像の参照先のファイルがない", lambda f: f"  - {f['target']}  （{f['from_path']}）"
         + ("  wiki の外を指している" if f.get("outside") else "")
         + ("  ファイルのパスにできない" if f.get("invalid") else "")
         + (f"  → {f['suggest']} と書けば解決する" if f.get("suggest") else "")),
        ("index_type_mismatch", "索引の見出しと、そこに並ぶページの type が食い違っている",
         lambda f: f"  - [[{f['link']}]]  （{f['index']}"
         + (f" の「{f['heading']}」" if f.get("heading") else "")
         + f"。{f['expected']} の欄にあるが、{f['path']} の type は {f['type']}）"),
        ("unresolved_sources", "sources: が source ページを指していない",
         lambda f: f"  - {f['path']}  {f['source']}: "
         + ("ページがない" if f["reason"] == "missing" else f"type が {f.get('type')} のページを指している")),
        ("missing_raw", "source ページの raw: に一致する原本がない（版・身元の不一致を含む）",
         lambda f: f"  - {f['path']}  raw: {f['raw']}" +
                   ("  不一致: " + "、".join(x["path"] + "（" + x["reason"] + "）" for x in f["mismatches"]) if f.get("mismatches") else "")),
        ("unknown_tags", "タグの体系にないタグ", lambda f: f"  - {f['path']}  {', '.join(f['tags'])}"),
        ("caption_corrections", "YouTube の字幕の訂正と留保の印、定義、confidence の食い違い",
         lambda f: f"  - {f['path']}:{f['line']}  {f['message']}"),
        ("overview_stale", "元のページが変わった概要（/wiki:overview <ページ> --refresh で作り直す）",
         lambda f: f"  - {f['page']}  （{f['sidecar']}）"),
        ("overview_problems", "概要の破損、元のページの不在、読み取りの問題",
         lambda f: f"  - {f['path']}  {f['message']}"),
        ("links", "記事か記事までの道のリンク（読まずに飛ばした。プラグインは直さない。直し方は lint の手順の文書にある）",
         lambda f: f"  - {f['path']}  （{LINK_KIND_LABELS.get(f['kind'], f['kind'])}）"),
        ("links_unchecked", "リンクかを調べられなかった項目や確認できなかった raw の根の記録",
         lambda f: f"  - {f['path']}" + (f"  {f['reason']}" if f.get("reason") else "")),
        ("raw_ambiguous", "raw: の候補が複数ある（自動で選ばず、書き換えない）",
         lambda f: f"  - {f['path']}  raw: {f['raw']}  候補: {', '.join(f['candidates'])}"),
        ("raw_unknown", "raw: を確かめられない", lambda f: f"  - {f['path']}  raw: {f['raw']}  {f['reason']}"),
        ("raw_outside", "raw: が記事の対象外を指す（読まずに飛ばした）",
         lambda f: f"  - {f['path']}  raw: {f['raw']}  {f['reason']}" +
                   ("  不一致: " + "、".join(x["path"] + "（" + x["reason"] + "）" for x in f["mismatches"]) if f.get("mismatches") else "")),
        ("raw_version_unverified", "raw の版を確かめていない（raw_sha256 がないか値の数・形が合わない）",
         lambda f: f"  - {f['path']}  raw: {f['raw']}"),
        ("raw_paths_unchecked", "raw の範囲か途中の道を調べていない（--raw-root で根を指定する）",
         lambda f: f"  - {f['path']}  raw: {f['raw']}  候補: {f['candidate']}" + ("  " + f["reason"] if f.get("reason") else "")),
    ]

    for key, label, formatter in sections:
        items = findings[key]
        if not items:
            continue
        out.append(f"{label}（{len(items)} 件）:")
        for item in items[:50]:
            out.append(formatter(item))
        if len(items) > 50:
            out.append(f"  ……ほか {len(items) - 50} 件")
        out.append("")

    if findings["suggested_pages"]:
        out.append(f"ページの候補（{len(findings['suggested_pages'])} 件）:")
        out.append("  多くのページに出てくるのに、専用のページがない語:")
        for item in findings["suggested_pages"]:
            out.append(f"  - 「{item['phrase']}」  （{item['page_count']} ページ）")
        out.append("")

    if s.get("unknown_tags") is None:
        out.append("タグの検査はしていない（--allowed-tags にタグの体系を渡すと、体系にないタグを報告する）。")
        out.append("")

    if findings["overview_problems"] is None:
        out.append("概要の検査は完了していない（wiki のルートが渡されていないか、概要の一覧を作れない）。")
        out.append("")

    if s.get("links_raw") is None:
        out.append("wiki の根を渡していないため、raw の側のリンクを調べていない。")
        out.append("")
    if s.get("links_wiki") is None:
        out.append("wiki の側のリンクは調べていない。links は、検査できた範囲の知らせの件数である。")
        out.append("")

    # None は検査していない項目なので、問題がないものとして扱う。
    if all(not v for k, v in s.items() if k != "total_pages"):
        out.append("問題は見つからなかった。")

    return "\n".join(out)


def main():
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    parser.add_argument("wiki", nargs="?", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    parser.add_argument("--soft-cap", type=int, default=100, help="ページサイズのソフト上限の行数（既定: 100）")
    parser.add_argument("--hard-cap", type=int, default=200, help="ページサイズのハード上限の行数（既定: 200）")
    parser.add_argument("--soft-char-cap", type=int, default=10000, help="ページサイズのソフト上限の文字数。空白を除いて数える（既定: 10000）")
    parser.add_argument("--hard-char-cap", type=int, default=25000, help="ページサイズのハード上限の文字数。空白を除いて数える（既定: 25000）")
    parser.add_argument("--required-fm", default=",".join(DEFAULT_REQUIRED_FM), help="必須の frontmatter のフィールド。カンマで区切る（既定: type,title,tags,created,updated）")
    parser.add_argument("--suggest-pages", action="store_true", help="多くのページに出てくるのに専用のページがない語を挙げる")
    parser.add_argument("--suggest-min", type=int, default=5, help="--suggest-pages で挙げる最小の出現ページ数（既定: 5）")
    parser.add_argument("--allowed-tags", default=None,
                        help="タグの体系（SCHEMA.md の「タグの体系」に並ぶタグ）。カンマで区切る。渡すと、ここにないタグを報告する。"
                             "渡さないか空にすると、タグの検査はしない")
    parser.add_argument("--json", action="store_true", help="テキストの代わりに JSON を出力する。キーは英語のまま")
    parser.add_argument("--raw-root", type=Path, help="利用者が決めた raw の根。そこから原本までの道も検査する")
    parser.add_argument("--release-id", help="検査する公開版")
    args = parser.parse_args()
    if args.wiki is None:
        args.wiki = wiki_workspace.default_wiki_or_exit()
    # SCHEMA.md のないディレクトリ（ほかの用途の wiki/ など）は、初期化された wiki とみなさない（ADR-0019）。
    wiki_initialized.require_or_exit(args.wiki)
    from wiki_apply import resolve_publication

    try:
        release = resolve_publication(args.wiki, args.release_id)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if release:
        if args.raw_root is not None:
            parser.error("公開版の lint にライブの raw は指定できません")
        args.wiki = release.root

    pages = collect_pages(args.wiki)
    required_fm = [f.strip() for f in args.required_fm.split(",") if f.strip()]
    allowed_tags = None
    if args.allowed_tags is not None:
        allowed_tags = [t.strip() for t in args.allowed_tags.split(",") if t.strip()] or None
    findings = lint(pages, args.soft_cap, args.hard_cap, required_fm, args.suggest_pages, args.suggest_min,
                    args.soft_char_cap, args.hard_char_cap, index_entries=collect_index_entries(args.wiki),
                    allowed_tags=allowed_tags, wiki_root=args.wiki, raw_root=args.raw_root, wiki_root_given=args.wiki)

    if release:
        resolve_publication(release.wiki, release.release_id)
        findings["release_id"] = release.release_id
    if args.json:
        print(json.dumps(findings, indent=2, default=str, ensure_ascii=False))
    else:
        print(render_text(findings))


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
