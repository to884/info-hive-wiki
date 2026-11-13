#!/usr/bin/env python3
"""正規の Python 版検索器における、セクション分割の契約を直接確かめるテスト。"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "skills/info-hive/scripts/wiki_search.py"
SPEC = importlib.util.spec_from_file_location("wiki_search", SCRIPT)
WIKI_SEARCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WIKI_SEARCH)


class SplitSectionsContractTest(unittest.TestCase):
    def test_retains_empty_preface_and_empty_heading_section(self):
        sections = WIKI_SEARCH.split_sections("Page", "# Heading\n")
        self.assertEqual(
            sections,
            [
                # links は、本文全体の解析から集めたセクションの wikilink（0.9.0 から。判定レイヤーが使う）。
                {"heading_path": [], "level": 0, "text": "", "links": [], "section_index": 0},
                {"heading_path": ["Heading"], "level": 1, "text": "", "links": [], "section_index": 1},
            ],
        )

    def test_tracks_nested_paths_and_resets_at_parent_level(self):
        sections = WIKI_SEARCH.split_sections(
            "Page",
            "preface\n# One\nalpha\n## Two\nbeta\n# Reset\ngamma",
        )
        self.assertEqual(
            [section["heading_path"] for section in sections],
            [[], ["One"], ["One", "Two"], ["Reset"]],
        )

    def test_replaces_same_level_siblings_when_levels_are_skipped(self):
        sections = WIKI_SEARCH.split_sections(
            "Page",
            "## Alpha\none\n## Beta\ntwo\n# One\n### X\nx\n### Y\ny",
        )
        self.assertEqual(
            [section["heading_path"] for section in sections],
            [[], ["Alpha"], ["Beta"], ["One"], ["One", "X"], ["One", "Y"]],
        )

    def test_ignores_heading_markers_inside_backtick_and_tilde_fences(self):
        sections = WIKI_SEARCH.split_sections(
            "Page",
            "# Real\n```md\n## Backtick\n```\n~~~md\n### Tilde\n~~~\n## Next\nbody",
        )
        self.assertEqual(
            [section["heading_path"] for section in sections],
            [[], ["Real"], ["Real", "Next"]],
        )
        self.assertIn("## Backtick", sections[1]["text"])
        self.assertIn("### Tilde", sections[1]["text"])


EMPTY_QUERY = "、。！？"


class TokenizeContractTest(unittest.TestCase):
    """検索のトークン化（計画の方針 E）の契約。"""

    def test_ascii_words_are_unchanged(self):
        # 英数字だけのテキストは、移植元の [a-z0-9]+ と同じトークンになる。
        self.assertEqual(
            WIKI_SEARCH.tokenize("ZeRO stage-3, past_key_values"),
            ["zero", "stage", "3", "past", "key", "values"],
        )

    def test_kanji_gets_bigrams_and_unigrams(self):
        self.assertEqual(
            WIKI_SEARCH.tokenize("消費税"),
            ["消費", "費税", "消", "費", "税"],
        )

    def test_single_character_runs_stay_single(self):
        self.assertEqual(WIKI_SEARCH.tokenize("税"), ["税"])
        self.assertEqual(WIKI_SEARCH.tokenize("RAG の chunk"), ["rag", "の", "chunk"])

    def test_kana_gets_bigrams_without_unigrams(self):
        self.assertEqual(WIKI_SEARCH.tokenize("ひらがな"), ["ひら", "らが", "がな"])
        self.assertEqual(WIKI_SEARCH.tokenize("インデックス"), ["イン", "ンデ", "デッ", "ック", "クス"])

    def test_script_boundaries_split_runs(self):
        # 漢字・ひらがな・カタカナ・英数字の境界をまたぐ bigram は作らない。中黒も区切りにする。
        self.assertEqual(WIKI_SEARCH.tokenize("税を"), ["税", "を"])
        self.assertEqual(WIKI_SEARCH.tokenize("ジャパン・ネット"), ["ジャ", "ャパ", "パン", "ネッ", "ット"])
        self.assertEqual(WIKI_SEARCH.tokenize("UTF8版"), ["utf8", "版"])

    def test_nfkc_folds_fullwidth_and_halfwidth(self):
        self.assertEqual(WIKI_SEARCH.tokenize("ＢＭ２５"), ["bm25"])
        self.assertEqual(WIKI_SEARCH.tokenize("ﾃｽﾄ"), WIKI_SEARCH.tokenize("テスト"))

    def test_long_vowel_mark_and_iteration_mark_join_runs(self):
        self.assertEqual(WIKI_SEARCH.tokenize("サーバー"), ["サー", "ーバ", "バー"])
        self.assertEqual(WIKI_SEARCH.tokenize("人々"), ["人々", "人", "々"])

    def test_supplementary_kanji_is_kept(self):
        self.assertEqual(WIKI_SEARCH.tokenize("𠮷野"), ["𠮷野", "𠮷", "野"])

    def test_symbols_produce_no_tokens(self):
        self.assertEqual(WIKI_SEARCH.tokenize(EMPTY_QUERY), [])


class CoverageContractTest(unittest.TestCase):
    """日本語のクエリで、被覆率の低いページを語彙検索から外す契約。"""

    def make_pages(self, texts):
        return [
            {"rel_path": f"concepts/p{i}.md", "tokens": WIKI_SEARCH.tokenize(text)}
            for i, text in enumerate(texts)
        ]

    def test_ascii_query_is_not_filtered(self):
        pages = self.make_pages(["attention heads", "kv cache"])
        self.assertIsNone(WIKI_SEARCH.covered_pages(WIKI_SEARCH.tokenize("attention sourdough"), pages))

    def test_page_sharing_a_single_bigram_is_dropped(self):
        pages = self.make_pages([
            "消費税の軽減税率は飲食料品に適用される。",
            "京都の寺院を巡る旅行の記録。",
        ])
        # 「京都」だけが一致するクエリは、寺院のページにも当たらない語が多いので外れる。
        covered = WIKI_SEARCH.covered_pages(WIKI_SEARCH.tokenize("東京都の住宅ローン金利"), pages)
        self.assertEqual(covered, set())
        covered = WIKI_SEARCH.covered_pages(WIKI_SEARCH.tokenize("軽減税率"), pages)
        self.assertEqual(covered, {"concepts/p0.md"})

    def test_single_kanji_query_hits_compound_word(self):
        pages = self.make_pages(["消費税の仕組み", "富士山の登山道"])
        covered = WIKI_SEARCH.covered_pages(WIKI_SEARCH.tokenize("税"), pages)
        self.assertEqual(covered, {"concepts/p0.md"})


class EmptyTokenQueryTest(unittest.TestCase):
    """トークンが 0 個のクエリでも、--json なら空の結果を JSON で返す。

    記号だけのクエリは、日本語のトークン化（方針 E）を入れたあともトークンが 0 個になる。
    """

    def run_search(self, *extra):
        corpus = REPO / "eval/retrieval/corpus/wiki"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), EMPTY_QUERY, "--wiki", str(corpus), "--no-embed", *extra],
            capture_output=True, cwd=str(REPO),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc

    def test_json_returns_empty_results(self):
        proc = self.run_search("--json")
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["query"], EMPTY_QUERY)
        self.assertEqual(payload["results"], [])

    def test_json_page_granularity_returns_empty_results(self):
        proc = self.run_search("--json", "--granularity", "page")
        self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["results"], [])

    def test_text_output_reports_empty_query(self):
        proc = self.run_search()
        self.assertEqual(proc.stdout, b"")
        self.assertIn("クエリに検索できる語がない。", proc.stderr.decode("utf-8", "replace"))


# --json の結果の tags と sources に入る frontmatter の書き方と、期待する値。
# 値が 1 つだけの書き方（`tags: 検索`）も、1 要素の配列にする。空の文字と空白だけの要素は除く。
LIST_FIELD_PAGES = {
    "single": ("tags: 検索\nsources: paper", ["検索"], ["paper"]),
    "alias": ("タグ: 検索\n出典: paper", ["検索"], ["paper"]),
    "empty_value": ("tags:\nsources:", [], []),
    "empty_string": ('tags: ""\nsources: ""', [], []),
    "empty_array": ("tags: []\nsources: []", [], []),
    "array": ("tags: [a, b]\nsources: [p, q]", ["a", "b"], ["p", "q"]),
    "number": ("tags: 2024\nsources: 7", ["2024"], ["7"]),
    "blank_items": ('tags: ["", " x "]\nsources: [" ", y]', ["x"], ["y"]),
    "missing": ("", [], []),
}
LIST_FIELD_QUERY = "共通語"


def is_string_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


class JsonListFieldsContractTest(unittest.TestCase):
    """--json の結果の tags と sources は、frontmatter の書き方によらず、いつも文字の配列にする。

    browse の埋め込みの検索の契約（info-hive-dev の tests/wiki/browse/search_result_contract.json）が、
    この形を前提にする。MCP の query も同じ json_result() で結果を作る。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wiki = Path(self._tmp.name) / "wiki"
        (self.wiki / "concepts").mkdir(parents=True)
        # CLI は SCHEMA.md のないディレクトリを断る（ADR-0019）。SCHEMA.md はページに数えない。
        (self.wiki / "SCHEMA.md").write_bytes("# スキーマ\n".encode("utf-8"))
        for name, (fields, _tags, _sources) in LIST_FIELD_PAGES.items():
            text = f"---\ntitle: {name}\n{fields}\n---\n{LIST_FIELD_QUERY}の本文。\n"
            (self.wiki / "concepts" / f"{name}.md").write_bytes(text.encode("utf-8"))

    def tearDown(self):
        self._tmp.cleanup()

    def search(self, granularity: str, *extra) -> list[dict]:
        # 判定のバックエンドを止めておく。--decide はどの行も残し（fail open）、外には何も送らない。
        env = {**os.environ, "INFO_HIVE_DECISIONS": "off"}
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), LIST_FIELD_QUERY, "--wiki", str(self.wiki), "--no-embed",
             "--json", "--granularity", granularity, "--top", "100", *extra],
            capture_output=True, cwd=str(REPO), env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        results = json.loads(proc.stdout.decode("utf-8"))["results"]
        self.assertEqual({row["slug"] for row in results}, set(LIST_FIELD_PAGES))
        return results

    def assert_list_fields(self, results: list[dict], expected: dict | None = None) -> None:
        for row in results:
            with self.subTest(slug=row["slug"]):
                self.assertTrue(is_string_list(row["tags"]), row["tags"])
                self.assertTrue(is_string_list(row["sources"]), row["sources"])
                if expected is not None:
                    self.assertEqual((row["tags"], row["sources"]), expected[row["slug"]])

    def test_section_and_page_granularity(self):
        expected = {name: (tags, sources) for name, (_fields, tags, sources) in LIST_FIELD_PAGES.items()}
        for granularity in ("section", "page"):
            with self.subTest(granularity=granularity):
                self.assert_list_fields(self.search(granularity), expected)

    def test_decide(self):
        # --decide の行は、判定の結果を足す前に json_result() で作る。
        expected = {name: (tags, sources) for name, (_fields, tags, sources) in LIST_FIELD_PAGES.items()}
        for granularity in ("section", "page"):
            with self.subTest(granularity=granularity):
                self.assert_list_fields(self.search(granularity, "--decide", "--decide-pool", "100"), expected)

    def test_parse_cache(self):
        # 2 回目は、パースキャッシュに残した meta から結果を作る。
        expected = {name: (tags, sources) for name, (_fields, tags, sources) in LIST_FIELD_PAGES.items()}
        cache = Path(self._tmp.name) / "cache.json"
        for _ in range(2):
            self.assert_list_fields(self.search("section", "--cache", str(cache)), expected)

    def test_parse_cache_with_non_string_values(self):
        # キャッシュの meta は辞書であることしか確かめないので、文字でない値が入っていても配列にする。
        cache = Path(self._tmp.name) / "cache.json"
        self.search("section", "--cache", str(cache))
        payload = json.loads(cache.read_text(encoding="utf-8"))
        values = {"single": 5, "alias": None, "array": [1, None, "a"], "number": 2.5, "empty_value": True,
                  "empty_string": {"k": "v"}}
        for name, value in values.items():
            meta = payload["files"][f"concepts/{name}.md"]["meta"]
            meta["tags"] = value
            meta["sources"] = value
        cache.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        results = self.search("section", "--cache", str(cache))
        self.assert_list_fields(results)
        by_slug = {row["slug"]: row for row in results}
        self.assertEqual(by_slug["single"]["tags"], ["5"])
        self.assertEqual(by_slug["alias"]["tags"], [])
        self.assertEqual(by_slug["array"]["tags"], ["1", "a"])

    def test_json_result_direct(self):
        # パーサーは文字か文字の配列しか返さないが、json_result() は渡された meta の値によらず配列を返す。
        sections = [{"heading_path": [], "level": 0, "text": "本文", "links": [], "section_index": 0}]
        cases = [
            ("検索", ["検索"]), ("[a, b", ["[a, b"]), ("", []), ("  ", []), (None, []), ([], []),
            (["x"], ["x"]), ([1, None, "a", " "], ["1", "a"]), (5, ["5"]), (2.5, ["2.5"]), (True, None),
            ({"k": "v"}, None),
        ]
        for value, expected in cases:
            for key in ("tags", "sources"):
                with self.subTest(key=key, value=value):
                    page = {"slug": "p", "rel_path": "concepts/p.md", "meta": {"title": "t", key: value},
                            "sections": sections}
                    for row in (WIKI_SEARCH.json_result(1.0, page),
                                WIKI_SEARCH.json_result(1.0, page, sections[0], sections, ["bm25"])):
                        self.assertTrue(is_string_list(row[key]), row[key])
                        if expected is not None:
                            self.assertEqual(row[key], expected)
        # キーがないときも空の配列にする。
        page = {"slug": "p", "rel_path": "concepts/p.md", "meta": {}, "sections": sections}
        row = WIKI_SEARCH.json_result(1.0, page)
        self.assertEqual((row["tags"], row["sources"]), ([], []))


if __name__ == "__main__":
    unittest.main()
