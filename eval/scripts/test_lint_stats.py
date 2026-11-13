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
        # CLI は SCHEMA.md のないディレクトリを断る（ADR-0019）。SCHEMA.md はページに数えない。
        (self.root / "SCHEMA.md").write_bytes("# スキーマ\n".encode("utf-8"))
        for rel, text in pages.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def lint(self, **kwargs):
        pages = WIKI_LINT.collect_pages(self.root)
        args = dict(soft_cap=100, hard_cap=200, required_fm=["type", "title"], suggest_pages=False, suggest_min=5)
        args.update(kwargs)
        return WIKI_LINT.lint(pages, **args)

    def close(self):
        self.tmp.cleanup()


class PageSizeTests(unittest.TestCase):
    def test_long_japanese_page_is_oversized_by_characters(self):
        # 行数は少ないが、空白を除いて 25,000 文字を超える日本語のページ。
        long_line = "消費税の軽減税率の説明。" * 100
        wiki = WikiFixture({
            "concepts/long.md": page("長い", "\n".join([long_line] * 22)),
            "concepts/medium.md": page("中くらい", "\n".join([long_line] * 10)),
            "concepts/short.md": page("短い", "短い本文。"),
        })
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        hard = {f["path"]: f for f in findings["oversized_hard"]}
        soft = {f["path"]: f for f in findings["oversized_soft"]}
        self.assertIn("concepts/long.md", hard)
        self.assertLess(hard["concepts/long.md"]["lines"], 100)
        self.assertGreater(hard["concepts/long.md"]["chars"], 25000)
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


SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>\n'
MERMAID = "```mermaid\nflowchart LR\n" + "".join(f"  A{i}[ノード{i}] --> A{i + 1}[ノード{i + 1}]\n" for i in range(150)) + "```"


class FigureTests(unittest.TestCase):
    def test_fence_and_image_lines_are_not_counted_for_size(self):
        # mermaid のブロックだけで 150 行を超えるが、本文は短い。
        body = "流れは次のとおりである。\n\n" + MERMAID + "\n\n![流れ図](../assets/a-01.svg)\n\n> Claude Fable 5.1 補足: 流れの図。"
        wiki = WikiFixture({"concepts/a.md": page("図", body), "assets/a-01.svg": SVG})
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        self.assertEqual(findings["oversized_hard"], [])
        self.assertEqual(findings["oversized_soft"], [])
        self.assertEqual(findings["missing_assets"], [])

    def test_strip_fences_keeps_caption_and_text(self):
        text = "前\n~~~~\n中身\n~~~\nまだ中\n~~~~\n後\n![a](x.svg)\n> 補足: 説明\n```js\ncode\n```"
        self.assertEqual(WIKI_TEXT.strip_fences_and_images(text), "前\n後\n> 補足: 説明")

    def test_missing_asset_is_reported_relative_to_page(self):
        wiki = WikiFixture({
            "concepts/ok.md": page("ok", "![図](../assets/ok-01.svg)"),
            # ADR-0008 の不具合の形。wiki のルートを基準に書くと concepts/assets/ を指す。
            "concepts/root-relative.md": page("root", "![図](assets/ok-01.svg)"),
            "concepts/missing.md": page("missing", "![図](../assets/none-01.svg \"題\")"),
            "concepts/outside.md": page("outside", "![図](../../secret.svg)"),
            "concepts/ignored.md": page("ignored", "![外](https://example.com/a.png)\n\n`![コード](x.svg)`\n\n"
                                        "```markdown\n![フェンス](y.svg)\n```"),
            "assets/ok-01.svg": SVG,
            "assets/notes.md": "# assets の中の .md はページとして扱わない\n",
        })
        try:
            findings = wiki.lint()
            stats = subprocess.run([sys.executable, str(SCRIPTS / "wiki_stats.py"), str(wiki.root)],
                                   capture_output=True).stdout.decode("utf-8")
        finally:
            wiki.close()
        by_page = {f["from_path"]: f for f in findings["missing_assets"]}
        self.assertEqual(sorted(by_page), ["concepts/missing.md", "concepts/outside.md", "concepts/root-relative.md"])
        self.assertEqual(by_page["concepts/root-relative.md"]["suggest"], "../assets/ok-01.svg")
        self.assertEqual(by_page["concepts/missing.md"]["target"], "../assets/none-01.svg")
        self.assertTrue(by_page["concepts/outside.md"]["outside"])
        self.assertEqual(findings["summary"]["missing_assets"], 3)
        self.assertNotIn("assets/notes.md", [p["path"] for p in findings["orphans"]])
        self.assertEqual(findings["summary"]["total_pages"], 5)
        # 統計も wiki/assets/ の中をページとして数えない。
        self.assertFalse([line for line in stats.splitlines() if line.startswith("  assets")])
        self.assertIn("concepts", stats)

    def test_fences_inside_list_and_quote(self):
        text = "- ```text\n  中身\n  ```\n\n後の本文\n\n> ```text\n> ![例](missing.svg)\n> ```\n\n最後"
        self.assertEqual(WIKI_TEXT.strip_fences_and_images(text), "\n後の本文\n\n\n最後")
        # 閉じのあとに全角の空白があれば閉じとみなさない（CommonMark は空白とタブだけを許す）。
        self.assertEqual(WIKI_TEXT.lines_outside_fences("```\na\n```　\nb\n```\nc"), ["c"])

    def test_container_fence_ends_with_its_container(self):
        # 引用の中で閉じずに終わったフェンスは、引用が終わったところで閉じる。
        self.assertEqual(WIKI_TEXT.lines_outside_fences("> ```text\n> example\n\nOrdinary prose"),
                         ["", "Ordinary prose"])
        # リストの項目の中のフェンスは、項目より浅い行で閉じる。
        self.assertEqual(WIKI_TEXT.lines_outside_fences("- ```\n  code\n本文"), ["本文"])

    def test_image_syntax_variants(self):
        found = lambda s: [WIKI_TEXT.image_target(m) for m in WIKI_TEXT.IMAGE_RE.finditer(s)]
        self.assertEqual(found("![a](../assets/a(1).svg)"), ["../assets/a(1).svg"])
        self.assertEqual(found("![a](<../assets/a b.svg>)"), ["../assets/a b.svg"])
        self.assertEqual(found("![状態 [A]](../assets/x.svg)"), ["../assets/x.svg"])
        self.assertEqual(found("![a](../assets/x.svg '題')"), ["../assets/x.svg"])
        self.assertEqual(found(r"\![a](../assets/x.svg)"), [])

    def test_missing_assets_edge_cases(self):
        wiki = WikiFixture({
            "concepts/paren.md": page("paren", "![図](../assets/a(1).svg)"),
            "concepts/spaced.md": page("spaced", "![図](<../assets/a b.svg>)"),
            "concepts/quoted-dir.md": page("q", "- ```text\n  x\n  ```\n\n![図](../assets/none.svg)"),
            "concepts/codespan.md": page("c", "``foo ` ![例](missing.svg) ``\n\n![alt]`gap`(missing2.svg)"),
            "concepts/hash.md": page("h", "![図](assets/a%23b.svg)"),
            "assets/a(1).svg": SVG,
            "assets/a b.svg": SVG,
            "assets/a#b.svg": SVG,
        })
        outside = wiki.root.parent / f"{wiki.root.name}-secret.svg"
        outside.write_text(SVG, encoding="utf-8")
        (wiki.root / "concepts" / "secret.md").write_text(
            page("s", f"![図](../{wiki.root.name}-secret.svg)"), encoding="utf-8")
        try:
            findings = wiki.lint()
        finally:
            outside.unlink()
            wiki.close()
        by_page = {f["from_path"]: f for f in findings["missing_assets"]}
        self.assertEqual(sorted(by_page), ["concepts/hash.md", "concepts/quoted-dir.md", "concepts/secret.md"])
        self.assertEqual(by_page["concepts/quoted-dir.md"]["target"], "../assets/none.svg")
        # 示す相対パスは、そのまま書けば同じファイルに解決する形にする。
        self.assertEqual(by_page["concepts/hash.md"]["suggest"], "../assets/a%23b.svg")
        # ページからは wiki の中の存在しないファイルを指し、wiki のルートから見ると wiki の外のファイルになる。
        # wiki の外のファイルは、正しい相対パスとして示さない。
        self.assertNotIn("suggest", by_page["concepts/secret.md"])
        self.assertNotIn("outside", by_page["concepts/secret.md"])

    def test_escaped_destination_and_invalid_path(self):
        wiki = WikiFixture({
            "concepts/escaped.md": page("e", r"![図](../assets/a\(1\).svg)"),
            "concepts/nul.md": page("n", "![図](../assets/bad%00.svg)"),
            "concepts/other.md": page("o", "![図](../assets/none.svg)"),
            "assets/a(1).svg": SVG,
        })
        try:
            findings = wiki.lint()
        finally:
            wiki.close()
        by_page = {f["from_path"]: f for f in findings["missing_assets"]}
        # escape した括弧の参照先は解決する。パスにできない参照先があっても、ほかのページの指摘は出る。
        self.assertEqual(sorted(by_page), ["concepts/nul.md", "concepts/other.md"])
        self.assertTrue(by_page["concepts/nul.md"]["invalid"])

    def test_missing_assets_in_text_report(self):
        wiki = WikiFixture({"concepts/a.md": page("a", "![図](assets/a-01.svg)"), "assets/a-01.svg": SVG})
        try:
            text = subprocess.run([sys.executable, str(SCRIPTS / "wiki_lint.py"), str(wiki.root)],
                                  capture_output=True).stdout.decode("utf-8")
        finally:
            wiki.close()
        self.assertIn("画像の参照先のファイルがない", text)
        self.assertIn("../assets/a-01.svg と書けば解決する", text)


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
