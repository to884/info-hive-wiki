#!/usr/bin/env python3
"""frontmatter の type、kind、updated、created を配列で書いたときの、検索、統計、lint、グラフの扱いの試験。

1 つの値を入れる欄でも、パーサーは書かれた形のまま返すので、`type: [concept]` は配列、`updated:` は空の配列になる。
今までは、検索の --json の type と updated に配列のまま出た。--type と --since の絞り込みや統計は、そうした
ページを外していた。グラフの抽出は、配列の type や kind を sqlite に渡して止まった。
この修正から、wiki_pages.scalar_value()、type_value()、kind_value() でそろえる。文字と None はそのまま返す。
配列は空でない要素を「, 」でつなぎ（type と kind は要素ごとに日本語の別名を英語に直す）、空なら None にする。
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

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

QUERY = "共通語"
# スラッグ: (ディレクトリ, frontmatter の行, --json の type, --json の updated)。
PAGES = {
    "plain": ("concepts", 'type: "concept"\nupdated: 2026-01-01', "concept", "2026-01-01"),
    "type_one": ("concepts", "type: [concept]\nupdated: 2026-01-01", "concept", "2026-01-01"),
    "type_alias": ("concepts", "type: [概念]\nupdated: 2026-01-01", "concept", "2026-01-01"),
    "type_two": ("concepts", "type: [concept, entity]\nupdated: 2026-01-01", "concept, entity", "2026-01-01"),
    "type_empty": ("concepts", "type:\nupdated: 2026-01-01", None, "2026-01-01"),
    "type_blank": ("concepts", 'type: ""\nupdated: 2026-01-01', "", "2026-01-01"),
    "upd_one": ("concepts", 'type: "concept"\nupdated: [2026-03-01]', "concept", "2026-03-01"),
    "upd_two": ("concepts", 'type: "concept"\nupdated: [2026-03-01, 2026-04-01]', "concept",
                "2026-03-01, 2026-04-01"),
    "upd_empty": ("concepts", 'type: "concept"\nupdated:', "concept", None),
    "upd_blank": ("concepts", 'type: "concept"\nupdated: ""', "concept", ""),
    "missing": ("concepts", "", None, None),
    "src_one": ("sources", "type: [source]\nupdated: 2026-01-01", "source", "2026-01-01"),
    "uses_src": ("concepts", 'type: "concept"\nupdated: 2026-01-01\nsources: ["src_one"]', "concept", "2026-01-01"),
    # グラフの lint が、配列の kind から主語の型（person）を、配列の type から出典のページ（source）を読むことを確かめる関係。
    "kind_one": ("entities", 'type: "entity"\nkind: [person]\ncreated: [2026-01-01]\nupdated: 2026-01-01\n'
                 "graph:\n  relationships:\n    - predicate: works_on\n      object: concept:plain\n"
                 '      source: src_one\n      evidence: "共通語の本文"\n      confidence: high\n      status: current',
                 "entity", "2026-01-01"),
    "kind_alias": ("entities", 'type: [エンティティ]\nkind: [人物]\nupdated: 2026-01-01', "entity", "2026-01-01"),
}


class ScalarWiki:
    """PAGES のページを置いた使い捨ての wiki。どのページも plain にリンクする。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wiki"
        # CLI は SCHEMA.md のないディレクトリを断る（info-hive-dev の ADR-0019）。SCHEMA.md はページに数えない。
        self.root.mkdir(parents=True)
        (self.root / "SCHEMA.md").write_bytes("# スキーマ\n".encode("utf-8"))
        for slug, (folder, fields, _type, _updated) in PAGES.items():
            (self.root / folder).mkdir(exist_ok=True)
            link = "" if slug == "plain" else "[[plain]]"
            text = f'---\ntitle: "{slug}"\n{fields}\n---\n{QUERY}の本文。{link}\n'
            (self.root / folder / f"{slug}.md").write_bytes(text.encode("utf-8"))

    def close(self):
        self.tmp.cleanup()


def run_script(name: str, *args) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INFO_HIVE_DECISIONS")}
    env["INFO_HIVE_DECISIONS"] = "off"
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPTS / name), *map(str, args)],
                          capture_output=True, cwd=str(REPO), env=env)


class ValueFunctionTest(unittest.TestCase):
    def test_scalar_value(self):
        cases = [
            ("2026-01-01", "2026-01-01"), ("", ""), ("  x  ", "  x  "), (None, None), ([], None), (["", " "], None),
            (["2026-03-01"], "2026-03-01"), (["a", "b"], "a, b"), (5, "5"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(WIKI_PAGES.scalar_value(value), expected)

    def test_type_and_kind_value(self):
        cases = [
            (WIKI_PAGES.type_value, "concept", "concept"), (WIKI_PAGES.type_value, "概念", "概念"),
            (WIKI_PAGES.type_value, "", ""), (WIKI_PAGES.type_value, None, None), (WIKI_PAGES.type_value, [], None),
            (WIKI_PAGES.type_value, ["概念"], "concept"), (WIKI_PAGES.type_value, ["概念", "ソース"], "concept, source"),
            (WIKI_PAGES.kind_value, ["人物"], "person"), (WIKI_PAGES.kind_value, "person", "person"),
            (WIKI_PAGES.kind_value, [], None),
        ]
        for func, value, expected in cases:
            with self.subTest(func=func.__name__, value=value):
                self.assertEqual(func(value), expected)


class SearchScalarTest(unittest.TestCase):
    def setUp(self):
        self.wiki = ScalarWiki()
        self.addCleanup(self.wiki.close)

    def search(self, *extra) -> list[dict]:
        proc = run_script("wiki_search.py", QUERY, "--wiki", self.wiki.root, "--no-embed", "--json", "--top", "100",
                          *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))["results"]

    def test_json_type_and_updated(self):
        for granularity in ("section", "page"):
            rows = {row["slug"]: row for row in self.search("--granularity", granularity)}
            self.assertEqual(set(rows), set(PAGES))
            for slug, (_folder, _fields, page_type, updated) in PAGES.items():
                with self.subTest(granularity=granularity, slug=slug):
                    self.assertEqual((rows[slug]["type"], rows[slug]["updated"]), (page_type, updated))

    def test_parse_cache(self):
        cache = Path(self.wiki.tmp.name) / "cache.json"
        for _ in range(2):
            rows = {row["slug"]: row for row in self.search("--cache", cache)}
            self.assertEqual(rows["type_alias"]["type"], "concept")
            self.assertEqual(rows["upd_one"]["updated"], "2026-03-01")

    def test_type_filter(self):
        concept = {slug for slug, (_f, _x, page_type, _u) in PAGES.items() if page_type == "concept"}
        for wanted in ("concept", "概念"):
            with self.subTest(type=wanted):
                self.assertEqual({row["slug"] for row in self.search("--type", wanted)}, concept)
        self.assertEqual({row["slug"] for row in self.search("--type", "source")}, {"src_one"})
        self.assertEqual({row["slug"] for row in self.search("--type", "エンティティ")}, {"kind_one", "kind_alias"})

    def test_since_filter(self):
        # 配列で書いた updated も日付として読む。つないだ値は、今までどおり先頭の 10 文字を日付として読む。
        self.assertEqual({row["slug"] for row in self.search("--since", "2026-02-01")}, {"upd_one", "upd_two"})

    def test_text_output(self):
        proc = run_script("wiki_search.py", QUERY, "--wiki", self.wiki.root, "--no-embed", "--top", "100")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        out = proc.stdout.decode("utf-8")
        self.assertNotIn("['", out)
        self.assertNotIn("[[]", out)
        lines = {line.split("] ", 2)[-1].strip(): line for line in out.splitlines() if line.startswith("  [")}
        self.assertIn("[concept  ]", lines["type_alias"])
        self.assertIn("[?        ]", lines["missing"])
        self.assertIn("[?        ]", lines["type_empty"])
        self.assertIn("[         ]", lines["type_blank"])


class StatsScalarTest(unittest.TestCase):
    def test_pages_by_type(self):
        wiki = ScalarWiki()
        self.addCleanup(wiki.close)
        proc = run_script("wiki_stats.py", wiki.root, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        by_type = json.loads(proc.stdout.decode("utf-8"))["pages_by_type"]
        expected = {}
        for _folder, _fields, page_type, _updated in PAGES.values():
            if page_type:
                expected[page_type] = expected.get(page_type, 0) + 1
        for page_type, count in expected.items():
            with self.subTest(type=page_type):
                self.assertEqual(by_type.get(page_type), count)


class LintScalarTest(unittest.TestCase):
    def test_source_as_array_is_a_source(self):
        # type: [source] のページを、lint は source として読む（グラフの抽出と同じ）。
        wiki = ScalarWiki()
        self.addCleanup(wiki.close)
        findings = WIKI_LINT.lint(WIKI_LINT.collect_pages(wiki.root), soft_cap=100, hard_cap=200,
                                  required_fm=["type", "title"], suggest_pages=False, suggest_min=5)
        self.assertEqual([f for f in findings["unresolved_sources"] if f["path"] == "concepts/uses_src.md"], [])
        # 書き方の指摘は、書かれた形のまま残す。
        non_iso = {f["path"]: f["value"] for f in findings["non_iso_dates"]}
        self.assertEqual(non_iso.get("concepts/upd_one.md"), ["2026-03-01"])


@unittest.skipUnless(HAS_YAML, "グラフの抽出には PyYAML が要る")
class GraphScalarTest(unittest.TestCase):
    def test_extract_and_lint(self):
        wiki = ScalarWiki()
        self.addCleanup(wiki.close)
        (wiki.root / "graph").mkdir()
        shutil.copyfile(SCRIPTS.parent / "assets" / "ontology.yaml.template", wiki.root / "graph" / "ontology.yaml")
        proc = run_script("wiki_graph_extract.py", wiki.root)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        nodes = {n["slug"]: n for n in (json.loads(line) for line in
                 (wiki.root / "graph" / "nodes.jsonl").read_bytes().decode("utf-8").splitlines() if line)
                 if n.get("slug")}
        self.assertEqual((nodes["type_alias"]["page_type"], nodes["type_alias"]["node_type"]), ("concept", "concept"))
        self.assertEqual(nodes["src_one"]["id"], "source:src_one")
        self.assertEqual((nodes["kind_one"]["kind"], nodes["kind_one"]["node_type"]), ("person", "person"))
        self.assertEqual(nodes["kind_one"]["created"], "2026-01-01")
        self.assertEqual((nodes["kind_alias"]["kind"], nodes["kind_alias"]["node_type"]), ("person", "person"))
        edges = [json.loads(line) for line in
                 (wiki.root / "graph" / "edges.jsonl").read_bytes().decode("utf-8").splitlines() if line]
        triples = {(e["subject"], e["predicate"], e["object"]) for e in edges}
        self.assertIn(("concept:uses_src", "sourced_from", "source:src_one"), triples)
        self.assertIn(("person:kind_one", "works_on", "concept:plain"), triples)
        for extra in ([], ["--decide"]):
            with self.subTest(extra=extra):
                proc = run_script("wiki_graph_lint.py", wiki.root, "--json", *extra)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
                findings = json.loads(proc.stdout.decode("utf-8"))
                # 主語の kind: [person] を person と読み、source: src_one を type: [source] のページと読む。
                for key in ("subject_type_mismatch", "object_type_mismatch", "broken_source_refs", "broken_object_refs"):
                    self.assertEqual([f for f in findings[key] if f.get("page") == "entities/kind_one.md"], [], key)


if __name__ == "__main__":
    unittest.main()
