#!/usr/bin/env python3
"""
wiki_lint.py — LLM Wiki の構造の健全性をチェックする。

孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の問題、更新の止まったページ、
重複したスラッグと、（--suggest-pages を付けた場合は）多くのページに出てくるのに専用のページがない語を報告する。
日本語の wiki で起きやすい書き損じとして、英語のキーと日本語の別名の重複、全角の「：」を使った
frontmatter のキー、「、」で区切ったリストの値、全角の「｜」「＃」を使った wikilink も報告する。
frontmatter の文字列の値（配列の要素を含む）が二重引用符で囲まれていないページも報告する。

ファイル名、パス、スラッグ、wikilink の行き先は NFC にそろえてから比べる。frontmatter の
日本語のキー（タイトル、種類など）と type・kind の日本語の値は、英語に直してから検査する。

保守的な設計で、検出結果を報告するだけで編集はしない。

使い方:
    python wiki_lint.py [<wiki-dir>] [options]

オプション:
    --soft-cap N        ページサイズのソフト上限の行数（既定: 100）
    --hard-cap N        ページサイズのハード上限の行数（既定: 200）
    --soft-char-cap N   ページサイズのソフト上限の文字数。空白を除いて数える（既定: 10000）
    --hard-char-cap N   ページサイズのハード上限の文字数。空白を除いて数える（既定: 25000）
    --required-fm a,b   必須の frontmatter のフィールド（既定: type,title,tags,created,updated）
    --suggest-pages     多くのページに出てくるのに専用のページがない語を挙げる
    --suggest-min N     --suggest-pages で挙げる最小の出現ページ数（既定: 5）
    --json              テキストの代わりに JSON を出力する

例:
    python wiki_lint.py wiki/
    python wiki_lint.py wiki/ --suggest-pages
    python wiki_lint.py wiki/ --json > lint.json
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

# 同じディレクトリの wiki_text を import する。スクリプトとして実行したときも、
# テストから import したときも見つかるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import (  # noqa: E402
    canonical_meta,
    char_count,
    fullwidth_colon_keys,
    has_fullwidth_link_syntax,
    ideographic_comma_values,
    is_iso_date,
    localize_argparse,
    nfc,
    nfkc,
    parse_frontmatter_text,
    posix_rel,
    unquoted_string_keys,
)


WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# 日本語のページの候補。カタカナ語は 3 文字以上、漢字の複合語は 3 文字以上の漢字の並びとする。
# 2 文字の漢字の並びは「方法」「場合」のような一般語が多く、候補として役に立たない。
KATAKANA_TERM_RE = re.compile(r"[\u30a1-\u30fa\u30fc]{3,}")
KANJI_COMPOUND_RE = re.compile(r"[\u3005\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{3,}")
CAPITALIZED_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,3})\b")


def clean_link_target(link: str) -> str:
    """wikilink から別名、アンカー、.md を取り除き、パスだけにする。

    ファイル名と同じく NFC にそろえる。全角の「｜」「＃」は区切りとして扱わない（Obsidian と同じ）。
    """
    target = nfc(link).split("|", 1)[0]      # 表示用の別名があれば取り除く
    target = target.split("#", 1)[0]    # 見出しのアンカーを取り除く
    target = target.replace("\\", "/").strip().strip("/")
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target.strip()


def resolve_link(link: str, by_path: dict, by_slug) -> "str | None":
    """wikilink を、それが指すページのスラッグに解決する。リンク切れなら None を返す。

    wikilink には、パスのない形（[[kalman-filter]]）とパスを付けた形（[[entities/kalman-filter]]）があり、
    見出しのアンカーや明示的な .md が付くこともある。ページはファイル名のステムで識別されるので、
    リンクの生のテキストで比べると、パスを付けた形をすべて見落とす。その結果リンク切れと報告され、
    被リンクにも数えられないので、よく参照されているページまで孤立ページと誤って報告される。

    ディレクトリの接頭辞は飾りではなく「制約」として扱う。パスを付けたリンクは、そのパスが
    ちょうど存在する場合にだけ解決する。ステムだけの照合に切り替えると、[[raw/foo]] が
    sources/foo.md に黙って結び付いてしまう。ソースページを raw ファイルにちなんで名付けている
    wiki ならどこでも起きる衝突で、ツリーの外を指す本当のリンク切れを誤って合格にしてしまう。
    解決できないと答えるのは安全だが、誤って解決するのは安全ではない。

    照合は、どちらの形でも大文字と小文字を区別する。パスだけ（パスのないスラッグは除く）大文字と
    小文字を同一視すると非対称になる。誤り方が2通りあるとすれば、誤った「リンク切れ」の報告は
    目に見えて害がないが、誤った解決は気づかれない。
    """
    target = clean_link_target(link)
    if not target:
        return None
    if "/" in target:
        return by_path.get(target)
    return target if target in by_slug else None


SKIP_TOP_LEVEL_FILES = {"SCHEMA.md", "index.md", "log.md", "README.md"}
SKIP_TOP_LEVEL_DIRS = {"indexes", "graph", "raw", ".evolution"}


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


def collect_pages(wiki_root: Path) -> list[dict]:
    pages = []
    for md_path in wiki_root.rglob("*.md"):
        rel = md_path.relative_to(wiki_root)
        if rel.parts[0] in SKIP_TOP_LEVEL_FILES or rel.parts[0] in SKIP_TOP_LEVEL_DIRS:
            continue
        if rel.name.startswith("."):
            continue
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
        line_count = text.count("\n") + 1
        links = [nfc(m.group(1).strip()) for m in WIKILINK_RE.finditer(body)]
        pages.append({
            "path": str(md_path),
            "rel_path": rel_path,
            "disk_rel_path": disk_rel_path,
            "slug": slug,
            "meta": meta,
            "body": body,
            "line_count": line_count,
            "char_count": char_count(text),
            "links": links,
            "malformed_fm": malformed,
            "alias_conflicts": alias_conflicts,
            "fullwidth_colon_keys": fullwidth_colon_keys(fm_text) if fm_text else [],
            "ideographic_comma_keys": ideographic_comma_values(raw_meta),
            "unquoted_keys": unquoted_string_keys(fm_text) if fm_text else [],
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


def lint(pages: list[dict], soft_cap: int, hard_cap: int, required_fm: list[str], suggest_pages: bool, suggest_min: int,
         soft_char_cap: int = 10000, hard_char_cap: int = 25000) -> dict:
    findings = {
        "orphans": [],
        "broken_links": [],
        "oversized_hard": [],
        "oversized_soft": [],
        "missing_frontmatter": [],
        "malformed_frontmatter": [],
        "duplicate_slugs": [],
        "stale_pages": [],
        "read_errors": [],
        "frontmatter_alias_conflicts": [],
        "fullwidth_colon_keys": [],
        "ideographic_comma_lists": [],
        "fullwidth_link_syntax": [],
        "non_iso_dates": [],
        "unquoted_strings": [],
        "suggested_pages": [],
        "summary": {},
    }

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

    # 被リンクの対応表
    inbound = defaultdict(set)
    all_slugs = set(slug_to_pages.keys())
    # 解決用のインデックス。パスを付けたリンクには wiki からの完全な相対パス（.md なし）を、
    # パスのないリンクにはステムだけを使う。
    by_slug = all_slugs
    by_path = {}
    for p in pages:
        rel = p["rel_path"].replace("\\", "/")
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        by_path[rel] = p["slug"]

    for p in pages:
        for link in p["links"]:
            resolved = resolve_link(link, by_path, by_slug)
            if resolved:
                inbound[resolved].add(p["slug"])

    # 孤立ページ、リンク切れ、サイズ超過、frontmatter、更新の停滞
    for p in pages:
        # 孤立ページ
        if not inbound.get(p["slug"]):
            findings["orphans"].append({"slug": p["slug"], "path": p["rel_path"]})

        # リンク切れ
        for link in p["links"]:
            if resolve_link(link, by_path, by_slug) is None:
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

        # 日付は ISO 形式（YYYY-MM-DD）にする。「2026年11月3日」や「2026/11/03」は、更新の停滞の
        # 判定や --since の絞り込みで読めない。
        for field in DATE_FIELDS:
            value = p["meta"].get(field)
            if value not in (None, "", []) and not is_iso_date(value):
                findings["non_iso_dates"].append({"path": p["rel_path"], "key": field, "value": value})

        # 更新の停滞: ヒューリスティック。90 日間更新されておらず、かつ最近の取り込みで触れられたページ。
        # 近似: 最終更新が 90 日より前で、よくリンクされている（ハブである）ページを指摘する。
        updated = parse_date(p["meta"].get("updated"))
        if updated:
            age_days = (date.today() - updated).days
            if age_days > 90 and len(inbound.get(p["slug"], [])) >= 3:
                findings["stale_pages"].append({
                    "path": p["rel_path"],
                    "updated": p["meta"].get("updated"),
                    "age_days": age_days,
                    "inbound_count": len(inbound.get(p["slug"], [])),
                })

    # ページの提案: 多くのページに出てくるのに専用のページがない、大文字で始まる複数語の語句
    if suggest_pages:
        phrase_pages = defaultdict(set)
        for p in pages:
            seen = set()
            for m in CAPITALIZED_PHRASE_RE.finditer(p["body"]):
                phrase = m.group(1).strip()
                seen.add(phrase)
            # 半角のカタカナも全角にそろえてから探す。
            body = nfkc(p["body"])
            for regex in (KATAKANA_TERM_RE, KANJI_COMPOUND_RE):
                seen.update(m.group() for m in regex.finditer(body))
            for phrase in seen:
                phrase_pages[phrase].add(p["slug"])

        # 絞り込みに使うタイトルの集合
        existing_titles = {nfkc(p["meta"].get("title", "")).lower() for p in pages}
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
        "stale_pages": len(findings["stale_pages"]),
        "read_errors": len(findings["read_errors"]),
        "frontmatter_alias_conflicts": len(findings["frontmatter_alias_conflicts"]),
        "fullwidth_colon_keys": len(findings["fullwidth_colon_keys"]),
        "ideographic_comma_lists": len(findings["ideographic_comma_lists"]),
        "fullwidth_link_syntax": len(findings["fullwidth_link_syntax"]),
        "non_iso_dates": len(findings["non_iso_dates"]),
        "unquoted_strings": len(findings["unquoted_strings"]),
        "suggested_pages": len(findings["suggested_pages"]),
    }
    return findings


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
        ("stale_pages", "更新の止まったページ（よくリンクされているのに 90 日以上更新されていない）",
         lambda f: f"  - {f['path']}  （更新 {f['updated']}、{f['age_days']} 日前、被リンク {f['inbound_count']}）"),
        ("read_errors", "読み込めないファイル", lambda f: f"  - {f['path']}: {f['error']}"),
        ("frontmatter_alias_conflicts", "英語のキーと日本語の別名を両方書いている", lambda f: f"  - {f['path']}  {f['key']}: {', '.join(f['keys'])}"),
        ("fullwidth_colon_keys", "frontmatter のキーのあとが全角の「：」になっている", lambda f: f"  - {f['path']}  キー: {', '.join(f['keys'])}"),
        ("ideographic_comma_lists", "リストの値を「、」で区切っている", lambda f: f"  - {f['path']}  キー: {', '.join(f['keys'])}"),
        ("fullwidth_link_syntax", "wikilink に全角の「｜」か「＃」がある", lambda f: f"  - [[{f['link']}]]  （{f['from_path']}）"),
        ("non_iso_dates", "日付が YYYY-MM-DD の形になっていない", lambda f: f"  - {f['path']}  {f['key']}: {f['value']}"),
        ("unquoted_strings", "文字列の値が二重引用符で囲まれていない", lambda f: f"  - {f['path']}  キー: {', '.join(f['keys'])}"),
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

    if all(v == 0 for k, v in s.items() if k != "total_pages"):
        out.append("問題は見つからなかった。")

    return "\n".join(out)


def main():
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    parser.add_argument("wiki", nargs="?", type=Path, default=Path("wiki"), help="wiki のディレクトリ（既定: ./wiki）")
    parser.add_argument("--soft-cap", type=int, default=100, help="ページサイズのソフト上限の行数（既定: 100）")
    parser.add_argument("--hard-cap", type=int, default=200, help="ページサイズのハード上限の行数（既定: 200）")
    parser.add_argument("--soft-char-cap", type=int, default=10000, help="ページサイズのソフト上限の文字数。空白を除いて数える（既定: 10000）")
    parser.add_argument("--hard-char-cap", type=int, default=25000, help="ページサイズのハード上限の文字数。空白を除いて数える（既定: 25000）")
    parser.add_argument("--required-fm", default="type,title,tags,created,updated", help="必須の frontmatter のフィールド。カンマで区切る（既定: type,title,tags,created,updated）")
    parser.add_argument("--suggest-pages", action="store_true", help="多くのページに出てくるのに専用のページがない語を挙げる")
    parser.add_argument("--suggest-min", type=int, default=5, help="--suggest-pages で挙げる最小の出現ページ数（既定: 5）")
    parser.add_argument("--json", action="store_true", help="テキストの代わりに JSON を出力する。キーは英語のまま")
    args = parser.parse_args()

    if not args.wiki.exists():
        print(f"wiki のディレクトリが見つからない: {args.wiki}", file=sys.stderr)
        sys.exit(1)

    pages = collect_pages(args.wiki)
    required_fm = [f.strip() for f in args.required_fm.split(",") if f.strip()]
    findings = lint(pages, args.soft_cap, args.hard_cap, required_fm, args.suggest_pages, args.suggest_min,
                    args.soft_char_cap, args.hard_char_cap)

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
