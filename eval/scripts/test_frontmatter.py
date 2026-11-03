#!/usr/bin/env python3
"""wiki_text の frontmatter の共通のパーサー、引用符の検査、書き手のテスト。"""

import importlib.util
import sys
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


WIKI_TEXT = load("wiki_text")
parse = WIKI_TEXT.parse_frontmatter_text
unquoted = WIKI_TEXT.unquoted_string_keys
fmt = WIKI_TEXT.format_fm_value


class ParseTests(unittest.TestCase):
    def test_comma_inside_quotes_stays_in_one_item(self):
        meta = parse('authors: ["Kingma, D.", "Ba, J."]\n')
        self.assertEqual(meta["authors"], ["Kingma, D.", "Ba, J."])

    def test_trailing_comment_outside_quotes_is_dropped(self):
        meta = parse('url: "https://example.com/a?b=1#x" # 該当する場合\nraw: raw/x.md # メモ\n')
        self.assertEqual(meta["url"], "https://example.com/a?b=1#x")
        self.assertEqual(meta["raw"], "raw/x.md")

    def test_hash_inside_quotes_is_kept(self):
        meta = parse('title: "C# の #1"\n')
        self.assertEqual(meta["title"], "C# の #1")

    def test_escapes_are_restored(self):
        meta = parse('title: "Foo \\"Bar\\" C:\\\\dir"\n')
        self.assertEqual(meta["title"], 'Foo "Bar" C:\\dir')

    def test_block_list_and_inline_list(self):
        meta = parse('sources:\n  - "a"\n  - "b"\ntags: ["税制", "間接税"]\n')
        self.assertEqual(meta["sources"], ["a", "b"])
        self.assertEqual(meta["tags"], ["税制", "間接税"])

    def test_nested_mapping_is_not_parsed(self):
        meta = parse('graph:\n  node_id: "person:x"\n  relationships:\n    - predicate: "works_at"\ntitle: "x"\n')
        self.assertEqual(meta["graph"], [])
        self.assertEqual(meta["title"], "x")

    def test_unquoted_values_still_parse(self):
        meta = parse("type: source\ntags: [a, b]\ncreated: 2026-11-03\n")
        self.assertEqual(meta, {"type": "source", "tags": ["a", "b"], "created": "2026-11-03"})

    def test_matches_pyyaml_for_escapes(self):
        try:
            import yaml
        except ImportError:  # PyYAML がない環境では飛ばす
            self.skipTest("PyYAML がない")
        text = 'title: "Foo \\"Bar\\" C:\\\\dir"\nauthors: ["Kingma, D."]\ntags: ["税制"]\n'
        self.assertEqual(parse(text), yaml.safe_load(text))


class UnquotedTests(unittest.TestCase):
    def test_reports_unquoted_strings_only(self):
        text = (
            'type: source\ntitle: "x"\ntags: [a, "b"]\nsources:\n  - c\n'
            "created: 2026-11-03\ncount: 3\nflag: true\nempty: []\ngraph:\n  node_id: person:x\n"
        )
        self.assertEqual(unquoted(text), ["type", "tags", "sources"])

    def test_all_quoted_is_clean(self):
        text = 'type: "source"\ntitle: "x"\ntags: ["a", "b"]\ncreated: 2026-11-03\n'
        self.assertEqual(unquoted(text), [])


class FormatTests(unittest.TestCase):
    def test_strings_are_quoted_and_dates_are_not(self):
        self.assertEqual(fmt("消費税"), '"消費税"')
        self.assertEqual(fmt("2026-11-03"), "2026-11-03")
        self.assertEqual(fmt(3), "3")
        self.assertEqual(fmt(["a", "b"]), '["a", "b"]')
        self.assertEqual(fmt([]), "[]")
        self.assertEqual(fmt('a"b\\c'), '"a\\"b\\\\c"')

    def test_roundtrip(self):
        meta = {"title": 'x "y" \\z', "tags": ["a, b", "c"], "created": "2026-11-03"}
        text = WIKI_TEXT.format_frontmatter(meta)
        inner = text.split("---\n")[1]
        self.assertEqual(parse(inner), meta)
        self.assertEqual(unquoted(inner), [])


if __name__ == "__main__":
    unittest.main()
