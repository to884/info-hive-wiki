#!/usr/bin/env python3
"""
wiki_rawcheck.py — Web ページの raw を、取得した直後の内容と照らして検査する。

/wiki:ingest に URL を渡したとき、モデルは wiki_fetch.py が書いた raw を書き換える（雑音の削除、
訳、画像の補足）。このスクリプトは、その書き換えが規則に反していないかを、2 つの段で調べる。

    freeze  雑音の削除の段。raw が、取得した直後の写し（fetched.md）から消すことだけをしたものかを
            調べる。コードブロック、画像、リンク、インラインのコードは、丸ごと残すか丸ごと消すかしか
            できない。題名は消せない。通れば、削除のあとの内容を cleaned.md に固定する。
    check   訳と補足の段。補足を除いた raw を cleaned.md と照らす。ブロックの木の形、コードブロック、
            画像、リンク先、インラインのコード、改行の数が一致しなければ誤りにする。訳さないページでは、
            本文は 1 文字も変えてはならない。原文にない英字や記号、消えた固有の名前、文体の混在、
            極端に短い訳は警告にとどめる。

本文は、lint や閲覧と同じ設定のパーサー（同梱の markdown-it-py。wiki_pages.new_parser()）で読み、
トークンで照らす。行の接頭辞や正規表現で、入れ物や構造を推し量らない。

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
from wiki_text import localize_argparse, parse_frontmatter_text  # noqa: E402
import wiki_initialized  # noqa: E402
import wiki_pages  # noqa: E402

WORK_SUBDIR = (".wiki-cache", "ingest")
CAPTION_MARK = " 補足: "
CAPTION_HEAD_RE = re.compile(r"([^\s][^\n]{0,59}?)" + re.escape(CAPTION_MARK) + r"(.*)\Z", re.S)
_PARSER = wiki_pages.new_parser()


# ---------------------------------------------------------------- ファイルと作業場所


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def work_dir(wiki: Path, slug: str) -> Path:
    return wiki.joinpath(*WORK_SUBDIR, slug)


def work_dir_complete(wiki: Path, slug: str) -> bool:
    """作業用の写しがそろっているか。wiki_fetch.py は fetch.json を最後に書く。"""
    wd = work_dir(wiki, slug)
    if not ((wd / "fetched.md").is_file() and (wd / "fetch.json").is_file()):
        return False
    try:
        json.loads(read_text(wd / "fetch.json"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def check_slug(slug: str) -> None:
    """スラッグを検査する（wiki_fetch.py と同じ規則）。.. や区切りを含むスラッグで、作業用のフォルダーの外を
    指せないようにする。"""
    import wiki_fetch  # wiki_fetch.py が wiki_rawcheck を読み込むので、使うときに読み込む
    wiki_fetch.check_slug(slug)


def remove_work_dir(wiki: Path, slug: str) -> bool:
    """作業用のフォルダーを消す。スラッグが規則に反するか、途中の段がリンクか、消す先が作業用のフォルダーの
    置き場（.wiki-cache/ingest）の直下でなければ、消さずに ValueError。

    返り値は、消したかどうか（もともとなければ False）。
    """
    check_slug(slug)
    target = work_dir(wiki, slug)
    step = wiki
    for part in (*WORK_SUBDIR, slug):
        step = step / part
        if not os.path.lexists(step):
            return False
        if _is_link(step):
            raise ValueError(f"作業用のフォルダーの途中がリンクなので消さない: {step}")
    parent = os.path.realpath(wiki.joinpath(*WORK_SUBDIR))
    real = os.path.realpath(target)
    if os.path.dirname(real) != parent:
        raise ValueError(f"作業用のフォルダーの置き場の直下ではないので消さない: {real}")
    shutil.rmtree(target)
    return True


def split_frontmatter(text: str) -> tuple[list[str], str]:
    """frontmatter の行と、本文に分ける。"""
    lines = text.split("\n")
    if lines and lines[0] == "---":
        for i in range(1, len(lines)):
            if lines[i] == "---":
                return lines[: i + 1], "\n".join(lines[i + 1:])
    return [], text


def load_fetch(wd: Path) -> dict:
    return json.loads(read_text(wd / "fetch.json"))


def fetched_images(fetch: dict) -> dict[str, str]:
    """取得して保存した画像の、パーサーが正規化したパスから、元のパス（raw のファイルからの相対パス）への対応。

    本文の画像のパスは、パーサーが正規化する（日本語は %E6… になり、%20 はそのまま残る）。fetch.json の
    パスにも同じ正規化をかけてから比べ、ファイルがあるかは元のパスで確かめる。
    """
    return {_PARSER.normalizeLink(img["path"]): img["path"] for img in fetch.get("images", []) if img.get("path")}


# ---------------------------------------------------------------- トークンの木


class Node:
    """ブロックのトークンの木の節点。inline はインラインのトークン（子）の並び。"""

    __slots__ = ("type", "tag", "markup", "info", "attrs", "content", "children", "inline", "line", "end")

    def __init__(self, tok=None):
        self.type = tok.type.removesuffix("_open") if tok else "root"
        self.tag = tok.tag if tok else ""
        self.markup = tok.markup if tok else ""
        self.info = tok.info if tok else ""
        self.attrs = dict(tok.attrs) if tok and tok.attrs else {}
        self.content = tok.content if tok and tok.type in ("fence", "code_block") else ""
        self.children: list[Node] = []
        self.inline: list = []
        self.line = (tok.map[0] + 1) if tok is not None and tok.map else None
        self.end = tok.map[1] if tok is not None and tok.map else None

    def signature(self, with_start: bool = True) -> tuple:
        """木の形として比べる値。段落の中身や行番号は含めない。

        記法（見出しの # と下線、フェンスの ``` と ~~~、リストの記号）も比べる。消すことだけでは変わらない。
        番号つきのリストの開始番号（start）は、先頭の項目を消すと変わるので、削除の段では比べない（with_start）。
        """
        keys = ("start", "style") if with_start else ("style",)
        attrs = {k: v for k, v in self.attrs.items() if k in keys}
        return (self.type, self.tag, self.markup, tuple(sorted(attrs.items())))


def parse_tree(body: str) -> Node:
    root = Node()
    stack = [root]
    for tok in _PARSER.parse(body):
        if tok.nesting == 1:
            node = Node(tok)
            stack[-1].children.append(node)
            stack.append(node)
        elif tok.nesting == -1:
            stack.pop()
        elif tok.type == "inline":
            stack[-1].inline = list(tok.children or [])
        else:
            stack[-1].children.append(Node(tok))
    return root


def inline_text(children: list, newline: str = " ") -> str:
    """インラインの文字（コードと画像を除く。リンクの文言は含める）。改行は newline にする。"""
    return "".join(c.content if c.type == "text" else (newline if c.type in ("softbreak", "hardbreak") else "")
                   for c in children)


def links_of(children: list) -> list[str]:
    return [c.attrGet("href") or "" for c in children if c.type == "link_open"]


def image_src(token) -> str:
    """画像のパス（パーサーが正規化したもの）。fetch.json のパスとは、fetched_images() が同じ正規化をかけて
    から比べる。符号化を戻す向きで比べると、パスそのものに %20 などがあるときに別のパスになる。"""
    return token.attrGet("src") or ""


def images_of(children: list) -> list[tuple[str, str, str]]:
    return [(image_src(c), c.content, c.attrGet("title") or "") for c in children if c.type == "image"]


# 中身の行を持つブロック。参照の定義の行は、これらのどれにも覆われない（パーサーはトークンを作らない）。
_LEAF_BLOCKS = {"paragraph_open", "heading_open", "fence", "code_block", "hr", "table_open", "html_block"}


def references_of(body: str) -> Counter:
    """参照の定義。木には残らないので、別に比べる。

    パーサーが読んだ定義（ラベル、URL、title。行番号は含めない）と、定義の行そのもの（パーサーは同じラベルの
    2 つ目の定義を捨てるので、書き足しを見つけるために行も数える）の多重集合を返す。定義の行は、正規表現で
    推し量らず、中身の行を持つブロックのどれにも覆われない行とする（入れ物の記号だけの行は除く）。
    """
    env: dict = {}
    tokens = _PARSER.parse(body, env)
    out: Counter = Counter()
    for label, ref in env.get("references", {}).items():
        out[("ref", label, ref.get("href", ""), ref.get("title", ""))] += 1
    covered: set[int] = set()
    for tok in tokens:
        if tok.type in _LEAF_BLOCKS and tok.map:
            covered.update(range(tok.map[0], tok.map[1]))
    for k, line in enumerate(body.split("\n")):
        if k not in covered and line.strip(" \t>"):
            out[("line", line.strip(" \t>"))] += 1
    return out


def code_spans_of(children: list) -> list[str]:
    return [c.content for c in children if c.type == "code_inline"]


def table_cells(lines: list[str], node: Node) -> list[int]:
    """表の節点の、元の行ごとのセルの数（エスケープしていない | で区切る）。"""
    if node.line is None or node.end is None:
        return []
    counts = []
    for line in lines[node.line - 1:node.end]:
        row = re.sub(r"^[ \t>]*", "", line).strip()
        cells = re.split(r"(?<!\\)\|", row)
        if cells and not cells[0].strip():
            cells = cells[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        counts.append(len(cells))
    return counts


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


# ---------------------------------------------------------------- freeze


def inline_units(children: list) -> list:
    """インラインを、削除の検査の単位の並びにする。

    画像、リンク（開きから閉じまで）、インラインのコードは、それぞれ 1 つの単位（丸ごと残すか丸ごと消すか）。
    強調（強い強調、取り消し線など）は、中の単位の並びを持つ 1 つの単位で、中は再帰的に照らす（強調の中の
    雑音だけを消せるように）。そのほかの文字は 1 文字ずつの単位。
    """
    out: list = []
    i = 0
    while i < len(children):
        c = children[i]
        if c.nesting == 1:
            depth, j = 0, i
            while j < len(children):
                depth += children[j].nesting
                if depth == 0:
                    break
                j += 1
            inner = children[i:j + 1]
            if c.type == "link_open":
                # リンクは文言も含めて丸ごと 1 つの単位にする。
                out.append(("link", tuple((x.type, x.content, x.markup, tuple(sorted((x.attrs or {}).items())))
                                          for x in inner)))
            else:
                out.append(("span", c.type, c.markup, inline_units(inner[1:-1])))
            i = j + 1
            continue
        if c.type == "image":
            out.append(("image", image_src(c), c.content, c.attrGet("title") or ""))
        elif c.type == "code_inline":
            out.append(("code", c.content, c.markup))
        elif c.type == "text":
            out.extend(("char", ch) for ch in c.content)
        elif c.type == "softbreak":
            out.append(("char", "\n"))
        elif c.type == "hardbreak":
            out.append(("hardbreak",))
        else:
            out.append((c.type, c.content, c.markup))
        i += 1
    return out


def same_unit(src, out) -> bool:
    """インラインの単位の対応。強調は、種類が同じで、中が消すことだけで作れること。"""
    if src[0] == "span" and out[0] == "span":
        return src[1:3] == out[1:3] and is_subsequence(out[3], src[3], same_unit)
    return src == out


def is_subsequence(short: list, long: list, same=lambda a, b: a == b) -> bool:
    """short が long の部分列か（same で対応を決める）。先頭から貪欲に照らす。

    対応の関係が何であっても、部分列があるかの判定は、いちばん早く対応するものを選ぶ貪欲で正しい。
    """
    i = 0
    for x in short:
        while i < len(long) and not same(long[i], x):
            i += 1
        if i >= len(long):
            return False
        i += 1
    return True


def derivable(src: Node, out: Node, failure: list[Node]) -> bool:
    """out が、src から消すことだけで作れるか。

    ブロックの種類と属性は同じ。コードブロックは中身も同じ。インラインは、単位の並びが部分列。子のブロックは、
    out の子の並びが src の子の部分列で、対応する子どうしも消すことだけで作れる。
    """
    if src.signature(with_start=False) != out.signature(with_start=False) or src.info != out.info \
            or src.content != out.content:
        return False
    if (src.inline or out.inline) and not is_subsequence(inline_units(out.inline), inline_units(src.inline), same_unit):
        return False

    def same(a: Node, b: Node) -> bool:
        return derivable(a, b, [])

    if not is_subsequence(out.children, src.children, same):
        # どの子で崩れたかを示すために、対応の取れない最初の子を探す。
        i = 0
        for child in out.children:
            while i < len(src.children) and not same(src.children[i], child):
                i += 1
            if i >= len(src.children):
                failure.append(child)
                break
            i += 1
        return False
    return True


def freeze(wiki: Path, raw_root: Path, slug: str) -> tuple[int, dict]:
    wd = work_dir(wiki, slug)
    raw_path = raw_root / f"{slug}.md"
    if not work_dir_complete(wiki, slug):
        raise FileNotFoundError(f"作業用の写しがそろっていない: {wd}（wiki_fetch.py に --force を付けて取り直す）")
    if not raw_path.is_file():
        raise FileNotFoundError(f"raw がない: {raw_path}")
    fetch = load_fetch(wd)
    src_text, out_text = read_text(wd / "fetched.md"), read_text(raw_path)
    src_fm, src_body = split_frontmatter(src_text)
    out_fm, out_body = split_frontmatter(out_text)
    errors: list[dict] = []
    if src_fm != out_fm:
        errors.append({"kind": "frontmatter", "message": "frontmatter が変わった"})

    if references_of(out_body) - references_of(src_body):
        errors.append({"kind": "edit", "message": "参照の定義（[x]: …）を書き足したか、変えた"})
    src_tree, out_tree = parse_tree(src_body), parse_tree(out_body)
    failure: list[Node] = []
    if not derivable(src_tree, out_tree, failure):
        bad = failure[0] if failure else out_tree
        errors.append({"kind": "edit", "line": bad.line,
                       "message": "取得した内容から消すことだけをしたものになっていない"
                       "（書き足し、書き換え、並べ替え、入れ子の変更、コード・画像・リンク・インラインのコードの一部だけの変更）",
                       "raw": (inline_text(bad.inline) or bad.content)[:200]})
    elif not is_subsequence(out_body, src_body):
        # 木の比較に加えて、本文全体を文字の上でも照らす。パーサーが捨てる部分（表の列数を超えるセルなど）への
        # 書き足しは、木には現れないためである。
        errors.append({"kind": "edit", "message": "取得した内容にない文字を書き足した（木に現れない部分、"
                       "たとえば表の列数を超えるセルへの書き足しを含む）"})
    # 題名（最初の h1 の見出し）は、そのまま残す。
    src_title = next((n for n in walk(src_tree) if n.type == "heading" and n.tag == "h1"), None)
    if src_title is not None:
        out_title = next((n for n in walk(out_tree) if n.type == "heading" and n.tag == "h1"), None)
        if out_title is None or inline_units(out_title.inline) != inline_units(src_title.inline):  # 完全一致
            errors.append({"kind": "title", "message": "題名の見出しを消したか、変えた"})

    # 画像のファイルは、残った画像から参照されるかで決まる。
    refs = Counter(src for n in walk(parse_tree(out_body)) for src, _a, _t in images_of(n.inline))
    base = raw_path.parent
    for norm, rel in sorted(fetched_images(fetch).items()):
        exists = (base / rel).is_file()
        if refs.get(norm) and not exists:
            errors.append({"kind": "image_file", "message": f"残した画像が参照するファイルがない: {rel}"})
        if not refs.get(norm) and exists:
            errors.append({"kind": "image_file", "message": f"画像を消したのに、ファイルが残っている: {rel}"})

    # 消したもの（人が読むための行の差分）。
    removed = [line[2:] for line in difflib.ndiff(src_body.split("\n"), out_body.split("\n"))
               if line.startswith("- ") and line[2:].strip()]
    changed = [{"fetched": a, "raw": b} for a, b in _changed_lines(src_body, out_body)]
    body_chars = len(re.sub(r"\s+", "", src_body))
    removed_chars = body_chars - len(re.sub(r"\s+", "", out_body))
    warnings = []
    if body_chars and removed_chars > body_chars * 0.2:
        warnings.append({"kind": "removed_much",
                         "message": f"消した文字が本文の 2 割を超える（{removed_chars} / {body_chars} 文字）。本文を消していないかを確かめる"})
    result = {"stage": "freeze", "ok": not errors, "errors": errors, "warnings": warnings,
              "removed_lines": removed, "shortened_lines": changed,
              "removed_by_rule": fetch.get("removed_by_rule", [])}
    if not errors:
        write_text(wd / "cleaned.md", out_text)
        write_text(wd / "removed.json", json.dumps({"removed_lines": removed, "shortened_lines": changed},
                                                   ensure_ascii=False, indent=1))
    return (0 if not errors else 1), result


def _changed_lines(a: str, b: str) -> list[tuple[str, str]]:
    """一部だけを消した行の組（取得、raw）。"""
    out = []
    sm = difflib.SequenceMatcher(None, a.split("\n"), b.split("\n"), autojunk=False)
    al, bl = a.split("\n"), b.split("\n")
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            for x, y in zip(al[i1:i2], bl[j1:j2]):
                out.append((x, y))
    return out


# ---------------------------------------------------------------- check


def caption_of(node: Node) -> tuple[str, str] | None:
    """引用の節点が補足なら (表示名, 本文) を返す。補足は、最初の段落が `<表示名> 補足: ` で始まる引用。"""
    if node.type != "blockquote" or not node.children or node.children[0].type != "paragraph":
        return None
    # 表示名は、引用の最初の行の、60 文字以内の名前。本文の途中に「補足:」がある普通の引用とは見分ける。
    m = CAPTION_HEAD_RE.match(inline_text(node.children[0].inline, newline="\n"))
    if not m:
        return None
    return m.group(1).strip(), m.group(2)


def strip_captions(node: Node, fetched: dict[str, str], base: Path, errors: list[dict], names: set[str],
                   removed: list[tuple[int, int]]) -> None:
    """木から補足の引用を取り除き、画像ごとの補足の数と表示名を調べる。

    補足は、画像を含むブロックのすぐあとに続く、補足の形の引用である。取り除くのは、そのブロックの取得した
    画像の数までに限る。それより後ろの補足の形の引用は、原文の引用かもしれないので、取り除かずに原文と照らす
    （モデルが足したものなら、ブロックの数の違いとして誤りになる）。取り除いた引用の行の範囲を removed に足す。
    """
    new_children: list[Node] = []
    i = 0
    while i < len(node.children):
        child = node.children[i]
        new_children.append(child)
        i += 1
        images = images_of(child.inline)
        if not images:
            strip_captions(child, fetched, base, errors, names, removed)
            continue
        needed = []
        for src, _alt, _title in images:
            if src in fetched:
                if not (base / fetched[src]).is_file():
                    errors.append({"kind": "image_file", "line": child.line,
                                   "message": f"取得した画像のファイルがない: {fetched[src]}"})
                needed.append(src)
        found = 0
        while found < len(needed) and i < len(node.children) and caption_of(node.children[i]) is not None:
            quote = node.children[i]
            name, body = caption_of(quote)
            names.add(name)
            if not body.strip():
                errors.append({"kind": "caption", "line": quote.line, "message": "補足の本文が空"})
            if any(p.type != "paragraph" for p in quote.children):
                errors.append({"kind": "caption", "line": quote.line,
                               "message": "補足の中に段落のほかのブロック（引用、リストなど）がある。補足は段落だけで書く"})
            later = [inline_text(p.inline, newline="\n") for p in walk(quote) if p is not quote.children[0] and p.inline]
            if re.search(r"\n[^\n]{1,60}?" + re.escape(CAPTION_MARK), body) or \
                    any(CAPTION_HEAD_RE.match(text) for text in later):
                errors.append({"kind": "caption", "line": quote.line,
                               "message": "1 つの引用に 2 つ以上の補足がある（補足は 1 つずつ別の引用にする）"})
            if quote.line is not None and quote.end is not None:
                removed.append((quote.line - 1, quote.end))
            found += 1
            i += 1
        if found < len(needed):
            errors.append({"kind": "caption", "line": child.line,
                           "message": f"画像の補足が足りない（取得した画像 {len(needed)}、補足 {found}）。"
                           "補足は、画像を含むブロック（段落）のすぐあとに、同じ入れ物の中で書く",
                           "raw": ", ".join(src for src, _a, _t in images)[:200]})
        strip_captions(child, fetched, base, errors, names, removed)
    node.children = new_children


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

    title_case_ok が False（見出しと表のセル）のときは、CamelCase の語とすべて大文字の語だけを返す。
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


def compare(a: Node, b: Node, translate: bool, errors: list[dict], warnings: list[dict], style: Counter,
            path: str = "") -> None:
    """2 つの木を照らす。a は取得した内容（削除のあと）、b は補足を除いた raw。"""
    where = {"line": b.line or a.line}
    if a.signature() != b.signature():
        errors.append({"kind": "structure", **where,
                       "message": f"ブロックの種類が違う（{a.type} {a.tag} → {b.type} {b.tag}）"})
        return
    if a.type in ("fence", "code_block") and (a.content != b.content or a.info != b.info):
        errors.append({"kind": "code", **where, "message": "コードブロックの中身が変わった",
                       "source": a.content[:200], "raw": b.content[:200]})
    if len(a.children) != len(b.children):
        errors.append({"kind": "blocks", **where,
                       "message": f"{a.type or '本文'} の中のブロックの数が違う（取得 {len(a.children)}、raw {len(b.children)}）。"
                       "段落を分けたり、つなげたり、消したり、入れ子を変えたりしていないか、補足の置き方を確かめる"})
    if a.inline or b.inline:
        ia, ib = a.inline, b.inline
        src_text, out_text = inline_text(ia), inline_text(ib)
        detail = {**where, "source": src_text[:200], "raw": out_text[:200]}
        if images_of(ia) != images_of(ib):
            errors.append({"kind": "image", **detail, "message": "画像（パス、alt、title）が変わった"})
        if Counter(links_of(ia)) != Counter(links_of(ib)):
            errors.append({"kind": "links", **detail, "message": "リンク先の URL が違う"})
        if Counter(code_spans_of(ia)) != Counter(code_spans_of(ib)):
            errors.append({"kind": "code_span", **detail, "message": "インラインのコードが変わった"})
        if sum(c.type == "hardbreak" for c in ia) != sum(c.type == "hardbreak" for c in ib):
            errors.append({"kind": "hardbreak", **detail, "message": "改行（行末の 2 つの空白や <br> から作った改行）の数が違う"})
        if not translate:
            if [(c.type, c.content, tuple(sorted((c.attrs or {}).items()))) for c in ia] != \
                    [(c.type, c.content, tuple(sorted((c.attrs or {}).items()))) for c in ib]:
                errors.append({"kind": "text", **detail, "message": "訳さないページで本文が変わった"})
        else:
            src_tokens = [x.lower() for x in TOKEN_RE.findall(src_text)]
            # 同じ語のくり返し、大文字と小文字の違い、原文の語の頭に当たる語（BLOBs → BLOB）は数えない。
            new = sorted({t for t in TOKEN_RE.findall(out_text) if not any(s.startswith(t.lower()) for s in src_tokens)})
            if new:
                warnings.append({"kind": "new_tokens", **detail, "message": f"原文にない英字か記号: {new}"})
            title_case_ok = a.type not in ("heading", "th", "td")
            missing = [w for w in dict.fromkeys(names_of(src_text, title_case_ok)) if w not in out_text]
            if missing:
                warnings.append({"kind": "missing_names", **detail, "message": f"訳文にない名前: {missing}"})
            letters = len(re.findall(r"[A-Za-z]", src_text))
            if letters >= 40 and len(re.sub(r"\s", "", out_text)) < letters * 0.2:
                warnings.append({"kind": "short", **detail, "message": "訳文が原文に比べて極端に短い"})
            style["desu"] += len(DESU_RE.findall(out_text))
            style["dearu"] += len(DEARU_RE.findall(out_text))
    for ca, cb in zip(a.children, b.children):
        compare(ca, cb, translate, errors, warnings, style)


def check(wiki: Path, raw_root: Path, slug: str, override: str | None, reason: str | None) -> tuple[int, dict]:
    wd = work_dir(wiki, slug)
    cleaned_path = wd / "cleaned.md"
    raw_path = raw_root / f"{slug}.md"
    if not cleaned_path.is_file():
        raise FileNotFoundError(f"cleaned.md がない: {cleaned_path}（先に freeze を実行する）")
    if not (wd / "fetch.json").is_file():
        raise FileNotFoundError(f"fetch.json がない: {wd / 'fetch.json'}")
    if not raw_path.is_file():
        raise FileNotFoundError(f"raw がない: {raw_path}")
    fetch = load_fetch(wd)
    translate = bool(fetch.get("needs_translation"))
    if override == "translate":
        translate = True
    elif override == "keep":
        translate = False

    src_fm, src_body = split_frontmatter(read_text(cleaned_path))
    out_fm, out_body = split_frontmatter(read_text(raw_path))
    errors: list[dict] = []
    warnings: list[dict] = []

    def without_translator(fm):
        return [line for line in fm if not line.startswith("translated_by:")]

    if without_translator(src_fm) != without_translator(out_fm):
        errors.append({"kind": "frontmatter", "message": "frontmatter が translated_by のほかで変わった"})
    # 値は、ほかのスクリプトと同じ frontmatter のパーサーで読む。キーが 2 つあれば誤りにする（読み方で値が変わる）。
    if sum(x.startswith("translated_by:") for x in out_fm) > 1:
        errors.append({"kind": "translated_by", "message": "translated_by が 2 つ以上ある"})
    value = parse_frontmatter_text("\n".join(out_fm[1:-1])).get("translated_by") if out_fm else None
    translator = value.strip() if isinstance(value, str) else ""
    if translate and not translator:
        errors.append({"kind": "translated_by", "message": "訳すページなのに translated_by が空"})
    if not translate and translator:
        errors.append({"kind": "translated_by", "message": "訳さないページなのに translated_by がある"})

    if references_of(src_body) != references_of(out_body):
        errors.append({"kind": "references", "message": "参照の定義（[x]: …）が変わった"})
    src_tree, out_tree = parse_tree(src_body), parse_tree(out_body)
    names: set[str] = set()
    caption_ranges: list[tuple[int, int]] = []
    strip_captions(out_tree, fetched_images(fetch), raw_path.parent, errors, names, caption_ranges)
    if not translate:
        # 訳さないページは、補足の行を除いた本文が、文字どおり同じでなければならない（記法の書き換えも含めて）。
        # 入れ物の記号だけの空の行（空の行、`>`、字下げだけの行）は、補足を挟むと増えるので比べない。
        # 段落の分け方とブロックの数は、木の比較（compare）で別に照らしている。
        lines = out_body.split("\n")
        drop: set[int] = set()
        for start, end in caption_ranges:
            drop.update(range(start, end))
        kept = [line for k, line in enumerate(lines) if k not in drop and line.strip(" \t>")]
        original = [line for line in src_body.split("\n") if line.strip(" \t>")]
        if kept != original:
            errors.append({"kind": "text", "message": "訳さないページで、補足のほかに本文を変えた"})
    if len(names) > 1:
        errors.append({"kind": "caption", "message": f"補足の表示名がそろっていない: {sorted(names)}"})
    if translate and names and translator and names != {translator}:
        errors.append({"kind": "caption", "message": f"補足の表示名が translated_by と違う: {sorted(names)}"})

    style: Counter = Counter()
    compare(src_tree, out_tree, translate, errors, warnings, style)
    # パーサーは表の列数を超えるセルを捨てるので、表の行ごとのセルの数を、元の行から数えて比べる。
    src_tables = [n for n in walk(src_tree) if n.type == "table"]
    out_tables = [n for n in walk(out_tree) if n.type == "table"]
    if len(src_tables) == len(out_tables):
        src_lines, out_lines = src_body.split("\n"), out_body.split("\n")
        for ta, tb in zip(src_tables, out_tables):
            if table_cells(src_lines, ta) != table_cells(out_lines, tb):
                errors.append({"kind": "table", "line": tb.line,
                               "message": "表の行ごとのセルの数が違う（列数を超えるセルへの書き足しを含む）"})
    total = style["desu"] + style["dearu"]
    if total and min(style["desu"], style["dearu"]) > total * 0.1:
        warnings.append({"kind": "style",
                         "message": f"です・ます調（{style['desu']} 文）とである調（{style['dearu']} 文）が混ざっている"})

    result = {"stage": "check", "ok": not errors, "needs_translation": translate,
              "language_override": {"value": override, "reason": reason} if override else None,
              "translated_by": translator, "caption_names": sorted(names),
              "errors": errors, "warnings": warnings}
    return (0 if not errors else 1), result


# ---------------------------------------------------------------- 出力


def render(result: dict) -> str:
    out = [f"{result['stage']}: {'通った' if result['ok'] else '規則に反している'}"]
    for e in result["errors"]:
        loc = f"{e['line']} 行目のあたり: " if e.get("line") else ""
        out.append(f"誤り: {loc}{e['message']}")
        if e.get("source") is not None:
            out.append(f"    取得: {e['source']}")
        if e.get("raw") is not None:
            out.append(f"    raw : {e['raw']}")
    for w in result["warnings"]:
        loc = f"{w['line']} 行目のあたり: " if w.get("line") else ""
        out.append(f"警告: {loc}{w['message']}")
    if result["stage"] == "freeze":
        for r in result.get("removed_by_rule", []):
            out.append(f"規則で消した（{r.get('rule')}）: {str(r.get('text'))[:160]}")
        for line in result.get("removed_lines", []):
            out.append(f"消した行: {line[:160]}")
        for c in result.get("shortened_lines", []):
            out.append(f"一部を消した行: {c['fetched'][:120]} → {c['raw'][:120]}")
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
        check_slug(args.slug)
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
