#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["pdfminer.six==20260107", "pypdfium2==5.15.2"]
# ///
"""
wiki_pdf.py — PDF の文字の層を、ページごとに判定して取り出す（ADR-0025）。

rag を使わずに PDF を取り込むとき、手順 2 で使う。`uv run --script` で動かす。取り出しと判定は
wiki_pdf_layer.py（rag の rag_pdf_layer.py と同じ中身）が行う。取り出した本文の写しは作らない。

使い方:
    uv run --script wiki_pdf.py <PDF> [--max-chars 20000] [--from <続きの位置>]
    uv run --script wiki_pdf.py <PDF> --json [--pages 1-20] [--list-from <位置>]

本文の出力:
    ページごとに `## p. <番号>`、空行、ページの本文を並べ、ページのあいだを空行（\\n\\n）でつなぐ。
    判定が vertical のページは本文の前に 1 行の注意を付け、garbled と scan のページは本文を出さずに
    1 行の説明だけを出し、blank のページは見出しだけを出す。
    1 回の出力は --max-chars 文字までである。続きがあれば、標準エラーに続きの位置を書く。
    続きの位置は `<PDF の SHA-256 の先頭 12 文字>:<ページ>:<ページの本文の中の文字の位置>` で、
    --from に渡すと、その位置から本文だけを続ける（見出しと注意の 1 行は出し直さない）。
    分けて読んだ出力をつなぐと、一度に出した出力と同じになる。PDF が変わっていたら止まる。

--json:
    全体の要約（PDF の SHA-256、ページの数、判定ごとのページの数、確かめるべきページの数、抽出の識別、
    ページの一覧）を返す。ページの一覧は、続いた番号をまとめた範囲で、範囲の数が 200 を超えると
    先頭の 200 個と、続きを取る位置（next）を返す。--list-from <next> で続きを取る。
    --pages を付けると、その範囲のページの詳細（判定、判定の数値、本文の崩れの数、本文の SHA-256）も返す。

終了コード: 0 は成功、1 は PDF が読めないなど、2 は引数の誤りか、続きの位置が今の PDF と合わないとき。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOTES = {
    "vertical": "（このページは縦書きと判定した。列の順が崩れていることがある）",
    "garbled": "（このページは、PDFium が文字を復号できなかった。本文は信頼できないので出さない）",
    "scan": "（このページはスキャンの画像と判定した。文字の層の本文はない）",
}
DEFAULT_MAX_CHARS = 20000


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def load_layer():
    try:
        import pdfminer  # noqa: F401
        import pypdfium2  # noqa: F401
    except ImportError:
        fail("pdfminer.six と pypdfium2 がない。`uv run --script wiki_pdf.py ...` で動かす（uv が要る）。")
    import wiki_pdf_layer
    return wiki_pdf_layer


def header(page: dict) -> str:
    """ページの見出しと、本文の前に置く 1 行。本文の中の位置は、この後ろから数える。"""
    separator = "" if page["page"] == 1 else "\n\n"
    note = NOTES.get(page["kind"], "")
    if page["kind"] == "vertical":
        note += "\n"
    return f"{separator}## p. {page['page']}\n\n{note}"


def body(page: dict) -> str:
    return page["text"] if page["kind"] in ("text", "vertical") else ""


def parse_token(token: str, digest: str) -> tuple[int, int]:
    parts = token.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        fail(f"続きの位置の形が違う: {token}（<SHA-256 の先頭 12 文字>:<ページ>:<位置>）", 2)
    if parts[0] != digest[:12]:
        fail(f"PDF が、続きの位置を出したときと違う（位置 {parts[0]}、今の PDF {digest[:12]}）。最初から読み直す。", 2)
    page, offset = int(parts[1]), int(parts[2])
    if page < 1:
        fail("続きの位置のページは 1 以上にする", 2)
    return page, offset


def render(layer, data: bytes, digest: str, max_chars: int, start_page: int, start_offset: int) -> tuple[str, str | None]:
    """出力する文字列と、続きの位置（なければ None）を返す。"""
    out: list[str] = []
    used = 0
    offset = start_offset
    count = layer.pdfium_page_count(data)
    if start_page > count:
        fail(f"続きの位置のページ（{start_page}）が、PDF のページの数（{count}）を超えている", 2)
    for page in layer.analyze_pages(data, start=start_page):
        number = page["page"]
        text = body(page)
        if offset > len(text):
            fail(f"続きの位置（{number} ページの {offset} 文字目）が、ページの本文の長さ（{len(text)}）を超えている。最初から読み直す。", 2)
        added_header = False
        if offset == 0:
            head = header(page)
            if used and used + len(head) > max_chars:
                return "".join(out), f"{digest[:12]}:{number}:0"
            out.append(head)
            used += len(head)
            added_header = True
        rest = text[offset:]
        room = max_chars - used
        if not rest or len(rest) <= room:
            out.append(rest)
            used += len(rest)
            offset = 0
            continue
        if room < 1 and added_header and len(out) > 1:
            # 見出しだけを出して本文を 1 文字も出せないなら、見出しの前で分ける
            out.pop()
            return "".join(out), f"{digest[:12]}:{number}:0"
        cut = max(1, room)
        newline = rest.rfind("\n", 0, cut)
        if newline + 1 > cut // 2:
            cut = newline + 1
        out.append(rest[:cut])
        return "".join(out), f"{digest[:12]}:{number}:{offset + cut}"
    return "".join(out), None


def parse_pages(value: str, count: int) -> list[int]:
    found: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            low, _, high = part.partition("-")
            if not (low.isdigit() and high.isdigit()):
                fail(f"--pages の形が違う: {value}", 2)
            found.update(range(int(low), int(high) + 1))
        elif part.isdigit():
            found.add(int(part))
        else:
            fail(f"--pages の形が違う: {value}", 2)
    if not found or min(found) < 1 or max(found) > count:
        fail(f"--pages は 1〜{count} の範囲にする", 2)
    return sorted(found)


def summary(layer, data: bytes, digest: str, path: Path, pages_arg: str | None, list_from: int) -> dict:
    pages = list(layer.analyze_pages(data))
    count = len(pages)
    kinds = {kind: [p["page"] for p in pages if p["kind"] == kind] for kind in layer.KINDS}
    check = [p["page"] for p in pages if p["needs_check"]]
    result = {
        "pdf": str(path),
        "sha256": digest,
        "pages": count,
        "kinds": {kind: len(numbers) for kind, numbers in kinds.items()},
        "needs_check": len(check),
        "extraction": layer.extraction_identity(),
        "list_from": list_from,
        "lists": {**{f"kind:{kind}": layer.page_list(numbers, list_from) for kind, numbers in kinds.items()},
                  "needs_check": layer.page_list(check, list_from)},
    }
    if pages_arg:
        chosen = set(parse_pages(pages_arg, count))
        result["page_details"] = [
            {"page": p["page"], "kind": p["kind"],
             "quality": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in p["quality"].items()},
             "garble": p["garble"], "needs_check": p["needs_check"], "text_sha256": p["text_sha256"]}
            for p in pages if p["page"] in chosen]
    return result


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="PDF の文字の層を、ページごとに判定して取り出す")
    parser.add_argument("pdf")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--from", dest="start", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pages", default=None)
    parser.add_argument("--list-from", type=int, default=0)
    args = parser.parse_args(argv)
    if args.max_chars < 1:
        fail("--max-chars は 1 以上にする", 2)
    if args.list_from < 0:
        fail("--list-from は 0 以上にする", 2)
    if args.json and args.start:
        fail("--json と --from は一緒に使わない", 2)
    if (args.pages or args.list_from) and not args.json:
        fail("--pages と --list-from は --json と一緒に使う", 2)
    layer = load_layer()
    path = Path(args.pdf)
    try:
        data = path.read_bytes()
    except OSError as error:
        fail(f"PDF を読めない: {error}")
    digest = hashlib.sha256(data).hexdigest()
    try:
        if args.json:
            print(json.dumps(summary(layer, data, digest, path, args.pages, args.list_from), ensure_ascii=False, indent=1))
            return 0
        page, offset = parse_token(args.start, digest) if args.start else (1, 0)
        text, following = render(layer, data, digest, args.max_chars, page, offset)
    except SystemExit:
        raise
    except Exception as error:  # pdfminer と PDFium の例外は種類が多いので、まとめて伝える
        fail(f"PDF を取り出せない: {type(error).__name__}: {error}")
    sys.stdout.write(text)
    sys.stdout.flush()
    if following:
        print(f"\n続きがある。続きは --from {following} で読む。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
