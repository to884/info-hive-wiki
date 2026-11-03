#!/usr/bin/env python3
"""wiki_encoding.py（取り込む前に文字コードを調べて UTF-8 に変換する）のテスト。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "skills" / "info-hive" / "scripts" / "wiki_encoding.py"
SPEC = importlib.util.spec_from_file_location("wiki_encoding", SCRIPT)
ENC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENC)

TEXT = "消費税の軽減税率は、飲食料品の譲渡に適用される。\r\n外食は対象外である。①〜③の例を示す。\n"


class DetectTests(unittest.TestCase):
    def test_utf8_and_ascii(self):
        self.assertEqual(ENC.detect(TEXT.encode("utf-8"))["encoding"], "utf-8")
        self.assertEqual(ENC.detect(b"plain ascii\n")["encoding"], "utf-8")

    def test_boms(self):
        self.assertEqual(ENC.detect(TEXT.encode("utf-8-sig"))["encoding"], "utf-8-sig")
        self.assertEqual(ENC.detect(TEXT.encode("utf-16"))["encoding"][:6], "utf-16")

    def test_shift_jis_with_windows_extensions(self):
        # 「①」は Shift_JIS にはなく、Windows の CP932 で加わった文字である。
        result = ENC.detect(TEXT.encode("cp932"))
        self.assertEqual(result["encoding"], "cp932")

    def test_euc_jp(self):
        text = "消費税の軽減税率は、飲食料品の譲渡に適用される。外食は対象外である。\n"
        self.assertEqual(ENC.detect(text.encode("euc_jp"))["encoding"], "euc_jp")

    def test_iso2022_jp(self):
        text = "消費税の軽減税率\n"
        self.assertEqual(ENC.detect(text.encode("iso2022_jp"))["encoding"], "iso2022_jp")

    def test_binary_and_unknown(self):
        self.assertEqual(ENC.detect(b"\x89PNG\r\n\x1a\n\x00\x00")["encoding"], "binary")
        # CP932 ではエラーなく読めてしまうが、日本語のテキストらしくないバイト列。
        self.assertEqual(ENC.detect(bytes(range(0x80, 0x100)))["encoding"], "unknown")


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True)

    def test_converts_to_utf8_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "memo.txt"
            source.write_bytes(TEXT.encode("cp932"))
            output = Path(tmp) / "memo.utf8.txt"
            proc = self.run_cli(source, "--output", output)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertEqual(json.loads(proc.stdout.decode("utf-8"))["encoding"], "cp932")
            # 改行コード（CRLF と LF の混在）もそのまま残す。CP932 の「〜」は「～」（U+FF5E）になる。
            self.assertEqual(output.read_bytes(), TEXT.replace("〜", "～").encode("utf-8"))
            self.assertEqual(source.read_bytes(), TEXT.encode("cp932"))

    def test_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "memo.txt"
            source.write_bytes(TEXT.encode("cp932"))
            self.assertEqual(self.run_cli(source, "--output", source).returncode, 1)
            existing = Path(tmp) / "exists.txt"
            existing.write_text("keep", encoding="utf-8")
            self.assertEqual(self.run_cli(source, "--output", existing).returncode, 1)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            self.assertEqual(self.run_cli(source, "--output", existing, "--force").returncode, 0)

    def test_unknown_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "image.bin"
            source.write_bytes(b"\x00\x01\x02")
            self.assertEqual(self.run_cli(source).returncode, 2)


if __name__ == "__main__":
    unittest.main()
