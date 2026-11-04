#!/usr/bin/env python3
"""init_wiki.py の基準のディレクトリの扱いのテスト。

初回のユーザースコープの構成（~/info-hive）では基準のディレクトリがまだないので、親があれば作る。
親もないときと、アップグレードのときは止める。実行環境のインストールは差し替えて飛ばす。
"""

import contextlib
import importlib.util
import io
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


INIT_WIKI = load("init_wiki")


class BaseDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.installed = []
        # uv による実行環境のインストールは、このテストの対象ではないので飛ばす。
        self._orig = INIT_WIKI.install_runtime
        INIT_WIKI.install_runtime = lambda wiki: self.installed.append(wiki)

    def tearDown(self):
        INIT_WIKI.install_runtime = self._orig
        self.tmp.cleanup()

    def run_init(self, base, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            INIT_WIKI.init_wiki(base, "wiki", "raw", **kwargs)
        return out.getvalue()

    def test_missing_base_with_existing_parent_is_created(self):
        base = self.root / "info-hive"
        out = self.run_init(base)
        self.assertTrue((base / "wiki" / "SCHEMA.md").is_file())
        self.assertTrue((base / "raw" / "assets").is_dir())
        self.assertIn("基準のディレクトリがなかったので作った", out)
        self.assertEqual(self.installed, [base.resolve() / "wiki"])

    def test_missing_parent_stops(self):
        base = self.root / "nope" / "info-hive"
        with self.assertRaises(SystemExit) as cm:
            self.run_init(base)
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(base.exists())
        self.assertEqual(self.installed, [])

    def test_upgrade_requires_existing_base(self):
        base = self.root / "info-hive"
        with self.assertRaises(SystemExit) as cm:
            self.run_init(base, upgrade=True)
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(base.exists())

    def test_existing_base_is_not_reported_as_created(self):
        base = self.root / "project"
        base.mkdir()
        out = self.run_init(base)
        self.assertNotIn("基準のディレクトリがなかったので作った", out)
        self.assertTrue((base / "wiki" / "index.md").is_file())


if __name__ == "__main__":
    unittest.main()
