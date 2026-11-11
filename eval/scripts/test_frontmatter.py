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


class WiderYamlTests(unittest.TestCase):
    """規約にない YAML の書き方も読む（0.9.6）。値は、型に直さない YAML の値と同じ文字列かその配列である。"""

    def test_block_list_with_any_indent(self):
        self.assertEqual(parse('sources:\n- "a"\n- b # コメント\ntags:\n    - "税制"\n'),
                         {"sources": ["a", "b"], "tags": ["税制"]})

    def test_block_list_shape_is_decided_by_the_first_line(self):
        # 値のないキーの次の行が要素でなければ、入れ子か別の形の値である。あとの `- ` の行を要素にしない。
        meta = parse('graph:\n  aliases:\n  - "a"\nsources:\n  junk\n  - b\ntitle: "x"\n')
        self.assertEqual(meta, {"graph": [], "sources": [], "title": "x"})

    def test_multiline_inline_list(self):
        meta = parse('sources: ["a",\n  "b, c",  # コメント\n  d,# コメント\n]\ntags: ["x"]\n')
        self.assertEqual(meta, {"sources": ["a", "b, c", "d"], "tags": ["x"]})

    def test_unclosed_bracket_keeps_the_following_fields(self):
        # ] を書き忘れても、後ろの欄を配列に取り込まない。0.9.5 までと同じ値になる。
        meta = parse('tags: [a,\ntype: "source"\ntitle: X]\n')
        self.assertEqual(meta, {"tags": "[a,", "type": "source", "title": "X]"})
        self.assertEqual(parse("title: [下書き] 題名\n"), {"title": "[下書き] 題名"})

    def test_quoted_keys_and_space_before_colon(self):
        meta = parse('"type": "concept"\n\'kind\' : "x"\ntitle : "T"\n"種類": "概念"\n')
        self.assertEqual(meta, {"type": "concept", "kind": "x", "title": "T", "種類": "概念"})
        self.assertEqual(WIKI_TEXT.top_level_keys('"type": x\nkind : y\n? z\n'), ["type", "kind"])

    def test_yaml_escapes(self):
        meta = parse('title: "a\\tb\\nc \\u6D88\\u8CBB\\u7A0E \\xE9 \\U0001F600 \\uD83D\\uDE00"\n')
        self.assertEqual(meta["title"], "a\tb\nc 消費税 \N{LATIN SMALL LETTER E WITH ACUTE} \U0001F600 \U0001F600")

    def test_value_with_an_unknown_escape_is_read_as_before(self):
        # YAML にないエスケープが 1 つでもある値は、\" と \\ だけを戻す。\n を改行にしない。
        meta = parse('raw: "C:\\Users\\new.pdf"\npattern: "\\d+\\n \\"x\\" \\\\"\nlone: "\\uD83D\\t"\n')
        self.assertEqual(meta, {"raw": "C:\\Users\\new.pdf", "pattern": '\\d+\\n "x" \\', "lone": "\\uD83D\\t"})

    def test_block_scalars(self):
        meta = parse("a: |\n  x\n  y\nb: >\n  長い\n  題名\n\n  次\nc: >-\n  x\n  y\nd: |+\n  x\n\ne: |\nf: z\n")
        self.assertEqual(meta, {"a": "x\ny\n", "b": "長い 題名\n次\n", "c": "x y", "d": "x\n\n", "e": "", "f": "z"})
        self.assertEqual(parse("a: |\n  x"), {"a": "x\n"})   # 最後の欄でも、末尾の改行が付く

    def test_matches_pyyaml_without_type_conversion(self):
        try:
            import yaml
        except ImportError:  # PyYAML がない環境では飛ばす
            self.skipTest("PyYAML がない")
        text = ('"type": concept\ntitle : "a\\tb \\u00e9"\nsources:\n- a\n- "b, c"\ntags: [x,\n  "y"]\n'
                "raw: >-\n  raw/a.md\nnote: |\n  1 行目\n  2 行目\ncount: 007\nflag: true")
        self.assertEqual(parse(text), yaml.load(text + "\n", Loader=yaml.BaseLoader))

    def test_writer_output_reads_back(self):
        meta = {"title": "タブ\tと改行\nと制御 \x07 と絵文字 \U0001F600", "tags": ["a\\b", 'c"d']}
        inner = WIKI_TEXT.format_frontmatter(meta).split("---\n")[1]
        self.assertEqual(parse(inner), meta)


class UnquotedTests(unittest.TestCase):
    def test_new_spellings_are_checked_too(self):
        self.assertEqual(unquoted('sources:\n- "a"\n- b\ntags: ["x",\n  "y"]\n"type": "z"\nnote: >-\n  n\n'),
                         ["sources", "note"])

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
