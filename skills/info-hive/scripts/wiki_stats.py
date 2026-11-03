#!/usr/bin/env python3
"""
wiki_stats.py — wiki の規模、構成、リンク密度の簡単な集計。

インデックスを分割する時期や、ページを分割する時期を決めるのに役立つ。

使い方:
    python wiki_stats.py [<wiki-dir>]

例:
    python wiki_stats.py wiki/
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import canonical_meta, char_count, ljust_display, nfc, posix_rel, rjust_display  # noqa: E402


WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
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


SKIP_TOP_LEVEL_FILES = {"SCHEMA.md", "log.md", "README.md"}
SKIP_TOP_LEVEL_DIRS = {"indexes", "graph", "raw", ".evolution"}


def parse_type(text: str) -> str | None:
    """frontmatter の type を返す。「種類: 概念」のような日本語の別名も英語に直して返す。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm = m.group(1)
    meta: dict[str, str] = {}
    for line in fm.split("\n"):
        kv = re.match(r"^(\w+):\s*(.*)$", line)
        if kv:
            # 同じキーが何度もあれば、これまでどおり最初の行を使う。
            meta.setdefault(kv.group(1), kv.group(2).strip().strip('"').strip("'"))
    meta, _conflicts = canonical_meta(meta)
    return meta.get("type")


# ページの大きさの目安（wiki_lint の既定値と同じ）。行数と、空白を除いた文字数の両方で見る。
SOFT_LINES, HARD_LINES = 400, 800
SOFT_CHARS, HARD_CHARS = 4000, 8000


def size_warning(lines: int, chars: int) -> str:
    if lines > HARD_LINES or chars > HARD_CHARS:
        return "  ← ハード上限を超えている"
    if lines > SOFT_LINES or chars > SOFT_CHARS:
        return "  ← ソフト上限を超えている"
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki", nargs="?", type=Path, default=Path("wiki"), help="wiki のディレクトリ（既定: ./wiki）")
    args = parser.parse_args()

    if not args.wiki.exists():
        print(f"wiki のディレクトリが見つからない: {args.wiki}", file=sys.stderr)
        sys.exit(1)

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

    for md_path in args.wiki.rglob("*.md"):
        rel = md_path.relative_to(args.wiki)
        try:
            # 先頭に BOM があっても読めるようにする。
            text = md_path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue

        if rel.name == "index.md" and len(rel.parts) == 1:
            index_lines = text.count("\n") + 1
            continue
        if rel.parts[0] in SKIP_TOP_LEVEL_FILES:
            continue
        if rel.parts[0] in SKIP_TOP_LEVEL_DIRS:
            continue
        if rel.name.startswith("."):
            continue

        total_pages += 1
        line_count = text.count("\n") + 1
        # 語数は空白で区切って数えるので、日本語のページではほぼ意味をなさない。文字数も数える。
        word_count = len(text.split())
        chars = char_count(text)
        total_lines += line_count
        total_words += word_count
        total_chars += chars
        # wikilink を数える前に frontmatter を取り除く。frontmatter ではパスのないスラッグを使う。
        body = FRONTMATTER_RE.sub("", text, count=1) if text.startswith("---") else text
        links = WIKILINK_RE.findall(body)
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

    print("=" * 60)
    print(f"wiki の統計: {args.wiki}")
    print("=" * 60)
    print(f"ページ数:       {total_pages}")
    print(f"総行数:         {total_lines:,}")
    print(f"総文字数:       {total_chars:,}（空白を除く）")
    print(f"総語数:         {total_words:,}（空白で区切った数）")
    print(f"総リンク数:     {total_links:,}")
    if total_pages:
        print(f"1 ページの平均: {total_lines // total_pages} 行、{total_chars // total_pages:,} 文字、{total_words // total_pages} 語")
        print(f"リンクの密度:   1 ページあたり {total_links / total_pages:.1f} 本")
    print(f"index.md:       {index_lines} 行" + ("  ← 分割を勧める（300 行超）" if index_lines > 300 else ""))
    print()

    print("type ごとのページ数:")
    for t, n in pages_by_type.most_common():
        print(f"  {ljust_display(t, 15)} {n}")
    print()

    print("ディレクトリごとのページ数:")
    for d, n in pages_by_dir.most_common():
        print(f"  {ljust_display(d, 15)} {n}")
    print()

    if largest:
        print("大きいページ（文字数の多い順）:")
        print(f"  {rjust_display('文字数', 7)}  {rjust_display('行数', 5)}  パス")
        for chars, lines, path in largest[:10]:
            print(f"  {chars:7,d}  {lines:5d}  {path}{size_warning(lines, chars)}")
        print()

    if most_linked_in:
        print("被リンクの多いページ（ハブ）:")
        for slug, count in most_linked_in.most_common(10):
            print(f"  {count:4d}  [[{slug}]]")
        print()

    # スケーリングについての推奨
    print("規模のしきい値:")
    if total_pages < 50:
        print("  → 最初のしきい値より小さい。平らな構成のままでよい。")
    elif total_pages < 150 and index_lines < 300:
        print("  → 分割のしきい値より小さい。index.md は 1 つのままでよい。")
    elif (total_pages >= 150 or index_lines >= 300) and not (args.wiki / "indexes").exists():
        print("  → 分割のしきい値に達した。index.md を wiki/indexes/<type>.md に分けることを検討する。")
        print("    references/scaling-playbook.md を参照。")
    elif total_pages >= 300:
        print("  → 300 ページを超えた。scripts/wiki_search.py をふだんの手段として使う。")
    if total_pages >= 500:
        print("  → 500 ページを超えた。lint を毎週、または数回の取り込みごとに実行する。")


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
