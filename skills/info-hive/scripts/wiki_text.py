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
