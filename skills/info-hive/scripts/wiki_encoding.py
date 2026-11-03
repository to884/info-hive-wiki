#!/usr/bin/env python3
"""
wiki_encoding.py — テキストのソースの文字コードを調べ、必要なら UTF-8 に変換する。

日本語のテキストのソースは、Shift_JIS（Windows の CP932）、EUC-JP、ISO-2022-JP で
書かれていることがある。UTF-8 だと思って読むと文字化けするので、取り込む前にこのスクリプトで
文字コードを確かめる。元のファイルは書き換えない。--output を付けると、UTF-8（BOM なし）に
変換したものを別のファイルに書き出す。改行コードは元のファイルのまま残す。

判定の順序:
    1. BOM があれば、それに従う（UTF-8、UTF-16、UTF-32）。
    2. エスケープシーケンス（ESC $ B など）を含み、ISO-2022-JP として読めれば ISO-2022-JP。
    3. UTF-8 として読めれば UTF-8（ASCII だけのファイルも UTF-8 とみなす）。
    4. CP932 と EUC-JP の両方を試す。片方でしか読めなければそれにする。両方で読めるときは、
       かな・漢字・ASCII の割合が高いほうにし、ambiguous を true にする。
    5. どれでも読めないか、読めても日本語のテキストらしくなければ unknown（終了コード 2）。
       NUL を含むファイルは binary（終了コード 2）。

CP932 の「〜」（波ダッシュ）は、UTF-8 では「～」（全角チルダ、U+FF5E）になる。CP932 の対応表どおりの
変換で、Windows で作ったファイルを Windows で読んだときと同じ文字になる。

使い方:
    python wiki_encoding.py <file>                       文字コードを JSON で出力する
    python wiki_encoding.py <file> --output <utf8-file>  UTF-8 に変換して書き出す

終了コード: 0 = 読めた（変換した）、2 = 文字コードを判定できない、1 = そのほかのエラー。
"""

from __future__ import annotations

import argparse
import codecs
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import localize_argparse  # noqa: E402

BOMS = [
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
]

# 両方の文字コードで読めたときに比べる候補。CP932 は Shift_JIS に Windows の拡張文字を足したもの。
LEGACY_CANDIDATES = ("cp932", "euc_jp")
# CP932 は、ほとんどどんなバイト列でもエラーなく読めてしまう。読めた結果のうち、かな・漢字・
# ASCII などの割合がこれより低ければ、日本語のテキストではないとみなす。
MIN_JAPANESE_RATIO = 0.8


def _japanese_ratio(text: str) -> float:
    """かな・漢字・全角の記号と、表示できる ASCII の文字が占める割合。"""
    if not text:
        return 0.0
    good = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x20 <= cp < 0x7F
            or ch in "\t\r\n"
            or 0x3000 <= cp <= 0x30FF  # 和文の記号、ひらがな、カタカナ
            or 0x4E00 <= cp <= 0x9FFF  # CJK 統合漢字
            or 0xFF01 <= cp <= 0xFF9F  # 全角の英数字と記号、半角カタカナ
        ):
            good += 1
    return good / len(text)


def detect(data: bytes) -> dict:
    """バイト列の文字コードを判定する。{encoding, bom, ambiguous} を返す。

    encoding は Python の codecs の名前。判定できなければ "unknown"、バイナリなら "binary" にする。
    """
    for bom, name in BOMS:
        if data.startswith(bom):
            return {"encoding": name, "bom": True, "ambiguous": False}
    if b"\x00" in data:
        return {"encoding": "binary", "bom": False, "ambiguous": False}
    if b"\x1b" in data:
        try:
            data.decode("iso2022_jp")
            return {"encoding": "iso2022_jp", "bom": False, "ambiguous": False}
        except UnicodeDecodeError:
            pass
    try:
        data.decode("utf-8")
        return {"encoding": "utf-8", "bom": False, "ambiguous": False}
    except UnicodeDecodeError:
        pass
    decoded = {}
    for name in LEGACY_CANDIDATES:
        try:
            decoded[name] = data.decode(name)
        except UnicodeDecodeError:
            continue
    if not decoded:
        return {"encoding": "unknown", "bom": False, "ambiguous": False}
    best = max(decoded, key=lambda name: _japanese_ratio(decoded[name]))
    if _japanese_ratio(decoded[best]) < MIN_JAPANESE_RATIO:
        return {"encoding": "unknown", "bom": False, "ambiguous": False}
    return {"encoding": best, "bom": False, "ambiguous": len(decoded) > 1}


def convert(source: Path, output: Path, encoding: str) -> None:
    """source を encoding で読み、UTF-8（BOM なし）で output に書く。改行コードはそのまま残す。"""
    text = source.read_bytes().decode(encoding)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    parser.add_argument("file", type=Path, help="調べるテキストファイル")
    parser.add_argument("--output", type=Path, help="UTF-8 に変換したものを書き出すパス。元のファイルとは別のパスにする")
    parser.add_argument("--force", action="store_true", help="--output のファイルがすでにあれば上書きする")
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"ファイルが見つからない: {args.file}", file=sys.stderr)
        return 1
    result = {"path": args.file.as_posix(), **detect(args.file.read_bytes())}
    if result["encoding"] in ("unknown", "binary"):
        print(json.dumps(result, ensure_ascii=False))
        reason = "バイナリのファイルである" if result["encoding"] == "binary" else "文字コードを判定できない"
        print(f"{reason}: {args.file}", file=sys.stderr)
        return 2
    if args.output:
        if args.output.resolve() == args.file.resolve():
            print("--output には元のファイルとは別のパスを指定する。元のファイルは書き換えない。", file=sys.stderr)
            return 1
        if args.output.exists() and not args.force:
            print(f"書き出し先がすでにある: {args.output}（上書きするには --force を付ける）", file=sys.stderr)
            return 1
        convert(args.file, args.output, result["encoding"])
        result["output"] = args.output.as_posix()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
