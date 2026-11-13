#!/usr/bin/env python3
"""
wiki_rawcheck.py — Web ページの raw を、取得した直後の内容と照らして検査する。

/wiki:ingest に URL を渡したとき、モデルは wiki_fetch.py が書いた raw を書き換える（雑音の削除、
訳、画像の補足）。このスクリプトは、その書き換えが規則に反していないかを、2 つの段で調べる。

    freeze  雑音の削除の段。raw が、取得した直後の写し（fetched.md）から文字列を消しただけの
            ものかを調べる。コードブロックの中、インラインのコード、リンクの記法の一部、題名は
            消せない。通れば、削除のあとの内容を cleaned.md に固定し、消したものを removed.json に書く。
    check   訳と補足の段。補足を除いた raw を cleaned.md とブロックごとに照らす。ブロックの数と種類、
            コードブロック、画像の行、リンク先、インラインのコード、表とリストの形が一致しなければ
            誤りにする。訳さないページでは、本文は 1 文字も変えてはならない。原文にない英字や記号、
            消えた固有の名前、文体の混在、極端に短い訳は警告にとどめる。

作業用のフォルダーは <WIKI_ROOT>/.wiki-cache/ingest/<スラッグ>/ で、wiki_fetch.py が作る。
check --finish は、誤りがなければこのフォルダーを消す。

使い方:
    python wiki_rawcheck.py freeze --wiki <WIKI_ROOT> --raw-root <RAW_ROOT> --slug <スラッグ> [--json]
    python wiki_rawcheck.py check  --wiki <WIKI_ROOT> --raw-root <RAW_ROOT> --slug <スラッグ> [--json]
                                   [--language-override translate|keep --reason <理由>] [--finish]

終了コード: 0 = 通った（警告があってもよい）、1 = 規則に反している、またはファイルのエラー。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import localize_argparse  # noqa: E402
import wiki_initialized  # noqa: E402

WORK_SUBDIR = (".wiki-cache", "ingest")
CAPTION_MARK = " 補足: "
UNAVAILABLE_MARK = "（未取得）"

# 行の先頭の入れ物の接頭辞（字下げ、リストの記号、引用の記号）。
CONTAINER_RE = re.compile(r"^((?:[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+|>[ \t]?))*)")
LIST_MARK_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
LINK_RE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\((?:[^()\s]|\([^()]*\))+(?:\s+\"[^\"]*\")?\)")
URL_IN_LINK_RE = re.compile(r"\]\(((?:[^()\s]|\([^()]*\))+)")
CODE_SPAN_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
HEADING_RE = re.compile(r"^(#{1,6})\s")
TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")


# ---------------------------------------------------------------- ファイルと作業場所


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def work_dir(wiki: Path, slug: str) -> Path:
    return wiki.joinpath(*WORK_SUBDIR, slug)


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def remove_work_dir(wiki: Path, slug: str) -> bool:
    """作業用のフォルダーを消す。途中の段がリンクか、wiki の外に解決されるなら、消さずに ValueError。

    返り値は、消したかどうか（もともとなければ False）。
    """
    target = work_dir(wiki, slug)
    step = wiki
    for part in (*WORK_SUBDIR, slug):
        step = step / part
        if not os.path.lexists(step):
            return False
        if _is_link(step):
            raise ValueError(f"作業用のフォルダーの途中がリンクなので消さない: {step}")
    root = os.path.realpath(wiki)
    real = os.path.realpath(target)
    if os.path.commonpath([root, real]) != root:
        raise ValueError(f"作業用のフォルダーが wiki の外にあるので消さない: {real}")
    shutil.rmtree(target)
    return True


def split_frontmatter(text: str) -> tuple[list[str], list[str]]:
    """frontmatter の行と、本文の行に分ける。"""
    lines = text.split("\n")
    if lines and lines[0] == "---":
        for i in range(1, len(lines)):
            if lines[i] == "---":
                return lines[: i + 1], lines[i + 1:]
    return [], lines


# ---------------------------------------------------------------- 行の部品


def container_prefix(line: str) -> str:
    return CONTAINER_RE.match(line).group(1)


def continuation_prefix(line: str) -> str:
    """画像の行と同じ入れ物で続ける行の接頭辞。リストの記号は同じ幅の空白にする。"""
    prefix = container_prefix(line)
    return re.sub(r"(?:[-*+]|\d+[.)])([ \t]+)", lambda m: " " * len(m.group(0)), prefix)


def is_blank(line: str) -> bool:
    return not re.sub(r"[>\s]", "", line)


IMAGE_RE = re.compile(r"!\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# 行の中で、丸ごと残すか丸ごと消すかしかできないかたまり（画像、リンク、インラインのコード）。
# 組み合わせるとグループの番号がずれるので、後方参照は名前で書く。
ATOM_RE = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\((?:[^()\s]|\([^()]*\))+(?:\s+\"[^\"]*\")?\)"
    r"|(?P<bt>`+)(?!`).+?(?<!`)(?P=bt)(?!`)")


def image_targets(line: str) -> list[str]:
    """行の中の画像のパス（コードの外）。リストの項目の途中にある画像も含める。"""
    return IMAGE_RE.findall(CODE_SPAN_RE.sub(" ", line))


def image_marks(block: list[str]) -> list[str]:
    """ブロックの中の画像の記法（コードの外）を、出てくる順に並べる。"""
    flags = code_line_flags(block)
    return [m.group(0) for line, f in zip(block, flags) if not f
            for m in IMAGE_RE.finditer(CODE_SPAN_RE.sub(" ", line))]


def is_image_line(line: str) -> bool:
    return bool(image_targets(line))


def image_target(line: str) -> str:
    targets = image_targets(line)
    return targets[0] if targets else ""


def atoms(line: str) -> list[str]:
    """行を、かたまり（画像、リンク、インラインのコード）と、そのほかの 1 文字ずつに分ける。"""
    out: list[str] = []
    pos = 0
    for m in ATOM_RE.finditer(line):
        out.extend(line[pos:m.start()])
        out.append(m.group(0))
        pos = m.end()
    out.extend(line[pos:])
    return out


def code_spans(text: str) -> list[str]:
    return [m.group(0) for m in CODE_SPAN_RE.finditer(text)]


def links(text: str) -> list[str]:
    return [m.group(0) for m in LINK_RE.finditer(text)]


def link_urls(text: str) -> list[str]:
    return URL_IN_LINK_RE.findall(text)


def is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def fence_of(line: str) -> str | None:
    body = line[len(container_prefix(line)):]
    m = FENCE_RE.match(body)
    return m.group(1) if m else None


def code_line_flags(lines: list[str]) -> list[int]:
    """各行がどのコードブロックに属するか（属さなければ 0、フェンスの行も含める）。"""
    flags = [0] * len(lines)
    current, opener, number = 0, "", 0
    for i, line in enumerate(lines):
        fence = fence_of(line)
        if current:
            flags[i] = current
            if fence and fence[0] == opener[0] and len(fence) >= len(opener) and \
                    not line[len(container_prefix(line)) + len(fence):].strip():
                current = 0
        elif fence:
            number += 1
            current, opener = number, fence
            flags[i] = current
    return flags


# ---------------------------------------------------------------- freeze


def _line_ok(src: str, out: str) -> str | None:
    """out が src から文字列を消しただけのものかを調べる。誤りなら理由を返す。"""
    if out == src:
        return None
    if not is_subsequence(out, src):
        return "書き足しか書き換えがある"
    # 画像、リンク、インラインのコードは、丸ごと残すか丸ごと消すかしかできない。
    # かたまりを 1 つの単位として、out が src の部分列になっているかを調べる。
    if not is_subsequence(atoms(out), atoms(src)):
        return "画像、リンク、インラインのコードの一部だけを消した（消すなら丸ごと消す）"
    return None


def freeze(wiki: Path, raw_root: Path, slug: str) -> tuple[int, dict]:
    wd = work_dir(wiki, slug)
    fetched_path, fetch_json = wd / "fetched.md", wd / "fetch.json"
    raw_path = raw_root / f"{slug}.md"
    for p in (fetched_path, fetch_json, raw_path):
        if not p.is_file():
            raise FileNotFoundError(f"ファイルがない: {p}（wiki_fetch.py で取り直す）")
    fetch = json.loads(read_text(fetch_json))
    src_fm, src = split_frontmatter(read_text(fetched_path))
    out_fm, out = split_frontmatter(read_text(raw_path))
    errors: list[dict] = []
    if src_fm != out_fm:
        errors.append({"kind": "frontmatter", "message": "frontmatter が変わった"})

    flags = code_line_flags(src)
    title_index = next((i for i, l in enumerate(src) if l.startswith("# ")), None)
    removed: list[dict] = []
    kept_src: set[int] = set()

    # 一致する行を先に固定し、一致しなかった範囲の中でだけ「文字列を消した行」を探す。
    # 書き換えた短い行が、離れた長い行の部分列として通ってしまうのを避けるためである。
    matcher = difflib.SequenceMatcher(None, src, out, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            kept_src.update(range(i1, i2))
            continue
        i = i1
        for j in range(j1, j2):
            line = out[j]
            def can_match(x: int) -> bool:
                if src[x] == line:
                    return True
                return bool(line) and not flags[x] and _line_ok(src[x], line) is None

            k = i
            while k < i2 and not can_match(k):
                k += 1
            if k >= i2:
                # この範囲に元の行がない。どの行を書き換えたかを示すために、最初の候補の理由を出す。
                cand = next((x for x in range(i, i2) if not flags[x]), None)
                reason = _line_ok(src[cand], line) if cand is not None else None
                errors.append({"kind": "edit" if reason else "added", "line": j + 1,
                               "message": reason or "取得した内容にない行か、並びの変わった行がある",
                               "raw": line[:200], **({"fetched": src[cand][:200]} if cand is not None else {})})
                continue
            for x in range(i, k):
                removed.append({"line": x + 1, "text": src[x]})
            kept_src.add(k)
            if src[k] != line:
                removed.append({"line": k + 1, "text": src[k], "kept": line})
            i = k + 1
        for x in range(i, i2):
            removed.append({"line": x + 1, "text": src[x]})
    removed.sort(key=lambda r: r["line"])

    # コードブロックは、全体を残すか、全体を消すか。中の行は変えない。
    blocks: dict[int, list[int]] = {}
    for k, f in enumerate(flags):
        if f:
            blocks.setdefault(f, []).append(k)
    for number, rows in blocks.items():
        kept = [k for k in rows if k in kept_src]
        if kept and len(kept) != len(rows):
            errors.append({"kind": "code", "line": rows[0] + 1,
                           "message": "コードブロックの一部だけを消した（消すならフェンスを含めて全体を消す）"})
    if title_index is not None and title_index not in kept_src:
        errors.append({"kind": "title", "line": title_index + 1, "message": "題名の見出しを消した"})

    # 画像のファイルは、残った画像の行から参照されるかで決まる。
    refs = Counter(t for l in out for t in image_targets(l))
    base = raw_path.parent
    for img in fetch.get("images", []):
        rel = img.get("path") or ""
        if not rel:
            continue
        exists = (base / rel).is_file()
        if refs.get(rel) and not exists:
            errors.append({"kind": "image_file", "message": f"残した画像の行が参照するファイルがない: {rel}"})
        if not refs.get(rel) and exists:
            errors.append({"kind": "image_file", "message": f"画像の行を消したのに、ファイルが残っている: {rel}"})

    body_chars = len(re.sub(r"\s+", "", "\n".join(src)))
    removed_chars = sum(len(re.sub(r"\s+", "", r["text"])) - len(re.sub(r"\s+", "", r.get("kept", "")))
                        for r in removed)
    warnings = []
    if body_chars and removed_chars > body_chars * 0.2:
        warnings.append({"kind": "removed_much",
                         "message": f"消した文字が本文の 2 割を超える（{removed_chars} / {body_chars} 文字）。本文を消していないかを確かめる"})
    result = {"stage": "freeze", "ok": not errors, "errors": errors, "warnings": warnings,
              "removed": removed, "removed_by_rule": fetch.get("removed_by_rule", [])}
    if not errors:
        write_text(wd / "cleaned.md", read_text(raw_path))
        write_text(wd / "removed.json", json.dumps(removed, ensure_ascii=False, indent=1))
    return (0 if not errors else 1), result


# ---------------------------------------------------------------- check


def strip_captions(lines: list[str]) -> tuple[list[str], list[dict]]:
    """画像の行の次の補足を取り除く。返り値は、補足を除いた行と、画像の行ごとの補足の一覧。"""
    out: list[str] = []
    captions: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        if not is_image_line(line):
            continue
        found: list[str] = []
        cont = continuation_prefix(line)
        while True:
            j = i
            if j < len(lines) and is_blank(lines[j]) and j + 1 < len(lines):
                j += 1
            if j < len(lines) and lines[j].startswith(cont + "> ") and CAPTION_MARK in lines[j]:
                head = lines[j][len(cont) + 2:]
                name = head.split(CAPTION_MARK, 1)[0]
                text = [head.split(CAPTION_MARK, 1)[1]]
                k = j + 1
                while k < len(lines) and lines[k].startswith(cont + "> ") and not is_blank(lines[k]):
                    text.append(lines[k][len(cont) + 2:])
                    k += 1
                found.append({"name": name.strip(), "text": " ".join(t.strip() for t in text)})
                i = k
                continue
            break
        captions.append({"line": line, "captions": found})
    return out, captions


def split_blocks(lines: list[str]) -> list[list[str]]:
    """空の行で区切ったブロックに分ける。区切りの空の行（引用の中の `>` など）は、次のブロックの
    先頭に、区切りの形として持たせる（引用の中の区切りを引用の外の区切りに変えると、引用が割れるため）。
    """
    blocks: list[list[str]] = []
    cur: list[str] = []
    sep = ""
    flags = code_line_flags(lines)
    for line, flag in zip(lines, flags):
        if not flag and is_blank(line):
            if cur:
                blocks.append(cur)
                cur = []
            sep = re.sub(r"\s", "", line)
            continue
        if not cur:
            cur.append("\x00sep:" + sep)
            sep = ""
        cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def block_sep(block: list[str]) -> str:
    return block[0][len("\x00sep:"):] if block and block[0].startswith("\x00sep:") else ""


def block_lines(block: list[str]) -> list[str]:
    return block[1:] if block and block[0].startswith("\x00sep:") else block


def block_kind(block: list[str]) -> str:
    first = block[0]
    prefix = container_prefix(first)
    body = first[len(prefix):]
    shape = re.sub(r"\d+[.)]", "1.", re.sub(r"[ \t]+", " ", prefix))
    if fence_of(first):
        kind = "code"
    elif m := HEADING_RE.match(body):
        kind = f"h{len(m.group(1))}"
    elif body.startswith("!["):
        kind = "image"
    elif TABLE_ROW_RE.match(body):
        kind = "table"
    else:
        kind = "text"
    return f"{shape}|{kind}"


def block_code(block: list[str]) -> list[str]:
    flags = code_line_flags(block)
    out: dict[int, list[str]] = {}
    for line, f in zip(block, flags):
        if f:
            out.setdefault(f, []).append(line[len(container_prefix(line)):])
    return ["\n".join(v) for v in out.values()]


def prose(block: list[str]) -> str:
    """コード、インラインのコード、リンク先を除いた、ブロックの文章。"""
    flags = code_line_flags(block)
    text = "\n".join(l[len(container_prefix(l)):] for l, f in zip(block, flags) if not f)
    text = CODE_SPAN_RE.sub(" ", text)
    text = re.sub(r"\]\((?:[^()\s]|\([^()]*\))+(?:\s+\"[^\"]*\")?\)", "]", text)
    return re.sub(r"https?://\S+", " ", text)


def table_shape(block: list[str]) -> list[int]:
    return [l.count("|") - l.count("\\|") - 1 for l in (x[len(container_prefix(x)):] for x in block)
            if TABLE_ROW_RE.match(l)]


def list_items(block: list[str]) -> int:
    """ブロックの中のリストの項目の数（入れ子の項目も数える）。"""
    flags = code_line_flags(block)
    count = 0
    for line, f in zip(block, flags):
        if f:
            continue
        count += len(re.findall(r"(?:[-*+]|\d+[.)])[ \t]+", re.sub(r"[ \t]*>[ \t]?", "", container_prefix(line))))
    return count


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'.-]*[A-Za-z0-9]|[A-Za-z]|[<>=!&|]{1,3}")
NAME_RE = re.compile(r"\b(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[A-Z]{2,}[A-Z0-9]*|[A-Z][a-z]+)\b")
DESU_RE = re.compile(r"(?:です|ます|でした|ました|ません|でしょう|ください)[。！？]")
DEARU_RE = re.compile(r"(?<!ま)(?<!でし)(?:である|だ|た|る|ない|う|い)[。！？]")


# 文の途中で大文字で始まっても、固有の名前ではないことが多い語。
FUNCTION_WORDS = {
    "A", "An", "The", "And", "Or", "But", "Nor", "So", "Yet", "If", "Then", "Else", "When", "While", "As",
    "At", "By", "For", "From", "In", "Into", "Of", "On", "Onto", "To", "With", "Without", "Within", "Via",
    "Is", "Are", "Was", "Were", "Be", "It", "Its", "This", "That", "These", "Those", "We", "You", "They",
    "He", "She", "I", "My", "Our", "Your", "Their", "Not", "No", "All", "Any", "Some", "Each", "Every",
    "Also", "Note", "See", "Update", "Example", "Examples",
}


def names_of(text: str, title_case_ok: bool = True) -> list[str]:
    """文の先頭以外の、大文字で始まる語、CamelCase の語、すべて大文字の語。

    title_case_ok が False（見出しと表）のときは、CamelCase の語とすべて大文字の語だけを返す。
    見出しと表の見出しは語ごとに大文字で始まるので、固有の名前と見分けられないためである。
    """
    out = []
    for sentence in re.split(r"(?<=[.!?:])\s+|\n", text):
        for m in NAME_RE.finditer(sentence):
            word = m.group(0)
            plain = bool(re.fullmatch(r"[A-Z][a-z]+", word))
            if plain and (not title_case_ok or word in FUNCTION_WORDS):
                continue
            at_start = not sentence[: m.start()].strip(" \t\"'(*_[-+0123456789.")
            if at_start and plain:
                continue
            out.append(word)
    return out


def check(wiki: Path, raw_root: Path, slug: str, override: str | None, reason: str | None) -> tuple[int, dict]:
    wd = work_dir(wiki, slug)
    cleaned_path, fetch_json = wd / "cleaned.md", wd / "fetch.json"
    raw_path = raw_root / f"{slug}.md"
    if not cleaned_path.is_file():
        raise FileNotFoundError(f"cleaned.md がない: {cleaned_path}（先に freeze を実行する）")
    for p in (fetch_json, raw_path):
        if not p.is_file():
            raise FileNotFoundError(f"ファイルがない: {p}")
    fetch = json.loads(read_text(fetch_json))
    needs_translation = bool(fetch.get("needs_translation"))
    if override == "translate":
        needs_translation = True
    elif override == "keep":
        needs_translation = False

    src_fm, src_lines = split_frontmatter(read_text(cleaned_path))
    out_fm, out_raw = split_frontmatter(read_text(raw_path))
    errors: list[dict] = []
    warnings: list[dict] = []

    def fm_without_translator(fm):
        return [l for l in fm if not l.startswith("translated_by:")]

    if fm_without_translator(src_fm) != fm_without_translator(out_fm):
        errors.append({"kind": "frontmatter", "message": "frontmatter が translated_by のほかで変わった"})
    translator_line = next((l for l in out_fm if l.startswith("translated_by:")), "")
    translator = translator_line.split(":", 1)[1].strip().strip('"') if translator_line else ""
    if needs_translation and not translator:
        errors.append({"kind": "translated_by", "message": "訳すページなのに translated_by が空"})
    if not needs_translation and translator:
        errors.append({"kind": "translated_by", "message": "訳さないページなのに translated_by がある"})

    out_lines, captions = strip_captions(out_raw)

    # 補足の数と表示名。
    images = {img.get("path"): img for img in fetch.get("images", []) if img.get("path")}
    names = set()
    base = raw_path.parent
    for entry in captions:
        line = entry["line"]
        target = image_target(line)
        unavailable = UNAVAILABLE_MARK in line or not target or not (base / target).is_file()
        count = len(entry["captions"])
        if unavailable and count:
            errors.append({"kind": "caption", "message": "取得できなかった画像に補足がある", "raw": line[:200]})
        if not unavailable and count != 1:
            errors.append({"kind": "caption",
                           "message": "画像の補足がない" if count == 0 else "画像の補足が 2 つ以上ある",
                           "raw": line[:200]})
        for c in entry["captions"]:
            names.add(c["name"])
            if not c["text"].strip():
                errors.append({"kind": "caption", "message": "補足の本文が空", "raw": line[:200]})
    if len(names) > 1:
        errors.append({"kind": "caption", "message": f"補足の表示名がそろっていない: {sorted(names)}"})
    if needs_translation and names and translator and names != {translator}:
        errors.append({"kind": "caption", "message": f"補足の表示名が translated_by と違う: {sorted(names)}"})

    src_split = split_blocks(src_lines)
    out_split = split_blocks(out_lines)
    if [block_sep(b) for b in src_split] != [block_sep(b) for b in out_split] and len(src_split) == len(out_split):
        errors.append({"kind": "blocks", "message": "ブロックの区切りの形が違う（引用の中の空の行 `>` を、引用の外の空の行に変えるなど）"})
    src_blocks = [block_lines(b) for b in src_split]
    out_blocks = [block_lines(b) for b in out_split]
    if len(src_blocks) != len(out_blocks):
        errors.append({"kind": "blocks", "message": f"ブロックの数が違う（取得 {len(src_blocks)}、raw {len(out_blocks)}）。"
                       "段落を分けたり、つなげたり、消したり、補足の置き方を誤ったりしていないかを確かめる"})
    desu = dearu = 0
    for n, (a, b) in enumerate(zip(src_blocks, out_blocks), start=1):
        where = {"block": n, "source": "\n".join(a)[:200], "raw": "\n".join(b)[:200]}

        def err(kind, message):
            errors.append({"kind": kind, "message": message, **where})

        if block_kind(a) != block_kind(b):
            err("block_kind", f"ブロックの種類が違う（{block_kind(a)} → {block_kind(b)}）")
            continue
        if block_code(a) != block_code(b):
            err("code", "コードブロックの中身が変わった")
        if image_marks(a) != image_marks(b):
            err("image_line", "画像の記法（alt、パス、title）が変わった")
        if Counter(link_urls(prose_without_code(a))) != Counter(link_urls(prose_without_code(b))):
            err("links", "リンク先の URL が違う")
        if Counter(code_spans(text_without_code_blocks(a))) != Counter(code_spans(text_without_code_blocks(b))):
            err("code_span", "インラインのコードが変わった")
        if table_shape(a) != table_shape(b):
            err("table", "表の行か列の数が違う")
        if list_items(a) != list_items(b):
            err("list", "リストの項目の数が違う")
        if not needs_translation and a != b:
            err("text", "訳さないページで本文が変わった")
        if not needs_translation:
            continue

        pa, pb = prose(a), prose(b)
        # 同じ語のくり返しと、大文字と小文字の違いは、訳ではふつうに起きるので数えない。
        # 原文の語の頭に当たる語（BLOBs → BLOB、FROM-clause → FROM）も、訳で短くなっただけとみなす。
        src_tokens = [x.lower() for x in TOKEN_RE.findall(pa)]
        new = sorted({t for t in TOKEN_RE.findall(pb)
                      if not any(s.startswith(t.lower()) for s in src_tokens)})
        if new:
            warnings.append({"kind": "new_tokens", "message": f"原文にない英字か記号: {new}", **where})
        kind = block_kind(a).split("|")[1]
        title_case_ok = not (kind.startswith("h") or kind == "table")
        missing = [w for w in dict.fromkeys(names_of(pa, title_case_ok)) if w not in pb]
        if missing:
            warnings.append({"kind": "missing_names", "message": f"訳文にない名前: {missing}", **where})
        letters_a = len(re.findall(r"[A-Za-z]", pa))
        chars_b = len(re.sub(r"\s", "", pb))
        if letters_a >= 40 and chars_b < letters_a * 0.2:
            warnings.append({"kind": "short", "message": "訳文が原文に比べて極端に短い", **where})
        desu += len(DESU_RE.findall(pb))
        dearu += len(DEARU_RE.findall(pb))
    total = desu + dearu
    if total and min(desu, dearu) > total * 0.1:
        warnings.append({"kind": "style", "message": f"です・ます調（{desu} 文）とである調（{dearu} 文）が混ざっている"})

    result = {"stage": "check", "ok": not errors, "needs_translation": needs_translation,
              "language_override": {"value": override, "reason": reason} if override else None,
              "translated_by": translator, "caption_names": sorted(names),
              "errors": errors, "warnings": warnings}
    return (0 if not errors else 1), result


def text_without_code_blocks(block: list[str]) -> str:
    flags = code_line_flags(block)
    return "\n".join(l for l, f in zip(block, flags) if not f)


def prose_without_code(block: list[str]) -> str:
    return CODE_SPAN_RE.sub(" ", text_without_code_blocks(block))


# ---------------------------------------------------------------- 出力


def render(result: dict) -> str:
    out = [f"{result['stage']}: {'通った' if result['ok'] else '規則に反している'}"]
    for e in result["errors"]:
        loc = f"ブロック {e['block']}: " if "block" in e else (f"{e['line']} 行目: " if "line" in e else "")
        out.append(f"誤り: {loc}{e['message']}")
        if e.get("source") is not None:
            out.append(f"    取得: {e['source']}")
        if e.get("fetched") is not None:
            out.append(f"    取得: {e['fetched']}")
        if e.get("raw") is not None:
            out.append(f"    raw : {e['raw']}")
    for w in result["warnings"]:
        loc = f"ブロック {w['block']}: " if "block" in w else ""
        out.append(f"警告: {loc}{w['message']}")
    if result["stage"] == "freeze":
        for r in result.get("removed_by_rule", []):
            out.append(f"規則で消した（{r.get('rule')}）: {str(r.get('text'))[:120]}")
        for r in result.get("removed", []):
            if "kept" in r:
                out.append(f"消した（{r['line']} 行目の一部）: {r['text'][:120]} → {r['kept'][:120]}")
            elif r["text"].strip():
                out.append(f"消した（{r['line']} 行目）: {r['text'][:120]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("freeze", "雑音の削除の段を検査し、削除のあとの内容を固定する"),
                            ("check", "訳と補足の段を検査する")):
        p = localize_argparse(sub.add_parser(name, help=help_text))
        p.add_argument("--wiki", type=Path, required=True, help="wiki のルート（<WIKI_ROOT>）")
        p.add_argument("--raw-root", type=Path, required=True, help="raw ソースのルート（<RAW_ROOT>）")
        p.add_argument("--slug", required=True, help="取り込みのスラッグ")
        p.add_argument("--json", action="store_true", help="結果を JSON で出力する。キーは英語のまま")
        if name == "check":
            p.add_argument("--language-override", choices=("translate", "keep"),
                           help="訳すかどうかの判定（fetch.json の needs_translation）を覆す")
            p.add_argument("--reason", help="--language-override の理由")
            p.add_argument("--finish", action="store_true", help="誤りがなければ作業用のフォルダーを消す")
    args = parser.parse_args(argv)
    # 作業用のフォルダーを読み書きする前に、初期化された wiki かを確かめる（ADR-0019）。
    found = wiki_initialized.problem(args.wiki)
    if found is not None:
        print(f"{found}\n{wiki_initialized.INIT_HINT}", file=sys.stderr)
        return 1
    try:
        if args.command == "freeze":
            code, result = freeze(args.wiki, args.raw_root, args.slug)
        else:
            if args.language_override and not args.reason:
                print("--language-override には --reason で理由を付ける", file=sys.stderr)
                return 1
            code, result = check(args.wiki, args.raw_root, args.slug, args.language_override, args.reason)
            if args.finish and code == 0:
                result["work_dir_removed"] = remove_work_dir(args.wiki, args.slug)
    except (FileNotFoundError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return code


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
