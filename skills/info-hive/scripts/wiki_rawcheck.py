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
check --finish は、誤りも警告もなければ（警告を受け入れる --accept-warnings を付ければ、誤りがなければ）
このフォルダーを消す。

使い方:
    python wiki_rawcheck.py freeze --wiki <WIKI_ROOT> --raw-root <RAW_ROOT> --slug <スラッグ> [--json]
    python wiki_rawcheck.py check  --wiki <WIKI_ROOT> --raw-root <RAW_ROOT> --slug <スラッグ> [--json]
                                   [--language-override translate|keep --reason <理由>]
                                   [--finish [--accept-warnings]]

終了コード: 0 = 通った（警告があってもよい）、1 = 規則に反している、またはファイルのエラー。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
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

# 表の規則（同梱の markdown-it-py）。パッケージの属性の table は関数なので、モジュールは import_module で取る。
_TABLE_RULE = importlib.import_module("markdown_it.rules_block.table")
_Token = importlib.import_module("markdown_it.token").Token


def _row_cells(state, line: int) -> list[str]:
    """表の行の、本文に実際にあるセル。表の規則と同じ行の取り出し（getLine。リストや引用の中でも、入れ物の
    字下げを除いた行）と分け方（escapedSplit と、両端の空のセルの除き方）で分ける。"""
    cells = _TABLE_RULE.escapedSplit(_TABLE_RULE.getLine(state, line).strip())
    if cells and cells[0] == "":
        cells.pop(0)
    if cells and cells[-1] == "":
        cells.pop()
    return cells


def _table_with_real_cells(state, start_line: int, end_line: int, silent: bool) -> bool:
    """表の規則を呼び、データの行のセルを、本文の行に実際にあるセルだけにする。

    パーサーは、データの行のセルを見出しの列数にそろえる。多いセルは捨て（トークンを作らない。画面にも出ない）、
    足りないセルは空のセルで補う。そのままの木では、捨てたセルの文字を照らせず、セルを消して後ろのセルが前の
    列にずれたことも分からない。検査に使う木では、補った空のセルを除き、捨てたセルを足して、行の子の数を本文の
    行のセルの数にする。セルの数の違いは、取得と raw の木の対応（freeze の行の位置の対応、check の比較）の
    中で見つかる。足したセルも、ふつうのセルと同じくインラインの解析を通る（この規則は、インラインの解析の前に動く）。
    """
    start = len(state.tokens)
    if not _TABLE_RULE.table(state, start_line, end_line, silent):
        return False
    if silent:
        return True
    tokens = state.tokens[start:]
    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        out.append(tok)
        i += 1
        if tok.type != "tr_open" or not tok.map or tok.map[0] < start_line + 2:
            continue  # 見出しの行のセルの数は、表の規則が見出しと区切りの行で確かめている
        cells: list[list] = []
        while tokens[i].type == "td_open":  # td_open、inline、td_close の組
            cells.append(tokens[i:i + 3])
            i += 3
        real = _row_cells(state, tok.map[0])
        for group in cells[:len(real)]:
            out.extend(group)
        for text in real[len(cells):]:
            level = cells[0][0].level if cells else tok.level + 1
            td_open, inline, td_close = _Token("td_open", "td", 1), _Token("inline", "", 0), _Token("td_close", "td", -1)
            td_open.level, inline.level, td_close.level = level, level + 1, level
            inline.content, inline.children = text.strip(), []
            for t in (td_open, inline, td_close):
                t.map, t.block = list(tok.map), True
            out.extend((td_open, inline, td_close))
    state.tokens[start:] = out
    return True


_PARSER = wiki_pages.new_parser()
# wiki_pages.new_parser() の表の規則と同じ位置、同じ代わりの連鎖（段落と参照の定義を終わらせる）で差し替える。
# 差し替えるのは、このスクリプトの検査に使うパーサーだけである（lint と閲覧のパーサーは変えない）。
_PARSER.block.ruler.at("table", _table_with_real_cells, {"alt": ["paragraph", "reference"]})


# ---------------------------------------------------------------- ファイルと作業場所


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def write_atomic(path: Path, text: str) -> None:
    """一時の名前で書き終えてから、名前を変える。途中で止まっても、書きかけのファイルは正式な名前に現れない。"""
    tmp = path.with_name(path.name + ".tmp")
    write_text(tmp, text)
    os.replace(tmp, path)


def work_dir(wiki: Path, slug: str) -> Path:
    return wiki.joinpath(*WORK_SUBDIR, slug)


def work_dir_complete(wiki: Path, slug: str) -> bool:
    """作業用の写しがそろっているか。wiki_fetch.py は fetch.json を最後に、raw を書き終えてから置く。そろって
    いれば、raw はこの写しと同じ取得で書いたものである（raw をあとで書き換えたかは、freeze と check が照らす）。"""
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


def parsed_image_src(rel: str) -> str:
    """wiki_fetch.py が本文に書くとおりの画像の記法を、パーサーで読んだときのパス。

    パーサーはパスに、符号化（日本語は %E6…）や文字参照の復号（&amp; は &）をかける。正規化の一部をまねると
    取りこぼすので、同じ書き方の記法を同じパーサーで読んで、本文の画像のパスと同じものを作る。
    """
    import wiki_fetch  # wiki_fetch.py が wiki_rawcheck を読み込むので、使うときに読み込む
    tokens = _PARSER.parseInline(f"![]({wiki_fetch._md_url(rel)})")
    for tok in tokens:
        for child in tok.children or []:
            if child.type == "image":
                return image_src(child)
    return rel


def fetched_images(fetch: dict) -> dict[str, str]:
    """取得して保存した画像の、パーサーが読んだパスから、元のパス（raw のファイルからの相対パス）への対応。

    本文の画像のパスと、記録のパスを、同じパーサーを通してから比べ、ファイルがあるかは元のパスで確かめる。
    """
    return {parsed_image_src(img["path"]): img["path"] for img in fetch.get("images", []) if img.get("path")}


def remaining_images(fetch: dict, tree: "Node", offset: int) -> list[dict]:
    """本文に残った、取得して保存した画像の記録（本文の順。同じ画像を 2 か所で使えば 2 つ）。補足を書く対象である。

    取得の記録（fetch.json の images）は、雑音の削除で消した画像も含むので、削除のあとの本文から選び直す。
    記録は保存先のパスごとではなく出現ごとにある（同じ画像を alt を変えて 2 か所で使うことがある）ので、alt と
    元の URL は、本文に残った画像の記法そのもの（取得器は元の URL を title に書く）から取る。記録からは保存先の
    パスだけを使う。line は、その画像を含むブロックの始まりの、ファイルの中の行番号（frontmatter の offset 行を足す）。
    """
    fetched = fetched_images(fetch)
    out = []
    for node in walk(tree):
        for src, alt, title in images_of(node.inline):
            rel = fetched.get(src)
            if rel is not None:
                out.append({"path": rel, "url": title, "alt": alt,
                            "line": offset + node.line if node.line else None})
    return out


def to_file_lines(items: list[dict], offset: int) -> None:
    """誤りと警告の行番号（本文の中の行番号）を、raw のファイルの行番号にする（frontmatter の offset 行を足す）。

    木の行番号は本文の中で数える（補足の行の範囲など、本文の行を指すのに使う）。人とモデルが開くのはファイル
    なので、出力するときにだけ直す。取得の写しと raw の frontmatter の行の数は同じである（translated_by の値だけが違う）。
    """
    for item in items:
        if item.get("line"):
            item["line"] += offset


def frontmatter_lines(text: str) -> list[str]:
    """frontmatter の行（translated_by の行を除く）。取得の世代を見分けるのに使う。"""
    fm, _body = split_frontmatter(text.replace("\r\n", "\n"))
    return [line for line in fm if not line.startswith("translated_by:")]


def work_dir_matches_raw(wiki: Path, slug: str, raw_path: Path) -> bool:
    """raw の frontmatter が、作業用の写しの fetched.md と同じか（translated_by は除く）。

    同じ取得で書いたことの保証は、写しがそろった印を raw のあとに置く書き込みの順（work_dir_complete）にある。
    frontmatter は同じ秒の取り直しでは同じになりうるので、取得の同一性の印にはしない。これは、写しをそろえた
    あとで raw の frontmatter を書き換えたか、raw を別の版に戻したもの（手で戻した、など）を見分ける。
    """
    try:
        return frontmatter_lines(read_text(work_dir(wiki, slug) / "fetched.md")) == \
            frontmatter_lines(read_text(raw_path))
    except (OSError, UnicodeDecodeError):
        return False


# 途中から続けるときに返す、前の取得の出力の値（wiki_fetch.py の出力のキー）。取得の出力のうち、取得した内容から
# 作ったものすべてである（警告もここに入る。続きの手順は、前の取得の出力を見て、本文を取れたかを確かめる）。
RESUME_KEYS = ("source_sha256_of", "title", "source_lang", "encoding", "content_root", "ja_ratio",
               "needs_translation", "chars", "images", "dropped_images", "removed_by_rule", "warnings")
# 続けるときに使う、取得の記録のキーと型。取得器はいつもこれを書く。0.14.0 が書いた記録には fetched_sha256 と
# source_sha256_of がないので、続けられない記録になる（fetch.json と fetched.md を結べず、どの取得かも分からない）。
_FETCH_TYPES = {"url": str, "final_url": str, "source_sha256": str, "fetched_sha256": str, "source_sha256_of": str,
                "needs_translation": bool, "images": list, "removed_by_rule": list, "warnings": list}


def snapshot_sha256(data: bytes) -> str:
    """作業用の写しの fetched.md のバイト列の SHA-256。取得の記録（fetch.json の fetched_sha256）と、写しを結ぶ。"""
    return hashlib.sha256(data).hexdigest()


def _fetch_record_ok(saved) -> bool:
    """取得の記録が、取得器の書いた形か（続けるときに使うキーがその型である）。"""
    if not isinstance(saved, dict):
        return False
    if any(not isinstance(saved.get(key), kind) for key, kind in _FETCH_TYPES.items()):
        return False
    return (all(isinstance(img, dict) and isinstance(img.get("path"), str) for img in saved["images"])
            and all(isinstance(r, dict) and isinstance(r.get("rule"), str) and isinstance(r.get("text"), str)
                    for r in saved["removed_by_rule"])
            and all(isinstance(w, str) for w in saved["warnings"]))


def resumable_fetch(wiki: Path, slug: str, raw_path: Path) -> dict | None:
    """途中の取り込みを続けられるなら、写しの取得の記録（fetch.json の中身）を返す。続けられなければ None。

    保証するのは整合である。写しのファイルの組（fetched.md、fetch.json、あれば cleaned.md）と raw が、1 つの
    取得と、その取得のあとの freeze が残しうる組のときだけ続ける。どこで止まったときも（書く順）、ファイルを
    丸ごと別の取得のものに置き換えたとき（一部だけを戻したバックアップなど）も、この組でなければ続けない。
    どの取得がその組を残したか（取得の出自）は、ファイルからは決められないので保証しない。別の設定で取っても
    fetched.md が同じバイト列になれば、どちらの fetch.json も、その本文を作った取得の正しい記録である。
    作業用の写しは wiki_fetch.py と freeze だけが書く。ファイルの中を手で書き換えたものは前提の外である（形の
    違う fetch.json で例外にはしない）。続けられるのは、次のすべてを満たすときだけである。

    - 写しがそろい（work_dir_complete）、raw の frontmatter が写しの fetched.md と同じ（work_dir_matches_raw）。
    - fetch.json が取得器の書いた形である（_fetch_record_ok）。0.14.0 が書いた写しは、fetched_sha256 がなく、
      fetch.json と fetched.md を結べないので、続けない（取得して原文が同じでも、別の設定で取った記録かを
      見分けられない）。
    - fetch.json の fetched_sha256 が、fetched.md のバイト列のハッシュと同じ（取得器は、fetched.md に書いた
      バイト列のハッシュを記録する）。
    - fetch.json の取得元とハッシュ（url、final_url、source_sha256）が、fetched.md の frontmatter と同じ。
    - cleaned.md があれば、freeze が通す内容である。frontmatter が fetched.md と同じで、本文が fetched.md から
      消すことだけで作れる（deletion_errors。画像のファイルは、あとで raw の側で変わりうるので見ない）。
      止まった 2 回目の freeze の前の cleaned.md も、これを満たす。
    """
    if not (work_dir_complete(wiki, slug) and work_dir_matches_raw(wiki, slug, raw_path)):
        return None
    wd = work_dir(wiki, slug)
    saved = load_fetch(wd)
    if not _fetch_record_ok(saved):
        return None
    try:
        data = (wd / "fetched.md").read_bytes()
        src_fm, src_body = split_frontmatter(data.decode("utf-8").replace("\r\n", "\n"))
    except (OSError, UnicodeDecodeError):
        return None
    if saved["fetched_sha256"] != snapshot_sha256(data):
        return None
    fm = parse_frontmatter_text("\n".join(src_fm[1:-1]))
    final_url = fm["final_url"] if "final_url" in fm else fm.get("url")  # final_url は url と違うときだけ書く
    if (fm.get("url"), final_url, fm.get("source_sha256")) != \
            (saved["url"], saved["final_url"], saved["source_sha256"]):
        return None
    cleaned = wd / "cleaned.md"
    if cleaned.is_file():
        try:
            out_fm, out_body = split_frontmatter(read_text(cleaned))
        except (OSError, UnicodeDecodeError):
            return None
        if deletion_errors(src_fm, src_body, out_fm, out_body, parse_tree(out_body)):
            return None
    return saved


def deletion_report(fetch: dict, src_body: str, out_body: str, out_tree: "Node", offset: int) -> dict:
    """取得した直後の本文（src_body）から、削除のあとの本文（out_body）までに消したものの報告。

    freeze の出力のうち、人が読んで本文を消していないかを確かめるためのもの（消した行、一部を消した行、消した
    文字の割合の警告）と、補足を書く画像。freeze と、削除を固定したあとから続けるとき（resume_fields）の両方が
    使う。固定した内容（cleaned.md）は freeze が通したときの raw なので、同じ報告を作り直せる。
    """
    removed = [line[2:] for line in difflib.ndiff(src_body.split("\n"), out_body.split("\n"))
               if line.startswith("- ") and line[2:].strip()]
    changed = [{"fetched": a, "raw": b} for a, b in _changed_lines(src_body, out_body)]
    body_chars = len(re.sub(r"\s+", "", src_body))
    removed_chars = body_chars - len(re.sub(r"\s+", "", out_body))
    warnings = []
    if body_chars and removed_chars > body_chars * 0.2:
        warnings.append({"kind": "removed_much",
                         "message": f"消した文字が本文の 2 割を超える（{removed_chars} / {body_chars} 文字）。本文を消していないかを確かめる"})
    return {"removed_lines": removed, "shortened_lines": changed, "warnings": warnings,
            "images": remaining_images(fetch, out_tree, offset)}


def resume_fields(wiki: Path, slug: str, saved: dict) -> dict:
    """途中から続けるために、どの段まで進んだか（work_stage）と、最後に終えた段の出力を返す。

    続きの手順は、止まる前に読むはずだった出力を読み直して、確かめの段からやり直せる。

    - 取得のあと（fetched）: 前の取得の出力の値（RESUME_KEYS。警告と規則で消したものを含む）。saved は
      resumable_fetch が返した記録である。
    - 削除を固定したあと（frozen）: 上に加えて、freeze の報告（freeze。消した行、一部を消した行、警告）を、
      fetched.md と cleaned.md から作り直して返す。補足を書く画像（images）は、削除のあとの本文に残ったものだけ
      にする（freeze の出力と同じ）。
    """
    wd = work_dir(wiki, slug)
    frozen = (wd / "cleaned.md").is_file()
    fields = {"work_stage": "frozen" if frozen else "fetched"}
    for key in RESUME_KEYS:
        if key in saved:
            fields[key] = saved[key]
    if frozen:
        _src_fm, src_body = split_frontmatter(read_text(wd / "fetched.md"))
        fm, body = split_frontmatter(read_text(wd / "cleaned.md"))
        report = deletion_report(saved, src_body, body, parse_tree(body), len(fm))
        fields["images"] = report.pop("images")
        fields["freeze"] = report
    return fields


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
            node = stack[-1]
            node.inline = list(tok.children or [])
            # 表のセル（th、td）の開始トークンは行の位置（map）を持たず、中の inline が持つ。開始トークンに位置が
            # なければ、inline の位置を節点の位置にする（誤りと警告に行番号を付けるため）。
            if node.line is None and tok.map:
                node.line, node.end = tok.map[0] + 1, tok.map[1]
        else:
            stack[-1].children.append(Node(tok))
    return root


def inline_text(children: list, newline: str = " ", code: bool = False) -> str:
    """インラインの文字（画像を除く。リンクの文言は含める。code が真ならインラインのコードも含める）。
    改行は newline にする。"""
    kinds = ("text", "code_inline") if code else ("text",)
    return "".join(c.content if c.type in kinds else (newline if c.type in ("softbreak", "hardbreak") else "")
                   for c in children)


def links_of(children: list) -> list[tuple[str, str]]:
    """リンク先と title の組。title も原文のまま残すものとして比べる。"""
    return [(c.attrGet("href") or "", c.attrGet("title") or "") for c in children if c.type == "link_open"]


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


def first_unmatched(short: list, long: list, same=lambda a, b: a == b) -> int | None:
    """short を long の部分列として対応させ、対応の取れない最初の short の位置を返す（すべて取れれば None）。

    先頭から貪欲に照らす。対応の関係が何であっても、部分列があるかの判定は、いちばん早く対応するものを選ぶ
    貪欲で正しい。
    """
    i = 0
    for k, x in enumerate(short):
        while i < len(long) and not same(long[i], x):
            i += 1
        if i >= len(long):
            return k
        i += 1
    return None


def is_subsequence(short: list, long: list, same=lambda a, b: a == b) -> bool:
    """short が long の部分列か（same で対応を決める）。"""
    return first_unmatched(short, long, same) is None


_LISTS = ("bullet_list", "ordered_list")


def block_units(children: list[Node]) -> list[tuple[tuple | None, Node]]:
    """子のブロックを、削除の検査で対応させる単位の並びにする。

    リストは項目に開き、項目にリストの種類（記号と種類。開始番号は除く）を添える。CommonMark は、同じ記号の
    リストの間のブロックを消すと、前後のリストを 1 つにまとめる（リストの項目の間には空の行があってよい）。
    消すことしかしていなくても、取得のリスト 1 つと raw のリスト 1 つは対応しなくなるので、項目を単位にして、
    raw のリストの項目の並びを、取得の前後のリストの項目をつないだ並びと照らす。リストの外のブロックは、項目と
    対応しない（入れ子を変えて、項目の中身をリストの外に出すことはできない）。
    """
    out: list[tuple[tuple | None, Node]] = []
    for c in children:
        if c.type in _LISTS:
            kind = c.signature(with_start=False)
            out.extend((kind, item) for item in c.children)
        else:
            out.append((None, c))
    return out


def derivable(src: Node, out: Node, failure: list[Node] | None = None,
              memo: dict[tuple[int, int], bool] | None = None) -> bool:
    """out が、src から消すことだけで作れるか。

    ブロックの種類と属性は同じ。コードブロックは中身も同じ。インラインは、単位の並びが部分列。子のブロックは、
    out の子の並びが src の子の部分列で（リストは項目を単位にする。block_units）、対応する子どうしも消すことだけで
    作れる。表の行だけは、セルを位置で対応させる（セルの数が同じで、同じ位置のセルどうしが消すことだけで作れる）。
    セルを消すと、後ろのセルが前の列にずれるためである（表の列は消せない）。

    節点の組ごとの結果を memo に覚え、同じ組を 2 度照らさない（入れ子が深いと、照らし直しで呼び出しが指数的に
    増えるため）。failure を渡したときは、対応の取れない最初の子（表の行ならその行）を入れる。
    """
    if memo is None:
        memo = {}
    key = (id(src), id(out))
    if key in memo and failure is None:
        return memo[key]
    bad: Node | None = None
    if src.signature(with_start=False) != out.signature(with_start=False) or src.info != out.info \
            or src.content != out.content:
        bad = out
    elif (src.inline or out.inline) and not is_subsequence(inline_units(out.inline), inline_units(src.inline), same_unit):
        bad = out
    elif src.type == "tr":
        if len(src.children) != len(out.children) or \
                not all(derivable(a, b, None, memo) for a, b in zip(src.children, out.children)):
            bad = out
    else:
        out_units, src_units = block_units(out.children), block_units(src.children)
        k = first_unmatched(out_units, src_units,
                            lambda s, o: s[0] == o[0] and derivable(s[1], o[1], None, memo))
        bad = out_units[k][1] if k is not None else None
    ok = bad is None
    if not ok and failure is not None:
        failure.append(bad)
    memo[key] = ok
    return ok


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
    out_tree = parse_tree(out_body)
    errors = deletion_errors(src_fm, src_body, out_fm, out_body, out_tree)

    # 画像のファイルは、残った画像から参照されるかで決まる。
    refs = Counter(src for n in walk(out_tree) for src, _a, _t in images_of(n.inline))
    base = raw_path.parent
    for norm, rel in sorted(fetched_images(fetch).items()):
        exists = (base / rel).is_file()
        if refs.get(norm) and not exists:
            errors.append({"kind": "image_file", "message": f"残した画像が参照するファイルがない: {rel}"})
        if not refs.get(norm) and exists:
            errors.append({"kind": "image_file", "message": f"画像を消したのに、ファイルが残っている: {rel}"})

    # 消したもの（人が読むための行の差分）と、消した文字の割合の警告。
    report = deletion_report(fetch, src_body, out_body, out_tree, len(out_fm))
    removed, changed, warnings = report["removed_lines"], report["shortened_lines"], report["warnings"]
    to_file_lines(errors + warnings, len(out_fm))
    result = {"stage": "freeze", "ok": not errors, "errors": errors, "warnings": warnings,
              "removed_lines": removed, "shortened_lines": changed,
              "removed_by_rule": fetch.get("removed_by_rule", []),
              "images": report["images"]}
    if not errors:
        # どちらも一時の名前で書き終えてから名前を変える。cleaned.md は最後に置く。cleaned.md があることを、
        # 削除を固定し終えた印にする（wiki_fetch.py の work_stage）。書きかけの cleaned.md は残らない。
        write_atomic(wd / "removed.json", json.dumps({"removed_lines": removed, "shortened_lines": changed},
                                                     ensure_ascii=False, indent=1))
        write_atomic(wd / "cleaned.md", out_text)
    return (0 if not errors else 1), result


def deletion_errors(src_fm: list[str], src_body: str, out_fm: list[str], out_body: str, out_tree: "Node") -> list[dict]:
    """out（frontmatter の行と本文）が、src から消すことだけで作れるかの誤り。freeze の判定のうち、画像のファイル
    （raw のルートにあるか）を除いたもの。freeze と、削除を固定したあとから続けるとき（resumable_fetch が
    cleaned.md を照らす）の両方が使う。誤りがなければ空のリスト。行番号は本文の中の行番号である。"""
    errors: list[dict] = []
    if src_fm != out_fm:
        errors.append({"kind": "frontmatter", "message": "frontmatter が変わった（frontmatter は書き換えない。"
                       "raw と作業用の写しが別の取得のものなら、wiki_fetch.py に --force を付けて取り直す）"})

    if references_of(out_body) - references_of(src_body):
        errors.append({"kind": "edit", "message": "参照の定義（[x]: …）を書き足したか、変えた"})
    src_tree = parse_tree(src_body)
    failure: list[Node] = []
    if not derivable(src_tree, out_tree, failure):
        bad = failure[0] if failure else out_tree
        kind, hint = "edit", ""
        if bad.type in ("table", "thead", "tbody", "tr", "th", "td") or any(n.type == "table" for n in walk(bad)):
            # 対応の取れないブロックが表か、表を含む（リストの項目の中の表など）。誤りの場所の手がかりで、判定は変えない。
            kind, hint = "table", "。表では、セル（列）を消せない（セルの区切りの | も、\\| の \\ も消さない）。表は、行ごとか表ごと消す"
        elif bad.type == "paragraph":
            hint = "。2 つの段落が 1 つにつながっていれば、間の空の行を残す（行ごと消すときも、空の行は残す）"
        errors.append({"kind": kind, "line": bad.line,
                       "message": "取得した内容から消すことだけをしたものになっていない"
                       "（書き足し、書き換え、並べ替え、入れ子の変更、コード・画像・リンク・インラインのコードの一部だけの変更）"
                       + hint,
                       "raw": (inline_text(bad.inline) or bad.content)[:200]})
    elif not is_subsequence(out_body, src_body):
        # 消すことだけをしたなら、本文は文字の上でも取得の部分列になる。木はこれより粗い（文字の参照や、意味を
        # 変えないエスケープは、読んだ後の文字が同じになる）ので、文字の上でも照らす。
        errors.append({"kind": "edit", "message": "取得した内容にない文字を書き足したか、文字を並べ替えた"})
    # 題名（最初の h1 の見出し）は、そのまま残す。
    src_title = next((n for n in walk(src_tree) if n.type == "heading" and n.tag == "h1"), None)
    if src_title is not None:
        out_title = next((n for n in walk(out_tree) if n.type == "heading" and n.tag == "h1"), None)
        if out_title is None or inline_units(out_title.inline) != inline_units(src_title.inline):  # 完全一致
            errors.append({"kind": "title", "message": "題名の見出しを消したか、変えた"})
    return errors


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
    m = CAPTION_HEAD_RE.match(inline_text(node.children[0].inline, newline="\n", code=True))
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
            # 補足は丸ごと取り除くので、中の画像はファイルの有無や画像の比較から外れてしまう。画像は書かせない。
            if any(images_of(p.inline) for p in walk(quote)):
                errors.append({"kind": "caption", "line": quote.line,
                               "message": "補足の中に画像がある。補足は文だけで書く"})
            # 最初の段落と同じ読み方（インラインのコードも含める）で、後ろの段落の補足の始まりを探す。
            later = [inline_text(p.inline, newline="\n", code=True) for p in walk(quote)
                     if p is not quote.children[0] and p.inline]
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
# 文体は、文ごとに 1 回だけ分ける。文の終わり（。！？）の前の語尾が、です・ます調ならです・ます調、そうでなく
# である調の語尾ならである調にする。です・ます調の語尾の末尾（ください → い、ました → た、でしょう → う）は、
# である調の語尾にも当たるので、2 つの型を別々に数えると、です・ます調の文を両方に数えてしまう。
SENTENCE_RE = re.compile(r"([^。！？\n]*)[。！？]")
DESU_END_RE = re.compile(r"(?:です|ます|でした|ました|ません|でしょう|ください)\Z")
DEARU_END_RE = re.compile(r"(?:である|だ|た|る|ない|う|い)\Z")


def sentence_styles(text: str) -> Counter:
    """文の数を、です・ます調（desu）とである調（dearu）に分けて数える。どちらでもない文は数えない。"""
    out: Counter = Counter()
    for m in SENTENCE_RE.finditer(text):
        head = m.group(1)
        if DESU_END_RE.search(head):
            out["desu"] += 1
        elif DEARU_END_RE.search(head):
            out["dearu"] += 1
    return out

# 文の途中で大文字で始まっても、固有の名前ではないことが多い語。
FUNCTION_WORDS = {
    "A", "An", "The", "And", "Or", "But", "Nor", "So", "Yet", "If", "Then", "Else", "When", "While", "As",
    "At", "By", "For", "From", "In", "Into", "Of", "On", "Onto", "To", "With", "Without", "Within", "Via",
    "Is", "Are", "Was", "Were", "Be", "It", "Its", "This", "That", "These", "Those", "We", "You", "They",
    "He", "She", "I", "My", "Our", "Your", "Their", "Not", "No", "All", "Any", "Some", "Each", "Every",
    "Also", "Note", "See", "Update", "Example", "Examples",
}
# 大文字で始まるが、日本語に訳すのがふつうの語（月と曜日の名前、図の出典の定型）。
# Sonnet 5.5 で試したとき（docs/agent-runs/2026-11-13-wiki-web-ingest-check-review/sonnet-trial.md）、
# この語の警告はすべて空振りだった。
TRANSLATED_WORDS = {
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "Created", "Source", "Photo", "Image", "Figure", "Fig", "Credit", "Credits",
}
# 日本語の文でふつうに使う英字の略語。原文の英語を訳すと、原文にない形で現れる（laptop → ノート PC）。
# 「原文にない英字か記号」の警告で数えない。2 回の試行（sonnet-trial.md、sonnet-trial-2.md）で、`PC` の警告は
# どれも空振りだった。
JAPANESE_ABBREVIATIONS = {"PC", "OS", "IT", "AI", "ID", "CPU", "GPU", "SNS", "URL", "Web"}


def link_diff_message(src: list[tuple[str, str]], out: list[tuple[str, str]]) -> str:
    """リンクの誤りの文。足りないリンクと、増えたリンクを並べる（どれを直せばよいかが分かるように）。"""

    def show(pairs: Counter) -> str:
        return "、".join(f"{href}" + (f"（title: {title}）" if title else "") + (f" ×{n}" if n > 1 else "")
                        for (href, title), n in sorted(pairs.items()))

    missing = Counter(src) - Counter(out)
    added = Counter(out) - Counter(src)
    parts = [f"リンクの数: 取得 {len(src)}、raw {len(out)}"]
    if missing:
        parts.append(f"足りないリンク: {show(missing)}")
    if added:
        parts.append(f"増えたリンク: {show(added)}")
    return "リンク先の URL か title が違う（" + "。".join(parts) + "）"


def names_of(text: str, title_case_ok: bool = True) -> list[str]:
    """文の先頭以外の、大文字で始まる語、CamelCase の語、すべて大文字の語。

    title_case_ok が False（見出しと表のセル）のときは、CamelCase の語とすべて大文字の語だけを返す。
    見出しと表の見出しは語ごとに大文字で始まるので、固有の名前と見分けられないためである。
    月と曜日の名前、図の出典の定型の語は、訳すのがふつうなので数えない。用語を説明するリストの
    「**用語** - 説明」の区切り（` - `、` – `、` — `）のあとも、文の先頭として扱う。
    """
    out = []
    for sentence in re.split(r"(?<=[.!?:])\s+|\n|\s[-–—]\s", text):
        for m in NAME_RE.finditer(sentence):
            word = m.group(0)
            plain = bool(re.fullmatch(r"[A-Z][a-z]+", word))
            if plain and (not title_case_ok or word in FUNCTION_WORDS or word in TRANSLATED_WORDS):
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
    elif a.info != b.info:
        # コードブロックのほかで info を持つのは、番号つきのリストの項目（項目の番号）。freeze（derivable）と同じく照らす。
        errors.append({"kind": "structure", **where,
                       "message": f"番号つきのリストの項目の番号が変わった（{a.info} → {b.info}）"})
    if a.type == "tr" and len(a.children) != len(b.children):
        errors.append({"kind": "table", **where,
                       "message": f"表の行のセルの数が違う（取得 {len(a.children)}、raw {len(b.children)}）。"
                       "セルの区切りの | を足したり消したりしない。訳文の中の | は \\| と書く"})
    elif len(a.children) != len(b.children):
        errors.append({"kind": "blocks", **where,
                       "message": f"{a.type or '本文'} の中のブロックの数が違う（取得 {len(a.children)}、raw {len(b.children)}）。"
                       "段落を分けたり、つなげたり、消したり、入れ子を変えたりしていないか、補足の置き方を確かめる"})
    if a.inline or b.inline:
        ia, ib = a.inline, b.inline
        src_text, out_text = inline_text(ia), inline_text(ib)
        # 出力に添える文は、インラインのコードも含める（コードの誤りの場所と中身が分かるように）。
        detail = {**where, "source": inline_text(ia, code=True)[:200], "raw": inline_text(ib, code=True)[:200]}
        if images_of(ia) != images_of(ib):
            errors.append({"kind": "image", **detail, "message": "画像（パス、alt、title）が変わった"})
        if Counter(links_of(ia)) != Counter(links_of(ib)):
            errors.append({"kind": "links", **detail, "message": link_diff_message(links_of(ia), links_of(ib))})
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
            # 同じ語のくり返し、大文字と小文字の違い、原文の語の頭に当たる語（BLOBs → BLOB）、日本語の文で
            # ふつうに使う略語（JAPANESE_ABBREVIATIONS）は数えない。
            new = sorted({t for t in TOKEN_RE.findall(out_text)
                          if t not in JAPANESE_ABBREVIATIONS and not any(s.startswith(t.lower()) for s in src_tokens)})
            if new:
                warnings.append({"kind": "new_tokens", **detail, "message": f"原文にない英字か記号: {new}"})
            title_case_ok = a.type not in ("heading", "th", "td")
            missing = [w for w in dict.fromkeys(names_of(src_text, title_case_ok)) if w not in out_text]
            if missing:
                warnings.append({"kind": "missing_names", **detail,
                                 "message": f"訳文にない名前: {missing}（人名、製品名、地名、手法や検定の名前は英語表記に"
                                 "戻す。一般の語を訳したなら、このブロックで原語を括弧で添える）"})
            letters = len(re.findall(r"[A-Za-z]", src_text))
            if letters >= 40 and len(re.sub(r"\s", "", out_text)) < letters * 0.2:
                warnings.append({"kind": "short", **detail, "message": "訳文が原文に比べて極端に短い"})
            style.update(sentence_styles(out_text))
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
    total = style["desu"] + style["dearu"]
    if total and min(style["desu"], style["dearu"]) > total * 0.1:
        warnings.append({"kind": "style",
                         "message": f"です・ます調（{style['desu']} 文）とである調（{style['dearu']} 文）が混ざっている"})

    to_file_lines(errors + warnings, len(out_fm))
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
    if result.get("work_dir_kept") == "warnings":
        out.append("警告があるので、作業用の写しを残した。直したら --finish を付けて再検査する。直さない警告は、"
                   "理由を log.md の取り込みの行のサブ行に書いてから、--finish --accept-warnings で再検査する")
    elif result.get("work_dir_kept") == "errors":
        out.append("誤りがあるので、作業用の写しを残した。直して再検査する")
    elif result.get("work_dir_removed"):
        out.append("作業用の写しを消した")
    if result["stage"] == "freeze":
        for img in result.get("images", []):
            out.append(f"補足を書く画像: {img.get('line') or '?'} 行目のあたり: {img['path']}  alt: {img.get('alt') or '（なし）'}")
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
            p.add_argument("--finish", action="store_true",
                           help="誤りも警告もなければ、作業用のフォルダーを消す（検査の基準も消えるので、再検査はできなくなる）")
            p.add_argument("--accept-warnings", action="store_true",
                           help="--finish で、警告が残っていても消す。直さない警告の理由を log.md に書いてから付ける")
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
            if args.accept_warnings and not args.finish:
                print("--accept-warnings は --finish と一緒に付ける", file=sys.stderr)
                return 1
            code, result = check(args.wiki, args.raw_root, args.slug, args.language_override, args.reason)
            if args.finish:
                # 作業用のフォルダーは検査の基準なので、直す作業が残るあいだは消さない。誤りがあれば直して再検査する。
                # 警告は、直して再検査するか、直さない理由を残して受け入れる（--accept-warnings）まで残す。
                if code != 0:
                    result["work_dir_kept"] = "errors"
                elif result["warnings"] and not args.accept_warnings:
                    result["work_dir_kept"] = "warnings"
                else:
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
