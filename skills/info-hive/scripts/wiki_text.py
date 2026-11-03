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
