#!/usr/bin/env python3
"""lint・統計・評価ループの、日本語の wiki に合わせた動作のテスト（計画の P4）。"""

import importlib.util
import json
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


WIKI_LINT = load("wiki_lint")
WIKI_TEXT = load("wiki_text")


def page(title, body, created="2026-11-01", updated="2026-11-02", extra=""):
    return (
        f'---\ntype: "concept"\ntitle: "{title}"\ntags: ["テスト"]\n'
        f"created: {created}\nupdated: {updated}\n{extra}---\n\n{body}\n"
    )


class WikiFixture:
    def __init__(self, pages):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel, text in pages.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def lint(self, **kwargs):
        pages = WIKI_LINT.collect_pages(self.root)
        args = dict(soft_cap=400, hard_cap=800, required_fm=["type", "title"], suggest_pages=False, suggest_min=5)
        args.update(kwargs)
        return WIKI_LINT.lint(pages, **args)

    def close(self):
        self.tmp.cleanup()


class PageSizeTests(unittest.TestCase):
    def test_long_japanese_page_is_oversized_by_characters(self):
        # 行数は少ないが、空白を除いて 8,000 文字を超える日本語のページ。
        long_line = "消費税の軽減税率の説明。" * 100
        wiki = WikiFixture({
            "concepts/long.md": page("長い", "\n".join([long_line] * 7)),
            "concepts/medium.md": page("中くらい", "\n".join([long_line] * 4)),
            "concepts/short.md": page("短い", "短い本文。"),
        })
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        hard = {f["path"]: f for f in findings["oversized_hard"]}
        soft = {f["path"]: f for f in findings["oversized_soft"]}
        self.assertIn("concepts/long.md", hard)
        self.assertLess(hard["concepts/long.md"]["lines"], 400)
        self.assertGreater(hard["concepts/long.md"]["chars"], 8000)
        self.assertIn("concepts/medium.md", soft)
        self.assertNotIn("concepts/short.md", hard | soft)

    def test_char_count_ignores_whitespace(self):
        self.assertEqual(WIKI_TEXT.char_count("あ い\nう\tえ　お"), 5)

    def test_line_caps_still_apply(self):
        wiki = WikiFixture({"concepts/lines.md": page("行", "\n".join(["a"] * 900))})
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        self.assertEqual([f["path"] for f in findings["oversized_hard"]], ["concepts/lines.md"])


class DateTests(unittest.TestCase):
    def test_non_iso_dates_are_reported(self):
        wiki = WikiFixture({
            "concepts/a.md": page("和暦", "本文", created="2026年11月1日"),
            "concepts/b.md": page("スラッシュ", "本文", updated="2026/11/02"),
            "concepts/c.md": page("存在しない日", "本文", updated="2026-02-30"),
            "concepts/d.md": page("正しい", "本文"),
        })
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        got = {(f["path"], f["key"]) for f in findings["non_iso_dates"]}
        self.assertEqual(got, {
            ("concepts/a.md", "created"),
            ("concepts/b.md", "updated"),
            ("concepts/c.md", "updated"),
        })

    def test_japanese_date_key_is_checked(self):
        text = "---\n種類: 概念\nタイトル: 日本語のキー\n作成日: 令和8年11月1日\n---\n\n本文\n"
        wiki = WikiFixture({"concepts/ja.md": text})
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        self.assertEqual([f["key"] for f in findings["non_iso_dates"]], ["created"])


class QuotingTests(unittest.TestCase):
    def test_unquoted_strings_are_reported(self):
        wiki = WikiFixture({
            "concepts/a.md": "---\ntype: concept\ntitle: 引用符なし\ntags: [a]\ncreated: 2026-11-01\nupdated: 2026-11-01\n---\n\n本文\n",
            "concepts/b.md": page("引用符あり", "本文"),
        })
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        self.assertEqual(findings["unquoted_strings"], [{"path": "concepts/a.md", "keys": ["type", "title", "tags"]}])
        self.assertEqual(findings["summary"]["unquoted_strings"], 1)

    def test_quoted_comma_and_comment_are_parsed(self):
        wiki = WikiFixture({
            "sources/a.md": page("出典", "本文", extra='authors: ["Kingma, D.", "Ba, J."]\nurl: "https://x.test/?a=1#f" # 元のページ\n'),
        })
        try:
            pages = WIKI_LINT.collect_pages(wiki.root)
        finally:
            wiki.close()
        meta = pages[0]["meta"]
        self.assertEqual(meta["authors"], ["Kingma, D.", "Ba, J."])
        self.assertEqual(meta["url"], "https://x.test/?a=1#f")


class SuggestPagesTests(unittest.TestCase):
    def test_katakana_and_kanji_compounds_are_suggested(self):
        body = "インボイス制度では登録番号が必要になる。ﾌｧｲﾙの保存方法も決める。"
        pages = {f"concepts/p{i}.md": page(f"ページ{i}", body) for i in range(5)}
        pages["concepts/registration.md"] = page("登録番号", "専用のページ。")
        wiki = WikiFixture(pages)
        try:
            findings = wiki.lint(suggest_pages=True)
        finally:
            wiki.close()
        phrases = {f["phrase"] for f in findings["suggested_pages"]}
        self.assertIn("インボイス", phrases)
        self.assertIn("ファイル", phrases)  # 半角のカタカナも全角にそろえて数える
        self.assertNotIn("登録番号", phrases)  # 同じ題名のページがある
        self.assertNotIn("必要", phrases)  # 2 文字の漢字の並びは候補にしない
        self.assertNotIn("方法", phrases)


class TextReportTests(unittest.TestCase):
    def test_report_is_japanese_and_json_keys_stay_english(self):
        wiki = WikiFixture({"concepts/a.md": page("和暦", "本文", created="2026年11月1日")})
        try:
            text = subprocess.run(
                [sys.executable, str(SCRIPTS / "wiki_lint.py"), str(wiki.root)],
                capture_output=True,
            ).stdout.decode("utf-8")
            data = json.loads(subprocess.run(
                [sys.executable, str(SCRIPTS / "wiki_lint.py"), str(wiki.root), "--json"],
                capture_output=True,
            ).stdout.decode("utf-8"))
        finally:
            wiki.close()
        self.assertIn("日付が YYYY-MM-DD の形になっていない", text)
        self.assertIn("non_iso_dates", data["summary"])

    def test_stats_reports_characters_and_aligns_fullwidth(self):
        wiki = WikiFixture({
            "概念/消費税.md": page("消費税", "消費税は間接税である。"),
            "concepts/a.md": page("A", "abc"),
        })
        try:
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "wiki_stats.py"), str(wiki.root)],
                capture_output=True,
            ).stdout.decode("utf-8")
        finally:
            wiki.close()
        self.assertIn("総文字数", out)
        rows = [line for line in out.splitlines() if line.startswith("  概念") or line.startswith("  concepts")]
        self.assertEqual(len(rows), 2)
        # 全角の「概念」を 2 桁と数えるので、件数の列の位置が表示上そろう。
        widths = {WIKI_TEXT.display_width(row.rsplit(" ", 1)[0]) for row in rows}
        self.assertEqual(len(widths), 1)


class DisplayWidthTests(unittest.TestCase):
    def test_fullwidth_counts_as_two(self):
        self.assertEqual(WIKI_TEXT.display_width("検索abc"), 7)
        self.assertEqual(WIKI_TEXT.display_width("ＡＩ"), 4)
        self.assertEqual(WIKI_TEXT.ljust_display("検索", 6), "検索  ")


class GroundedVerdictTests(unittest.TestCase):
    def test_quote_across_japanese_line_break_is_grounded(self):
        loop = load("wiki_evolve_loop")
        sources = {"source.md": "消費税は、国内の取引に広く課される\n間接税である。"}
        judge = {"passed": True, "reason": "根拠あり",
                 "evidence": [{"source": "source.md", "quote": "広く課される間接税である"}]}
        self.assertTrue(loop.grounded_verdict(judge, sources, True))

    def test_spacing_that_changes_meaning_is_not_grounded(self):
        loop = load("wiki_evolve_loop")
        sources = {"source.md": "The answer is nowhere to be found."}
        judge = {"passed": True, "reason": "r", "evidence": [{"source": "source.md", "quote": "now here"}]}
        self.assertFalse(loop.grounded_verdict(judge, sources, True))

    def test_answer_normalization_matches_d2(self):
        evolve = load("wiki_evolve")
        self.assertIn(evolve.normalize("広く課される間接税"), evolve.normalize("広く課される\n間接税である"))
        self.assertNotIn(evolve.normalize("now here"), evolve.normalize("nowhere"))


if __name__ == "__main__":
    unittest.main()
