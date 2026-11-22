#!/usr/bin/env python3
"""
wiki_captions.py — YouTube の字幕の raw を作って読み、wiki のページの訂正と留保の印を照らし、confidence を
計算する（info-hive-dev の ADR-0027）。

wiki_youtube.py（raw を作る、verify）と wiki_lint.py（取り込みの外で直したページの照合）が共有する。
標準ライブラリと、同梱の markdown-it-py（wiki_pages 経由。ページの本文のどこがコードと引用かを決める）だけを
使う。Python 3.12 以上。

raw の形（スクリプトだけが書く）:
    frontmatter（キーは英語、文字列は二重引用符）
    # YouTube の字幕 <video_id>
    ## 区間 N [時刻]
    ```text            ← 本文の最長のバッククォートの並びより 1 つ長い（3 つ以上）
    題名: <チャプターの題名>   ← 題名のあるチャプターだけ。行の番号を振らない
    L1 [00:00] 字幕の本文
    ```
    字幕の行のない区間は「この区間に字幕はない」と書く。

ページの印と定義:
    訂正した語の直後に 〔訂正 L123-1〕、留保の語の直後に 〔留保 L123-1〕。関係するページでは
    〔訂正 [[要約ページ]] L123-1〕。定義は要約ページの `## 字幕の訂正と留保` の節に、1 項目 1 行で書く。
        - L123-1: 字幕 `15` を `50` に訂正（根拠 caption:L130）
        - L123-2: 字幕 `x`（2 つ目） は留保   ← 何番目かは、同じ行に 2 回以上出るときに書く
    根拠は metadata:title、metadata:channel、metadata:chapter:N、caption:L130、wiki_source:<スラッグ>、
    model:<理由> のどれか。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import format_fm_value, nfc, parse_frontmatter_text  # noqa: E402

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)  # wiki_lint.py と同じ分け方
LINE_RE = re.compile(r"^L([1-9][0-9]*) \[((?:[0-9]+:)?[0-9]{2}:[0-9]{2})\] (.+)$")
SECTION_RE = re.compile(r"^## 区間 ([1-9][0-9]*) \[((?:[0-9]+:)?[0-9]{2}:[0-9]{2})\]$")
FENCE_OPEN_RE = re.compile(r"^(`{3,})text$")
TITLE_PREFIX = "題名: "
NO_CAPTION = "この区間に字幕はない"
SECTION_SECONDS = 600
UNTITLED_RE = re.compile(r"^<Untitled Chapter [0-9]+>$")
DEF_HEADING = "字幕の訂正と留保"
CAPTION_KINDS = ("uploaded", "asr")
CONFIDENCE_VALUES = ("high", "medium", "low")
# 仮の値（ADR-0027。較正していない）。
HEAVY_WEIGHT = 3
LIGHT_WEIGHT = 1
LOW_RATE = 3.0

# 改行として扱う文字（str.splitlines が分ける文字）と、そのほかの制御文字は、字幕の行の中で空白にする。
# 字幕が raw の行やフェンスを偽らないためである（raw は \n だけで行に分けて読む）。
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


class CaptionError(Exception):
    """json3 か raw を読めない。"""


class EvidenceUnavailable(CaptionError):
    """訂正の根拠の raw を拒んだか、照合できない。欠落と区別する。"""


class EvidenceTexts(list):
    """読めた本文と、読まなかった原本の理由。本文を絞るときも理由を保つ。"""
    def __init__(self, texts, problems=()):
        super().__init__(texts)
        self.problems = list(problems)

    def without(self, text):
        return EvidenceTexts([t for t in self if t != text], self.problems)


# ---------------------------------------------------------------- json3 から行へ


def flatten(text: str) -> str:
    """NFC にそろえ、改行と制御文字を空白にして、前後の空白を除く。"""
    text = unicodedata.normalize("NFC", text)
    text = "".join(" " if (ch in _LINE_BREAKS or unicodedata.category(ch) == "Cc") else ch for ch in text)
    return text.strip()


def caption_events(data: bytes) -> list[tuple[int, str]]:
    """json3 のバイト列から、（tStartMs, 本文）の並びを返す。並びは tStartMs の昇順、同じ値なら json3 の順。"""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptionError(f"json3 を読めない（{exc}）") from None
    events = doc.get("events") if isinstance(doc, dict) else None
    if not isinstance(events, list):
        raise CaptionError("json3 に events の並びがない")
    found = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict) or "segs" not in ev:
            continue
        segs = ev["segs"]
        if not isinstance(segs, list):
            raise CaptionError(f"json3 の {i} 番目の出来事の segs が並びでない")
        pieces = []
        for seg in segs:
            if isinstance(seg, dict) and isinstance(seg.get("utf8"), str):
                pieces.append(seg["utf8"])
        text = flatten("".join(pieces))
        if not text:
            continue
        start = ev.get("tStartMs")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise CaptionError(f"json3 の {i} 番目の出来事に tStartMs がない")
        found.append((start, i, text))
    found.sort(key=lambda x: (x[0], x[1]))
    return [(start, text) for start, _i, text in found]


def format_time(seconds: int, long: bool) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if long:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def chapter_title(value) -> str | None:
    """チャプターの題名。題名のないチャプター（yt-dlp の <Untitled Chapter N>）と空の題名は None。"""
    if not isinstance(value, str):
        return None
    title = flatten(value)
    if not title or UNTITLED_RE.match(title):
        return None
    return title


@dataclass
class Section:
    start: int          # 見出しに書く秒（切り捨て）
    title: str | None
    start_ms: int = 0   # 行が属するかを決める開始（ミリ秒、切り捨て）
    lines: list = field(default_factory=list)  # (番号, tStartMs, 本文)


def build_sections(events: list[tuple[int, str]], chapters: list, duration) -> list[Section]:
    """行を区間に分ける。chapters は (開始の秒, 題名) の並び。なければ 10 分ごとに区間を作る。"""
    if chapters:
        # 区間は開始の時刻からで、行は tStartMs で属する区間を決める。開始を秒に切り捨ててから比べると、
        # 60.5 秒に始まるチャプターの前の 60.2 秒の行が、次の区間に入る。比べるのはミリ秒の開始である。
        ordered = sorted(((max(0, int(float(start) * 1000)), chapter_title(title)) for start, title in chapters),
                         key=lambda x: x[0])
        sections = [Section(ms // 1000, title, ms) for ms, title in ordered]
    else:
        last = (events[-1][0] // 1000) if events else 0
        total = max(int(duration or 0), last + 1)
        count = max(1, -(-total // SECTION_SECONDS))
        sections = [Section(i * SECTION_SECONDS, None, i * SECTION_SECONDS * 1000) for i in range(count)]
    starts_ms = [s.start_ms for s in sections]
    index = 0
    for number, (start, text) in enumerate(events, 1):
        # 行は tStartMs で属する区間を決める。最初の区間より前の行は、最初の区間に置く。
        while index + 1 < len(starts_ms) and start >= starts_ms[index + 1]:
            index += 1
        sections[index].lines.append((number, start, text))
    return sections


def fence_for(body: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def is_long(duration, events) -> bool:
    last = (events[-1][0] // 1000) if events else 0
    return max(int(duration or 0), last) >= 3600


def section_blocks(sections: list[Section], long: bool) -> list[tuple[str, list[str], bool]]:
    """区間ごとに（見出し, フェンスの中の行, 字幕の行があるか）を返す。"""
    out = []
    for n, sec in enumerate(sections, 1):
        inner = ([TITLE_PREFIX + sec.title] if sec.title else [])
        inner += [f"L{num} [{format_time(ms // 1000, long)}] {text}" for num, ms, text in sec.lines]
        out.append((f"## 区間 {n} [{format_time(sec.start, long)}]", inner, bool(sec.lines)))
    return out


def render_raw(fields: dict, events: list[tuple[int, str]], chapters: list, duration) -> str:
    """raw の文字列を作る。純粋な関数。自己検査を通らなければ CaptionError。"""
    long = is_long(duration, events)
    sections = build_sections(events, chapters, duration)
    head = ["---"] + [f"{k}: {format_fm_value(v)}" for k, v in fields.items()] + ["---", ""]
    parts = ["\n".join(head) + "\n", f"# YouTube の字幕 {fields['video_id']}\n"]
    blocks = section_blocks(sections, long)
    for heading, inner, has_lines in blocks:
        text = f"\n{heading}\n\n"
        if inner:
            body = "\n".join(inner)
            fence = fence_for(body)
            text += f"{fence}text\n{body}\n{fence}\n"
        if not has_lines:
            text += ("\n" if inner else "") + NO_CAPTION + "\n"
        parts.append(text)
    rendered = "".join(parts)
    self_check(rendered, fields, events, blocks)
    return rendered


def self_check(text: str, fields: dict, events: list, blocks: list) -> None:
    """作った raw を読み戻し、frontmatter、行、区間が作ったものと同じかを確かめる。本文を同梱のパーサーでも読み、
    見出し、フェンス、段落の並びが区間と 1 対 1 に対応することを確かめる。"""
    import wiki_pages
    doc = parse_raw(text)
    for key, value in fields.items():
        got = doc.meta.get(key)
        if str(got) != str(value) if isinstance(value, int) else got != value:
            raise CaptionError(f"raw の frontmatter の {key} を読み戻せない")
    if set(doc.meta) != set(fields):
        raise CaptionError("raw の frontmatter のキーが違う")
    if [doc.lines[n][1] for n in sorted(doc.lines)] != [t for _s, t in events]:
        raise CaptionError("raw の行を読み戻せない")
    m = FRONTMATTER_RE.match(text)
    tokens = wiki_pages.new_parser().parse(text[m.end():])
    want = [("heading_open", "h1", None), ("inline", "", f"YouTube の字幕 {fields['video_id']}"),
            ("heading_close", "h1", None)]
    for heading, inner, has_lines in blocks:
        want += [("heading_open", "h2", None), ("inline", "", heading[3:]), ("heading_close", "h2", None)]
        if inner:
            want.append(("fence", "code", "\n".join(inner) + "\n"))
        if not has_lines:
            want += [("paragraph_open", "p", None), ("inline", "", NO_CAPTION), ("paragraph_close", "p", None)]
    got = []
    for t in tokens:
        if t.type == "fence":
            if t.info != "text":
                raise CaptionError("フェンスの情報の文字列が text でない")
            got.append(("fence", "code", t.content))
        elif t.type == "inline":
            got.append(("inline", "", t.content))
        else:
            got.append((t.type, t.tag, None))
    if got != want:
        raise CaptionError("raw の本文の構造が、区間の並びと一致しない")


# ---------------------------------------------------------------- raw を読む


@dataclass
class RawDoc:
    meta: dict
    lines: dict            # 番号 -> (時刻, 本文)
    chapters: dict         # 区間の番号 -> 題名（チャプターの題名のあるものだけ）
    sections: int
    text: str

    @property
    def count(self) -> int:
        return len(self.lines)

    @property
    def video_id(self) -> str:
        return str(self.meta.get("video_id") or "")

    @property
    def caption_kind(self) -> str:
        return str(self.meta.get("caption_kind") or "")


def read_meta(text: str) -> dict | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        return parse_frontmatter_text(m.group(1))
    except Exception:
        return None


def page_meta(text: str) -> dict:
    """ページの frontmatter を、日本語の別名を英語のキーに直して読む。"""
    from wiki_text import canonical_meta
    return canonical_meta(read_meta(text) or {})[0]


def parse_raw(text: str) -> RawDoc:
    """スクリプトが書いた形の raw を読む。形が違えば CaptionError。行は \\n だけで分ける。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise CaptionError("raw に frontmatter がない")
    meta = parse_frontmatter_text(m.group(1))
    if not meta.get("video_id"):
        raise CaptionError("raw の frontmatter に video_id がない")
    lines: dict = {}
    chapters: dict = {}
    section = 0
    fence = None
    first = False
    expected = 1
    heading = f"# YouTube の字幕 {meta.get('video_id')}"
    seen_heading = False
    records: list = []
    for no, line in enumerate(text[m.end():].split("\n"), 1):
        if fence is None:
            # フェンスの外に置くのは、生成器が書く行だけである（見出し、区間の見出し、フェンス、
            # 「この区間に字幕はない」、空行）。それ以外の行のある raw は、スクリプトが書いた形でない。
            if not line:
                continue
            if line == heading and not seen_heading and section == 0:
                seen_heading = True
                continue
            sm = SECTION_RE.match(line)
            if sm and seen_heading:
                if int(sm.group(1)) != section + 1:
                    raise CaptionError(f"区間の番号が続いていない（本文の {no} 行目）")
                section += 1
                records.append({"header": line, "title": None, "items": []})
                continue
            fm = FENCE_OPEN_RE.match(line)
            if fm and section > 0:
                fence, first = fm.group(1), True
                continue
            if line == NO_CAPTION and section > 0:
                continue
            raise CaptionError(f"スクリプトが書く形でない行がある（本文の {no} 行目）")
        if line == fence:
            fence = None
            continue
        if first and line.startswith(TITLE_PREFIX):
            chapters[section] = line[len(TITLE_PREFIX):]
            records[-1]["title"] = chapters[section]
            first = False
            continue
        first = False
        lm = LINE_RE.match(line)
        if not lm:
            raise CaptionError(f"字幕の行の形でない（本文の {no} 行目）")
        n = int(lm.group(1))
        if n != expected:
            raise CaptionError(f"行の番号が続いていない（L{n}、本文の {no} 行目）")
        lines[n] = (lm.group(2), lm.group(3))
        records[-1]["items"].append(line)
        expected += 1
    if fence is not None:
        raise CaptionError("raw のフェンスが閉じていない")
    if not records or not lines:
        raise CaptionError("raw に区間か字幕の行がない（スクリプトは、字幕の行が 1 つ以上ある raw だけを書く）")
    # 読んだ部品から、生成器（render_raw）と同じ形の本文を作り直し、元の本文と比べる。行の種類だけを見ると、
    # 区間の中のフェンスや「この区間に字幕はない」の重なり、題名の行の追加を受け入れてしまう（実装のレビューの
    # 2 回目）。作り直したものと同じなら、スクリプトが書いた形である。
    rebuilt = ["\n", heading, "\n"]
    for rec in records:
        rebuilt.append(f"\n{rec['header']}\n\n")
        inner = ([TITLE_PREFIX + rec["title"]] if rec["title"] is not None else []) + rec["items"]
        if inner:
            body = "\n".join(inner)
            fence_text = fence_for(body)
            rebuilt.append(f"{fence_text}text\n{body}\n{fence_text}\n")
        if not rec["items"]:
            rebuilt.append(("\n" if inner else "") + NO_CAPTION + "\n")
    # FRONTMATTER_RE は、閉じの --- のあとの空行も食うので、本文の先頭の改行の有無は問わない。
    if "".join(rebuilt).lstrip("\n") != text[m.end():].lstrip("\n"):
        raise CaptionError("raw の本文が、スクリプトが書く形と違う（行の並び、フェンス、空行のどれかが違う）")
    return RawDoc(meta, lines, chapters, section, text)


# ---------------------------------------------------------------- 定義と印を書く・読む


def code_span(word: str) -> str:
    """語をコードスパンにする。中の最長のバッククォートの並びより 1 つ長いバッククォートで囲む。先頭か末尾が
    バッククォートか空白なら、囲みの内側の前後に空白を 1 つずつ足す（CommonMark の読み方で外れる）。"""
    longest = max((len(m) for m in re.findall(r"`+", word)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if word[:1] in ("`", " ") or word[-1:] in ("`", " ") else ""
    return f"{ticks}{pad}{word}{pad}{ticks}"


def format_definition(ident: str, caption: str, nth: int | None, corrected: str | None, basis: str | None) -> str:
    """定義の 1 行を作る。corrected が None なら留保の定義。"""
    nth_text = f"（{nth} つ目）" if nth else ""
    if corrected is None:
        return f"- {ident}: 字幕 {code_span(caption)}{nth_text} は留保"
    return f"- {ident}: 字幕 {code_span(caption)}{nth_text} を {code_span(corrected)} に訂正（根拠 {basis}）"


def read_code_span(text: str, pos: int) -> tuple[str, int] | None:
    """pos から始まるコードスパンを読む。（中身, 終わりの位置）か None。"""
    m = re.compile(r"`+").match(text, pos)
    if not m:
        return None
    ticks = m.group()
    i = m.end()
    while True:
        j = text.find(ticks, i)
        if j < 0:
            return None
        k = j + len(ticks)
        if (j > 0 and text[j - 1] == "`" and j - 1 >= m.end()) or (k < len(text) and text[k] == "`"):
            # ちょうど同じ長さの並びでない。並びの終わりまで飛ばす。
            run = re.compile(r"`+").match(text, j)
            i = run.end()
            continue
        inner = text[m.end():j]
        if len(inner) >= 2 and inner[0] == " " and inner[-1] == " " and inner.strip(" "):
            inner = inner[1:-1]
        return inner, k


IDENT_RE = re.compile(r"L([1-9][0-9]*)-([1-9][0-9]*)")
DEF_START_RE = re.compile(r"^- (L([1-9][0-9]*)-([1-9][0-9]*)): 字幕 ")
NTH_RE = re.compile(r"（([1-9][0-9]*) つ目）")
BASIS_RE = re.compile(r"^(metadata:title|metadata:channel|metadata:chapter:([1-9][0-9]*)|caption:L([1-9][0-9]*)"
                      r"|wiki_source:(\S.*)|model:(\S.*))$")
MARK_RE = re.compile(r"〔(訂正|留保) (?:\[\[([^\[\]\n]+)\]\] )?(L([1-9][0-9]*)-([1-9][0-9]*))〕")
MARK_START_RE = re.compile(r"〔\s*(?:訂正|留保)")


@dataclass
class Definition:
    ident: str
    line: int              # raw の行の番号
    kind: str              # "訂正" か "留保"
    caption: str
    nth: int | None
    corrected: str | None
    basis: str | None
    page: str = ""
    page_line: int = 0

    def key(self) -> tuple:
        return (self.kind, self.caption, self.nth or 1, self.corrected, self.basis)

    @property
    def basis_kind(self) -> str | None:
        return self.basis.split(":", 1)[0] if self.basis else None

    @property
    def weight(self) -> int:
        if self.kind == "留保" or self.basis_kind == "model":
            return HEAVY_WEIGHT
        return LIGHT_WEIGHT


def parse_definition(line: str) -> Definition | str:
    """定義の 1 行を読む。読めなければ理由の文字列を返す。"""
    m = DEF_START_RE.match(line)
    if not m:
        return "定義の形（- L123-1: 字幕 `語` …）でない"
    ident, n = m.group(1), int(m.group(2))
    span = read_code_span(line, m.end())
    if span is None or not span[0]:
        return "字幕の語のコードスパンを読めない"
    caption, pos = span
    nth = None
    nm = NTH_RE.match(line, pos)
    if nm:
        nth = int(nm.group(1))
        pos = nm.end()
    rest = line[pos:]
    if rest == " は留保":
        return Definition(ident, n, "留保", caption, nth, None, None)
    if not rest.startswith(" を "):
        return "字幕の語のあとが「 を `語` に訂正（根拠 …）」か「 は留保」でない"
    span = read_code_span(line, pos + 3)
    if span is None or not span[0]:
        return "訂正した語のコードスパンを読めない"
    corrected, pos = span
    tail = line[pos:]
    if not tail.startswith(" に訂正（根拠 ") or not tail.endswith("）"):
        return "訂正した語のあとが「 に訂正（根拠 種類:場所）」でない"
    basis = tail[len(" に訂正（根拠 "):-1]
    if not BASIS_RE.match(basis):
        return f"根拠の種類と場所の形が違う（{basis}）"
    if corrected == caption:
        return "訂正した語が字幕の語と同じ"
    if any(unicodedata.category(c) in INVISIBLE_CATEGORIES for c in corrected) or visible_text(corrected) != \
            unicodedata.normalize("NFKC", corrected):
        return "訂正した語に、目に見えない文字（書式の文字、制御文字など）か改行を入れない"
    return Definition(ident, n, "訂正", caption, nth, corrected, basis)


INVISIBLE_CATEGORIES = frozenset({"Cf", "Co", "Cn", "Cc", "Cs"})


def visible_text(text: str) -> str:
    """NFKC にそろえ、空白の制御文字（改行、タブ）を空白にし、目に見えない文字（書式 Cf、私用 Co、未割り当て Cn、
    そのほかの制御 Cc、サロゲート Cs）を取り除いた文字の並び。語の境目を照らすのに使う。"""
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) not in INVISIBLE_CATEGORIES:
            out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


def char_class(ch: str) -> str | None:
    """語の境目を確かめるための文字の種類。全角と半角は同じ種類にする（NFKC にそろえる）。

    種類は、文字の範囲を並べず、Unicode の分類と文字の名前の文字体系で決める。範囲を並べると、補助面の
    カタカナ（U+1B155 など）を見落とした（実装のレビューの 10 回目）。数字は N の全体を "digit"、文字（L）は名前の
    最初の語（KATAKANA、LATIN、GREEK、CYRILLIC、HANGUL など）を小文字にしたものにする。漢字（CJK）とひらがなは、
    語を空白で区切らないので照らさない（None。ADR-0027 で受け入れた限界）。
    """
    if not ch:
        return None
    c = unicodedata.normalize("NFKC", ch)[:1]
    if not c:
        return None
    cat = unicodedata.category(c)
    if cat.startswith("N"):
        return "digit"
    if not cat.startswith("L"):
        return None
    name = unicodedata.name(c, "")
    script = name.split(" ", 1)[0] if name else ""
    if script.startswith("KATAKANA") or ("KATAKANA" in name and script in ("HALFWIDTH", "CIRCLED", "SQUARED")):
        return "katakana"
    if script in ("", "CJK", "HIRAGANA", "IDEOGRAPHIC"):
        return None
    return script.lower()


def tail_problem(prefix: str, word: str, context: str | None = None) -> str | None:
    """印の直前の本文の末尾が word で、語の途中で切れていないかを確かめる。

    prefix は、表示される文字の並びのうち、印と同じ続き（書式、リンク、コード、改行で切れない本文の文字）の、
    印より前の部分である。語はこの続きの中で終わらなければならない（印の直前の語に書式を付けない）。context は、
    同じ inline の表示される文字の、印より前の全体で、語の前の文字の種類はこちらで見る（`**1**5` の 1 は、
    続きの外にあるが、表示では 5 の前の文字である）。
    """
    if not prefix.endswith(word):
        return f"印の直前の本文の末尾が `{word}` でない（印の直前の語に書式やリンクを付けない）"
    # 語の前の文字は、NFKC にそろえた表示の文字で見る。NFKC は、全角と半角（`ﾄﾞ`）、合成済みの文字と分解した
    # 文字（`ト` と結合の濁点）をそろえる。1 つ前のコードポイントだけを見ると、濁点や半角の濁点で語の途中の
    # 切れ目を見落とした（実装のレビューの 6 回目と 7 回目）。NFKC にした語で、表示の末尾が語で終わらない
    # （語の始まりが前の文字と合成された）か、語の前に字母でない記号（結合文字、半角の濁点など）が残るときも誤りにする。
    # 境目は、目に見える文字の並び（visible_text）で照らす。目に見えない文字（WORD JOINER、ZERO WIDTH JOINER、
    # SOFT HYPHEN など）は表示で語をつなげたまま切れ目を作るので、取り除いてから前の文字を見る。目に見えない
    # 文字を語の前に置いても、語の先頭に入れても、取り除いたあとの前の文字で語の途中の切れ目が分かる（実装の
    # レビューの 8 回目と 9 回目）。改行は、閲覧と同じく空白にする。
    text = visible_text(prefix if context is None else context)
    nword = visible_text(word)
    if not nword:
        return f"印の直前の `{word}` に、目に見える文字がない"
    if not text.endswith(nword) or unicodedata.category(nword[0]).startswith("M"):
        return f"印の直前の `{word}` の始まりが、前の文字と結合している"
    before = text[: len(text) - len(nword)][-1:]
    if before and (unicodedata.category(before)[0] == "M" or before in "\u309b\u309c\uff9e\uff9f"):
        return f"印の直前の `{word}` の前に、結合する文字（`{before!r}`）がある"
    cls = char_class(nword[:1])
    if before and cls is not None and char_class(before) == cls:
        return f"印の直前の `{word}` が語の途中で切れている（前の文字 `{before}`）"
    return None


@dataclass
class Mark:
    kind: str
    link: str | None
    ident: str
    line: int              # raw の行の番号
    page: str
    page_line: int
    prefix: str
    context: str = ""


@dataclass
class PageScan:
    marks: list = field(default_factory=list)
    definitions: list = field(default_factory=list)
    errors: list = field(default_factory=list)      # (ページの行, code, message)
    prose: list = field(default_factory=list)       # 「raw にない語」を探す本文の行


@dataclass
class InlineText:
    """1 つの inline（段落、見出し、表のセル、リストの項目の文）の、表示される文字の並び。

    display は、閲覧（wiki_browse_core.inline_text）と同じ規則で作った表示の文字である。バックスラッシュの
    エスケープと文字参照は元の文字に戻り、wikilink は表示名に、行き先を残さない Markdown のリンクは
    「文字（行き先）」に、画像は代替の文字になる。文字ごとに次を持つ。

    - kinds: "text"（本文）、"link"（wikilink の表示名）、"code"（コードスパン）、"break"（改行）、"image"（画像の
      代替の文字）、"other"（リンクの行き先の添え書き）
    - runs: 書式、Markdown のリンク、コード、改行で切れない続きの番号（wikilink では切れない）
    - segs: wikilink の表示名の文字なら、その wikilink の番号（ほかは -1）
    - lines: 本文の行（0 から）

    targets は、wikilink の番号から行き先（`[[行き先|表示名]]` の行き先）を引く。
    """
    display: str
    kinds: list
    runs: list
    segs: list
    lines: list
    targets: dict
    line: int
    quote: bool
    workflow_value: bool = False   # `## 取り込んだ内容と載せたページ` の表の掲載方法を示す列で、手順が決めた値だけのセル


def _wikilink_label(match: "re.Match") -> str:
    return (match.group(2) or match.group(1)).strip()


def inline_texts(body_lines: list[str]) -> list[InlineText]:
    """本文を、表示と同じパーサーで読み、inline ごとの表示される文字の並びを返す。

    印と語の境目は、この並びだけで照らす。読み手に見えるものと照合が読むものを 1 つにするためである。原文の
    字面やかけらで照らすと、コードの境目（実装のレビューの 1 回目）、書式の前の文字と書式の入った印（2 回目）、
    wikilink の表示名（3 回目）、wikilink の行き先に隠れた印（4 回目）で、表示と照合が食い違った。
    """
    import wiki_browse_core
    import wiki_pages
    tokens = wiki_pages.new_parser().parse("\n".join(body_lines))
    out: list[InlineText] = []
    depth = 0
    # 取り込みで記録した表の掲載方法を示す列を、パーサーの見出しと表のトークンから見分ける。
    section = ""
    headers: list = []
    col = -1
    cell = None        # 次の inline が入るセル（"th" か "td"）
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.level == 0 and tok.tag in ("h1", "h2"):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            section = nxt.content.strip() if tok.tag == "h2" and nxt is not None else ""
        elif tok.type == "table_open":
            headers = []
        elif tok.type == "tr_open":
            col = -1
        elif tok.type in ("th_open", "td_open"):
            col += 1
            cell = tok.type[:2]
        elif tok.type in ("th_close", "td_close"):
            cell = None
        if tok.type == "blockquote_open":
            depth += 1
            continue
        if tok.type == "blockquote_close":
            depth -= 1
            continue
        if tok.type != "inline" or tok.map is None:
            continue
        workflow_value = False
        if cell == "th":
            headers.append(tok.content.strip())
        elif (cell == "td" and section in CLAIMS_HEADINGS and 0 <= col < len(headers)
              and headers[col] in CLAIMS_DESTINATION_COLUMNS and tok.content.strip() in CLAIMS_DESTINATIONS):
            workflow_value = True
        chars: list = []
        kinds: list = []
        runs: list = []
        segs: list = []
        lines: list = []
        targets: dict = {}
        state = {"line": tok.map[0], "run": 0, "seg": 0}
        pending: list = []

        def put(text: str, kind: str, seg: int = -1):
            for ch in text:
                chars.append(ch)
                kinds.append(kind)
                runs.append(state["run"])
                segs.append(seg)
                lines.append(state["line"])

        prev_text = False
        children = list(tok.children or [])
        for i, child in enumerate(children):
            if child.type == "text":
                if not prev_text:
                    state["run"] += 1
                prev_text = True
                pos = 0
                for m in wiki_pages.WIKILINK_FULL_RE.finditer(child.content):
                    put(child.content[pos:m.start()], "text")
                    state["seg"] += 1
                    targets[state["seg"]] = m.group(1).strip()
                    put(_wikilink_label(m), "link", state["seg"])
                    pos = m.end()
                put(child.content[pos:], "text")
                continue
            prev_text = False
            state["run"] += 1
            if child.type == "code_inline":
                put(child.content, "code")
            elif child.type in ("softbreak", "hardbreak"):
                put("\n", "break")
                state["line"] += 1
            elif child.type == "image":
                put(child.content, "image")
            elif child.type == "link_open":
                keep = wiki_browse_core._keeps_link(children, i) or child.markup == "autolink"
                pending.append(None if keep else wiki_browse_core.display_href(child.attrs.get("href", "")))
            elif child.type == "link_close":
                suffix = pending.pop() if pending else None
                if suffix is not None:
                    put(f"（{suffix}）", "other")
            # そのほか（強調の開きと閉じ）は文字を持たず、続きを切るだけである。
            state["run"] += 1
        out.append(InlineText("".join(chars), kinds, runs, segs, lines, targets, tok.map[0], depth > 0,
                              workflow_value))
    return out


def _line_kinds(body_lines: list[str]) -> tuple[list[str], list[tuple[int, int]]]:
    """本文の行の種類（code、quote、other）と、定義の節の行の範囲を返す。"""
    import wiki_pages
    kinds = ["other"] * len(body_lines)
    def_ranges = []
    tokens = wiki_pages.new_parser().parse("\n".join(body_lines))
    open_def = None
    for i, tok in enumerate(tokens):
        if tok.type in ("fence", "code_block", "html_block") and tok.map:
            for j in range(tok.map[0], min(tok.map[1], len(body_lines))):
                kinds[j] = "code"
        elif tok.type == "blockquote_open" and tok.map:
            for j in range(tok.map[0], min(tok.map[1], len(body_lines))):
                if kinds[j] != "code":
                    kinds[j] = "quote"
        elif tok.type == "heading_open" and tok.map and tok.level == 0:
            if open_def is not None and tok.tag in ("h1", "h2"):
                def_ranges.append((open_def, tok.map[0]))
                open_def = None
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            if tok.tag == "h2" and inline is not None and inline.content.strip() == DEF_HEADING:
                open_def = tok.map[1]
    if open_def is not None:
        def_ranges.append((open_def, len(body_lines)))
    return kinds, def_ranges


# 表示の文字の中の印。表示名の部分（関係するページの [[要約ページ]]）は、1 つの wikilink の表示名の全体でなければ
# ならない（下の _read_mark が確かめる）。
DISPLAY_MARK_RE = re.compile(r"〔(訂正|留保) (?:([^〔〕\n]+?) )?(L([1-9][0-9]*)-([1-9][0-9]*))〕")


def _read_mark(it: InlineText, start: int):
    """表示の文字の start から始まる印を読む。決まった形なら（種類, 行き先か None, 識別子, 行, 終わり）、
    そうでなければ None。"""
    mk = DISPLAY_MARK_RE.match(it.display, start)
    if not mk:
        return None
    span = range(mk.start(), mk.end())
    if len({it.runs[i] for i in span}) != 1:
        return None
    link_span = range(mk.start(2), mk.end(2)) if mk.group(2) is not None else range(0)
    for i in span:
        if i in link_span:
            if it.kinds[i] != "link":
                return None
        elif it.kinds[i] != "text":
            return None
    target = None
    if link_span:
        seg_ids = {it.segs[i] for i in link_span}
        if len(seg_ids) != 1:
            return None
        seg = next(iter(seg_ids))
        whole = [i for i, s in enumerate(it.segs) if s == seg]
        if whole != list(link_span):
            return None
        target = it.targets[seg]
    return mk.group(1), target, mk.group(3), int(mk.group(4)), mk.end()


def _is_code_example(it: InlineText, start: int) -> bool:
    """start から始まる印の始まりが、書き方の例（決まった形の印の全体が、1 つのコードスパンの中にあるもの）か。

    始まりの部分だけがコードの中にあるもの（`` 50`〔訂正` L1-1〕 ``）は例でなく、形の誤りにする（実装のレビューの
    5 回目）。
    """
    if it.kinds[start] != "code":
        return False
    mk = DISPLAY_MARK_RE.match(it.display, start)
    if not mk:
        return False
    run = it.runs[start]
    return all(it.kinds[i] == "code" and it.runs[i] == run for i in range(mk.start(), mk.end()))


# 取り込み手順 7b の表。旧見出しのページも同じ規則で検査する。
CLAIMS_HEADINGS = ("取り込んだ内容と載せたページ", "主張と行き先")
CLAIMS_DESTINATION_COLUMNS = ("掲載方法", "行き先")
CLAIMS_DESTINATIONS = ("create", "update", "source_only")


def _prose(it: InlineText) -> str:
    """「raw にない語」を探す文字（本文、wikilink の表示名、画像の代替の文字。コードは空白にし、印を除く）。

    `## 取り込んだ内容と載せたページ` の表の「掲載方法」（旧表記は「行き先」）の列で、中身がちょうど手順の値（create、update、source_only）のセルは、本文の
    語でないので探さない。節や字面で外すと、表の主張や本文の同じ語まで隠れた（wiki 0.23.2 のレビューの 1 回目と 2 回目）。
    """
    if it.workflow_value:
        return ""
    text = "".join(ch if k in ("text", "link", "image") else " " for ch, k in zip(it.display, it.kinds))
    return DISPLAY_MARK_RE.sub(" ", text)


def scan_page(rel: str, text: str) -> PageScan:
    """ページの印と定義を読む。行の番号はファイルの 1 から数える。

    定義は、定義の節の行の原文から読む（コードスパンの書き方を決まった形で照らすため）。印は、表示される文字の
    並び（inline_texts）だけで読む。表示に現れた印の始まり（`〔訂正`、`〔留保`）は、全体がコードスパンの中の
    もの（書き方の例）を除き、どれも決まった形の印でなければ形の誤りにする。表示に現れない文字（リンクの行き先
    など）の中の印は、読み手に見えないので印として数えない（定義があれば、使われていない定義になる）。
    """
    scan = PageScan()
    m = FRONTMATTER_RE.match(text)
    offset = text[: m.end()].count("\n") if m else 0
    body = text[m.end():] if m else text
    body_lines = body.split("\n")
    # 印の文字（〔）は文字参照でも書けるので、& のあるページも読む。
    if "〔" not in body and DEF_HEADING not in body and "&" not in body:
        return scan
    kinds, def_ranges = _line_kinds(body_lines)
    in_def = set()
    if len(def_ranges) > 1:
        scan.errors.append((offset + def_ranges[1][0] + 1, "two_definition_sections",
                            f"`## {DEF_HEADING}` の節が 2 つ以上ある"))
    for start, end in def_ranges[:1]:
        in_def.update(range(start, end))
    seen = set()
    for j in sorted(in_def):
        line = body_lines[j]
        if kinds[j] == "code" or not line.strip():
            continue
        no = offset + j + 1
        d = parse_definition(line)
        if isinstance(d, str):
            scan.errors.append((no, "bad_definition", d))
            continue
        d.page, d.page_line = rel, no
        if d.ident in seen:
            scan.errors.append((no, "duplicate_definition", f"{d.ident} を同じページで 2 回定義している"))
            continue
        seen.add(d.ident)
        scan.definitions.append(d)
    for it in inline_texts(body_lines):
        for sm in MARK_START_RE.finditer(it.display):
            if _is_code_example(it, sm.start()):
                continue
            no = offset + it.lines[sm.start()] + 1
            read = _read_mark(it, sm.start())
            if read is None:
                scan.errors.append((no, "bad_mark", "〔訂正 …〕か〔留保 …〕の印の形が違う（印の中に書式、リンク、"
                                                    "コード、改行を入れない。[[要約ページ]] は 1 つの wikilink にする）"))
                continue
            kind, target, ident, line_no, _end = read
            if it.lines[sm.start()] in in_def:
                scan.errors.append((no, "mark_in_definitions", f"`## {DEF_HEADING}` の節に印を置かない"))
                continue
            if it.quote:
                scan.errors.append((no, "mark_in_quote", "引用の中に印を置かない（引用は raw の逐語のままにする）"))
                continue
            # 語は、印と同じ続きの、本文の文字（wikilink の表示名でない）の中で終わらなければならない。
            run = it.runs[sm.start()]
            j = sm.start()
            while j > 0 and it.runs[j - 1] == run and it.kinds[j - 1] == "text":
                j -= 1
            scan.marks.append(Mark(kind, target, ident, line_no, rel, no, it.display[j:sm.start()],
                                   it.display[:sm.start()]))
        if not it.quote and it.line not in in_def:
            scan.prose.append(_prose(it))
    return scan


# ---------------------------------------------------------------- 照合


@dataclass
class Finding:
    path: str
    line: int
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"path": self.path, "line": self.line, "code": self.code, "message": self.message}


@dataclass
class RawResult:
    raw: str                     # raw の参照（wiki の親からのパス）
    video_id: str
    caption_kind: str
    parent: str | None
    summaries: list
    definitions: list
    confidence: str | None       # 計算した値
    written: str | None          # 親の要約ページに書かれた値
    weight: int
    rate: float | None
    reasons: list

    def as_dict(self) -> dict:
        return {
            "raw": self.raw, "video_id": self.video_id, "caption_kind": self.caption_kind, "parent": self.parent,
            "summaries": self.summaries, "confidence": self.confidence, "written_confidence": self.written,
            "weight": self.weight, "rate": self.rate, "reasons": self.reasons,
            "definitions": [{
                "id": d.ident, "kind": d.kind, "caption": d.caption, "nth": d.nth, "corrected": d.corrected,
                "basis": d.basis, "weight": d.weight, "page": d.page, "page_line": d.page_line,
            } for d in self.definitions],
        }


def occurrences(text: str, word: str) -> int:
    """text の中の word の出現の数（重ならない数え方、左から）。"""
    count = 0
    i = text.find(word)
    while i >= 0:
        count += 1
        i = text.find(word, i + len(word))
    return count


def check_definition(d: Definition, raw: RawDoc, other_raw_texts) -> str | None:
    """(1) 字幕の語の位置と (2) 根拠の場所を確かめる。other_raw_texts(slug) は、そのスラッグのソースの raw の
    文字列の並び（ない、または同じ raw なら空）。"""
    if d.line not in raw.lines:
        return f"{d.ident} の行 L{d.line} が raw にない（字幕の行は L1〜L{raw.count}）"
    line_text = raw.lines[d.line][1]
    count = occurrences(line_text, d.caption)
    if count == 0:
        return f"{d.ident} の字幕の語 `{d.caption}` が、raw の L{d.line} にない"
    if d.nth is None and count >= 2:
        return f"{d.ident} の字幕の語 `{d.caption}` は L{d.line} に {count} 回出るので、（N つ目）を書く"
    if d.nth is not None and d.nth > count:
        return f"{d.ident} の字幕の語 `{d.caption}` は L{d.line} に {count} 回しか出ない（{d.nth} つ目と書いた）"
    if d.kind == "留保":
        return None
    basis = d.basis or ""
    w = d.corrected or ""
    if basis == "metadata:title":
        if w not in str(raw.meta.get("title") or ""):
            return f"{d.ident} の訂正した語 `{w}` が、raw の題名にない"
    elif basis == "metadata:channel":
        if w not in str(raw.meta.get("channel") or ""):
            return f"{d.ident} の訂正した語 `{w}` が、raw のチャンネルの名前にない"
    elif basis.startswith("metadata:chapter:"):
        n = int(basis.rsplit(":", 1)[1])
        if n not in raw.chapters:
            return f"{d.ident} の根拠の区間 {n} に、チャプターの題名がない"
        if w not in raw.chapters[n]:
            return f"{d.ident} の訂正した語 `{w}` が、区間 {n} のチャプターの題名にない"
    elif basis.startswith("caption:L"):
        n = int(basis[len("caption:L"):])
        if n == d.line:
            return f"{d.ident} の根拠に、同じ行 L{n} を使えない"
        if n not in raw.lines:
            return f"{d.ident} の根拠の行 L{n} が raw にない"
        if w not in raw.lines[n][1]:
            return f"{d.ident} の訂正した語 `{w}` が、raw の L{n} にない"
    elif basis.startswith("wiki_source:"):
        slug = basis[len("wiki_source:"):]
        try:
            texts = other_raw_texts(slug)
        except EvidenceUnavailable as exc:
            return f"{d.ident} の根拠のソース `{slug}` の raw を使えない（{exc}）"
        problems = getattr(texts, "problems", [])
        if not texts:
            if problems:
                return f"{d.ident} の根拠のソース `{slug}` の raw を使えない（{'、'.join(problems)}）"
            return f"{d.ident} の根拠のソース `{slug}` の raw が見つからない（同じ raw は使えない）"
        if not any(w in t for t in texts):
            if problems:
                return f"{d.ident} の訂正した語 `{w}` を、ソース `{slug}` の raw で確かめられない（{'、'.join(problems)}）"
            return f"{d.ident} の訂正した語 `{w}` が、ソース `{slug}` の raw にない"
    elif basis.startswith("model:"):
        pass
    else:
        return f"{d.ident} の根拠の形が違う（{basis}）"
    return None


def compute_confidence(caption_kind: str, definitions: list, count: int) -> tuple[str | None, int, float | None, list]:
    """confidence を計算する。（値, 重みの合計, 疑いの率, 理由）。"""
    if caption_kind not in CAPTION_KINDS:
        return None, 0, None, [f"caption_kind が {caption_kind!r} で、出発点を決められない"]
    value = "high" if caption_kind == "uploaded" else "medium"
    reasons = [f"出発点: caption_kind が {caption_kind} なので {value}"]
    weight = sum(d.weight for d in definitions)
    heavy = [d.ident for d in definitions if d.weight == HEAVY_WEIGHT]
    if heavy and value == "high":
        value = "medium"
        reasons.append(f"model の訂正か留保がある（{'、'.join(heavy[:5])}{' ほか' if len(heavy) > 5 else ''}）ので medium")
    rate = (weight * 100.0 / count) if count else None
    if rate is not None and rate >= LOW_RATE:
        value = "low"
        reasons.append(f"疑いの率が {rate:.2f}（重み {weight} ÷ 字幕の行 {count} × 100）で {LOW_RATE:g} 以上なので low")
    elif rate is not None:
        reasons.append(f"疑いの率は {rate:.2f}（重み {weight} ÷ 字幕の行 {count} × 100）")
    return value, weight, rate, reasons


@dataclass
class PageIn:
    rel: str               # wiki からの相対パス（/ 区切り、NFC）
    text: str
    meta: dict
    slug: str


@dataclass
class CheckResult:
    findings: list
    raws: list
    unknown_words: dict
    raw_warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"findings": [f.as_dict() for f in self.findings], "raws": [r.as_dict() for r in self.raws],
                "unknown_words": self.unknown_words, "raw_warnings": self.raw_warnings}


def _type(meta: dict) -> str:
    from wiki_text import canonical_type
    value = meta.get("type")
    return str(canonical_type(value)) if isinstance(value, str) else ""


def _refs(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def resolve_raw_path(ref: str, page_path: Path, wiki_root: Path) -> Path | None:
    """単独の参照用の互換口。共通の raw 解決で 1 つに決まった通常のファイルだけを返す。"""
    from wiki_pages import resolve_raw
    result = resolve_raw({"type": "source", "raw": ref}, page_path, wiki_root)[0]
    return result.path if result.state == "resolved" and result.path.is_file() else None


def raw_key(path: Path) -> str:
    """raw のファイルの実体の鍵。Windows の大文字と小文字の違いや、`..` を含む書き方で、同じ raw を別の raw として
    数えず（実装のレビューの 4 回目）、大文字と小文字を区別するディレクトリの別のファイルを同じ raw として数えない
    （15 回目）。決め方は _identity と同じである。"""
    return _identity(path)


def raw_label(path: Path, wiki_root: Path) -> str:
    base = Path(os.path.abspath(wiki_root)).parent
    p = Path(os.path.abspath(path))
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return p.as_posix()


def _word_runs(text: str):
    """文字の種類（char_class）が同じ文字の続きを、（種類, 続き）で返す。数字、英字などの文字体系、カタカナ。"""
    run, kind = [], None
    for ch in text:
        cls = char_class(ch)
        if cls is not None and cls == kind:
            run.append(ch)
            continue
        if run:
            yield kind, "".join(run)
        run, kind = ([ch], cls) if cls is not None else ([], None)
    if run:
        yield kind, "".join(run)


_URL_RE = re.compile(r"https?://\S+|\]\([^)]*\)")


def unknown_words(prose: list[str], raw: RawDoc, corrected: list[str]) -> list[str]:
    """本文（引用、コード、印、定義を除く）の数字の並びと、2 文字以上の英字かカタカナの並びのうち、raw（字幕と
    frontmatter）にも定義の訂正した語にもないもの。印を付けずに直した語を見つける目印で、保証ではない。"""
    hay = unicodedata.normalize("NFKC", raw.text).casefold()
    hay_corr = [unicodedata.normalize("NFKC", c).casefold() for c in corrected]
    out = []
    seen = set()
    for line in prose:
        line = _URL_RE.sub(" ", unicodedata.normalize("NFKC", line))
        line = re.sub(r"\[\[[^\]]*\]\]", lambda m: m.group().replace("[", " ").replace("]", " "), line)
        # 語の続きは、語の境目と同じ文字の種類（char_class）で切る。範囲を並べた正規表現にすると、補助面の
        # カタカナなどが漏れた（実装のレビューの 11 回目）。数字は 1 文字から、ほかは 2 文字から挙げる。
        for kind, word in _word_runs(line):
            if kind != "digit" and len(word) < 2:
                continue
            key = word.casefold()
            if key in seen:
                continue
            seen.add(key)
            if key in hay or any(key in c for c in hay_corr):
                continue
            out.append(word)
    return out


def check_pages(pages: list[PageIn], wiki_root: Path, only_raw: str | None = None,
                unknown_for: set | None = None, *, raw_roots=(), recorded_raw_roots=(),
                wiki_root_given=None) -> CheckResult:
    """wiki のページの集合を照らす（ADR-0027 の (1)〜(5)）。

    指摘は、いつも wiki 全体のものを返す。1 つの raw に関わる指摘だけに絞ると、「関わる」の決め方の漏れ（分類から
    外したページ、壊れた印のページ、別の綴りの raw）から指摘が消えた（実装のレビューの 2〜4 回目）。only_raw
    （raw_label の形か、raw のパス）を渡すと、raws の結果と「raw にない語」だけを、その raw に絞る。unknown_for に
    挙げたページについて「raw にない語」を返す。
    """
    wiki_root = Path(wiki_root)
    findings: list[Finding] = []
    by_slug: dict = {}
    by_path: dict = {}
    for p in pages:
        by_slug.setdefault(p.slug, []).append(p)
        rel = p.rel[:-3] if p.rel.lower().endswith(".md") else p.rel
        by_path.setdefault(rel, []).append(p)

    raw_cache: dict = {}

    def load(path: Path):
        key = raw_key(path)
        if key not in raw_cache and path.suffix.lower() != ".md":
            # 動画の raw は、wiki_youtube.py がいつも .md で書く。PDF などのほかの形式の原本は動画の raw でないので、
            # 読まずに動画でないものとする（UTF-8 で読もうとして誤りにしない。to884/info-hive-wiki#74 のレビュー）。
            raw_cache[key] = (None, None, None)
        if key not in raw_cache:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                raw_cache[key] = (None, f"raw を読めない（{exc}）", None)
            else:
                meta = read_meta(text) or {}
                if not meta.get("video_id"):
                    raw_cache[key] = (None, None, text)
                else:
                    try:
                        raw_cache[key] = (parse_raw(text), None, text)
                    except CaptionError as exc:
                        raw_cache[key] = (None, f"動画の raw を読めない（{exc}）", text)
        return raw_cache[key]

    # 拒否はすべて知らせる。ただし字幕の欠陥として止めるのは、動画と結び付く参照だけである。
    # 訂正の根拠に使う他の source は、other_raw_texts と check_definition が依存する時点で検査する。
    from wiki_pages import resolve_raw
    paths_by_page = {}
    problems_by_page = {}
    raw_warnings = []
    scans = {p.rel: scan_page(p.rel, p.text) for p in pages}
    relevant = {}
    selected_path = None
    if only_raw:
        selected_path = Path(os.path.abspath(only_raw if os.path.isabs(only_raw) else wiki_root.parent / only_raw))
    for p in pages:
        paths_by_page[p.rel] = []
        problems_by_page[p.rel] = []
        is_video = bool(p.meta.get("video_id") or p.meta.get("caption_kind") or scans[p.rel].definitions)
        relevant[p.rel] = is_video
        for resolved in resolve_raw(p.meta, wiki_root / p.rel, wiki_root, wiki_root_given,
                                    raw_roots=raw_roots, recorded_raw_roots=recorded_raw_roots):
            selected = selected_path is not None and any(Path(c) == selected_path for c in resolved.candidates)
            relevant[p.rel] = relevant[p.rel] or selected
            if resolved.state not in ("resolved", "outside") or (resolved.state == "outside" and resolved.candidates):
                problems_by_page[p.rel].append(f"{resolved.raw}: {resolved.state}: {resolved.reason}")
            if (resolved.state in ("link", "unknown", "ambiguous")
                    or (resolved.state == "outside" and resolved.candidates)
                    or (resolved.state == "missing" and resolved.mismatches)):
                raw_warnings.append({"source": p.rel, "raw": resolved.raw, "state": resolved.state,
                                     "reason": resolved.reason, "candidates": resolved.candidates,
                                     "links": resolved.links, "unchecked": resolved.unchecked,
                                     "mismatches": resolved.mismatches})
                if is_video or selected:
                    findings.append(Finding(p.rel, 1, "unreadable_raw", f"{resolved.raw}: {resolved.reason}"))
            elif resolved.state == "resolved" and resolved.path.is_file():
                paths_by_page[p.rel].append(resolved.path)
            if resolved.parents_unchecked:
                raw_warnings.append({"source": p.rel, "raw": resolved.raw, "state": "parents_unchecked",
                                     "candidates": resolved.parents_unchecked})
            if resolved.state == "resolved" and resolved.version_unverified and not resolved.external:
                raw_warnings.append({"source": p.rel, "raw": resolved.raw, "state": "version_unverified"})
            if resolved.state == "resolved" and resolved.scope_unverified:
                raw_warnings.append({"source": p.rel, "raw": resolved.raw, "state": "scope_unverified",
                                     "reason": "raw の根が未指定のため、外の原本かは未確認"})

    # 要約ページ（type が source で、raw が動画の raw のもの）を集める。
    summaries: dict = {}        # rel -> (raw の鍵, RawDoc)
    raw_docs: dict = {}         # 鍵 -> (RawDoc, raw の実体のファイル名の stem, label)
    for p in pages:
        if _type(p.meta) != "source":
            continue
        videos = []
        for path in paths_by_page[p.rel]:
            doc, err, _text = load(path)
            key = raw_key(path)
            real = Path(os.path.realpath(path))
            label = raw_label(real, wiki_root)
            if err:
                if relevant[p.rel]:
                    findings.append(Finding(p.rel, 1, "unreadable_raw", f"{label}: {err}"))
                else:
                    raw_warnings.append({"source": p.rel, "raw": label, "state": "unknown", "reason": err})
                continue
            if doc is None:
                continue
            raw_docs[key] = (doc, nfc(real.stem), label)
            if key not in [v[0] for v in videos]:
                videos.append((key, doc))
        if len(videos) == 1:
            summaries[p.rel] = videos[0]
        elif len(videos) > 1:
            # どの raw の行と照らし、どの raw の confidence に数えるかを決められない。黙って 1 つを選ばない。
            findings.append(Finding(p.rel, 1, "several_video_raws",
                                    "動画の要約ページは、動画の raw を 1 つだけ `raw` に持つ（"
                                    + "、".join(raw_docs[v[0]][2] for v in videos) + "）"))

    def other_raw_texts(slug: str) -> list[str]:
        texts = []
        problems = []
        for sp in by_slug.get(nfc(slug), []):
            if _type(sp.meta) != "source":
                continue
            problems.extend(problems_by_page[sp.rel])
            for path in paths_by_page[sp.rel]:
                _doc, err, text = load(path)
                if err or text is None:
                    if err:
                        problems.append(err)
                    continue
                texts.append(text)
        return EvidenceTexts(texts, problems)

    for p in pages:
        for no, code, msg in scans[p.rel].errors:
            findings.append(Finding(p.rel, no, code, msg))

    # 要約ページの定義と印。
    defs_by_raw: dict = {}
    for p in pages:
        scan = scans[p.rel]
        if p.rel not in summaries:
            for d in scan.definitions:
                findings.append(Finding(p.rel, d.page_line, "definition_outside_summary",
                                        f"{d.ident} の定義は、動画の raw を `raw` に持つ要約ページにだけ書く"))
            continue
        key, doc = summaries[p.rel]
        written_kind = p.meta.get("caption_kind")
        if written_kind != doc.caption_kind:
            findings.append(Finding(p.rel, 1, "caption_kind_mismatch",
                                    f"要約ページの caption_kind（{written_kind!r}）が raw の値（{doc.caption_kind!r}）と違う"))
        defs = {}
        for d in scan.definitions:
            problem = check_definition(d, doc, lambda s: other_raw_texts(s).without(doc.text))
            if problem:
                findings.append(Finding(p.rel, d.page_line, "bad_definition", problem))
            defs[d.ident] = d
            defs_by_raw.setdefault(key, []).append(d)
        marked = set()
        for mk in scan.marks:
            if mk.link is not None:
                findings.append(Finding(p.rel, mk.page_line, "link_in_summary_mark",
                                        f"要約ページの印には [[…]] を付けない（{mk.ident}）"))
                continue
            d = defs.get(mk.ident)
            if d is None:
                findings.append(Finding(p.rel, mk.page_line, "undefined_mark",
                                        f"{mk.ident} の印に、このページの定義がない"))
                continue
            marked.add(mk.ident)
            _check_mark(mk, d, findings)
        for ident, d in defs.items():
            if ident not in marked:
                findings.append(Finding(p.rel, d.page_line, "unused_definition",
                                        f"{ident} の定義に、このページの印がない"))

    # 関係するページの印。
    for p in pages:
        if p.rel in summaries:
            continue
        for mk in scans[p.rel].marks:
            if mk.link is None:
                findings.append(Finding(p.rel, mk.page_line, "mark_without_summary",
                                        f"要約ページでないページの印には、[[要約ページ]] を書く（{mk.ident}）"))
                continue
            from wiki_pages import link_lookup
            lookup = link_lookup(mk.link)
            targets = []
            if lookup is not None:
                kind, target = lookup
                targets = (by_path if kind == "path" else by_slug).get(target, [])
            if len(targets) != 1 or targets[0].rel not in summaries:
                findings.append(Finding(p.rel, mk.page_line, "bad_mark_summary",
                                        f"[[{mk.link}]] が、動画の raw を持つ要約ページ 1 つに当たらない（{mk.ident}）"))
                continue
            target = targets[0]
            d = next((x for x in scans[target.rel].definitions if x.ident == mk.ident), None)
            if d is None:
                findings.append(Finding(p.rel, mk.page_line, "undefined_mark",
                                        f"[[{mk.link}]] に {mk.ident} の定義がない"))
                continue
            _check_mark(mk, d, findings)

    # raw ごとに、定義の食い違い、同じ位置、confidence。
    results = []
    result_keys: list = []
    only_key = None
    if only_raw is not None:
        only_path = Path(only_raw)
        only_key = raw_key(only_path if only_path.is_absolute() else wiki_root.parent / only_path)
    for key, (doc, stem, label) in raw_docs.items():
        defs = defs_by_raw.get(key, [])
        merged: dict = {}
        positions: dict = {}
        for d in defs:
            if d.ident in merged:
                if merged[d.ident].key() != d.key():
                    findings.append(Finding(d.page, d.page_line, "conflicting_definition",
                                            f"{d.ident} の定義が {merged[d.ident].page} の定義と違う"))
                continue
            merged[d.ident] = d
            pos = (d.line, d.caption, d.nth or 1)
            if pos in positions and positions[pos] != d.ident:
                findings.append(Finding(d.page, d.page_line, "same_position",
                                        f"{d.ident} と {positions[pos]} が、L{d.line} の同じ `{d.caption}` を指す"))
            positions.setdefault(pos, d.ident)
        unique = list(merged.values())
        value, weight, rate, reasons = compute_confidence(doc.caption_kind, unique, doc.count)
        if value is None:
            findings.append(Finding(label, 1, "bad_caption_kind", reasons[0]))
        mine = sorted(rel for rel, (k, _d) in summaries.items() if k == key)
        parent = next((rel for rel in mine if nfc(Path(rel).stem) == stem), None)
        written = None
        if not mine:
            findings.append(Finding(label, 1, "no_summary",
                                    f"raw {label} を照らせる要約ページがない（raw を 1 つだけ持つ要約ページが要る）"))
        if parent is None:
            if mine:
                findings.append(Finding(mine[0], 1, "no_parent_summary",
                                        f"raw {label} の親の要約ページ（スラッグが {stem}）がない"))
        else:
            ppage = next(p for p in pages if p.rel == parent)
            written = ppage.meta.get("confidence")
            if written is None:
                findings.append(Finding(parent, 1, "missing_confidence",
                                        f"動画のソースの親の要約ページに confidence がない（計算した値は {value}）"))
            elif written not in CONFIDENCE_VALUES:
                findings.append(Finding(parent, 1, "bad_confidence", f"confidence の値 {written!r} が high、medium、low でない"))
            elif value is not None and written != value:
                findings.append(Finding(parent, 1, "confidence_mismatch",
                                        f"confidence は {written} と書かれているが、計算すると {value}（{'。'.join(reasons)}）"))
        for rel in mine:
            if rel != parent:
                child = next(p for p in pages if p.rel == rel)
                if "confidence" in child.meta:
                    findings.append(Finding(rel, 1, "confidence_on_child",
                                            "confidence は親の要約ページにだけ書く（分割したページには書かない）"))
        results.append(RawResult(label, doc.video_id, doc.caption_kind, parent, mine, unique, value, written,
                                 weight, rate, reasons))
        result_keys.append(key)

    unknown: dict = {}
    if unknown_for:
        for p in pages:
            if p.rel not in unknown_for:
                continue
            docs = []
            if p.rel in summaries:
                docs.append(summaries[p.rel])
            for mk in scans[p.rel].marks:
                if mk.link:
                    from wiki_pages import link_lookup
                    lookup = link_lookup(mk.link)
                    if lookup:
                        kind, target = lookup
                        for t in (by_path if kind == "path" else by_slug).get(target, []):
                            if t.rel in summaries and summaries[t.rel] not in docs:
                                docs.append(summaries[t.rel])
            # 印を 1 つも付けなかった関係するページも、`sources` の要約ページの raw と照らす（実装のレビューの 10 回目）。
            for ref in _refs(p.meta.get("sources")):
                from wiki_pages import link_lookup, ref_link
                lookup = link_lookup(ref_link(ref))
                if lookup:
                    kind, target = lookup
                    for t in (by_path if kind == "path" else by_slug).get(target, []):
                        if t.rel in summaries and summaries[t.rel] not in docs:
                            docs.append(summaries[t.rel])
            for key, doc in docs:
                if only_key is not None and key != only_key:
                    continue
                corrected = [d.corrected for d in defs_by_raw.get(key, []) if d.corrected]
                words = unknown_words(scans[p.rel].prose or _prose_of(p.text), doc, corrected)
                if words:
                    unknown.setdefault(p.rel, []).extend(w for w in words if w not in unknown.get(p.rel, []))

    if only_key is not None:
        results = [r for r, k in zip(results, result_keys) if k == only_key]
    return CheckResult(findings, results, unknown, raw_warnings)


def _prose_of(text: str) -> list[str]:
    """「raw にない語」を探す本文（引用と定義の節を除く、表示される本文の文字）。"""
    m = FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    lines = body.split("\n")
    _kinds, def_ranges = _line_kinds(lines)
    in_def = set()
    for start, end in def_ranges[:1]:
        in_def.update(range(start, end))
    return [_prose(it) for it in inline_texts(lines) if not it.quote and it.line not in in_def]


def _check_mark(mk: Mark, d: Definition, findings: list) -> None:
    if mk.kind != d.kind:
        findings.append(Finding(mk.page, mk.page_line, "mark_kind_mismatch",
                                f"{mk.ident} の印は〔{mk.kind}〕だが、定義は{d.kind}"))
        return
    word = d.corrected if d.kind == "訂正" else d.caption
    problem = tail_problem(mk.prefix, word or "", mk.context)
    if problem:
        findings.append(Finding(mk.page, mk.page_line, "mark_tail", f"{mk.ident}: {problem}"))


def is_overlay_page(rel: str) -> bool:
    """wiki からの相対パスが、書いたあとに wiki のページとして読まれる場所か（wiki_pages の一覧と同じ規則）。

    `.wiki-cache/`、`raw/`、`assets/`、`indexes/` などに置いたファイルは、ページとして読まれない。そこに置いた
    ページを照らして通しても、書いたあとの wiki では要約ページがない（実装のレビューの 6 回目）。
    """
    import wiki_pages
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return False
    if not parts[-1].lower().endswith(".md"):
        return False
    # Windows が名前を読み替える形（末尾の点と空白、8.3 の短い名前の `~`、使えない文字、予約された名前）は、
    # どの OS でも拒む。`graph./a.md` は Windows では既存の `graph/` の中に書かれる（実装のレビューの 8 回目）。
    import wiki_browse_core
    for p in parts:
        if p != p.strip() or p.endswith(".") or "~" in p or any(c in _WINDOWS_BAD or ord(c) < 32 for c in p):
            return False
        if p.split(".", 1)[0].strip().casefold() in _WINDOWS_RESERVED or wiki_browse_core.is_reserved_windows(p):
            return False
    # 除外の名前は、大文字と小文字によらず比べる。Windows では `GRAPH/a.md` は既存の `graph/` の中に書かれ、
    # ページとして読まれない（実装のレビューの 7 回目）。どの OS でも同じに拒む。
    if len(parts) == 1:
        return parts[0].casefold() not in {n.casefold() for n in wiki_pages.SKIP_TOP_LEVEL_FILES}
    return parts[0].casefold() not in {n.casefold() for n in wiki_pages.SKIP_TOP_LEVEL_DIRS}


_WINDOWS_BAD = frozenset('<>:"|?*\\')
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(10)}
                              | {f"lpt{i}" for i in range(10)})


def lands_in_skipped(wiki_root: Path, rel: str) -> bool:
    """用意したページを書くと、ページとして読まれない最上位の項目（`graph/`、`index.md` など）と同じ実体に入るか。

    名前の字面では、ファイルシステムが読み替える別の書き方を挙げきれないので、実体（samefile）で確かめる。
    """
    import wiki_pages
    parts = rel.split("/")
    first = Path(wiki_root) / parts[0]
    names = wiki_pages.SKIP_TOP_LEVEL_FILES if len(parts) == 1 else wiki_pages.SKIP_TOP_LEVEL_DIRS
    for name in names:
        other = Path(wiki_root) / name
        try:
            if first.exists() and other.exists() and os.path.samefile(first, other):
                return True
        except OSError:
            return True
    return False


def _identity(path: Path) -> str:
    """ファイルかディレクトリの実体の鍵。ファイルシステムが返す識別子（st_dev と st_ino）で決める。

    パスの文字列（実際のパスを normcase したもの）で決めると、大文字と小文字を区別する Windows のディレクトリで、
    `Detail.md` と `detail.md` の 2 つの別のファイルを同じ実体とみなした（実装のレビューの 15 回目）。識別子なら、
    ジャンクションとシンボリックリンクの別名は同じ鍵に、別のファイルは名前によらず別の鍵になる。識別子を返さない
    ファイルシステム（st_ino が 0）だけ、実際のパスの文字列に戻す。
    """
    try:
        st = os.stat(path)
    except OSError:
        st = None
    if st is not None and st.st_ino:
        return f"id:{st.st_dev}:{st.st_ino}"
    return "path:" + os.path.normcase(os.path.realpath(os.path.abspath(path)))


def load_pages(wiki_root: Path, overlay: dict | None = None) -> tuple[list[PageIn], list, dict]:
    """wiki のページを読み、用意したページ（overlay。wiki からの相対パス → 文字列）を、書いたあとの状態として重ねる。
    （ページ, 読めなかったページの指摘, 渡した相対パスからページの相対パスへの対応）を返す。

    用意したページの場所は、ファイルの実体で照らす。綴りで照らすと、大文字と小文字、末尾の点、ジャンクションの
    別名など、ファイルシステムが同じ場所とみなす別の書き方を挙げきれなかった（実装のレビューの 6〜12 回目）。

    - 既存のファイルなら、ページの一覧が読んだページでなければならない。
    - 新しいファイルなら、ページの一覧が降りた既存のディレクトリの直下でなければならない（新しいディレクトリが
      要るときは、先に作る。こうすると、ファイルとディレクトリのぶつかる組を作れない）。
    - 2 つの用意したページが同じ実体（既存のファイルか、同じディレクトリの、大文字と小文字をそろえて同じ名前）を
      指せば止まる。上書きして片方を黙って捨てない。
    - ページの一覧が別名で読んだページも、実体が用意したページと同じなら、用意した中身で読む。新しいページも、
      一覧がそのディレクトリに降りるすべての道筋（ジャンクションの別名を含む）に置いたものとして数える。
    """
    import wiki_pages
    wiki_root = Path(wiki_root)
    # 渡した場所の名前は、ファイルシステムに尋ねるまで、そろえも直しもしない。NFC にそろえると、分解した形の新しい
    # 名前を、合成済みの既存のページと取り違えた（実装のレビューの 17 回目）。NFC にそろえるのは、ページを見分ける
    # 相対パス（PageIn.rel）だけである。
    given = {k.replace("\\", "/"): v for k, v in (overlay or {}).items()}
    unreadable: list = []
    visited: list = []
    walk = wiki_pages.walk_pages(wiki_root, visited=visited)
    visited_set = {nfc(v) for v in visited}
    by_identity: dict = {}      # 実体の鍵 -> 用意した中身
    new_pages: dict = {}        # 新しいページの相対パス -> 用意した中身
    # 一覧が降りたディレクトリを、実体ごとにまとめる。ジャンクションの別名のように、同じディレクトリに 2 つの
    # 道筋で降りるなら、新しいページは書いたあとに両方の道筋で読まれる（実装のレビューの 13 回目）。
    dirs_by_identity: dict = {}
    for v in sorted(visited_set):
        dirs_by_identity.setdefault(_identity(wiki_root / v if v else wiki_root), []).append(v)
    renamed: dict = {}
    owner: dict = {}            # 実体の鍵 -> 渡した相対パス
    # 綴りを推して直さない。渡した場所にファイルがあるか、どの実体かは、ファイルシステムに尋ねる。大文字と小文字
    # だけが違う既存の項目へ名前を直すと、大文字と小文字を区別するディレクトリで、新しく作る detail.md を既存の
    # Detail.md と取り違えた（実装のレビューの 16 回目）。
    walk_ids: dict = {}
    for f in walk.files:
        walk_ids.setdefault(_identity(wiki_root / f.rel), nfc(f.rel))
    for rel, text in given.items():
        refuse = f"書いたあとにページの一覧が読む場所でないので、用意したページとして照らせない: {rel}"
        if not is_overlay_page(rel) or lands_in_skipped(wiki_root, rel):
            raise ValueError(refuse)
        target = wiki_root / rel
        if os.path.lexists(target):
            key = _identity(target)
            if key not in walk_ids:
                raise ValueError(refuse)
            renamed[rel] = walk_ids[key]
        else:
            parent_path = target.parent
            parent_key = _identity(parent_path) if parent_path.is_dir() else None
            if parent_key not in dirs_by_identity:
                raise ValueError(f"{refuse}（新しいページは、ページの一覧が降りる既存のディレクトリの直下に置く。"
                                 "新しいディレクトリが要るなら、先に作る）")
            key = parent_key + os.sep + target.name.casefold()
            aliases = dirs_by_identity[parent_key]
            for alias in aliases:
                new_pages[f"{alias}/{target.name}" if alias else target.name] = text
            renamed[rel] = nfc(f"{aliases[0]}/{target.name}" if aliases[0] else target.name)
        if key in owner:
            raise ValueError(f"用意したページ {owner[key]} と {rel} が、同じ場所を指す")
        owner[key] = rel
        by_identity[key] = text
    for rel in list(walk.unlistable) + list(walk.unknown):
        unreadable.append(Finding(nfc(str(rel)), 1, "unreadable_page",
                                  "ディレクトリか項目を調べられないので、その中のページを照らせない"))
    pages = []
    for f in walk.files:
        path = wiki_root / f.rel
        rel = nfc(f.rel)
        key = _identity(path)
        if key in by_identity:
            text = by_identity[key]
        else:
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                # 読めないページに定義があると、数えずに confidence が上がる。黙って除かず、指摘にする。
                unreadable.append(Finding(rel, 1, "unreadable_page", f"ページを読めないので照らせない（{exc}）"))
                continue
        pages.append(PageIn(rel, text, page_meta(text), nfc(path.stem)))
    for rel, text in new_pages.items():
        # NFC にそろえた名前が既存のページと重なれば、下の重なりの検査が、書く前に照らせないことを指摘する。
        pages.append(PageIn(nfc(rel), text, page_meta(text), nfc(Path(rel).stem)))
    # ページは NFC にそろえた相対パスで区別する。NFC にそろえると同じになる別のファイル（合成済みの é と、e と
    # 結合アクセント）があると、片方の解析が片方を上書きし、定義を数えずに confidence が上がった（実装のレビューの
    # 14 回目）。黙って 1 つにまとめず、照らせないので指摘にする（照合は、重なった名前のページを最初の 1 つだけ読む）。
    unique: list = []
    seen: set = set()
    for page in pages:
        if page.rel in seen:
            unreadable.append(Finding(page.rel, 1, "ambiguous_page_path",
                                      "NFC にそろえると同じ名前になる別のファイルがあり、どちらのページかを決められない"
                                      "ので照らせない（ファイル名を NFC にそろえる）"))
            continue
        seen.add(page.rel)
        unique.append(page)
    return unique, unreadable, renamed


def check_wiki(wiki_root: Path, overlay: dict | None = None, only_raw: str | None = None,
               unknown_for: set | None = None, *, raw_roots=(), recorded_raw_roots=(),
               wiki_root_given=None) -> CheckResult:
    """wiki のページ（overlay を重ねたもの）を照らす。読めないページは、only_raw によらず指摘に入れる
    （そのページがこの raw に関わるかを確かめられないため）。"""
    pages, unreadable, renamed = load_pages(wiki_root, overlay)
    # 「raw にない語」を探すページも、ディスクの綴りに直した名前で指す（実装のレビューの 8 回目）。
    if unknown_for:
        unknown_for = {renamed.get(r.replace("\\", "/"), nfc(r)) for r in unknown_for}
    result = check_pages(pages, wiki_root, only_raw, unknown_for, raw_roots=raw_roots,
                         recorded_raw_roots=recorded_raw_roots, wiki_root_given=wiki_root_given)
    result.findings = unreadable + result.findings
    return result
