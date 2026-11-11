#!/usr/bin/env python3
"""wikilink の解決の回帰テスト。

標準ライブラリだけを使い、テストフレームワークは使わない。直接実行する:

    python3 skills/info-hive/scripts/test_link_resolution.py

成功すれば 0、1件でも失敗すれば 1 で終了する。

このファイルがある理由
----------------------
`wiki_lint.py`、`wiki_graph_extract.py`、`wiki_search.py` はどれも、wikilink の生のテキストを
ページのスラッグと比べていた。スラッグはファイル名のステムそのものである。そのため、
実際の wiki でとてもよく使われる `[[entities/kalman-filter]]` のような、パスを付けたリンクは
どれにも一致しなかった。linter はそれらをすべてリンク切れと報告し、グラフの抽出器は
それらの `mentions` エッジを黙って出力せず、`--backlinks` も見落としていた。
ある 55 ページの wiki では、153 件の「リンク切れ」の報告のうち 142 件が誤りで、
さらに 15 ページが誤って孤立ページと報告されていた。

修正では、比べる前に正規化する。注意が必要な点であり、下のガードのケースを置く理由でもあるのは、
ディレクトリの接頭辞を取り除かずに「制約」として扱わなければならないことである。
単純にステムに縮めると、`[[raw/foo]]` が `sources/foo.md` に解決されてしまう。
この衝突は仮定の話ではない。ソースページは、要約する raw ファイルにちなんで名付けるのが慣例なので、
ステムだけにすると、ツリーの外を指す本当のリンク切れを誤って合格にしてしまう。

日本語の wiki のケース
----------------------
macOS は濁点付きのファイル名を NFD（「カ」＋「゛」）で返す。NFC で書いた [[ガイド]] と
ファイル名の「ガイド」が一致しないと、どのスクリプトでもリンク切れや孤立ページになる。
後半のケースでは NFD のファイル名を実際に作り、lint、search、stats、graph_extract のすべてで
NFC のリンクが解決されることを確かめる。あわせて、日本語の frontmatter のキーが読めること、
lint が日本語の書き損じ（別名の衝突、全角の「：」「｜」、「、」区切りのリスト）を指摘することを確かめる。
"""

import contextlib
import io
import sys
import tempfile
import unicodedata
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiki_graph_extract  # noqa: E402
import wiki_lint  # noqa: E402
import wiki_search  # noqa: E402
import wiki_stats  # noqa: E402


# sources/<stem>.md と、ツリーの外にある raw/<stem>.md の両方を含む wiki。
# パスによる制約は、まさにこの衝突を扱うためにある。
BY_PATH = {
    "sources/2024-01-15-field-notes": "2024-01-15-field-notes",
    "entities/kalman-filter": "kalman-filter",
    "concepts/battery-runtime": "battery-runtime",
}
BY_SLUG = {
    "2024-01-15-field-notes",
    "kalman-filter",
    "battery-runtime",
    "shannon-entropy",
}

RESOLVE_CASES = [
    # (リンク, 期待する結果, 理由)
    ("entities/kalman-filter", "kalman-filter", "path-qualified, path exists"),
    ("shannon-entropy", "shannon-entropy", "bare stem, page exists"),
    ("entities/kalman-filter#History", "kalman-filter", "heading anchor stripped"),
    ("entities/kalman-filter.md", "kalman-filter", "explicit .md stripped"),
    ("entities/kalman-filter|Kalman", "kalman-filter", "display alias stripped"),
    ("  entities/kalman-filter  ", "kalman-filter", "surrounding whitespace"),
    ("/entities/kalman-filter/", "kalman-filter", "stray leading/trailing slashes"),
    ("Entities/Kalman-Filter", None,
     "matching is case-SENSITIVE, symmetric with bare-slug matching"),
    # --- ガード: これらは解決されてはならない ---
    ("raw/2024-01-15-field-notes", None,
     "GUARD: out-of-tree path must not bind to the same-stem sources page"),
    ("personas/kalman-filter", None,
     "GUARD: wrong directory must not bind to entities/kalman-filter"),
    ("nonexistent-page", None, "unknown bare stem"),
    ("", None, "empty link"),
    ("#anchor-only", None, "anchor with no page"),
]

STEM_CASES = [
    ("entities/kalman-filter", "kalman-filter", "prefix dropped"),
    ("kalman-filter", "kalman-filter", "bare stem unchanged"),
    ("entities/kalman-filter#History", "kalman-filter", "anchor dropped"),
    ("raw/whatever", "whatever", "out-of-tree link still counted by stem"),
]

KEY_CASES = [
    ("entities/kalman-filter", "kalman-filter", "resolved path collapses to its slug"),
    ("missing-page", "missing-page", "unresolved bare link remains reportable"),
    ("raw/2024-01-15-field-notes", "raw/2024-01-15-field-notes",
     "unresolved qualified link keeps its path constraint"),
]


GUIDE = unicodedata.normalize("NFC", "ガイド")
GUIDE_NFD = unicodedata.normalize("NFD", "ガイド")

# NFD で書かれたリンクも、NFC のインデックスで解決されなければならない。
NFD_BY_PATH = {f"concepts/{GUIDE}": GUIDE}
NFD_BY_SLUG = {GUIDE}
NFD_RESOLVE_CASES = [
    (GUIDE_NFD, GUIDE, "NFD bare link resolves to the NFC slug"),
    (f"concepts/{GUIDE_NFD}", GUIDE, "NFD path-qualified link resolves"),
    (GUIDE, GUIDE, "NFC bare link resolves"),
]

# 日本語のスラッグ。英語のスラッグと同じ規則で解決されなければならない。
JA_BY_PATH = {
    "concepts/消費税": "消費税",
    "sources/文字化け障害報告": "文字化け障害報告",
    "entities/東京都": "東京都",
}
JA_BY_SLUG = {"消費税", "文字化け障害報告", "東京都"}
JA_RESOLVE_CASES = [
    ("消費税", "消費税", "Japanese bare stem"),
    ("concepts/消費税", "消費税", "Japanese path-qualified link"),
    ("消費税#税率の変遷", "消費税", "Japanese heading anchor stripped"),
    ("消費税|税金", "消費税", "Japanese display alias stripped"),
    ("sources/文字化け障害報告.md", "文字化け障害報告", "Japanese name with explicit .md"),
    ("　東京都　", "東京都", "ideographic spaces around the target"),
    # --- ガード: これらは解決されてはならない ---
    ("raw/文字化け障害報告", None, "GUARD: out-of-tree Japanese path"),
    ("entities/消費税", None, "GUARD: wrong directory for a Japanese slug"),
    ("東京", None, "GUARD: a prefix of a Japanese slug is not the slug"),
    ("京都", None, "GUARD: a substring of a Japanese slug is not the slug"),
]

REQUIRED_FM = ["type", "title", "tags", "created", "updated"]

# 一時ディレクトリに作る wiki。ガイド.md だけはファイル名を NFD で作る。
FIXTURE_PAGES = {
    # 日本語のキーと日本語の type の値だけで書いたページ
    f"concepts/{GUIDE_NFD}.md": (
        "---\n"
        "種類: 概念\n"
        "タイトル: ガイド\n"
        "タグ: [入門]\n"
        "作成日: 2026-01-01\n"
        "更新日: 2026-01-02\n"
        "---\n"
        "日本語の wiki の使い方。\n"
    ),
    # NFC で書いたリンクを2つの形で持つページ
    "concepts/目次.md": (
        "---\n"
        "type: concept\n"
        "title: 目次\n"
        "tags: [入門]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-02\n"
        "---\n"
        f"[[{GUIDE}]] と [[concepts/{GUIDE}]] を読む。\n"
    ),
    # NFD で書いたリンクを持つページ
    "concepts/索引.md": (
        "---\n"
        "type: concept\n"
        "title: 索引\n"
        "tags: [入門]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-02\n"
        "---\n"
        f"[[{GUIDE_NFD}]] と [[目次]] を参照する。\n"
    ),
    # 英語のキーと日本語の別名の両方がある
    "concepts/conflict.md": (
        "---\n"
        "type: concept\n"
        "title: Conflict\n"
        "タイトル: 衝突\n"
        "tags: [test]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-02\n"
        "---\n"
        f"[[{GUIDE}]]\n"
    ),
    # 全角の「：」のキーと、「、」で区切ったリスト
    "concepts/typos.md": (
        "---\n"
        "type: concept\n"
        "title: Typos\n"
        "tags: [機械学習、深層学習]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-02\n"
        "メモ：あとで書く\n"
        "---\n"
        f"[[{GUIDE}]]\n"
    ),
    # 全角の「｜」を区切りのつもりで使った wikilink
    "concepts/fullwidth.md": (
        "---\n"
        "type: concept\n"
        "title: Fullwidth\n"
        "tags: [test]\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-02\n"
        "---\n"
        f"[[{GUIDE}｜案内]]\n"
    ),
}

CHECKED = [0]


def check(label, got, expected, why, failures):
    CHECKED[0] += 1
    ok = got == expected
    if not ok:
        failures.append(f"{label}: {why} -> got {got!r}, expected {expected!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<7} {str(got):<22} {why}")
    return ok


def write_fixture(root: Path, pages: dict) -> None:
    for rel, text in pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def check_fixture_lint(root: Path, failures) -> None:
    print("lint: NFD filename, Japanese frontmatter, Japanese typos")
    pages = wiki_lint.collect_pages(root)
    findings = wiki_lint.lint(pages, 400, 800, REQUIRED_FM, False, 5)
    guide_path = f"concepts/{GUIDE}.md"

    guide_page = next((p for p in pages if p["slug"] == GUIDE), None)
    check("lint", guide_page is not None and guide_page["rel_path"], guide_path,
          "NFD filename yields NFC slug and NFC posix rel_path", failures)
    check("lint", guide_page is not None and guide_page["meta"].get("type"), "concept",
          "種類: 概念 is read as type: concept", failures)

    broken_to_guide = [f for f in findings["broken_links"]
                       if unicodedata.normalize("NFC", f["to"]).endswith(GUIDE)]
    check("lint", broken_to_guide, [],
          "[[ガイド]] / [[concepts/ガイド]] (NFC and NFD) are not broken", failures)
    check("lint", any(f["slug"] == GUIDE for f in findings["orphans"]), False,
          "NFD-named page is not an orphan", failures)
    check("lint", [f for f in findings["missing_frontmatter"] if f["path"] == guide_path], [],
          "Japanese frontmatter keys satisfy required fields", failures)

    check("lint", findings["frontmatter_alias_conflicts"],
          [{"path": "concepts/conflict.md", "key": "title", "keys": ["title", "タイトル"]}],
          "title + タイトル reported as alias conflict", failures)
    check("lint", findings["fullwidth_colon_keys"],
          [{"path": "concepts/typos.md", "keys": ["メモ"]}],
          "メモ：... reported as full-width colon key", failures)
    check("lint", findings["ideographic_comma_lists"],
          [{"path": "concepts/typos.md", "keys": ["tags"]}],
          "[機械学習、深層学習] reported as ideographic comma list", failures)
    check("lint", findings["fullwidth_link_syntax"],
          [{"from_path": "concepts/fullwidth.md", "link": f"{GUIDE}｜案内"}],
          "[[ガイド｜案内]] reported as full-width link syntax", failures)
    check("lint", any(f["to"] == f"{GUIDE}｜案内" for f in findings["broken_links"]), True,
          "full-width ｜ is not a separator, so the link is broken", failures)
    for key in ("frontmatter_alias_conflicts", "fullwidth_colon_keys",
                "ideographic_comma_lists", "fullwidth_link_syntax"):
        check("lint", findings["summary"].get(key), 1, f"summary counts {key}", failures)
    print()


def check_fixture_search(root: Path, failures) -> None:
    print("search: NFD filename resolves NFC links")
    pages = wiki_search.collect_pages(root)
    by_path, by_slug = wiki_search.build_link_index(pages)
    for name in ("目次", "索引"):
        page = next((p for p in pages if p["slug"] == name), None)
        links = page["links"] if page else []
        guide_links = [l for l in links if unicodedata.normalize("NFC", l).endswith(GUIDE)]
        resolved = [wiki_search.resolve_link(l, by_path, by_slug) for l in guide_links]
        check("search", bool(resolved) and all(r == GUIDE for r in resolved), True,
              f"links from {name} resolve to NFC slug", failures)

    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        wiki_search.cmd_backlinks(SimpleNamespace(backlinks=GUIDE), pages)
    text = output.getvalue()
    check("search", "concepts/目次.md" in text and "concepts/索引.md" in text, True,
          "--backlinks ガイド finds NFC and NFD linkers", failures)
    print()


def check_fixture_stats(root: Path, failures) -> None:
    print("stats: NFC and NFD links counted together")
    output = io.StringIO()
    argv = sys.argv
    sys.argv = ["wiki_stats.py", str(root)]
    try:
        with contextlib.redirect_stdout(output):
            wiki_stats.main()
    finally:
        sys.argv = argv
    # 目次 から2件、索引・conflict・typos から1件ずつ。NFD のリンクを別に数えると 4 になる。
    check("stats", f"{5:4d}  [[{GUIDE}]]" in output.getvalue(), True,
          "hub count for [[ガイド]] is 5", failures)
    print()


def check_fixture_graph(root: Path, failures) -> None:
    print("graph: NFD filename gets mentions edges from NFC links")
    pages = wiki_graph_extract.collect_pages(root)
    ontology = {"node_types": {}, "predicates": {}}
    nodes, page_node_ids, _aliases = wiki_graph_extract.build_nodes(pages, ontology)
    edges = wiki_graph_extract.build_edges(pages, page_node_ids)

    def node_of(slug: str):
        page = next((p for p in pages if p["slug"] == slug), None)
        return page_node_ids.get(wiki_graph_extract.page_key(page)) if page else None

    guide_id = node_of(GUIDE)
    check("graph", guide_id is not None, True, "node for NFD file is found by its NFC slug", failures)
    for name in ("目次", "索引"):
        subject = node_of(name)
        found = any(e["predicate"] == "mentions" and e["subject"] == subject
                    and e["object"] == guide_id for e in edges) if guide_id else False
        check("graph", found, True, f"{name} -> ガイド mentions edge", failures)
    print()


def check_nfc_collision(root: Path, failures) -> None:
    print("lint: NFC and NFD files with the same name are duplicate slugs")
    root.mkdir(parents=True)
    page = "---\ntype: concept\ntitle: x\ntags: [x]\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n"
    (root / f"{GUIDE}.md").write_text(page, encoding="utf-8")
    (root / f"{GUIDE_NFD}.md").write_text(page, encoding="utf-8")
    if len(list(root.iterdir())) < 2:
        # macOS の APFS などは正規化の違いを同じ名前とみなすので、2つのファイルを作れない。
        print("  SKIP  lint    filesystem treats NFC and NFD names as the same file")
        return
    findings = wiki_lint.lint(wiki_lint.collect_pages(root), 400, 800, REQUIRED_FM, False, 5)
    dup = findings["duplicate_slugs"]
    check("lint", len(dup) == 1 and dup[0]["slug"] == GUIDE, True,
          "one duplicate-slug finding for ガイド", failures)
    check("lint", bool(dup) and sorted(dup[0]["paths"]) == sorted([f"{GUIDE}.md", f"{GUIDE_NFD}.md"]),
          True, "finding lists both on-disk paths", failures)
    check("lint", bool(dup) and dup[0].get("normalization_collision"), True,
          "finding is marked as a normalization collision", failures)
    print()


def main():
    failures = []

    resolvers = [
        ("lint", wiki_lint.resolve_link),
        ("graph", wiki_graph_extract.resolve_link_slug),
        ("search", wiki_search.resolve_link),
    ]
    for label, fn in resolvers:
        print(f"{label}: path-constrained resolution")
        for link, expected, why in RESOLVE_CASES:
            check(label, fn(link, BY_PATH, BY_SLUG), expected,
                  f"[[{link}]] — {why}", failures)
        print()

    print("search: report keys preserve unresolved links")
    for link, expected, why in KEY_CASES:
        check("key", wiki_search.link_key(link, BY_PATH, BY_SLUG), expected,
              f"[[{link}]] — {why}", failures)

    pages = [
        {"slug": "kalman-filter", "rel_path": "entities/kalman-filter.md",
         "meta": {"title": "Kalman Filter"}, "links": []},
        {"slug": "source", "rel_path": "concepts/source.md",
         "meta": {"title": "Source"},
         "links": ["entities/kalman-filter", "missing-page"]},
    ]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        wiki_search.cmd_top_linked(SimpleNamespace(top_linked=10), pages)
    check("top", "missing-page  (missing-page)  ［リンク切れ］" in output.getvalue(),
          True, "--top-linked retains broken-link reports", failures)

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        wiki_search.cmd_backlinks(SimpleNamespace(backlinks="missing-page"), pages)
    check("backlink", "Source  (concepts/source.md)" in output.getvalue(), True,
          "--backlinks can inspect a missing target", failures)
    print()

    # wiki_stats は被リンクの多さを数えるもので、意図して解決の正しさを検証しない。
    # そのためステムに縮める。この文書化した違いをテストで固定し、誰かが誤って
    # 正しさのチェックに「直して」しまわないようにする。
    print("stats: stem-only counting (resolution deliberately NOT verified)")
    for link, expected, why in STEM_CASES:
        check("stats", wiki_stats.link_stem(link), expected,
              f"[[{link}]] — {why}", failures)

    print()
    print("unicode: NFD link text resolves against NFC index")
    for label, fn in resolvers:
        for link, expected, why in NFD_RESOLVE_CASES:
            check(label, fn(link, NFD_BY_PATH, NFD_BY_SLUG), expected, why, failures)
    check("stats", wiki_stats.link_stem(f"concepts/{GUIDE_NFD}"), GUIDE,
          "NFD link stem counted as NFC", failures)
    print()

    print("unicode: Japanese slugs follow the same resolution rules")
    for label, fn in resolvers:
        for link, expected, why in JA_RESOLVE_CASES:
            check(label, fn(link, JA_BY_PATH, JA_BY_SLUG), expected, f"[[{link}]] — {why}", failures)
    check("stats", wiki_stats.link_stem("concepts/消費税#税率"), "消費税",
          "Japanese link stem drops prefix and anchor", failures)
    print()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "wiki"
        write_fixture(root, FIXTURE_PAGES)
        check_fixture_lint(root, failures)
        check_fixture_search(root, failures)
        check_fixture_stats(root, failures)
        check_fixture_graph(root, failures)

    with tempfile.TemporaryDirectory() as tmp:
        check_nfc_collision(Path(tmp) / "wiki", failures)

    total = CHECKED[0]
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
