#!/usr/bin/env python3
"""
wiki_stats.py — wiki の規模、構成、リンク密度の簡単な集計。

インデックスを分割する時期や、ページを分割する時期を決めるのに役立つ。

使い方:
    python wiki_stats.py [<wiki-dir>] [--json]

オプション:
    --json   テキストの代わりに JSON を出力する。キーは英語のまま

例:
    python wiki_stats.py wiki/
    python wiki_stats.py wiki/ --json
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import (  # noqa: E402
    canonical_meta, char_count, ljust_display, localize_argparse, nfc, parse_frontmatter_text, posix_rel,
    rjust_display, strip_fences_and_images,
)
import wiki_initialized  # noqa: E402
import wiki_workspace  # noqa: E402
# ページの集合とリンクの抜き出しは、lint と同じ規則（wiki_pages）を使う。
from wiki_pages import (  # noqa: E402,F401
    SKIP_TOP_LEVEL_DIRS, SKIP_TOP_LEVEL_FILES, WIKILINK_RE, extract_wikilinks, is_page_path, read_os, walk_markdown,
)


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def link_stem(link: str) -> str:
    """被リンクの多さを数えるために、wikilink をそれが指すページのステムに縮める。

    パスのない形（[[kalman-filter]]）、パスを付けた形（[[entities/kalman-filter]]）、
    アンカー付きの形、.md 付きの形は、どれも同じページを指す。リンクの生のテキストで数えると、
    1ページの被リンク数がそれらの形に分かれ、ハブを少なく見積もってしまう。

    注意: wiki_lint や wiki_search と違い、この関数はリンク先が解決されるかを検証しない。
    [[raw/foo]] のようなツリーの外のパスへのリンクも、ステム "foo" として数える。
    ここで数えるのは被リンクの多さであり、正しさを判定するゲートではないので、これでよい。
    リンク切れかどうかの判定に、この関数を再利用してはならない。
    """
    target = link.split("|", 1)[0]      # 表示用の別名があれば取り除く
    target = target.split("#", 1)[0]    # 見出しのアンカーを取り除く
    target = target.replace("\\", "/").strip().strip("/")
    target = target.rsplit("/", 1)[-1]  # ディレクトリの接頭辞を取り除く
    if target.lower().endswith(".md"):
        target = target[:-3]
    # NFD で書かれたリンクも、NFC のファイル名と同じページとして数える。
    return nfc(target.strip())


def parse_type(text: str) -> str | None:
    """frontmatter の type を返す。「種類: 概念」のような日本語の別名も英語に直して返す。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    meta, _conflicts = canonical_meta(parse_frontmatter_text(m.group(1)))
    value = meta.get("type")
    return value if isinstance(value, str) else None


# ページの大きさの目安（wiki_lint の既定値と同じ）。行数と、空白を除いた文字数の両方で見る。
SOFT_LINES, HARD_LINES = 100, 200
SOFT_CHARS, HARD_CHARS = 10000, 25000


# 大きいページとハブを、いくつまで挙げるか。
TOP_N = 10


def size_level(lines: int, chars: int) -> "str | None":
    if lines > HARD_LINES or chars > HARD_CHARS:
        return "hard"
    if lines > SOFT_LINES or chars > SOFT_CHARS:
        return "soft"
    return None


def scale_advice(total_pages: int, index_lines: int, has_indexes: bool) -> list[dict]:
    """規模のしきい値についての推奨。code は機械が読む識別子で、英語のまま残す。"""
    advice = []
    if total_pages < 50:
        advice.append({"code": "flat", "message": "最初のしきい値より小さい。平らな構成のままでよい。"})
    elif total_pages < 150 and index_lines < 300:
        advice.append({"code": "single_index", "message": "分割のしきい値より小さい。index.md は 1 つのままでよい。"})
    elif (total_pages >= 150 or index_lines >= 300) and not has_indexes:
        advice.append({"code": "split_index",
                       "message": "分割のしきい値に達した。index.md を wiki/indexes/<type>.md に分けることを検討する。",
                       "reference": "references/scaling-playbook.md"})
    elif total_pages >= 300:
        advice.append({"code": "use_search",
                       "message": "300 ページを超えた。scripts/wiki_search.py をふだんの手段として使う。"})
    if total_pages >= 500:
        advice.append({"code": "lint_weekly",
                       "message": "500 ページを超えた。lint を毎週、または数回の取り込みごとに実行する。"})
    return advice


def by_count(counter: Counter) -> list:
    """件数の多い順、同じなら名前のコードポイントの順。Counter.most_common() は、同点を入れた順（ファイルを列挙した順）に
    並べるので、同点のある wiki では環境によって並びが変わる。"""
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def compute_stats(wiki: Path) -> dict:
    """wiki の規模、構成、リンク密度を集計して返す。キーは英語のまま（--json と MCP の stats が使う）。"""
    wiki = Path(wiki)
    total_pages = 0
    total_lines = 0
    total_words = 0
    total_chars = 0
    total_links = 0
    pages_by_type = Counter()
    pages_by_dir = Counter()
    largest = []
    most_linked_in = Counter()
    index_lines = 0

    # ページと同じたどり方（wiki_pages.walk_markdown）で、最上位の除くディレクトリには降りない。読むのは、最上位の
    # index.md（行数を数える）と、ページ（lint と同じ is_page_path）だけで、読むかどうかは読む前に決める。
    for found in walk_markdown(wiki, skip_top=True).files:
        top_index = found.rel == "index.md"
        if not top_index and not is_page_path(found.rel):
            continue
        rel = PurePosixPath(found.rel)
        try:
            # 先頭に BOM があっても読めるようにする。
            text = (wiki / found.rel).read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue

        if top_index:
            index_lines = text.count("\n") + 1
            continue

        total_pages += 1
        # 大きさは lint と同じく、コードフェンス（mermaid を含む）の中身と画像だけの行を除いて数える。
        sized = strip_fences_and_images(text)
        line_count = sized.count("\n") + 1
        # 語数は空白で区切って数えるので、日本語のページではほぼ意味をなさない。文字数も数える。
        word_count = len(sized.split())
        chars = char_count(sized)
        total_lines += line_count
        total_words += word_count
        total_chars += chars
        # wikilink を数える前に frontmatter を取り除く。frontmatter ではパスのないスラッグを使う。
        body = FRONTMATTER_RE.sub("", text, count=1) if text.startswith("---") else text
        # コードフェンスとインラインのコードの中の [[...]] は数えない（lint と同じ）。
        links = extract_wikilinks(body)
        total_links += len(links)
        for link in links:
            most_linked_in[link_stem(link)] += 1
        page_type = parse_type(text) or "(なし)"
        pages_by_type[page_type] += 1
        if len(rel.parts) > 1:
            pages_by_dir[nfc(rel.parts[0])] += 1
        else:
            pages_by_dir["(ルート)"] += 1
        largest.append((chars, line_count, posix_rel(rel.as_posix())))

    # 日本語のページは 1 行が長いので、大きい順は文字数で並べる。
    largest.sort(reverse=True)

    average = None
    if total_pages:
        average = {"lines": total_lines // total_pages, "chars": total_chars // total_pages,
                   "words": total_words // total_pages, "links": round(total_links / total_pages, 1)}
    # indexes があるか（推奨の文に使う）は、名前の項目そのものに os.lstat() を 1 回かけて決める。リンクの先もその下も
    # 調べない。ないなら「ない」、権限で調べられなければ「ある」とみなし（分けることを勧めない）、ほかは送出する。
    has_indexes = read_os(os.lstat, os.path.join(str(wiki), "indexes"))[0] != "absent"
    return {
        "wiki": str(wiki),
        "pages": total_pages,
        "lines": total_lines,
        "chars": total_chars,
        "words": total_words,
        "links": total_links,
        "average": average,
        "index_lines": index_lines,
        "pages_by_type": dict(by_count(pages_by_type)),
        "pages_by_dir": dict(by_count(pages_by_dir)),
        "largest": [{"path": path, "chars": chars, "lines": lines, "size": size_level(lines, chars)}
                    for chars, lines, path in largest[:TOP_N]],
        "most_linked": [{"slug": slug, "count": count} for slug, count in by_count(most_linked_in)[:TOP_N]],
        "advice": scale_advice(total_pages, index_lines, has_indexes),
    }


SIZE_NOTES = {"hard": "  ← ハード上限を超えている", "soft": "  ← ソフト上限を超えている", None: ""}


def render_text(stats: dict) -> str:
    out = []
    out.append("=" * 60)
    out.append(f"wiki の統計: {stats['wiki']}")
    out.append("=" * 60)
    out.append(f"ページ数:       {stats['pages']}")
    out.append(f"総行数:         {stats['lines']:,}")
    out.append(f"総文字数:       {stats['chars']:,}（空白を除く）")
    out.append(f"総語数:         {stats['words']:,}（空白で区切った数）")
    out.append(f"総リンク数:     {stats['links']:,}")
    average = stats["average"]
    if average:
        out.append(f"1 ページの平均: {average['lines']} 行、{average['chars']:,} 文字、{average['words']} 語")
        out.append(f"リンクの密度:   1 ページあたり {stats['links'] / stats['pages']:.1f} 本")
    index_lines = stats["index_lines"]
    out.append(f"index.md:       {index_lines} 行" + ("  ← 分割を勧める（300 行超）" if index_lines > 300 else ""))
    out.append("")

    out.append("type ごとのページ数:")
    for t, n in stats["pages_by_type"].items():
        out.append(f"  {ljust_display(t, 15)} {n}")
    out.append("")

    out.append("ディレクトリごとのページ数:")
    for d, n in stats["pages_by_dir"].items():
        out.append(f"  {ljust_display(d, 15)} {n}")
    out.append("")

    if stats["largest"]:
        out.append("大きいページ（文字数の多い順）:")
        out.append(f"  {rjust_display('文字数', 7)}  {rjust_display('行数', 5)}  パス")
        for page in stats["largest"]:
            out.append(f"  {page['chars']:7,d}  {page['lines']:5d}  {page['path']}{SIZE_NOTES[page['size']]}")
        out.append("")

    if stats["most_linked"]:
        out.append("被リンクの多いページ（ハブ）:")
        for hub in stats["most_linked"]:
            out.append(f"  {hub['count']:4d}  [[{hub['slug']}]]")
        out.append("")

    # スケーリングについての推奨
    out.append("規模のしきい値:")
    for item in stats["advice"]:
        out.append(f"  → {item['message']}")
        if item.get("reference"):
            out.append(f"    {item['reference']} を参照。")
    return "\n".join(out)


def main():
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    parser.add_argument("wiki", nargs="?", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    parser.add_argument("--json", action="store_true", help="テキストの代わりに JSON を出力する。キーは英語のまま")
    args = parser.parse_args()
    if args.wiki is None:
        args.wiki = wiki_workspace.default_wiki_or_exit()
    # SCHEMA.md のないディレクトリは、初期化された wiki とみなさない（ADR-0019）。
    wiki_initialized.require_or_exit(args.wiki)

    stats = compute_stats(args.wiki)
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print(render_text(stats))


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
