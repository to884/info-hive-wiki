"""
wiki_text.py — 日本語の wiki を扱うための文字列の正規化と、frontmatter の日本語の別名。

検索・lint・統計・グラフのスクリプトが共通で使う。標準ライブラリだけで書き、単独では実行しない。

- ファイル名、パス、wikilink の行き先、スラッグは NFC にそろえる。macOS は濁点付きの
  ファイル名を NFD（「カ」＋「゛」）で返すので、NFC で書いたリンクと一致しなくなるのを防ぐ。
- タグは NFKC にそろえる。全角の「ＡＩ」と半角の「AI」を同じタグとして扱う。
- frontmatter のキーと type・kind の値は、日本語の別名でも読めるようにする。読み込み時に
  英語に直し、スクリプトとテンプレートが書くときは英語を使う（移植計画の方針 B）。

使い方:
    from wiki_text import canonical_meta, nfc
    meta, conflicts = canonical_meta(raw_meta)
"""

from __future__ import annotations

import re
import unicodedata

# frontmatter のキーの日本語の別名。値は英語のキー。
# 比較の前に NFKC をかけるので、全角・半角の違いはここに並べなくてよい。
KEY_ALIASES: dict[str, str] = {
    "タイトル": "title",
    "題名": "title",
    "種類": "type",
    "タグ": "tags",
    "出典": "sources",
    "作成日": "created",
    "更新日": "updated",
    "種別": "kind",
    "別名": "aliases",
    "著者": "authors",
    "原文": "raw",
    "取り込み日": "ingested",
    "質問": "question",
    "参照した出典": "sources_consulted",
}

# type の値の日本語の別名。
TYPE_ALIASES: dict[str, str] = {
    "ソース": "source",
    "資料": "source",
    "エンティティ": "entity",
    "実体": "entity",
    "概念": "concept",
    "コンセプト": "concept",
    "シンセシス": "synthesis",
    "統合": "synthesis",
    "まとめ": "synthesis",
}

# kind の値の日本語の別名。ontology.yaml.template の maps_from にある kind に対応させる。
KIND_ALIASES: dict[str, str] = {
    "人物": "person",
    "人": "person",
    "企業": "company",
    "会社": "company",
    "製品": "product",
    "論文": "paper",
    "場所": "place",
    "組織": "organization",
    "団体": "organization",
}

# 全角の記号。wikilink の区切りや frontmatter のキーの区切りとしては使えない。
FULLWIDTH_COLON = "："
FULLWIDTH_PIPE = "｜"
FULLWIDTH_HASH = "＃"
IDEOGRAPHIC_COMMA = "、"

# 「キー：値」の行。キーのあとが全角のコロンになっているものを見つける。
FULLWIDTH_COLON_KEY_RE = re.compile(r"^(\w+)\s*" + FULLWIDTH_COLON)


def nfc(text: str) -> str:
    """NFC にそろえる。ファイル名、パス、リンクの行き先、スラッグに使う。"""
    return unicodedata.normalize("NFC", text)


def nfkc(text: str) -> str:
    """NFKC にそろえる。タグのように、全角と半角を同じものとして扱いたい値に使う。"""
    return unicodedata.normalize("NFKC", text)


def posix_rel(rel_path: str) -> str:
    """wiki からの相対パスを、/ 区切りの NFC にそろえる。"""
    return nfc(rel_path.replace("\\", "/"))


def canonical_key(key: str) -> str:
    """frontmatter のキーを英語に直す。別名でなければ、そのまま返す。"""
    return KEY_ALIASES.get(nfkc(key).strip(), key)


def _canonical_value(value, table: dict[str, str]):
    if isinstance(value, str):
        return table.get(nfkc(value).strip(), value)
    return value


def canonical_meta(meta: dict) -> tuple[dict, list[dict]]:
    """frontmatter の日本語の別名を英語に直す。(英語に直した meta, 衝突の一覧) を返す。

    同じページに英語のキーと日本語の別名の両方がある（title と タイトル など）場合は、
    英語のキーの値を残し、衝突として返す。lint はこれを指摘する。
    type と kind の値も、日本語の別名なら英語に直す。
    """
    out: dict = {}
    origin: dict[str, str] = {}
    conflicts: list[dict] = []
    for key, value in meta.items():
        if not isinstance(key, str):
            out[key] = value
            continue
        canonical = canonical_key(key)
        if canonical in out:
            conflicts.append({"key": canonical, "keys": [origin[canonical], key]})
            # 英語のキーを優先する。先に入っていたのが別名なら置き換える。
            if key == canonical:
                out[canonical] = value
                origin[canonical] = key
            continue
        out[canonical] = value
        origin[canonical] = key
    if "type" in out:
        out["type"] = _canonical_value(out["type"], TYPE_ALIASES)
    if "kind" in out:
        out["kind"] = _canonical_value(out["kind"], KIND_ALIASES)
    return out, conflicts


def normalize_tags(tags) -> list[str]:
    """タグの一覧を NFKC にそろえる。"""
    if isinstance(tags, str):
        tags = [tags]
    return [nfkc(str(t)).strip() for t in (tags or [])]


def fullwidth_colon_keys(frontmatter_text: str) -> list[str]:
    """frontmatter の中で、キーのあとが全角の「：」になっている行のキーを返す。"""
    found = []
    for line in frontmatter_text.split("\n"):
        m = FULLWIDTH_COLON_KEY_RE.match(line)
        if m:
            found.append(m.group(1))
    return found


def ideographic_comma_values(meta: dict) -> list[str]:
    """リストの値が「、」で区切られていて、1 つの項目になってしまっているキーを返す。

    `tags: [機械学習、深層学習]` は YAML でも手書きのパーサーでも 1 つの項目になる。
    区切りは「,」で書く必要があるので、lint で指摘する。
    """
    keys = []
    for key, value in meta.items():
        items = value if isinstance(value, list) else []
        if any(isinstance(item, str) and IDEOGRAPHIC_COMMA in item for item in items):
            keys.append(str(key))
    return keys


def has_fullwidth_link_syntax(link: str) -> bool:
    """wikilink の中に、区切りのつもりの全角の「｜」や「＃」があれば True を返す。

    Obsidian はこれらを区切りとして扱わないので、[[機械学習｜ML]] は「機械学習｜ML」という
    名前のページへのリンクになる。スクリプトも同じく区切りとしては扱わず、lint で指摘する。
    """
    return FULLWIDTH_PIPE in link or FULLWIDTH_HASH in link


# ---------------------------------------------------------------------------
# frontmatter の解析と書き出し
# ---------------------------------------------------------------------------
#
# 文字列の値は、配列の要素も含めて二重引用符で囲む（要件 §6.3）。日付（YYYY-MM-DD）と数値は
# 囲まない。値の中の " は \"、\ は \\ と書く。これがページの規約で、スクリプトとテンプレートはこの形で書く。
#
# 読むときは、規約にない YAML の書き方も読む。lint、検索、統計、取得、グラフの最上位の欄が、この同じ
# パーサーを使う。
# - キーは、引用符で囲んでもよく（"type": x）、コロンの前に空白があってもよい（type : x）。
# - 配列は、1 行の [a, b]、複数行にまたがる [a, … ]、ブロックの `- a`（字下げは 0 でも 4 つでもよい）を読む。
# - 二重引用符の中のエスケープは、YAML が決めているもの（\t、\n、\u00e9 など）を YAML の意味で読む。
#   値の中に YAML にないエスケープ（\d など）が 1 つでもあれば、その値は \" と \\ だけを戻す。
# - `|` と `>` のブロックのスカラーを、YAML と同じ値に読む。
# 引用符の中のカンマで配列を分けず、引用符の外の「 #」から行末まではコメントとして捨てる。
# 行は LF（と CRLF）で分け、値の前後の空白は、全角の空白などの Unicode の空白も除く。
#
# 値はすべて、書かれた文字のままの文字列か、その配列になる（true、2024、2024-01-01、null も文字列）。
# 入れ子（graph: など）、次の行に書いた値、複数行にまたがるスカラー、アンカー（&、*）、タグ（!!str）、
# マージキー（<<）、`\w+` でないキー（source-url）は読まない。グラフのスクリプトも、最上位の欄は
# このパーサーで読み、graph: の欄だけを PyYAML から取る（wiki_graph_extract.read_frontmatter）。

# 「key: value」の行。キーは日本語でもよく、引用符で囲んでもよい。コロンの前の空白も許す。
# 行頭に空白があるものは入れ子なので読まない。
_FM_KEY_RE = re.compile(r"""^(?:(\w+)|"(\w+)"|'(\w+)') *:\s*(.*)$""")
# ブロックの配列の要素の行。`-` のあとは空白か行末である。
_FM_ITEM_RE = re.compile(r"^( *)-(?: +(.*)| *)$")
# ブロックのスカラーの見出し（`|`、`>-`、`|2`、`>+1` など）。コメントを除いたあとの値に当てる。
_FM_BLOCK_RE = re.compile(r"^([|>])(?:([+-])([1-9])?|([1-9])([+-])?)?$")
_FM_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_FM_BARE_WORDS = {"true", "false", "null", "~"}

# 二重引用符の中のエスケープ。YAML 1.2 が決めているものだけを戻す。
_FM_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
    "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}
_FM_ESCAPE_HEX = {"x": 2, "u": 4, "U": 8}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _fm_hex(text: str, start: int, width: int) -> "int | None":
    """text[start:start + width] が 16 進の数字だけなら、その値を返す。"""
    digits = text[start:start + width]
    if len(digits) != width or not all(ch in _HEX_DIGITS for ch in digits):
        return None
    return int(digits, 16)


def _fm_unescape(inner: str) -> "str | None":
    """二重引用符の中身のエスケープを、YAML の意味の文字に戻す。戻せないエスケープがあれば None を返す。

    戻すのは、YAML が決めているエスケープ（\\"、\\\\、\\t、\\n、\\xXX、\\uXXXX、\\UXXXXXXXX など）である。
    \\uD83D\\uDE00 のように、上位と下位のサロゲートを \\u で続けて書いたものは、1 文字にまとめる。
    戻せないのは、YAML にないエスケープ（\\d、16 進の桁が足りない \\u、末尾の \\。YAML としては誤り）と、
    UTF-8 に書けない文字になるもの（対にならないサロゲート、U+10FFFF を超える値）である。
    """
    out = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None
        mark = inner[i + 1]
        if mark in _FM_ESCAPES:
            out.append(_FM_ESCAPES[mark])
            i += 2
            continue
        width = _FM_ESCAPE_HEX.get(mark)
        code = _fm_hex(inner, i + 2, width) if width else None
        if code is None:
            return None
        end = i + 2 + width
        if mark == "u" and 0xD800 <= code <= 0xDBFF and inner[end:end + 2] == "\\u":
            low = _fm_hex(inner, end + 2, 4)
            if low is not None and 0xDC00 <= low <= 0xDFFF:
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                i = end + 6
                continue
        if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            return None
        out.append(chr(code))
        i = end
    return "".join(out)


def _fm_unescape_quotes_only(inner: str) -> str:
    """二重引用符の中身の \\" と \\\\ だけを戻す。ほかの \\ は、書かれた文字のまま残す（0.9.5 までの読み方）。"""
    out = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner) and inner[i + 1] in '"\\':
            out.append(inner[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _fm_unquote(token: str) -> str:
    """引用符で囲まれた 1 つの値から引用符を外し、エスケープを戻す。囲まれていなければそのまま返す。

    二重引用符の中のエスケープは、YAML の意味の文字にする。値の中に 1 つでも戻せないエスケープがあれば
    （YAML としては誤りの値）、その値の全体を 0.9.5 までと同じに読む。\\" と \\\\ だけを戻し、ほかの \\ は
    書かれた文字のまま残す（"C:\\Users\\new" の \\n を改行にしない）。
    """
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        inner = token[1:-1]
        if "\\" not in inner:
            return inner
        value = _fm_unescape(inner)
        return _fm_unescape_quotes_only(inner) if value is None else value
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1].replace("''", "'")
    return token


def _fm_strip_comment(value: str) -> str:
    """引用符の外にある「 #」から行末までを捨てる。"""
    quote = None
    i = 0
    while i < len(value):
        ch = value[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
        i += 1
    return value.rstrip()


def _fm_split_items(inner: str) -> list[str]:
    """inline の配列の中身を、引用符の中のカンマでは分けずに項目に分ける。項目は引用符つきのまま返す。"""
    items: list[str] = []
    buf: list[str] = []
    quote = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(inner):
                buf.append(inner[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    items.append("".join(buf).strip())
    return [item for item in items if item]


def _fm_flow_line(line: str, quote: "str | None", depth: int) -> "tuple[str, str | None, int, bool] | None":
    """複数行にまたがる [...] の 1 行を読む。(残す文字, 引用符の状態, 括弧の深さ, 閉じたか) を返す。

    quote と depth は、前の行までの状態である。引用符の外の # から行末までは、コメントとして捨てる。
    YAML と同じく、# がコメントになるのは、行頭、空白のあと、`,`・`[`・`]`・`{`・`}` のあと、閉じた引用符の
    あとである（`a#b` の # は値の一部）。引用符の読み方は _fm_split_items と同じである。括弧が閉じたあとに、
    コメントでない文字が続いていれば None を返す。
    """
    i = 0
    n = len(line)
    comment_ok = True   # この位置の # がコメントになるか
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
                comment_ok = True
        elif ch == "#" and comment_ok:
            return line[:i].strip(), quote, depth, False
        elif ch in "\"'":
            quote = ch
            comment_ok = False
        else:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    if _fm_strip_comment(line[i + 1:].strip()):
                        return None
                    return line[:i + 1].strip(), quote, depth, True
            comment_ok = ch.isspace() or ch in ",[]{}"
        i += 1
    return line.strip(), quote, depth, False


def _fm_multiline_flow(first: str, lines: list[str], start: int) -> "tuple[list[str], int] | None":
    """1 行で閉じない [ の配列を、閉じる ] の行まで読む。(項目の一覧, 次に読む行の番号) を返す。

    first は 1 行目の値（[ で始まり、コメントを除いてある）、start は次の行の番号である。行をまたぐところは
    空白 1 つにし、1 行に書いたときと同じ項目に分ける。次のときは None を返し、呼び出す側は、今までどおり
    1 行目だけの文字列として読む。
    - 1 行目で括弧が閉じている（`[a] b`）。
    - 閉じる前に、字下げのないキーの行が来る。] を書き忘れたものとして扱い、後ろの欄を配列に取り込まない。
    - frontmatter の終わりまで閉じない。
    """
    read = _fm_flow_line(first, None, 0)
    if read is None or read[3]:
        return None
    parts = [read[0]]
    _text, quote, depth, _closed = read
    for index in range(start, len(lines)):
        if _FM_KEY_RE.match(lines[index]):
            return None
        read = _fm_flow_line(lines[index], quote, depth)
        if read is None:
            return None
        text, quote, depth, closed = read
        if text:
            parts.append(text)
        if closed:
            return _fm_split_items(" ".join(parts)[1:-1]), index + 1
    return None


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _fm_block_scalar(header: "re.Match", lines: list[str], start: int) -> "tuple[str, int]":
    """`|` と `>` のブロックのスカラーを読む。(値, 次に読む行の番号) を返す。

    header は _FM_BLOCK_RE の一致、start は見出しの次の行の番号である。字下げの決め方、折りたたみ（`>`）、
    末尾の改行の扱い（`-` は除く、`+` は残す、指定がなければ 1 つ残す）は、YAML（PyYAML の
    scan_block_scalar）と同じにする。frontmatter の最後の行にも改行があるものとして読む。
    """
    folded = header.group(1) == ">"
    chomp = header.group(2) or header.group(5)
    explicit = header.group(3) or header.group(4)
    n = len(lines)
    i = start
    if explicit:
        indent = int(explicit)
    else:
        # 字下げは、最初の空でない行の字下げ。その前の空白だけの行のほうが長ければ、その長さ。
        indent = 1
        for line in lines[start:]:
            indent = max(indent, _leading_spaces(line))
            if line.strip(" "):
                break

    def take_breaks(index: int) -> "tuple[list[str], int]":
        # 空白だけで、長さが字下げ以下の行は、空行として数える。
        breaks = []
        while index < n and len(lines[index]) <= indent and not lines[index].strip(" "):
            breaks.append("\n")
            index += 1
        return breaks, index

    chunks: list[str] = []
    line_break = ""
    breaks, i = take_breaks(i)
    while i < n and _leading_spaces(lines[i]) >= indent:
        chunks.extend(breaks)
        text = lines[i][indent:]
        chunks.append(text)
        line_break = "\n"
        breaks, i = take_breaks(i + 1)
        if not (i < n and _leading_spaces(lines[i]) >= indent):
            break
        # 折りたたむのは、字下げどおりに始まる行どうしの間だけである。空行を挟まなければ空白 1 つにし、
        # 挟めば、その空行の数だけの改行にする。
        if folded and text[0] not in " \t" and lines[i][indent] not in " \t":
            if not breaks:
                chunks.append(" ")
        else:
            chunks.append(line_break)
    if chomp != "-":
        chunks.append(line_break)
    if chomp == "+":
        chunks.extend(breaks)
    return "".join(chunks), i


def _fm_entries(frontmatter_text: str):
    """frontmatter の最上位の欄を、(キー, 形, 中身, キーの行か) に分けて、書かれた順に返す。

    形は 3 つある。"scalar" の中身は値の字句 1 つ、"list" の中身は字句の一覧で、どちらも引用符つきのまま
    である。"block" の中身は、ブロックのスカラーを読んだあとの値である。値のないキーは、空の "list" を返し、
    続くブロックの配列の要素を、要素 1 つの "list" として返す（キーの行ではない）。
    """
    lines = [line[:-1] if line.endswith("\r") else line for line in frontmatter_text.split("\n")]
    n = len(lines)
    i = 0
    seq_key = None      # 値のないキー。続くブロックの配列を、このキーの要素として読む。
    seq_indent = None   # その配列の字下げ。最初の行で決まる。配列でなければ -1。
    while i < n:
        line = lines[i]
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = line[0] in " \t"
        if seq_key is not None:
            item = _FM_ITEM_RE.match(line)
            if seq_indent is None:
                # 値のないキーの次の行で形を決める。`- ` の行なら、その字下げの配列である。
                # 字下げのあるほかの行なら入れ子（graph: など）なので、字下げのない行まで読まない。
                seq_indent = len(item.group(1)) if item else -1
            if item and len(item.group(1)) == seq_indent:
                yield seq_key, "list", [_fm_strip_comment((item.group(2) or "").strip())], False
                continue
            if indented:
                continue
            seq_key = seq_indent = None
        if indented:
            continue
        kv = _FM_KEY_RE.match(line)
        if not kv:
            continue
        key = kv.group(1) or kv.group(2) or kv.group(3)
        value = _fm_strip_comment(kv.group(4).strip())
        seq_key = seq_indent = None
        if not value:
            seq_key = key
            yield key, "list", [], True
            continue
        if value.startswith("["):
            if value.endswith("]"):
                yield key, "list", _fm_split_items(value[1:-1]), True
                continue
            flow = _fm_multiline_flow(value, lines, i)
            if flow is not None:
                items, i = flow
                yield key, "list", items, True
                continue
        header = _FM_BLOCK_RE.match(value)
        if header:
            text, i = _fm_block_scalar(header, lines, i)
            yield key, "block", text, True
            continue
        yield key, "scalar", value, True


def top_level_keys(frontmatter_text: str) -> list[str]:
    """parse_frontmatter_text がキーの行として読む行のキーを、引用符を外して、出てきた順に返す。

    同じキーを 2 度書いていれば、2 つ返す。グラフの抽出が、YAML としては読めるのにこのパーサーが
    読まないキー（`? type`、エスケープを含む `"t\\u0079pe"`）を見つけるのに使う。
    """
    return [key for key, _form, _payload, is_key_line in _fm_entries(frontmatter_text) if is_key_line]


def parse_frontmatter_text(frontmatter_text: str) -> dict:
    """frontmatter の本文（--- の間）を読み、書かれたままのキーの dict を返す。

    値はすべて文字列か文字列の配列になる。1 行と複数行の [...]、ブロックの配列（字下げは問わない）、
    引用符の中のカンマ、二重引用符の中の YAML のエスケープ、行末のコメント、`|` と `>` のブロックの
    スカラー、引用符つきのキーとコロンの前の空白を扱う。入れ子（graph: など）は読まず、空の配列になる。
    同じキーを 2 度書くと、配列はつなぎ、文字列は後の値にする。英語への別名の変換は canonical_meta で別に行う。
    """
    meta: dict = {}
    for key, form, payload, _is_key_line in _fm_entries(frontmatter_text):
        if form == "list":
            values = [_fm_unquote(t) for t in payload]
            if key in meta and isinstance(meta[key], list):
                meta[key].extend(values)
            else:
                meta[key] = values
        elif form == "block":
            meta[key] = payload
        else:
            meta[key] = _fm_unquote(payload)
    return meta


def _fm_is_quoted(token: str) -> bool:
    return len(token) >= 2 and token[0] == '"' and token[-1] == '"'


def unquoted_string_keys(frontmatter_text: str) -> list[str]:
    """二重引用符で囲まれていない文字列の値を持つキーを返す。

    日付（YYYY-MM-DD）、数値、true・false・null、空の配列は文字列ではないので除く。
    `|` と `>` のブロックのスカラーは、引用符で囲まれていない文字列として返す。
    入れ子（graph: など）の中は見ない。
    """
    found: list[str] = []
    for key, form, payload, _is_key_line in _fm_entries(frontmatter_text):
        if form == "block":
            if key not in found:
                found.append(key)
            continue
        for token in payload if form == "list" else [payload]:
            if _fm_is_quoted(token):
                continue
            if is_iso_date(token) or _FM_NUMBER_RE.match(token) or token.lower() in _FM_BARE_WORDS:
                continue
            if key not in found:
                found.append(key)
            break
    return found


def format_fm_value(value) -> str:
    """frontmatter に書く値を文字列にする。文字列は二重引用符で囲み、日付と数値は囲まない。"""
    import json
    from datetime import date

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_fm_value(v) for v in value) + "]"
    text = str(value)
    if is_iso_date(text):
        return text
    return json.dumps(text, ensure_ascii=False)


def format_frontmatter(meta: dict) -> str:
    """dict を frontmatter の文字列（--- を含む）にする。キーの順は dict のまま。"""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {format_fm_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ページの大きさと表示幅
# ---------------------------------------------------------------------------

def char_count(text: str) -> int:
    """空白（改行を含む）を除いたコードポイントの数。ページの大きさの判定に使う（計画の方針 I）。"""
    return sum(1 for ch in text if not ch.isspace())


def display_width(text: str) -> int:
    """端末での表示幅。全角（East Asian Width が W か F）の文字を 2 桁と数える。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def ljust_display(text: str, width: int) -> str:
    """表示幅が width になるまで、右に空白を足す。"""
    return text + " " * max(0, width - display_width(text))


def rjust_display(text: str, width: int) -> str:
    """表示幅が width になるまで、左に空白を足す。"""
    return " " * max(0, width - display_width(text)) + text


# ---------------------------------------------------------------------------
# 引用の照合（計画の方針 H）
# ---------------------------------------------------------------------------

# 日本語の文字。漢字、ひらがな、カタカナ、和文の句読点・括弧、全角の英数字と記号。
_JA_CHAR = (
    r"　-〿぀-ゟ゠-ヿㇰ-ㇿ㐀-䶿一-鿿"
    r"豈-﫿＀-￯\U00020000-\U0003134f"
)
_JA_GAP_RE = re.compile(rf"(?<=[{_JA_CHAR}])\s+(?=[{_JA_CHAR}])")
_SPACE_RE = re.compile(r"\s+")


def normalize_quote(text: str) -> str:
    """引用が本文に含まれるかを比べるための形にする。

    大文字と小文字を区別せず、空白の並びを 1 つの空白にまとめる。日本語の文字どうしの間の
    改行と空白は取り除く（日本語の文は、行をまたいでも途中に空白を入れない）。英数字の間の
    空白は残す。空白をすべて除くと「now here」と「nowhere」を同じとみなしてしまうためである。
    NFKC はかけない。全角と半角の違いも、引用が本文と一致しない理由として残す。
    """
    text = _JA_GAP_RE.sub("", text.lower())
    return _SPACE_RE.sub(" ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    """normalize_quote でそろえたうえで、quote が text に含まれていれば True を返す。"""
    needle = normalize_quote(quote or "")
    return bool(needle) and needle in normalize_quote(text or "")


# ---------------------------------------------------------------------------
# 日付
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_iso_date(value) -> bool:
    """value が YYYY-MM-DD の形の、実在する日付なら True を返す。"""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# argparse の見出し
# ---------------------------------------------------------------------------

def localize_argparse(parser):
    """argparse が自動で出す英語（usage:、options: などの見出しと -h の説明）を日本語にする。

    サブコマンドの parser にもたどってかける。parser を返すので、生成した直後にかけてもよい。
    argparse 自身のエラーメッセージ（"the following arguments are required" など）は英語のまま残る。
    """
    import argparse

    parser._optionals.title = "オプション"
    parser._positionals.title = "引数"
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            action.help = "このヘルプを表示して終わる"
        elif isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                localize_argparse(sub)
    base = parser.formatter_class
    if not getattr(base, "_info_hive_ja", False):
        def add_usage(self, usage, actions, groups, prefix=None):
            return base.add_usage(self, usage, actions, groups, "使い方: " if prefix is None else prefix)

        members = {"add_usage": add_usage, "_info_hive_ja": True}
        # argparse は文字数で折り返すので、全角の文では行が端末の幅を超え、空白のない日本語の
        # 途中にある識別子が切れることもある。既定の折り返しを使う formatter だけ、表示幅で折り返す。
        if base._split_lines is argparse.HelpFormatter._split_lines:
            def _split_lines(self, text, width):
                text = self._whitespace_matcher.sub(" ", text).strip()
                return wrap_display(text, width)

            members["_split_lines"] = _split_lines
        parser.formatter_class = type("JapaneseHelpFormatter", (base,), members)
    return parser


# 行頭に置かない記号（禁則）。直前の行の末尾に付ける。
_NO_LINE_START = set("、。，．・：；？！）」』】〕〉》”’ー々")
_ASCII_WORD_RE = re.compile(r"[\x21-\x7e]+|\s+|.", re.DOTALL)


def wrap_display(text: str, width: int) -> list[str]:
    """表示幅で折り返す。英数字の語は途中で切らず、日本語は文字の間で折り返す。"""
    lines: list[str] = []
    line = ""
    for token in _ASCII_WORD_RE.findall(text):
        if token.isspace():
            if line:
                line += " "
            continue
        if line and display_width(line + token) > width and token[0] not in _NO_LINE_START:
            lines.append(line.rstrip())
            line = token
        else:
            line += token
    if line.strip():
        lines.append(line.rstrip())
    return lines


# ---------------------------------------------------------------------------
# 図表（コードフェンスと画像の行）
# ---------------------------------------------------------------------------

# ``` か ~~~ でフェンスが始まる。リストの項目（`- `、`1. `）と引用（`> `）の中のフェンスも読む。
# 閉じるのは、同じ文字で、開いたときと同じ長さ以上の並びのあとに空白とタブしかない行である。
_CONTAINER_PREFIX = r"[ \t]*(?:>[ \t]?)*[ \t]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?"
_FENCE_RE = re.compile(r"^(" + _CONTAINER_PREFIX + r")(`{3,}|~{3,})(.*)$")
_QUOTE_MARKS_RE = re.compile(r"^[ \t]*((?:>[ \t]?)*)")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*(`{3,}|~{3,})[ \t]*$")

# 標準の Markdown の画像。alt の中の 1 段の [ ]、<...> で囲んだ参照先（空白を含めてよい）、
# 1 段の ( ) を含む参照先、"…"・'…'・(…) のタイトルを読む。\! で escape したものは画像ではない。
IMAGE_PATTERN = (
    r"(?<!\\)!\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]"
    r"\(\s*(?:<([^<>\n]*)>|((?:[^\s()<>]|\([^\s()]*\))+))"
    r"""(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"""
)
IMAGE_RE = re.compile(IMAGE_PATTERN)
# 画像だけの行。1 行に画像を並べたものも含む。
_IMAGE_ONLY_LINE_RE = re.compile(r"^\s*(?:" + IMAGE_PATTERN + r"\s*)+$")
# インラインのコード。開きと閉じのバッククォートの数がそろうものだけをコードとみなす。
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def image_target(match: "re.Match") -> str:
    """IMAGE_RE の一致から参照先を取り出す。"""
    return match.group(1) if match.group(1) is not None else match.group(2)


def _quote_depth(line: str) -> int:
    return _QUOTE_MARKS_RE.match(line).group(1).count(">")


def lines_outside_fences(text: str) -> list[str]:
    """コードフェンスの外の行だけを返す。フェンスの開きと閉じの行も除く。

    引用（> ）やリストの項目の中で開いたフェンスは、その引用や項目が終わったところでも閉じる。
    引用なら > の数が減った行、項目なら項目の字下げより浅い文字のある行で終わる。
    """
    out: list[str] = []
    fence = None
    depth = indent = 0
    for line in text.split("\n"):
        if fence is not None:
            ended = (depth > 0 and _quote_depth(line) < depth) or (
                depth == 0 and indent > 0 and line.strip() and len(line) - len(line.lstrip(" \t")) < indent)
            if not ended:
                m = _FENCE_CLOSE_RE.match(line)
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                    fence = None
                continue
            fence = None
        m = _FENCE_RE.match(line)
        if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
            fence = m.group(2)
            prefix = m.group(1)
            depth = prefix.count(">")
            indent = len(prefix) if depth == 0 and prefix.strip() else 0
            continue
        out.append(line)
    return out


def strip_fences_and_images(text: str) -> str:
    """コードフェンス（mermaid を含む）の中身と、画像だけの行を除く。

    ページの大きさの判定に使う。上限は本文の文章の密度を見るためのもので、図のソースの長さは
    数えない（ADR-0005）。図の直後の説明（「> … 補足: 」）は文章なので残す。
    """
    return "\n".join(line for line in lines_outside_fences(text) if not _IMAGE_ONLY_LINE_RE.match(line))
