#!/usr/bin/env python3
"""正規の Python 版検索器における、セクション分割の契約を直接確かめるテスト。"""

import importlib.util
import json
import subprocess
import sys
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
                {"heading_path": [], "level": 0, "text": "", "section_index": 0},
                {"heading_path": ["Heading"], "level": 1, "text": "", "section_index": 1},
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


if __name__ == "__main__":
    unittest.main()
