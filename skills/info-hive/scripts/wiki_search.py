#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "fastembed==0.8.0",
#   "sqlite-vec==0.1.9",
# ]
# ///
"""
wiki_search.py — wiki のページを対象にした、セクション単位の BM25 とローカルのハイブリッド検索。

既定のセマンティック検索には FastEmbed と sqlite-vec を使う。BM25 は、--no-embed を付ければ
依存パッケージなしで使え、安全な代替手段としても残っている。

使い方:
    python wiki_search.py "検索語" [オプション]

オプション:
    --wiki <dir>            wiki のディレクトリ（既定: 作業領域の設定の dirs.wiki。設定がなければ ./wiki）
    --top N                 上位 N 件の結果を返す（既定: 10）
    --type <type>           frontmatter の type で絞り込む（source|entity|concept|synthesis|...）
    --tag <tag>             タグで絞り込む（複数指定できる）
    --since YYYY-MM-DD      この日付以降に更新されたページだけを対象にする
    --backlinks <slug>      <slug> にリンクしているページを探す。クエリは無視する
    --top-linked N          もっとも多くリンクされている N ページ（ハブ）を表示する。クエリは無視する
    --cache [<path>]        差分で更新するパースキャッシュ（既定: <wiki>/.wiki-cache/search-index.json）
    --granularity <mode>    セクション（既定）とページ全体のどちらを検索するか
    --per-page N            1ページあたりのセクションの結果の最大数（既定: 2）
    --json                  構造化された証拠の行を出力する
    --no-embed              語彙ベースの BM25 だけを使う
    --decide                判定モデルが関連ありと判定した行だけを残す（INFO_HIVE_DECISIONS が必要）
    --decide-pool N         --decide で採点する行の数（既定: 20）
    --decide-threshold X    行を残すのに必要な P(relevant)（既定: 0.65）

例:
    python wiki_search.py "diffusion training stability" --top 5
    python wiki_search.py "alignment" --type concept --tag safety
    python wiki_search.py "" --backlinks transformer
    python wiki_search.py "" --top-linked 10
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import (  # noqa: E402
    canonical_meta, ljust_display, localize_argparse, nfc, normalize_tags, parse_frontmatter_text, posix_rel,
)
import wiki_initialized  # noqa: E402
import wiki_workspace  # noqa: E402
# ページの集合とリンクの規則は wiki_pages の 1 か所に置く（ADR-0010 の決定の 1）。wiki_index、wiki_decide、
# 試験が wiki_search.resolve_link などの名前で使うので、同じ名前で読み込んでおく。
from wiki_pages import (  # noqa: E402
    WIKILINK_RE,
    build_link_index,
    clean_link_target,
    code_line_mask,
    PARSER_ID,
    heading_line_mask,
    links_by_line,
    split_lines,
    extract_wikilinks,
    iter_page_files,
    ref_values,
    resolve_link,
)


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# 文字種ごとの並び。英数字は小文字にしたあとの ASCII だけを語とみなす（移植元と同じ）。
# 漢字には 々 と 〆、CJK 統合漢字の拡張 B 以降（𠮷 など）を含める。
# カタカナには長音符 ー を含め、中黒 ・ は区切りとして扱う。
SCRIPT_RUN_RE = re.compile(
    r"(?P<ascii>[a-z0-9]+)"
    r"|(?P<kanji>[\u3005\u3006\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]+)"
    r"|(?P<hiragana>[\u3041-\u309f]+)"
    r"|(?P<katakana>[\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff]+)"
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# 使える埋め込みモデル（計画の方針 F）。英語専用のモデルは置かず、多言語モデルだけにする。
# モデルを加えるときは、max_distance を評価セットで測ってから加える。
#   max_distance    結果に残すコサイン距離の上限。モデルごとに評価セットで測って決める
#   query_prefix    クエリの前に付ける文字列。e5 系は "query: " と "passage: " を前提に学習している
#   passage_prefix  セクションの前に付ける文字列
#   custom          fastembed に登録されていないモデルを add_custom_model で登録するための情報
EMBED_MODELS = {
    # e5 系は距離が狭い範囲に集まる。P3 の評価セットでは、正例の正解までの距離は日本語で最大 0.190、
    # 英語で最大 0.196、負例の最も近いセクションまでの距離は日本語で最小 0.198、英語で最小 0.200
    # だった。0.190〜0.197 では成績が変わらないので、その中ほどの 0.193 にした。
    # 重みは fp16 を使う（約 0.25 GB）。int8 に量子化した版（約 0.14 GB）は、距離が fp16 から
    # 0.01 程度ずれ、CPU によっても結果が変わって、CI で負例がしきい値をまたいだ。
    "intfloat/multilingual-e5-small": {
        "max_distance": 0.193,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "custom": {
            "hf": "Xenova/multilingual-e5-small",
            "model_file": "onnx/model_fp16.onnx",
            "dim": 384,
            "size_in_gb": 0.25,
            "license": "mit",
        },
    },
    # 設定で選べる大きいモデル（約 2.24 GB、1024 次元、fp32）。正例と負例の距離は分かれず、0.19 は
    # 偽陽性率を 0 に保てる上限。P3 の評価セットでは、日本語の MRR は e5-small より高い（0.975）が、
    # 英語の recall@10 は低く（0.815）、埋め込みは 7 倍ほど遅い。
    "intfloat/multilingual-e5-large": {
        "max_distance": 0.19,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
}
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-small"
# 日本語のトークンを含むクエリで、BM25 の結果に残すのに必要なクエリの被覆率（lexical_ranking）。
# P3 の評価セットでは、負例の被覆率は最大 0.24、語彙で当たる正例はほぼ 0.25 以上だった。
MIN_CJK_QUERY_COVERAGE = 0.25
# 既定のモデルを置き換える環境変数。EMBED_MODELS にある名前だけを受け付ける。
EMBED_MODEL_ENV = "INFO_HIVE_EMBED_MODEL"
VECTOR_INDEX_SCHEMA = "2"
VECTOR_INDEX_NAME = "embeddings.sqlite"
# 埋め込みに渡す文章の形式。query・passage の接頭辞やセクションの分け方など、
# 埋め込みの入力が変わるときに上げる。上げるとベクトルの索引を作り直す。
# prefix-v1: EMBED_MODELS の接頭辞を付ける（P3）。
EMBED_INPUT_FORMAT = "prefix-v1"
# パースキャッシュの形式。日本語のキーを英語に直すようにしたので 2 に、frontmatter の解析を
# wiki_text の共通のパーサー（引用符の中のカンマと行末のコメントを扱う）に替えたので 3 に、
# コードの中の wikilink を links に入れないようにしたので 4 に、wiki_text のパーサーが読む YAML の書き方
# （字下げの違う配列、複数行の [...]、引用符つきのキー、エスケープ、ブロックのスカラー）を広げたので 5 に上げた。
PARSE_CACHE_SCHEMA = 5


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """YAML 風の frontmatter の軽量なパーサー。(metadata, body) を返す。

    キーは日本語でもよい。読み込んだあと、日本語の別名のキーと type・kind の値を英語に直す。
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():]
    meta, _conflicts = canonical_meta(parse_frontmatter_text(fm_text))
    return meta, body


def tokenize(text: str) -> list[str]:
    """検索用のトークンに分ける（計画の方針 E）。

    NFKC で正規化して小文字にしたあと、文字種（英数字、漢字、ひらがな、カタカナ）の境界で区切る。
    英数字は語単位のまま、かな・漢字は 2 文字ずつ重ねて切り出す（1 文字だけの並びは 1 文字）。
    「税」のような単漢字のクエリが「消費税」に当たるよう、漢字は 1 文字単位でも加える。
    """
    tokens = []
    for match in SCRIPT_RUN_RE.finditer(unicodedata.normalize("NFKC", text).lower()):
        run = match.group()
        if match.lastgroup == "ascii":
            tokens.append(run)
            continue
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        if match.lastgroup == "kanji":
            tokens.extend(run)
    return tokens




def slug_from_path(path: Path, wiki_root: Path) -> str:
    # macOS が NFD で返すファイル名を、リンクと同じ NFC にそろえる。
    return nfc(path.stem)


def cache_page_body(sections: list[dict]) -> str:
    """キャッシュしたセクションのデータから、検索できる Markdown を組み立て直す。"""
    chunks = []
    for section in sections:
        if section["heading_path"]:
            chunks.append("#" * section["level"] + " " + section["heading_path"][-1])
        chunks.append(section["text"])
    return "\n".join(chunks)


def load_parse_cache(cache_path: Path) -> dict:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("パースキャッシュを作り直す（ファイルがない）", file=sys.stderr)
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"パースキャッシュを作り直す（{exc}）", file=sys.stderr)
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != PARSE_CACHE_SCHEMA:
        print("パースキャッシュを作り直す（対応していない形式）", file=sys.stderr)
        return {}
    if payload.get("parser") != PARSER_ID:
        # 本文の解析器（同梱した markdown-it-py）が変わると、links とセクションも変わりうる。
        print("パースキャッシュを作り直す（解析器が変わった）", file=sys.stderr)
        return {}
    files = payload.get("files")
    if not isinstance(files, dict):
        print("パースキャッシュを作り直す（files が不正）", file=sys.stderr)
        return {}
    for rel_path, entry in files.items():
        sections = entry.get("sections") if isinstance(entry, dict) else None
        valid_sections = (
            isinstance(sections, list)
            and all(
                isinstance(section, dict)
                and isinstance(section.get("heading_path"), list)
                and all(isinstance(heading, str) for heading in section["heading_path"])
                and isinstance(section.get("level"), int)
                and isinstance(section.get("text"), str)
                and isinstance(section.get("links"), list)
                and all(isinstance(link, str) for link in section["links"])
                for section in sections
            )
        )
        if not (
            isinstance(rel_path, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("sha256"), str)
            and isinstance(entry.get("meta"), dict)
            and isinstance(entry.get("links"), list)
            and all(isinstance(link, str) for link in entry["links"])
            and valid_sections
        ):
            print(f"パースキャッシュを作り直す（不正な項目: {rel_path}）", file=sys.stderr)
            return {}
    return files


def write_parse_cache(cache_path: Path, files: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(cache_path) + ".tmp")
    temporary.write_text(
        json.dumps({"schema": PARSE_CACHE_SCHEMA, "parser": PARSER_ID, "files": files}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, cache_path)


def link_key(link: str, by_path: dict, by_slug) -> str:
    """報告に使う正規化したキー。解決できない対象はそのまま残す。"""
    return resolve_link(link, by_path, by_slug) or clean_link_target(link)


def collect_pages(wiki_root: Path, cache_path: Path | None = None) -> list[dict]:
    """wiki を走査し、[{path, slug, meta, body, tokens, links}] を返す。"""
    pages = []
    cached_files = load_parse_cache(cache_path) if cache_path else {}
    next_cache = {}
    # ページの集合は lint と同じ（wiki_pages）。最上位の README.md と assets/、.evolution/、.wiki-cache/ は含めない。
    for md_path in iter_page_files(wiki_root):
        rel = md_path.relative_to(wiki_root)
        rel_path = posix_rel(rel.as_posix())
        try:
            raw = md_path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        cached = cached_files.get(rel_path)
        if cached and cached.get("sha256") == digest:
            meta = cached.get("meta", {})
            links = cached.get("links", [])
            sections = cached.get("sections", [])
            body = cache_page_body(sections)
        else:
            try:
                # 先頭に BOM があっても読めるようにする。
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            meta, body = parse_frontmatter(text)
            links = extract_wikilinks(body)
            title = meta.get("title") or slug_from_path(md_path, wiki_root)
            sections = split_sections(title, body)
        next_cache[rel_path] = {
            "sha256": digest,
            "meta": meta,
            "links": links,
            "sections": [
                {
                    "heading_path": section["heading_path"],
                    "level": section["level"],
                    "text": section["text"],
                    "links": section["links"],
                }
                for section in sections
            ],
        }
        pages.append({
            "path": str(md_path),
            "rel_path": rel_path,
            "slug": slug_from_path(md_path, wiki_root),
            "meta": meta,
            "body": body,
            "tokens": tokenize(body + " " + meta.get("title", "")),
            "links": links,
            "sections": [
                {**section, "section_index": index}
                for index, section in enumerate(sections)
            ],
        })
    pages.sort(key=lambda page: page["rel_path"])
    if cache_path:
        write_parse_cache(cache_path, next_cache)
    return pages


def split_sections(title: str, body: str) -> list[dict]:
    """ページの本文を、コードフェンスの外にある ATX 見出しの位置でセクションに分ける。"""
    del title  # TypeScript の実装と動作をそろえるため、API に残している。
    sections = []
    heading_stack: list[tuple[int, str]] = []
    current_path: list[str] = []
    current_level = 0
    current_lines: list[str] = []
    current_links: list[str] = []
    # Markdown と同じく CR と LF だけで行に分ける（splitlines は U+2028 などでも分けるので、抜き出しと食い違う）。
    lines = split_lines(body)
    if len(lines) > 1 and lines[-1] == "":
        # 末尾の改行のあとの空の行は、0.8.0（splitlines）と同じく数えない。数えると、最後のセクションの文字列が
        # 変わり、変わっていないページまで検索の埋め込みを作り直すことになる。
        lines.pop()
    # 見出しは、wikilink の抜き出しと同じ wiki_pages の規則で決める。コードブロック（4 連のバッククォートの中の
    # 3 連、引用やリストの中のフェンス、字下げのコード）の中の「# 」と、「#　」（# のあとが全角空白）は見出しではない。
    is_heading = heading_line_mask(lines)
    # セクションの links は、本文全体を 1 度だけ解析した行ごとのリンクから集める。セクションの本文を切り出して
    # 読み直すと、参照リンクの定義（[r]: url）が別のセクションにあるとき、本文全体にはないリンクが生まれる。
    line_links = links_by_line(lines)

    def append_section() -> None:
        sections.append({
            "heading_path": current_path.copy(),
            "level": current_level,
            "text": "\n".join(current_lines),
            "links": current_links.copy(),
            "section_index": len(sections),
        })

    for line, heading_line, found in zip(lines, is_heading, line_links):
        heading = HEADING_RE.match(line) if heading_line else None
        if not heading:
            current_lines.append(line)
            current_links.extend(found)
            continue

        append_section()
        current_level = len(heading.group(1))
        heading_text = heading.group(2).strip()
        while heading_stack and heading_stack[-1][0] >= current_level:
            heading_stack.pop()
        heading_stack.append((current_level, heading_text))
        current_path = [text for _, text in heading_stack]
        current_lines = []
        current_links = []

    append_section()
    return sections


def collect_sections(pages: list[dict]) -> list[dict]:
    """ページを、検索できるセクションの行に、決まった順序で展開する。"""
    rows = []
    for page in pages:
        title = page["meta"].get("title") or page["slug"]
        for section in page.get("sections") or split_sections(title, page["body"]):
            searchable = title + " " + " ".join(section["heading_path"]) + " " + section["text"]
            rows.append({
                "page": page,
                **section,
                "searchable_text": searchable,
                "tokens": tokenize(searchable),
            })
    return rows


def build_bm25(pages: list[dict]) -> dict:
    """BM25 のインデックスを作る。{df, avgdl, N, doc_lens, term_freqs} を返す。"""
    N = len(pages)
    df = Counter()
    doc_lens = []
    term_freqs = []
    for page in pages:
        tokens = page["tokens"]
        doc_lens.append(len(tokens))
        tf = Counter(tokens)
        term_freqs.append(tf)
        for term in tf:
            df[term] += 1
    avgdl = sum(doc_lens) / N if N else 0
    return {"N": N, "df": df, "avgdl": avgdl, "doc_lens": doc_lens, "term_freqs": term_freqs}


def bm25_score(query_tokens: list[str], doc_idx: int, idx: dict, k1: float = 1.5, b: float = 0.75) -> float:
    score = 0.0
    N = idx["N"]
    df = idx["df"]
    avgdl = idx["avgdl"]
    dl = idx["doc_lens"][doc_idx]
    tf = idx["term_freqs"][doc_idx]
    for term in query_tokens:
        if term not in df:
            continue
        idf = math.log(1 + (N - df[term] + 0.5) / (df[term] + 0.5))
        f = tf.get(term, 0)
        if f == 0:
            continue
        denom = f + k1 * (1 - b + b * (dl / avgdl if avgdl else 1))
        score += idf * (f * (k1 + 1)) / denom
    return score


def bm25_idf(term: str, idx: dict) -> float:
    N = idx["N"]
    df = idx["df"].get(term, 0)
    return math.log(1 + (N - df + 0.5) / (df + 0.5))


def query_coverage(query_tokens: list[str], doc_idx: int, idx: dict) -> float:
    """クエリの異なるトークンのうち、文書に現れるものの割合。IDF で重みを付ける。

    索引にないトークンは IDF が大きくなるので、文書に現れない語を含むクエリほど割合が下がる。
    """
    terms = list(dict.fromkeys(query_tokens))
    total = sum(bm25_idf(term, idx) for term in terms)
    if not total:
        return 0.0
    tf = idx["term_freqs"][doc_idx]
    return sum(bm25_idf(term, idx) for term in terms if tf.get(term)) / total


def covered_pages(query_tokens: list[str], pages: list[dict]) -> set[str] | None:
    """クエリの被覆率が MIN_CJK_QUERY_COVERAGE 以上のページの rel_path を返す。

    かな・漢字の bigram は、関係のない文書とも偶然に一致しやすい。そこで日本語のトークンを含む
    クエリでは、被覆率の低いページを語彙検索の結果から外す。英数字だけのクエリは語単位で一致を
    見るので None を返し、移植元と同じ結果のままにする。被覆率はセクションではなくページで測る。
    セクションで測ると、長い言い換えのクエリがどのセクションにも届かず、ページ単位の検索より
    当たらなくなるためである。
    """
    if all(token.isascii() for token in query_tokens):
        return None
    idx = build_bm25(pages)
    return {
        page["rel_path"]
        for i, page in enumerate(pages)
        if query_coverage(query_tokens, i, idx) >= MIN_CJK_QUERY_COVERAGE
    }


def lexical_ranking(query_tokens: list[str], idx: dict, keep=None) -> list[tuple[float, int]]:
    """BM25 のスコアが正の文書を、スコアの高い順に (score, index) で返す。

    keep を渡すと、keep(index) が真の文書だけを残す。
    """
    ranked = []
    for i in range(idx["N"]):
        if keep is not None and not keep(i):
            continue
        score = bm25_score(query_tokens, i, idx)
        if score > 0:
            ranked.append((score, i))
    ranked.sort(key=lambda item: -item[0])
    return ranked


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def passes_filters(page: dict, args) -> bool:
    meta = page["meta"]
    if args.type:
        # --type 概念 のように日本語の別名で指定されても、英語の type と比べられるようにする。
        wanted_type = canonical_meta({"type": args.type})[0]["type"]
        if meta.get("type") != wanted_type:
            return False
    if args.tag:
        # 全角の「ＡＩ」と半角の「AI」を同じタグとして比べる。
        page_tags = set(normalize_tags(meta.get("tags", [])))
        if not all(t in page_tags for t in normalize_tags(args.tag)):
            return False
    if args.since:
        since = parse_date(args.since)
        updated = parse_date(meta.get("updated"))
        if since and updated and updated < since:
            return False
        if since and not updated:
            return False
    return True


class EmbeddingConfigError(RuntimeError):
    """埋め込みモデルの設定の誤り。メッセージにパスやペイロードを含めないので、そのまま表示してよい。"""


def embedding_failure_label(exc: Exception) -> str:
    """パスやペイロードを漏らさない、ローカルのバックエンドの安全なエラーラベルを返す。"""
    if isinstance(exc, EmbeddingConfigError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def embed_model_name() -> str:
    """使う埋め込みモデルの名前。INFO_HIVE_EMBED_MODEL があればそれを、なければ既定のモデルを返す。"""
    return os.environ.get(EMBED_MODEL_ENV, "").strip() or DEFAULT_EMBED_MODEL


def embed_model_spec(name: str | None = None) -> dict:
    name = name or embed_model_name()
    try:
        return EMBED_MODELS[name]
    except KeyError:
        raise EmbeddingConfigError(
            f"{EMBED_MODEL_ENV} のモデルには対応していない。次のどれかを指定する: {'、'.join(EMBED_MODELS)}"
        ) from None


def section_locator(section: dict) -> str:
    page = section["page"]
    return f"{page['rel_path']}\x1f{section['section_index']}"


def embed_model_file() -> str:
    """add_custom_model で登録するモデルの重みのファイル。fastembed に登録済みのモデルでは空文字列。"""
    try:
        return embed_model_spec().get("custom", {}).get("model_file", "")
    except EmbeddingConfigError:
        return ""


def section_content_hash(section: dict) -> str:
    text = f"{embed_model_name()}\n{section['searchable_text']}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def register_custom_model(text_embedding_cls, name: str, spec: dict) -> None:
    """fastembed に登録されていないモデルを登録する。登録済みなら何もしない。"""
    custom = spec.get("custom")
    if not custom:
        return
    registered = {m["model"].lower() for m in text_embedding_cls.list_supported_models()}
    if name.lower() in registered:
        return
    from fastembed.common.model_description import ModelSource, PoolingType

    text_embedding_cls.add_custom_model(
        model=name,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=custom["hf"]),
        dim=custom["dim"],
        model_file=custom["model_file"],
        license=custom.get("license", ""),
        size_in_gb=custom.get("size_in_gb", 0.0),
    )


def load_local_embedding_backend():
    # huggingface_hub 1.33 は、大きいファイルをキャッシュ全体で共有する置き場所（blobs/<2 文字>/）に置く。
    # 重みを model.onnx_data に分けたモデル（multilingual-e5-large など）では、model.onnx と
    # model.onnx_data の実体が別のディレクトリになり、onnxruntime 1.31 は「外部データがモデルの
    # ディレクトリの外にある」として読み込みを拒否する。共有をやめ、モデルごとに置かせる。
    # huggingface_hub は import のときにこの値を読むので、fastembed より先に設定する。
    os.environ.setdefault("HF_HUB_DISABLE_SHARED_BLOBS", "1")
    try:
        import sqlite_vec
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError(
            "ローカルの埋め込み検索に必要なパッケージがない。`uv run --script` で実行するか、"
            "fastembed と sqlite-vec をインストールする"
        ) from exc

    cache_dir = Path(
        os.environ.get(
            "FASTEMBED_CACHE_PATH",
            Path.home() / ".cache" / "info-hive" / "fastembed",
        )
    )
    name = embed_model_name()
    register_custom_model(TextEmbedding, name, embed_model_spec(name))
    model = TextEmbedding(model_name=name, cache_dir=str(cache_dir))
    dimension = TextEmbedding.get_embedding_size(name)
    return model, sqlite_vec, dimension


def open_vector_index(wiki_root: Path, sqlite_vec, dimension: int) -> sqlite3.Connection:
    path = wiki_root / ".wiki-cache" / VECTOR_INDEX_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS semantic_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        expected = {
            "schema": VECTOR_INDEX_SCHEMA,
            "model": embed_model_name(),
            # 同じモデル名で重みのファイル（fp16 と int8 など）を替えたときも作り直す。
            "model_file": embed_model_file(),
            "dimension": str(dimension),
            "input_format": EMBED_INPUT_FORMAT,
        }
        current = dict(connection.execute("SELECT key, value FROM semantic_meta"))
        if current != expected:
            with connection:
                connection.execute("DROP TABLE IF EXISTS semantic_vectors")
                connection.execute("DROP TABLE IF EXISTS semantic_sections")
                connection.execute("DELETE FROM semantic_meta")
                connection.executemany(
                    "INSERT INTO semantic_meta(key, value) VALUES (?, ?)",
                    expected.items(),
                )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS semantic_sections ("
            "id INTEGER PRIMARY KEY, "
            "locator TEXT NOT NULL UNIQUE, "
            "content_hash TEXT NOT NULL)"
        )
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS semantic_vectors "
            f"USING vec0(embedding float[{dimension}] distance_metric=cosine)"
        )
        return connection
    except Exception:
        connection.close()
        raise


def embed_passages(model, texts: list[str]) -> list[list[float]]:
    # fastembed 0.8.0 は e5 系の接頭辞を自動では付けないので、ここで付ける。
    prefix = embed_model_spec()["passage_prefix"]
    return [
        [float(value) for value in vector]
        for vector in model.passage_embed([prefix + text for text in texts], batch_size=64)
    ]


def embed_query(model, text: str) -> list[float]:
    vector = next(model.query_embed(embed_model_spec()["query_prefix"] + text))
    return [float(value) for value in vector]


def sync_vector_index(
    connection: sqlite3.Connection,
    sqlite_vec,
    model,
    sections: list[dict],
) -> dict[str, int]:
    existing = {
        locator: (row_id, content_hash)
        for row_id, locator, content_hash in connection.execute(
            "SELECT id, locator, content_hash FROM semantic_sections"
        )
    }
    vector_ids = {
        row_id for row_id, in connection.execute("SELECT rowid FROM semantic_vectors")
    }
    current = {
        section_locator(section): (section_content_hash(section), section)
        for section in sections
    }
    stale = set(existing) - set(current)
    changed = [
        (locator, content_hash, section)
        for locator, (content_hash, section) in current.items()
        if locator not in existing
        or existing[locator][1] != content_hash
        or existing[locator][0] not in vector_ids
    ]
    vectors = (
        embed_passages(model, [section["searchable_text"] for _, _, section in changed])
        if changed
        else []
    )
    if len(vectors) != len(changed):
        raise ValueError(
            f"ローカルの埋め込みモデルが返したベクトルの数（{len(vectors)}）が、"
            f"セクションの数（{len(changed)}）と合わない"
        )
    if changed:
        print(
            f"追加・変更された {len(changed)} 件のセクションを {embed_model_name()} で埋め込む……",
            file=sys.stderr,
        )

    with connection:
        for locator in stale:
            row_id = existing[locator][0]
            connection.execute("DELETE FROM semantic_vectors WHERE rowid = ?", (row_id,))
            connection.execute("DELETE FROM semantic_sections WHERE id = ?", (row_id,))
        for (locator, content_hash, _section), vector in zip(changed, vectors):
            row = existing.get(locator)
            if row:
                row_id = row[0]
                connection.execute(
                    "UPDATE semantic_sections SET content_hash = ? WHERE id = ?",
                    (content_hash, row_id),
                )
                connection.execute("DELETE FROM semantic_vectors WHERE rowid = ?", (row_id,))
            else:
                cursor = connection.execute(
                    "INSERT INTO semantic_sections(locator, content_hash) VALUES (?, ?)",
                    (locator, content_hash),
                )
                row_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO semantic_vectors(rowid, embedding) VALUES (?, ?)",
                (row_id, sqlite_vec.serialize_float32(vector)),
            )
    return {
        locator: row_id
        for row_id, locator in connection.execute(
            "SELECT id, locator FROM semantic_sections"
        )
    }


def local_semantic_order(
    query: str,
    all_sections: list[dict],
    allowed_locators: set[str],
    wiki_root: Path,
) -> list[str]:
    model, sqlite_vec, dimension = load_local_embedding_backend()
    max_distance = embed_model_spec()["max_distance"]
    connection = open_vector_index(wiki_root, sqlite_vec, dimension)
    try:
        locator_ids = sync_vector_index(connection, sqlite_vec, model, all_sections)
        allowed_ids = [
            locator_ids[locator]
            for locator in allowed_locators
            if locator in locator_ids
        ]
        if not allowed_ids:
            return []
        query_blob = sqlite_vec.serialize_float32(embed_query(model, query))
        limit = min(50, len(allowed_ids))
        if len(allowed_ids) == len(locator_ids):
            ranked_ids = [
                row_id
                for row_id, distance in connection.execute(
                    "SELECT rowid, distance FROM semantic_vectors "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (query_blob, limit),
                )
                if distance <= max_distance
            ]
        else:
            connection.execute(
                "CREATE TEMP TABLE allowed_sections(id INTEGER PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO allowed_sections(id) VALUES (?)",
                ((row_id,) for row_id in allowed_ids),
            )
            ranked_ids = [
                row_id
                for row_id, distance in connection.execute(
                    "SELECT vectors.rowid, "
                    "vec_distance_cosine(vectors.embedding, ?) AS distance "
                    "FROM semantic_vectors AS vectors "
                    "JOIN allowed_sections AS allowed ON allowed.id = vectors.rowid "
                    "ORDER BY distance LIMIT ?",
                    (query_blob, limit),
                )
                if distance <= max_distance
            ]
        by_id = {row_id: locator for locator, row_id in locator_ids.items()}
        return [by_id[row_id] for row_id in ranked_ids]
    finally:
        connection.close()


def collapse_snippet(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())[:400]


def json_result(
    score: float,
    page: dict,
    section: dict | None = None,
    sections: list[dict] | None = None,
    retrievers: list[str] | None = None,
) -> dict:
    meta = page["meta"]
    heading_path = section["heading_path"] if section else []
    section_index = section["section_index"] if section else None
    prev_heading = None
    next_heading = None
    snippet_text = section["text"] if section else cache_page_body(page["sections"])
    snippet = collapse_snippet(snippet_text)
    if section is not None and sections is not None:
        index = section["section_index"]
        if index > 0:
            previous = sections[index - 1]["heading_path"]
            prev_heading = previous[-1] if previous else None
        if index + 1 < len(sections):
            following = sections[index + 1]["heading_path"]
            next_heading = following[-1] if following else None
    return {
        "slug": page["slug"],
        "rel_path": page["rel_path"],
        "type": meta.get("type"),
        "title": meta.get("title") or page["slug"],
        "heading_path": heading_path,
        "section_index": section_index,
        "score": round(score, 4),
        "retrievers": retrievers or ["bm25"],
        "snippet": snippet,
        "updated": meta.get("updated"),
        # tags と sources は、値を 1 つだけ書いたページ（tags: 検索）でも文字の配列にする。lint が sources を
        # 読むのと同じ wiki_pages.ref_values() を使う。MCP の query も、この関数で結果を作る。
        "tags": ref_values(meta.get("tags")),
        "sources": ref_values(meta.get("sources")),
        "neighbors": {"prev": prev_heading, "next": next_heading},
    }


def emit_json(args, results: list[dict], mode: str = "lexical", decision: dict | None = None) -> None:
    payload = {
        "query": args.query,
        "wiki": Path(args.wiki).as_posix(),
        "granularity": args.granularity,
        "mode": mode,
        "results": results,
    }
    if decision is not None:
        payload["decision"] = decision
    print(json.dumps(payload, ensure_ascii=False))


def emit_empty_json(args, mode: str = "lexical") -> None:
    emit_json(args, [], mode)


def result_limit(args) -> int:
    """出力の前に集める行の数。--decide なら判定のプール、それ以外は --top。"""
    return max(args.top, args.decide_pool) if getattr(args, "decide", False) else args.top


def decide_gate(args, rows: list[dict], texts: list[str]) -> tuple[list[int], dict[int, dict], dict]:
    """任意の関連度のゲート（D1）を実行する。失敗したときは wiki_decide の中で処理を通す。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import wiki_decide

    try:
        client = wiki_decide.client_from_config()
    except wiki_decide.DecisionError as exc:
        wiki_decide.note(str(exc))
        client = None
    threshold = args.decide_threshold
    if threshold is None:
        threshold = wiki_decide.QUESTIONS["relevance"]["default_threshold"]
    kept, row_decisions, decision = wiki_decide.gate_pool(client, args.query, rows, texts, threshold, args.top)
    if not args.json:
        if decision["failed_open"]:
            state = "関連度のゲートで判定できなかったので、すべての行を残した"
        else:
            state = f"関連度のゲートで {decision['pool']} 行のうち {decision['kept']} 行を残した"
        wiki_decide.note(f"{state}（{decision['backend']}、しきい値 {threshold}）")
    return kept, row_decisions, decision


def decision_default_threshold(check: str) -> str:
    """判定チェックの既定の閾値。decision-questions.json から読む。"""
    path = Path(__file__).resolve().parent.parent / "assets" / "decision-questions.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["checks"][check]["default_threshold"])
    except (OSError, ValueError, KeyError):
        return f"{path.name} を参照"


def gated_json(args, items: list[tuple], rows: list[dict], texts: list[str]) -> tuple[list[tuple], list[dict], dict]:
    kept, row_decisions, decision = decide_gate(args, rows, texts)
    kept_rows = [{**rows[i], "decision": row_decisions[i]} if i in row_decisions else rows[i] for i in kept]
    return [items[i] for i in kept], kept_rows, decision


NO_MATCHES = "一致する結果がない。"
NO_ROWS_PASSED_GATE = "関連度のゲートを通った行がない。--decide を付けずに実行すると、ゲートの前の結果を見られる。"


def results_heading(query: str, count: int) -> str:
    """テキスト出力の見出し行。"""
    return f"「{query}」の上位 {count} 件:"


def type_cell(page_type) -> str:
    """結果の行の type の欄。全角の文字を 2 桁と数えて、幅を 9 桁にそろえる。"""
    return ljust_display(str(page_type), 9)


def cmd_search(args, pages: list[dict]) -> None:
    filtered = [page for page in pages if passes_filters(page, args)]
    # プログラムから呼ぶ場合、判定用のフラグを持たない Namespace が渡されることがある。
    decide = getattr(args, "decide", False)
    if not filtered:
        if args.json:
            emit_empty_json(args)
        else:
            print("絞り込みの条件に合うページがない。", file=sys.stderr)
        return
    query_tokens = tokenize(args.query)
    # 記号だけのクエリなどは、語彙側のトークンが 0 個になる。埋め込み検索は意味で当てられる
    # ことがあるので先に進む。埋め込みを使わない場合は、どのページにも当たらないのでここで終える。
    # --json では空の結果を返し、呼び出し側が JSON を読めるようにする。
    if not query_tokens and (args.no_embed or args.granularity == "page"):
        if args.json:
            emit_empty_json(args)
        else:
            print("クエリに検索できる語がない。", file=sys.stderr)
        return

    if args.granularity == "page":
        idx = build_bm25(filtered)
        covered = covered_pages(query_tokens, filtered)
        keep = None if covered is None else (lambda i: filtered[i]["rel_path"] in covered)
        top = [(score, filtered[i]) for score, i in lexical_ranking(query_tokens, idx, keep)[:result_limit(args)]]
        if not top:
            if args.json:
                emit_empty_json(args)
            else:
                print(NO_MATCHES, file=sys.stderr)
            return
        if decide:
            rows = [json_result(score, page) for score, page in top]
            top, rows, decision = gated_json(args, top, rows, [cache_page_body(page["sections"]) for _, page in top])
            if args.json:
                emit_json(args, rows, decision=decision)
                return
            if not top:
                print(NO_ROWS_PASSED_GATE, file=sys.stderr)
                return
        if args.json:
            emit_json(args, [json_result(score, page) for score, page in top])
            return
        print(results_heading(args.query, len(top)))
        print()
        for score, page in top:
            title = page["meta"].get("title") or page["slug"]
            page_type = page["meta"].get("type", "?")
            print(f"  [{score:6.2f}] [{type_cell(page_type)}] {title}")
            print(f"             {page['rel_path']}")
        return

    sections = collect_sections(filtered)
    idx = build_bm25(sections)
    covered = covered_pages(query_tokens, filtered)
    keep = None if covered is None else (lambda i: sections[i]["page"]["rel_path"] in covered)
    bm25_ranked = lexical_ranking(query_tokens, idx, keep)
    ranked = [(score, section_index, ["bm25"]) for score, section_index in bm25_ranked]
    mode = "lexical"
    if not args.no_embed:
        try:
            all_sections = collect_sections(pages)
            section_indices = {
                section_locator(section): index
                for index, section in enumerate(sections)
            }
            semantic_order = local_semantic_order(
                args.query,
                all_sections,
                set(section_indices),
                args.wiki,
            )
            embedding_top = [
                (0.0, section_indices[locator])
                for locator in semantic_order
                if locator in section_indices
            ]
            lexical_top = bm25_ranked[:50]
            candidate_order = [index for _, index in lexical_top]
            seen_candidates = set(candidate_order)
            for _, index in embedding_top:
                if index not in seen_candidates:
                    candidate_order.append(index)
                    seen_candidates.add(index)
            lexical_ranks = {index: rank for rank, (_, index) in enumerate(lexical_top, 1)}
            embedding_ranks = {index: rank for rank, (_, index) in enumerate(embedding_top, 1)}
            ranked = []
            for index in candidate_order:
                score = 0.0
                retrievers = []
                if index in lexical_ranks:
                    score += 1.0 / (60 + lexical_ranks[index])
                    retrievers.append("bm25")
                if index in embedding_ranks:
                    score += 1.0 / (60 + embedding_ranks[index])
                    retrievers.append("embedding")
                ranked.append((score, index, retrievers))
            ranked.sort(key=lambda item: -item[0])
            mode = "hybrid"
        except Exception as exc:
            label = embedding_failure_label(exc)
            print(
                f"警告: ローカルの埋め込み検索を使えない（{label}）。語彙検索だけで続ける",
                file=sys.stderr,
            )

    page_counts = Counter()
    top_sections = []
    for score, section_index, retrievers in ranked:
        section = sections[section_index]
        rel_path = section["page"]["rel_path"]
        if page_counts[rel_path] >= args.per_page:
            continue
        page_counts[rel_path] += 1
        top_sections.append((score, section, retrievers))
        if len(top_sections) >= result_limit(args):
            break
    if not top_sections:
        if args.json:
            emit_empty_json(args, mode)
        else:
            print(NO_MATCHES, file=sys.stderr)
        return
    if args.json or decide:
        sections_by_page = {page["rel_path"]: page["sections"] for page in filtered}
        rows = [
            json_result(
                score,
                section["page"],
                section,
                sections_by_page[section["page"]["rel_path"]],
                retrievers,
            )
            for score, section, retrievers in top_sections
        ]
        decision = None
        if decide:
            top_sections, rows, decision = gated_json(args, top_sections, rows, [s["text"] for _, s, _ in top_sections])
        if args.json:
            emit_json(args, rows, mode, decision)
            return
        if not top_sections:
            print(NO_ROWS_PASSED_GATE, file=sys.stderr)
            return
    print(results_heading(args.query, len(top_sections)))
    print()
    for score, section, _retrievers in top_sections:
        page = section["page"]
        title = page["meta"].get("title") or page["slug"]
        page_type = page["meta"].get("type", "?")
        print(f"  [{score:6.2f}] [{type_cell(page_type)}] {title}")
        print(f"             {page['rel_path']}")
        if section["heading_path"]:
            print(f"             § {' > '.join(section['heading_path'])}")


def cmd_backlinks(args, pages: list[dict]) -> None:
    by_path, by_slug = build_link_index(pages)
    target = link_key(nfc(args.backlinks), by_path, by_slug)
    inbound = []
    for page in pages:
        if any(link_key(link, by_path, by_slug) == target
               for link in page["links"]):
            inbound.append(page)
    if not inbound:
        print(f"[[{target}]] にリンクしているページはない。", file=sys.stderr)
        return
    print(f"[[{target}]] にリンクしているページ（{len(inbound)} 件）:")
    for page in inbound:
        title = page["meta"].get("title") or page["slug"]
        print(f"  - {title}  ({page['rel_path']})")


def cmd_top_linked(args, pages: list[dict]) -> None:
    by_path, by_slug = build_link_index(pages)
    inbound_count = Counter()
    for page in pages:
        for link in page["links"]:
            target = link_key(link, by_path, by_slug)
            if target:
                inbound_count[target] += 1
    top = inbound_count.most_common(args.top_linked)
    if not top:
        print("wiki に wikilink がない。", file=sys.stderr)
        return
    print(f"被リンクの多いページ（ハブ）の上位 {len(top)} 件:")
    for slug, count in top:
        # タイトルに対応するページを探す
        match = next((p for p in pages if p["slug"] == slug), None)
        title = (match["meta"].get("title") if match else None) or slug
        marker = "" if match else "  ［リンク切れ］"
        print(f"  {count:4d}  {title}  ({slug}){marker}")


def main():
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    parser.add_argument("query", nargs="?", default="", help="検索語")
    parser.add_argument("--wiki", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    parser.add_argument("--top", type=int, default=10, help="上位 N 件の結果を返す（既定: 10）")
    parser.add_argument("--type", help="frontmatter の type で絞り込む")
    parser.add_argument("--tag", action="append", default=[], help="タグで絞り込む（複数指定できる）")
    parser.add_argument("--since", help="YYYY-MM-DD 以降に更新されたページだけを対象にする")
    parser.add_argument("--backlinks", help="このスラッグにリンクしているページを探す")
    parser.add_argument("--top-linked", type=int, help="被リンクの多い N ページを表示する")
    parser.add_argument("--cache", nargs="?", const="AUTO", default=None,
                        help="差分で更新するパースキャッシュのパス（値を省くと <wiki>/.wiki-cache/search-index.json）")
    parser.add_argument("--granularity", choices=("section", "page"), default="section",
                        help="セクション（既定）とページ全体のどちらを検索するか")
    parser.add_argument("--per-page", type=int, default=2,
                        help="1 ページあたりのセクションの結果の最大数（既定: 2）")
    parser.add_argument("--json", action="store_true", help="構造化した検索結果を JSON で出力する（キーは英語のまま）")
    parser.add_argument("--no-embed", action="store_true", help="語彙検索（BM25）だけを使う")
    parser.add_argument("--decide", action="store_true",
                        help="判定モデルが関連ありと判定した行だけを残す。INFO_HIVE_DECISIONS が必要")
    parser.add_argument("--decide-pool", type=int, default=20,
                        help="--decide で採点する行の数（既定: 20）")
    parser.add_argument("--decide-threshold", type=float, default=None,
                        help=f"--decide で行を残すのに必要な P(relevant)"
                             f"（既定: {decision_default_threshold('relevance')}）")
    args = parser.parse_args()
    if args.wiki is None:
        args.wiki = wiki_workspace.default_wiki_or_exit()
    # SCHEMA.md のないディレクトリは、初期化された wiki とみなさない（ADR-0019）。パースキャッシュを書く前に確かめる。
    wiki_initialized.require_or_exit(args.wiki)
    cache_path = None
    if args.cache:
        cache_path = args.wiki / ".wiki-cache" / "search-index.json" if args.cache == "AUTO" else Path(args.cache)

    pages = collect_pages(args.wiki, cache_path)
    if not pages:
        print(f"wiki のページが見つからない: {args.wiki}", file=sys.stderr)
        sys.exit(0)

    if args.backlinks:
        cmd_backlinks(args, pages)
    elif args.top_linked:
        cmd_top_linked(args, pages)
    elif args.query:
        cmd_search(args, pages)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
