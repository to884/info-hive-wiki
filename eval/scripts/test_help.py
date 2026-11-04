#!/usr/bin/env python3
"""wiki_help.py（/wiki:help の一覧）のテスト。"""

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


WIKI_HELP = load("wiki_help")


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root, ".claude-plugin/plugin.json", '{"name": "wiki"}\n')
        # 引数あり。引数の説明もある。
        write(self.root, "commands/query.md",
              '---\ndescription: 質問に答える。\nargument-hint: "<質問>"\n'
              'argument-description: "wiki に問い合わせる質問。"\n---\n\n本文。\n')
        # 引数なし。
        write(self.root, "commands/stats.md", "---\ndescription: 規模を表示する。\n---\n\n本文。\n")
        # 引数はあるが、説明がない（警告の対象）。固定順にない名前なので、後ろに並ぶ。
        write(self.root, "commands/zeta.md",
              '---\ndescription: 後ろに並ぶ。\nargument-hint: "<x>"\n---\n\n本文。\n')
        # 固定順にない名前。zeta より前（名前順）。
        write(self.root, "commands/alpha.md", "---\ndescription: 固定順の後ろの先頭。\n---\n\n本文。\n")
        # frontmatter がない（飛ばす）。
        write(self.root, "commands/broken.md", "frontmatter がない。\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_is_three_line_bullets_in_workflow_order(self):
        commands, warnings = WIKI_HELP.collect_commands(self.root, "wiki")
        self.assertEqual([c["name"] for c in commands], ["query", "stats", "alpha", "zeta"])
        self.assertEqual(
            WIKI_HELP.render(commands),
            "- /wiki:query: <質問>\n"
            "  - 質問に答える。\n"
            "  - wiki に問い合わせる質問。\n"
            "- /wiki:stats\n"
            "  - 規模を表示する。\n"
            "  - 引数はない。\n"
            "- /wiki:alpha\n"
            "  - 固定順の後ろの先頭。\n"
            "  - 引数はない。\n"
            "- /wiki:zeta: <x>\n"
            "  - 後ろに並ぶ。\n"
            "  - （引数の説明は未記入）\n",
        )
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("broken.md" in w and "frontmatter" in w for w in warnings))
        self.assertTrue(any("zeta.md" in w and "argument-description" in w for w in warnings))

    def test_plugin_name_comes_from_manifest(self):
        self.assertEqual(WIKI_HELP.plugin_name(self.root), "wiki")
        write(self.root, ".claude-plugin/plugin.json", '{"name": "other"}\n')
        self.assertEqual(WIKI_HELP.plugin_name(self.root), "other")
        (self.root / ".claude-plugin" / "plugin.json").unlink()
        self.assertEqual(WIKI_HELP.plugin_name(self.root), "wiki")

    def test_missing_commands_dir_returns_warning(self):
        commands, warnings = WIKI_HELP.collect_commands(self.root / "nowhere", "wiki")
        self.assertEqual(commands, [])
        self.assertEqual(len(warnings), 1)


class RealCommandsTests(unittest.TestCase):
    """実際の commands/ に対して、スクリプトとして実行する。"""

    EXPECTED = ["init", "ingest", "query", "lint", "stats", "graph", "learn", "evolve", "upgrade", "help"]

    def run_script(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "wiki_help.py"), str(REPO), *extra],
            capture_output=True, encoding="utf-8", check=False,
        )

    def test_all_commands_are_listed_without_warnings(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        heads = [line for line in result.stdout.splitlines() if line.startswith("- /wiki:")]
        self.assertEqual([h.split(":")[1].split(" ")[0] for h in heads], self.EXPECTED)
        # どのコマンドも 3 行（名前、説明、引数の説明）になっている。
        self.assertEqual(len(result.stdout.splitlines()), 3 * len(self.EXPECTED))

    def test_json_has_argument_description_for_every_hint(self):
        result = self.run_script("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = json.loads(result.stdout)
        self.assertEqual([c["name"] for c in commands], self.EXPECTED)
        for c in commands:
            if c["argument_hint"]:
                self.assertTrue(c["argument_description"], f"{c['name']} に argument-description がない")
            else:
                self.assertIsNone(c["argument_description"], f"{c['name']} は引数がないのに説明がある")


if __name__ == "__main__":
    unittest.main()
