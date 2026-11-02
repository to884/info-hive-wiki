#!/usr/bin/env python3
"""wikilink の解決の回帰テスト。

標準ライブラリだけを使い、テストフレームワークは使わない。直接実行する:

    python3 skills/llm-wiki/scripts/test_link_resolution.py

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
"""

import contextlib
import io
import sys
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


def check(label, got, expected, why, failures):
    ok = got == expected
    if not ok:
        failures.append(f"{label}: {why} -> got {got!r}, expected {expected!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<7} {str(got):<22} {why}")
    return ok


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
    check("top", "missing-page  (missing-page)  [BROKEN LINK]" in output.getvalue(),
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

    total = len(RESOLVE_CASES) * len(resolvers) + len(KEY_CASES) + len(STEM_CASES) + 2
    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
