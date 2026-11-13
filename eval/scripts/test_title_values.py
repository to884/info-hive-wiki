#!/usr/bin/env python3
"""frontmatter の title が文字でないとき（値なし、配列）の、検索、lint、グラフ、判定の扱いの試験（0.12.2）。

title は 1 つの名前を入れる欄だが、パーサーは書かれた形のまま、文字か文字の配列を返す。`title:` と書くと
空の配列、`title: [WIP]` と書くと 1 要素の配列になる。0.12.1 までは、こうしたページが 1 つあると、検索
（CLI と MCP の索引）、lint の --suggest-pages、グラフの抽出が TypeError などで止まった。
0.12.2 から、wiki_pages.text_value() で文字にそろえる。文字はそのまま、配列は空でない要素を「, 」でつなぐ。
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "skills" / "info-hive" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WIKI_PAGES = load("wiki_pages")
WIKI_SEARCH = load("wiki_search")
WIKI_LINT = load("wiki_lint")
WIKI_DECIDE = load("wiki_decide")

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# title の書き方と、期待する題名（None なら題名なしで、表示はスラッグ）。
TITLE_PAGES = {
    "plain": ('title: "消費税の本"', "消費税の本"),
    "alias": ('タイトル: "別名の題名"', "別名の題名"),
    "empty_value": ("title:", None),
    "empty_string": ('title: ""', None),
    "empty_array": ("title: []", None),
    "one_item": ("title: [検索窓]", "検索窓"),
    "two_items": ("title: [甲板, 乙板]", "甲板, 乙板"),
    "block": ("title:\n- 丙種\n- 丁種", "丙種, 丁種"),
    "blank_items": ('title: ["", " 戊辰 "]', "戊辰"),
    "missing": ("", None),
}
QUERY = "共通語"


def expected_title(slug: str) -> str:
    title = TITLE_PAGES[slug][1]
    return title if title is not None else slug


class TitleWiki:
    """TITLE_PAGES のページを置いた使い捨ての wiki。どのページも plain にリンクし、plain は one_item にリンクする。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wiki"
        (self.root / "concepts").mkdir(parents=True)
        # CLI は SCHEMA.md のないディレクトリを断る（info-hive-dev の ADR-0019）。SCHEMA.md はページに数えない。
        (self.root / "SCHEMA.md").write_bytes("# スキーマ\n".encode("utf-8"))
        for slug, (fields, _title) in TITLE_PAGES.items():
            link = "[[one_item]]" if slug == "plain" else "[[plain]]"
            text = (f'---\n{fields}\ntype: "concept"\ntags: ["a"]\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n'
                    f"{QUERY}の本文。検索窓の話。{link}\n")
            (self.root / "concepts" / f"{slug}.md").write_bytes(text.encode("utf-8"))

    def close(self):
        self.tmp.cleanup()


def run_script(name: str, *args, decisions: dict | None = None) -> subprocess.CompletedProcess:
    # 判定のバックエンドは、渡されなければ止めておく（どの行も残す fail open で、外には何も送らない）。
    env = {k: v for k, v in os.environ.items() if not k.startswith("INFO_HIVE_DECISIONS")}
    env.update(decisions or {"INFO_HIVE_DECISIONS": "off"})
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / name), *map(str, args)],
                          capture_output=True, cwd=str(REPO), env=env)


class TextValueTest(unittest.TestCase):
    def test_values(self):
        cases = [
            ("消費税", "消費税"), ("  前後  ", "  前後  "), ("", ""), (None, ""), ([], ""), (["WIP"], "WIP"),
            (["x", "y"], "x, y"), (["", " z ", None], "z"), ([1, "a"], "1, a"), (5, "5"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(WIKI_PAGES.text_value(value), expected)


class SearchTitleTest(unittest.TestCase):
    """title が文字でないページがあっても、検索はほかのページを返し、結果の title は文字である。"""

    def setUp(self):
        self.wiki = TitleWiki()
        self.addCleanup(self.wiki.close)

    def search_json(self, query: str, *extra) -> list[dict]:
        proc = run_script("wiki_search.py", query, "--wiki", self.wiki.root, "--no-embed", "--json", "--top", "100",
                          *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))["results"]

    def assert_titles(self, results: list[dict]) -> None:
        self.assertEqual({row["slug"] for row in results}, set(TITLE_PAGES))
        for row in results:
            with self.subTest(slug=row["slug"]):
                self.assertIsInstance(row["title"], str)
                self.assertEqual(row["title"], expected_title(row["slug"]))

    def test_section_and_page_granularity(self):
        for granularity in ("section", "page"):
            with self.subTest(granularity=granularity):
                self.assert_titles(self.search_json(QUERY, "--granularity", granularity))

    def test_decide_fail_open(self):
        # 判定できないとき（バックエンドを止めた）も、行は json_result() で作る。
        self.assert_titles(self.search_json(QUERY, "--decide", "--decide-pool", "100"))

    def test_decide_sends_text_titles(self):
        # 判定できるときは、各行の title を判定モデルに渡す。同梱の偽のバックエンド（127.0.0.1）で受け取って確かめる。
        sys.path.insert(0, str(REPO / "eval" / "decisions"))
        import fake_backend

        states = []

        def handler(request):
            states.append(request.get("state") or {})
            return fake_backend.default_handler(request)

        server, url = fake_backend.serve(handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        proc = run_script("wiki_search.py", QUERY, "--wiki", self.wiki.root, "--no-embed", "--json", "--top", "100",
                          "--decide", "--decide-pool", "100", "--decide-threshold", "0",
                          decisions={"INFO_HIVE_DECISIONS": "local", "INFO_HIVE_DECISIONS_URL": url})
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(payload["decision"]["failed_open"], payload["decision"])
        self.assert_titles(payload["results"])
        titles = [state.get("title") for state in states]
        self.assertTrue(titles)
        self.assertTrue(all(isinstance(t, str) for t in titles), titles)
        self.assertEqual(set(titles), {expected_title(slug) for slug in TITLE_PAGES})

    def test_parse_cache(self):
        cache = Path(self.wiki.tmp.name) / "cache.json"
        for _ in range(2):
            self.assert_titles(self.search_json(QUERY, "--cache", cache))

    def test_title_words_are_searchable(self):
        # 配列で書いた title の語も、今までの文字の title と同じく、検索の語になる。
        for query, slug in (("甲板", "two_items"), ("丁種", "block"), ("戊辰", "blank_items")):
            with self.subTest(query=query):
                self.assertIn(slug, [row["slug"] for row in self.search_json(query)])

    def test_text_output(self):
        # 人が読む出力も止まらず、題名に Python の配列の表記（['甲板', '乙板']）を出さない。
        for args in ([QUERY], ["", "--backlinks", "plain"], ["", "--top-linked", "3"]):
            with self.subTest(args=args):
                proc = run_script("wiki_search.py", *args, "--wiki", self.wiki.root, "--no-embed")
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
                self.assertNotIn("['", proc.stdout.decode("utf-8"))

    def test_backlinks_lists_every_title(self):
        proc = run_script("wiki_search.py", "", "--wiki", self.wiki.root, "--backlinks", "plain")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        out = proc.stdout.decode("utf-8")
        for slug in TITLE_PAGES:
            if slug != "plain":
                self.assertIn(f"  - {expected_title(slug)}  (concepts/{slug}.md)", out)

    def test_json_result_with_cached_values(self):
        # パースキャッシュの meta は辞書であることしか確かめないので、文字でない値も文字にする。
        sections = [{"heading_path": [], "level": 0, "text": "本文", "links": [], "section_index": 0}]
        for value, expected in ((["a", "b"], "a, b"), (5, "5"), (None, "p"), ([], "p"), ("t", "t")):
            with self.subTest(value=value):
                page = {"slug": "p", "rel_path": "concepts/p.md", "meta": {"title": value}, "sections": sections}
                self.assertEqual(WIKI_SEARCH.json_result(1.0, page)["title"], expected)


class LintTitleTest(unittest.TestCase):
    def test_suggest_pages(self):
        wiki = TitleWiki()
        self.addCleanup(wiki.close)
        pages = WIKI_LINT.collect_pages(wiki.root)
        findings = WIKI_LINT.lint(pages, soft_cap=100, hard_cap=200, required_fm=["type", "title"],
                                  suggest_pages=True, suggest_min=3)
        phrases = {item["phrase"] for item in findings["suggested_pages"]}
        # 「検索窓」は one_item の題名（[検索窓]）なので、専用のページがあるとみなし、候補にしない。
        self.assertNotIn("検索窓", phrases)
        # 題名のないページは、今までどおり title がないと指摘する。
        missing = {item["path"] for item in findings["missing_frontmatter"]}
        self.assertEqual(missing, {f"concepts/{s}.md" for s in ("empty_value", "empty_string", "empty_array",
                                                                 "missing")})


class DecideTitleTest(unittest.TestCase):
    def test_strip_frontmatter(self):
        cases = [
            ("---\ntitle: [x, y]\n---\n# 見出し\n本文\n", "x, y"),
            ("---\ntitle:\n---\n# 見出し\n本文\n", "見出し"),
            ('---\ntitle: "題"\n---\n# 見出し\n本文\n', "題"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(WIKI_DECIDE.strip_frontmatter(text)[0], expected)


@unittest.skipUnless(HAS_YAML, "グラフの抽出には PyYAML が要る")
class GraphTitleTest(unittest.TestCase):
    def test_extract_and_lint(self):
        wiki = TitleWiki()
        self.addCleanup(wiki.close)
        (wiki.root / "graph").mkdir()
        shutil.copyfile(SCRIPTS.parent / "assets" / "ontology.yaml.template", wiki.root / "graph" / "ontology.yaml")
        proc = run_script("wiki_graph_extract.py", wiki.root)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        nodes = [json.loads(line) for line in
                 (wiki.root / "graph" / "nodes.jsonl").read_bytes().decode("utf-8").splitlines() if line]
        titles = {node["slug"]: node["title"] for node in nodes if node.get("slug") in TITLE_PAGES}
        self.assertEqual(titles, {slug: expected_title(slug) for slug in TITLE_PAGES})
        # グラフの lint は、--decide のときにページの題名を集める（判定のバックエンドは止めておく）。
        for extra in ([], ["--decide"]):
            with self.subTest(extra=extra):
                proc = run_script("wiki_graph_lint.py", wiki.root, "--json", *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
                self.assertIn("summary", json.loads(proc.stdout.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
