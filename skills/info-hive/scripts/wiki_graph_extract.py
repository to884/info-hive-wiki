#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyyaml==6.0.3",
# ]
# ///
"""
wiki_graph_extract.py — Markdown の wiki を、クエリできるグラフにコンパイルする。

正本は Markdown のままである。このスクリプトは wiki のすべてのページを読み、ノードとエッジ
（`graph.relationships` から作る型付きの意味のエッジと、暗黙の `mentions`、`sourced_from`、
`summarizes_raw` のエッジ）を導き、`<wiki>/graph/` の下に成果物を出力する。成果物はいつでも削除して作り直せる。

PyYAML の固定した依存を持つので、`uv run --script` で実行する。

オントロジーがないときは、グラフ層を導入する前の wiki として扱い、何も書き出さずに
終了コード 1 で止まる。生成物はすべて一時ファイルに書き終えてから置き換えるので、書いている
途中で止まっても前の生成物が残る。同じ出力先の extract は、ロックで 1 つずつ動かす。
手順の正本は references/graph-workflow.md にある。

使い方:
    uv run --script wiki_graph_extract.py <wiki-dir> [options]

オプション:
    --out <dir>                  出力ディレクトリ（既定: <wiki-dir>/graph）
    --formats jsonl,sqlite,...   出力する形式のカンマ区切りのリスト
                                 （jsonl、sqlite、graphml。既定: 3つすべて）
    --ontology <path>            オントロジーのパスを上書きする
                                 （既定: <wiki-dir>/graph/ontology.yaml）

例:
    uv run --script wiki_graph_extract.py wiki/
    uv run --script wiki_graph_extract.py wiki/ --out wiki/graph --formats jsonl,sqlite
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "wiki_graph_extract.py には PyYAML が必要である。"
        "`uv run --script wiki_graph_extract.py ...` で実行する。",
        file=sys.stderr,
    )
    sys.exit(2)

# 同じディレクトリにある共通モジュールを import する。
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from wiki_text import (  # noqa: E402
    canonical_key, canonical_meta, ljust_display, localize_argparse, nfc, parse_frontmatter_text, posix_rel,
    top_level_keys,
)
import wiki_graph_common as common  # noqa: E402


# ページの集合とリンクの規則は wiki_pages の 1 か所に置く（ADR-0010 の決定の 1）。
from wiki_pages import (  # noqa: E402
    SKIP_TOP_LEVEL_DIRS,
    SKIP_TOP_LEVEL_FILES,
    WIKILINK_RE,
    clean_link_target,
    extract_wikilinks,
    build_page_index,
    classify_source_ref,
    iter_page_files,
    link_candidates,
    link_lookup,
    ref_values,
)
from wiki_pages import resolve_link as resolve_link_slug  # noqa: E402

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

DEFAULT_FORMATS = ["jsonl", "sqlite", "graphml"]

# build_nodes と build_edges の診断の種類。グラフの lint は、同じ名前の検出結果として挙げる。
DIAGNOSTIC_LABELS = {
    "duplicate_node_id": "同じ node_id のページ",
    "ambiguous_object": "目的語の node_id を持つページが複数ある関係",
    "ambiguous_source": "sources: の値に当たるページが複数ある",
    "unresolved_source": "sources: の値に当たるページがない",
    "order_dependent_edge_ids": "並べ替えるとエッジの ID が入れ替わりうる関係の項目",
    "unreadable_graph": "graph: を読めなかったページ",
    "unread_field": "lint のパーサーが読めない書き方の欄",
}

# `graph:` の中で、ページやノードを指す欄。normalize_refs が NFC にそろえ、read_frontmatter が書かれた文字で読む。
GRAPH_REF_FIELDS = ("node_id", "aliases")
RELATIONSHIP_REF_FIELDS = ("object", "source", "contradicts", "supersedes")
# グラフがエッジとノードの種類に使う最上位の欄。lint のパーサーが読めなかったら、unread_field の診断に挙げる。
WATCHED_TOP_FIELDS = ("type", "kind", "sources", "raw")
# YAML として読めない frontmatter に `graph` の欄があるかを、行頭の書き方で見る（`graph:`、`"graph":`、`graph :`）。
GRAPH_KEY_LINE_RE = re.compile(r"""^(?:graph|"graph"|'graph')[ \t]*:""", re.MULTILINE)
_YAML_STR_TAG = "tag:yaml.org,2002:str"
_YAML_NULL_TAG = "tag:yaml.org,2002:null"


# ---------------------------------------------------------------------------
# ページの収集
# ---------------------------------------------------------------------------


def _nfc_value(value):
    """文字列なら NFC にそろえ、文字列のリストなら各要素をそろえる。それ以外はそのまま返す。"""
    if isinstance(value, str):
        return nfc(value)
    if isinstance(value, list):
        return [nfc(v) if isinstance(v, str) else v for v in value]
    return value


def normalize_refs(meta: dict) -> dict:
    """ノードやページを指す frontmatter の値を NFC にそろえる。

    NFD で入力された ID（macOS のファイル名からコピーしたものなど）が、NFC のスラッグを持つ
    ページと一致するようにする。対象は graph.node_id、graph.aliases、
    graph.relationships[] の object・source・contradicts・supersedes、`sources:`、`raw:` である。
    evidence や notes のような本文の値には手を付けない。
    """
    out = dict(meta)
    for key in ("sources", "raw"):
        if key in out:
            out[key] = _nfc_value(out[key])
    graph_meta = out.get("graph")
    if isinstance(graph_meta, dict):
        graph_meta = dict(graph_meta)
        for key in GRAPH_REF_FIELDS:
            if key in graph_meta:
                graph_meta[key] = _nfc_value(graph_meta[key])
        rels = graph_meta.get("relationships")
        if isinstance(rels, list):
            new_rels = []
            for rel in rels:
                if isinstance(rel, dict):
                    rel = dict(rel)
                    for key in RELATIONSHIP_REF_FIELDS:
                        if key in rel:
                            rel[key] = _nfc_value(rel[key])
                new_rels.append(rel)
            graph_meta["relationships"] = new_rels
        out["graph"] = graph_meta
    return out


def _is_typed_scalar(value) -> bool:
    """PyYAML が、文字列でも None でもないスカラー（真偽値、数、日付、日時など）に読んだ値なら True。"""
    return value is not None and not isinstance(value, (str, list, dict))


def _child(node, key: str):
    """マッピングのノードから、文字列のキー key の値のノードを返す。なければ None。

    同じキーが複数あれば、PyYAML が辞書を作るときと同じく、後のものを返す。PyYAML が値に組み立てたあとの
    ノードは、マージキー（<<）が展開されているので、マージで来たキーも引ける。
    """
    found = None
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.tag == _YAML_STR_TAG and key_node.value == key:
                found = value_node
    return found


def _written(node):
    """値のノードの、書かれた文字を返す。スカラーは文字列、配列は要素ごとの文字列（スカラーでない要素は None）。

    それ以外（マッピング、ノードがない）は None を返す。ノードの中には入らないので、自分を含むアンカーでも止まらない。
    """
    if isinstance(node, yaml.ScalarNode):
        return node.value
    if isinstance(node, yaml.SequenceNode):
        return [n.value if isinstance(n, yaml.ScalarNode) else None for n in node.value]
    return None


def _as_written(value, written):
    """参照の欄の値のうち、PyYAML が文字列でないスカラーに読んだものを、書かれた文字に置き換えて返す。

    written は、同じ位置のノードの _written である。配列は、長さが同じときだけ要素ごとに置き換える（入れ子の
    配列には入らない）。文字列、None（値なし）、位置が合わない値は、そのまま返す。
    value は書き換えず、新しい値を返す（アンカーで共有している別の欄を変えない）。
    """
    def one(v, w):
        return w if _is_typed_scalar(v) and isinstance(w, str) and w else v

    if isinstance(value, list):
        if isinstance(written, list) and len(written) == len(value):
            return [one(v, w) for v, w in zip(value, written)]
        return value
    return one(value, written)


def _graph_refs_as_written(graph_meta: dict, graph_node) -> dict:
    """`graph:` の中の、ページやノードを指す欄を、書かれた文字で読み直す。

    `source: 007` は 7 ではなく "007"、`object: no` は False ではなく "no" にする。対象は GRAPH_REF_FIELDS と
    RELATIONSHIP_REF_FIELDS だけで、ほかの欄（canonical、valid_from、notes など）は PyYAML の値のまま残す。
    None（null、~、値なし）は値なしのまま残す。graph_node は、graph_meta を組み立てたノードである。
    """
    out = dict(graph_meta)
    for key in GRAPH_REF_FIELDS:
        if key in out:
            out[key] = _as_written(out[key], _written(_child(graph_node, key)))
    rels, rels_node = out.get("relationships"), _child(graph_node, "relationships")
    if isinstance(rels, list) and isinstance(rels_node, yaml.SequenceNode) and len(rels) == len(rels_node.value):
        new_rels = []
        for rel, rel_node in zip(rels, rels_node.value):
            if isinstance(rel, dict):
                rel = dict(rel)
                for key in RELATIONSHIP_REF_FIELDS:
                    if key in rel:
                        rel[key] = _as_written(rel[key], _written(_child(rel_node, key)))
            new_rels.append(rel)
        out["relationships"] = new_rels
    return out


def _unread_fields(own_pairs: list, root, meta: dict, fm_text: str) -> list[str]:
    """YAML には値があるのに、lint のパーサーが読めなかった WATCHED_TOP_FIELDS の欄を返す。

    own_pairs は、マージキーを展開する前の最上位の (キーのノード, 値のノード) で、root は展開したあとの
    最上位のノードである。次のどれかに当たる欄を返す。
    - その欄を書いた回数（値なしを除く）が、lint のパーサーがキーの行として読んだ回数より多い
      （`"type": x`、`type : x`。読めるキーと読めないキーで 2 度書いたものを含む）。
    - YAML の配列の要素の数（2 度書いたら合計）より少ない数しか、lint のパーサーが読んでいない
      （字下げのない `- x`、字下げ 4 つの `    - x`、複数行の `[a,` … `]`）。
    - マージキーで来た値があるのに、lint の形にその欄がない。
    値の中身は比べない。
    """
    written_times: dict[str, int] = {}
    items: dict[str, int] = {}
    for key_node, value_node in own_pairs:
        if not (isinstance(key_node, yaml.ScalarNode) and key_node.tag == _YAML_STR_TAG):
            continue
        field = canonical_key(key_node.value)
        if field not in WATCHED_TOP_FIELDS:
            continue
        if isinstance(value_node, yaml.SequenceNode):
            items[field] = items.get(field, 0) + len(value_node.value)
        if not (isinstance(value_node, yaml.ScalarNode) and value_node.tag == _YAML_NULL_TAG):
            written_times[field] = written_times.get(field, 0) + 1
    read_times: dict[str, int] = {}
    for key in top_level_keys(fm_text):
        field = canonical_key(key)
        read_times[field] = read_times.get(field, 0) + 1
    effective: set[str] = set()
    for key_node, value_node in root.value:
        if (isinstance(key_node, yaml.ScalarNode) and key_node.tag == _YAML_STR_TAG
                and not (isinstance(value_node, yaml.ScalarNode) and value_node.tag == _YAML_NULL_TAG)):
            effective.add(canonical_key(key_node.value))
    found = []
    for field in WATCHED_TOP_FIELDS:
        value = meta.get(field)
        if (written_times.get(field, 0) > read_times.get(field, 0)
                or (items.get(field) and not (isinstance(value, list) and len(value) >= items[field]))
                or (field in effective and field not in meta)):
            found.append(field)
    return found


def _read_graph(fm_text: str, meta: dict) -> "tuple[dict | None, list[dict]]":
    """frontmatter の `graph` の欄だけを PyYAML で読む。(graph のマッピングか None, 診断の一覧) を返す。

    PyYAML には frontmatter を構文として読ませ、値に組み立てるのは `graph` のノードだけにする。ほかの欄の値
    （PyYAML が ValueError を出す `created: 2026-13-01` など）には左右されない。meta は lint の形の辞書で、
    lint のパーサーが `graph` のキーを読んだかどうかと、unread_field の判定に使う。例外は外に出さない。
    """
    # 構文として読めないときに、graph の欄がありそうか。あれば、関係を黙って失わないように挙げる。
    declared = "graph" in meta or bool(GRAPH_KEY_LINE_RE.search(fm_text))
    unreadable = {"kind": "unreadable_graph", "reason": "yaml_error"}
    not_mapping = {"kind": "unreadable_graph", "reason": "not_mapping"}
    try:
        # YAML に書けない文字（NUL など）があると、ここで ReaderError になる。
        loader = yaml.SafeLoader(fm_text)
    except Exception:
        return None, [unreadable] if declared else []
    try:
        try:
            root = loader.get_single_node()
        except Exception:
            return None, [unreadable] if declared else []
        if not isinstance(root, yaml.MappingNode):
            # 最上位がマッピングでない（`type:source` だけの frontmatter は、1 つの文字列になる）。
            return None, [unreadable] if declared and root is not None else []
        own_pairs = list(root.value)
        try:
            loader.flatten_mapping(root)  # 最上位のマージキー（<<）を、PyYAML が読むときと同じに展開する
        except Exception:
            pass  # 展開できないマージキー（`<<: 3`）は、ないものとして扱う。ほかの欄は読む。
        problems = [{"kind": "unread_field", "field": field}
                    for field in _unread_fields(own_pairs, root, meta, fm_text)]
        graph_node = _child(root, "graph")
        if graph_node is None or (isinstance(graph_node, yaml.ScalarNode) and graph_node.tag == _YAML_NULL_TAG):
            return None, problems
        if not isinstance(graph_node, yaml.MappingNode):
            return None, problems + [not_mapping]
        try:
            graph_meta = loader.construct_document(graph_node)
        except Exception:
            # 値を組み立てられない（`valid_from: 2026-13-01`、`!!float nope`、配列のキーなど）。
            return None, problems + [unreadable]
        if not isinstance(graph_meta, dict):
            return None, problems + [not_mapping]
        return _graph_refs_as_written(graph_meta, graph_node), problems
    finally:
        loader.dispose()


def read_frontmatter(text: str) -> "tuple[dict, str, list[dict]]":
    """frontmatter を読む。(meta, body, 診断の一覧) を返す。

    最上位の欄は、wiki の lint と同じ文字列のパーサー（wiki_text.parse_frontmatter_text）と canonical_meta で読む。
    値は書かれた文字のままの文字列か、その配列になり、lint、検索、統計と同じ値になる（`sources: [true]` は
    ["true"]、`kind: 2024` は "2024"）。日本語の別名のキーと type・kind の値は英語に直す。
    `graph` の欄だけを PyYAML から取る（lint のパーサーは入れ子を読まない）。マッピングに読めなければ、
    `graph` のキーを置かない。`graph:` の中のキーは直さない。ノードやページを指す値は NFC にそろえる。

    診断は、ページを付ける前の {"kind": ...} で、build_nodes がページを足して diagnostics に入れる。
    - unreadable_graph: `graph` の欄を書いたのに、マッピングに読めなかった。reason は "yaml_error" か "not_mapping"。
    - unread_field: YAML には値があるのに、lint のパーサーが読めなかった最上位の欄（field）。
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, []
    fm_text = m.group(1)
    body = text[m.end():]
    meta, _conflicts = canonical_meta(parse_frontmatter_text(fm_text))
    graph_meta, problems = _read_graph(fm_text, meta)
    # lint の形の graph（入れ子を読まないので []。字下げ 2 つの `  - x` が紛れ込むこともある）は使わない。
    meta.pop("graph", None)
    if graph_meta is not None:
        meta["graph"] = graph_meta
    return normalize_refs(meta), body, problems


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """read_frontmatter の (meta, body) だけを返す。"""
    meta, body, _problems = read_frontmatter(text)
    return meta, body


def collect_pages(wiki_root: Path) -> list[dict]:
    pages = []
    for md_path in iter_page_files(wiki_root):
        rel = md_path.relative_to(wiki_root)
        try:
            # BOM 付きの UTF-8 でも frontmatter を読めるように utf-8-sig で読む。
            text = md_path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        meta, body, fm_problems = read_frontmatter(text)
        # コードフェンスとインラインのコードの中の [[...]] からは mentions のエッジを作らない。
        links = extract_wikilinks(body)
        pages.append({
            "path": str(md_path),
            # スラッグとパスは NFC にそろえる。lint が重複を報告するときのために、
            # ディスク上の実際のパスも disk_rel_path に残しておく。
            "rel_path": posix_rel(str(rel)),
            # as_posix は Windows の区切りだけを / にする。Linux でファイル名に含まれる \ は、そのまま残す
            # （/ に替えると、concepts/a\b.md が concepts/a/b.md と同じキーになる）。
            "disk_rel_path": rel.as_posix(),
            "slug": nfc(md_path.stem),
            "meta": meta,
            "body": body,
            "links": links,
            # frontmatter を読んだときの診断（unreadable_graph、unread_field）。build_nodes が diagnostics に入れる。
            "fm_problems": fm_problems,
        })
    return pages


# ---------------------------------------------------------------------------
# オントロジー
# ---------------------------------------------------------------------------


class OntologyError(Exception):
    """オントロジーのファイルはあるが、読めない。"""


def read_ontology(path: Path) -> dict:
    """オントロジーを読む。読めなければ OntologyError を送出する。

    ファイルがあるかどうかは、呼び出す側が先に `wiki_graph_common.require_ontology` で確かめる。
    extract と lint は、この関数で同じように読む。node_types[*].maps_from の type と kind は、ページの値と
    同じく、書かれた文字にそろえる（_maps_from_as_written）。ほかの値は PyYAML の値のままである。
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        loader = yaml.SafeLoader(text)
        try:
            # yaml.safe_load と同じ読み方。書かれた文字を取るために、ノードを手元に残す。
            root = loader.get_single_node()
            data = loader.construct_document(root) if root is not None else None
        finally:
            loader.dispose()
    except Exception as e:
        # YAML の誤りのほか、PyYAML が値を組み立てられないとき（`2026-13-01` のような日付で ValueError）も含む。
        raise OntologyError(f"オントロジーを読めない（{path}）: {e}") from e
    if data is None:  # 空のファイルは、空のオントロジーとして扱う
        data = {}
    # []、false、0 などを空のマッピングとして通さない。
    if not isinstance(data, dict):
        raise OntologyError(f"オントロジーの最上位がマッピングでない（{path}）")
    for key in ("node_types", "predicates"):
        if data.get(key) is None:
            data[key] = {}
    _maps_from_as_written(data, root)
    return data


def _maps_from_as_written(data: dict, root) -> None:
    """node_types[*].maps_from の type と kind を、書かれた文字にそろえる。root は data を組み立てたノード。

    ページの type と kind は文字で読む（read_frontmatter）ので、照らす側も文字にする。`maps_from: {kind: 2024}` は、
    ページの `kind: 2024`（"2024"）に当たる。マージキー（<<）で来た値も、書かれた文字になる。
    """
    node_types = data.get("node_types")
    types_node = _child(root, "node_types")
    if not isinstance(node_types, dict):
        return
    for name, nt_def in node_types.items():
        maps = nt_def.get("maps_from") if isinstance(nt_def, dict) else None
        if not (isinstance(name, str) and isinstance(maps, dict)):
            continue
        maps_node = _child(_child(types_node, name), "maps_from")
        new_maps = dict(maps)
        for key in ("type", "kind"):
            if key in new_maps:
                new_maps[key] = _as_written(new_maps[key], _written(_child(maps_node, key)))
        nt_def["maps_from"] = new_maps


def load_ontology(path: Path) -> dict:
    """read_ontology と同じ。読めなければ終了コード 2 で止まる（CLI 用）。"""
    try:
        return read_ontology(path)
    except OntologyError as e:
        print(str(e), file=sys.stderr)
        sys.exit(common.EXIT_BAD_ONTOLOGY)


def derive_node_type(meta: dict, ontology: dict) -> str | None:
    """ontology[node_types][*].maps_from を使い、ページの frontmatter を node_type に対応付ける。"""
    page_type = meta.get("type")
    page_kind = meta.get("kind")
    explicit = (meta.get("graph") or {}).get("node_type") if isinstance(meta.get("graph"), dict) else None
    if explicit:
        return explicit
    # まず (type, kind) で照合し、次に type だけで照合する。
    type_kind_match = None
    type_only_match = None
    for nt_name, nt_def in ontology["node_types"].items():
        maps = (nt_def or {}).get("maps_from") or {}
        m_type = maps.get("type")
        m_kind = maps.get("kind")
        if m_type and m_type == page_type:
            if m_kind and m_kind == page_kind:
                type_kind_match = nt_name
                break
            if not m_kind and type_only_match is None:
                type_only_match = nt_name
    return type_kind_match or type_only_match


# ---------------------------------------------------------------------------
# ノードとエッジの構築
# ---------------------------------------------------------------------------


def _tag_list(tags) -> list:
    """frontmatter の tags をリストにする。文字列 1 つで書いたタグ（tags: ml）は、1 文字ずつに分けず、1 つのタグにする。"""
    if isinstance(tags, str):
        return [tags] if tags else []
    return list(tags) if isinstance(tags, list) else []


def page_key(p: dict) -> str:
    """ページを 1 つに決めるキー。ディスク上の相対パスを使う。

    スラッグは別のディレクトリのページと重なり、NFC にそろえた rel_path も、NFC と NFD の名前の違いだけの
    2 つのファイルで重なるので、どちらもページを 1 つに決めない。
    """
    return p.get("disk_rel_path") or p["rel_path"]


def page_node_id(meta: dict, slug: str, ontology: dict) -> str:
    """ページのノード ID。graph.node_id があればそれを文字列にし、なければ <node_type>:<slug> にする。

    グラフの lint の derive_node_id と同じ規則である（1 と "1" を同じ ID とみなす）。
    """
    graph_meta = meta.get("graph") if isinstance(meta.get("graph"), dict) else {}
    explicit = graph_meta.get("node_id")
    if explicit:
        return str(explicit)
    return f"{derive_node_type(meta, ontology) or 'concept'}:{slug}"


def build_nodes(pages: list[dict], ontology: dict,
                diagnostics: list | None = None) -> tuple[list[dict], dict, list[dict]]:
    """ノードのリスト、ページから node_id を引くインデックス、別名の行を作る。(nodes, page_node_ids, aliases) を返す。

    page_node_ids のキーは page_key(p) で、すべてのページを含む。スラッグでは引かない。別のディレクトリに
    同じスラッグのページがあると、別のページのノードを引いてしまうからである。
    同じ node_id のページが複数あるときは、ノードは最初のページから 1 つだけ作り（ノードの題名やパスは
    そのページのもの）、どのページも page_node_ids ではそのノードを指す。ページの関係は捨てない。
    diagnostics にリストを渡すと、{"kind": "duplicate_node_id", "node_id", "pages"} を足す。pages はディスク上の
    パス（page_key）である。
    """
    nodes: list[dict] = []
    page_node_ids: dict[str, str] = {}
    aliases: list[dict] = []
    pages_of_id: dict[str, list[str]] = defaultdict(list)

    for p in pages:
        meta = p["meta"]
        graph_meta = meta.get("graph") if isinstance(meta.get("graph"), dict) else {}
        node_type = derive_node_type(meta, ontology) or "concept"
        node_id = page_node_id(meta, p["slug"], ontology)
        page_node_ids[page_key(p)] = node_id
        if diagnostics is not None:
            for problem in p.get("fm_problems") or []:
                diagnostics.append({"kind": problem["kind"], "page": p["rel_path"],
                                    **{k: v for k, v in problem.items() if k != "kind"}})
        # 報告にはディスク上のパスを使う（lint の duplicate_node_ids と同じ）。NFC のパスでは、NFC と NFD の
        # 名前の違いだけの 2 つのファイルが同じに見える。
        pages_of_id[node_id].append(page_key(p))

        # 同じ node_id の 2 つ目からは、ノードを作らない。lint の duplicate_node_ids がこれを指摘する。
        if len(pages_of_id[node_id]) > 1:
            continue

        node = {
            "id": node_id,
            "slug": p["slug"],
            "title": meta.get("title") or p["slug"],
            "page_type": meta.get("type") or "",
            "node_type": node_type,
            "kind": meta.get("kind") or "",
            "tags": _tag_list(meta.get("tags")),
            "aliases": list(graph_meta.get("aliases") or []),
            "path": p["rel_path"],
            "created": meta.get("created") or "",
            "updated": meta.get("updated") or "",
            "canonical": bool(graph_meta.get("canonical", False)),
        }
        nodes.append(node)
        for alias in node["aliases"]:
            aliases.append({"alias": str(alias), "node_id": node_id})

    if diagnostics is not None:
        for node_id, paths in pages_of_id.items():
            if len(paths) > 1:
                diagnostics.append({"kind": "duplicate_node_id", "node_id": node_id, "pages": paths})
    return nodes, page_node_ids, aliases


def edge_id(subject: str, predicate: str, obj: str, source: str | None, evidence: str | None,
            page: str | None = None, variant: str | None = None) -> str:
    # 96 ビットに切り詰める。現実的な wiki の規模なら衝突のおそれは無視でき、
    # ID が短いほうが JSONL、sqlite、graphml の出力が読みやすい。
    # page も入れる。別のページに同じ内容の関係を書いたら、別のエッジにする（どのページの関係かを失わない）。
    # variant には、graph.relationships の項目の relationship_variant_key と、番号を入れる前の ID が同じ項目の
    # 何番目かを入れる。status や valid_from だけが違う 2 件も、まったく同じ 2 件も、別のエッジになる。
    h = hashlib.sha256()
    parts = ["" if x in (None, "") else str(x) for x in (subject, predicate, obj, source, evidence, page, variant)]
    h.update("\x1f".join(parts).encode("utf-8"))
    return h.hexdigest()[:24]


# 型付きの関係のエッジの ID に入れる欄（主語、述語、目的語、出典、証拠のほか）。
VARIANT_FIELDS = ("confidence", "status", "valid_from", "valid_to", "notes", "raw_ref")
# 参照の欄。ノード ID などの値は ID に入れるが、エッジの ID の形（edge_id が返す 16 進 24 桁）の値は入れない。
# 入れると、参照を書き直すたびに自分の ID が変わり、互いを指す 2 件の参照が成り立たない。
REFERENCE_FIELDS = ("contradicts", "supersedes")
EDGE_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def _stable_json(value) -> str:
    """値を、比べられる文字列にする。辞書のキーは文字列にそろえる（_normalize_for_json）。"""
    return json.dumps(_normalize_for_json(value), sort_keys=True, ensure_ascii=False, default=str)


def relationship_variant_key(rel: dict) -> str:
    """graph.relationships の項目を見分けるキー。VARIANT_FIELDS と、エッジの ID でない参照の欄の値から作る。

    エッジの ID を書いた参照の欄と、未知の拡張の欄は入れない。それらだけが違う項目は、番号で分かれる
    （build_edges が order_dependent_edge_ids の診断に挙げる）。
    """
    refs = [[k, None if EDGE_ID_RE.match(str(rel.get(k) or "")) else rel.get(k)] for k in REFERENCE_FIELDS]
    return _stable_json([[k, rel.get(k)] for k in VARIANT_FIELDS] + refs)


def make_edge(*, subject, predicate, obj, source, evidence, confidence, status,
              extraction_method, page, extras: dict | None = None, page_id: str | None = None,
              variant: str | None = None) -> dict:
    """エッジを作る。page は出力するページのパス（NFC）、page_id はエッジの ID に入れるページのキー。

    page_id を省くと page を使う。NFC と NFD の名前の違いだけの 2 つのファイルは、page が同じになるので、
    ID にはディスク上のパス（page_key）を入れて、別のエッジに保つ。
    """
    return {
        "id": edge_id(subject, predicate, obj, source, evidence, page_id or page, variant),
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "source": source or "",
        "evidence": evidence or "",
        "confidence": confidence or "",
        "status": status or "",
        "extraction_method": extraction_method,
        "page": page,
        "extras": extras or {},
    }


def build_edges(pages: list[dict], page_node_ids: dict[str, str],
                diagnostics: list | None = None) -> list[dict]:
    """エッジを作る。page_node_ids は build_nodes が返す、page_key(p) から node_id を引くインデックス。

    - 主語は、そのエッジを書いたページのノードである。スラッグから引き直さない。
    - エッジの ID には page が入る。別のページに書いた同じ内容の関係は、別のエッジになる。
    - 型付きの関係の目的語は、ノード ID として引く。そのノード ID を持つページが 2 つ以上あれば、エッジを
      作らず、曖昧とする（ambiguous_object）。持つページがなければ、今までどおり文字列の目的語のエッジにする。
    - 本文の wikilink と `sources:` は、wiki_pages.link_candidates（resolve_link と同じ規則）でページに解決し、
      ページが 1 つのときだけ、そのページのノードへのエッジにする。パスのない形が、同じスラッグの複数の
      ページに当たるときは、どれにも寄せない（lint の duplicate_slugs がその重なりを挙げる）。
    - `sources:` の値で、ページが 2 つ以上のもの（ambiguous_source）と、ないもの（unresolved_source）は、
      エッジを作らない。

    diagnostics にリストを渡すと、ambiguous_object、ambiguous_source、unresolved_source、order_dependent_edge_ids の
    診断を足す。order_dependent_edge_ids は、ID に入る欄がすべて同じで、エッジの ID を書いた参照の欄か未知の欄だけが
    違う項目（indexes）で、並べ替えると ID が別の項目に移りうる。
    どれも {"kind", "page", ...} の形で、page はエッジの page と同じく、その参照を書いたページの rel_path（NFC）
    である。候補のページを並べる pages は、ディスク上のパス（page_key）である。
    """
    edges: list[dict] = []
    seen_ids: set[str] = set()

    # 行き先のページを引くインデックス。パスは .md を除いた wiki からの相対パス（NFC、大文字と小文字を区別）、
    # スラッグはファイル名の stem である。どちらも、当たるページのキーをすべて並べる。
    pages_by_path, pages_by_slug = build_page_index(pages, page_key)
    type_by_key = {page_key(p): p["meta"].get("type") for p in pages}

    # ノード ID ごとの、それを持つページの数。2 つ以上なら、目的語としては曖昧である。
    pages_per_node: dict[str, int] = defaultdict(int)
    for node_id in page_node_ids.values():
        pages_per_node[node_id] += 1

    def link_target(link: str) -> str | None:
        """wikilink が指すページのキー。ページがちょうど 1 つのときだけ返す。"""
        found = link_candidates(link, pages_by_path, pages_by_slug)
        return found[0] if len(found) == 1 else None

    def note(kind: str, p: dict, **fields) -> None:
        if diagnostics is not None:
            diagnostics.append({"kind": kind, "page": p["rel_path"], **fields})

    def push(edge: dict) -> None:
        if edge["id"] in seen_ids:
            return
        seen_ids.add(edge["id"])
        edges.append(edge)

    for p in pages:
        here = page_key(p)
        subject_id = page_node_ids.get(here)
        if not subject_id:
            continue
        meta = p["meta"]
        graph_meta = meta.get("graph") if isinstance(meta.get("graph"), dict) else {}

        # 1. graph.relationships[] から作る型付きの意味のエッジ。項目 1 つから、必ず 1 本のエッジを作る。
        seen_items: dict[str, int] = defaultdict(int)
        same_base: dict[str, list] = defaultdict(list)
        for index, rel in enumerate(graph_meta.get("relationships") or []):
            if not isinstance(rel, dict):
                continue
            obj = rel.get("object")
            predicate = rel.get("predicate")
            if not (obj and predicate):
                continue
            if pages_per_node.get(str(obj), 0) > 1:
                # 同じ node_id のページが複数あり、どのページのノードか決められない。
                note("ambiguous_object", p, index=index, predicate=str(predicate), object=str(obj),
                     pages=sorted(k for k, v in page_node_ids.items() if v == str(obj)))
                continue
            extras = {
                k: rel[k] for k in ("valid_from", "valid_to", "notes", "raw_ref",
                                    "contradicts", "supersedes")
                if k in rel and rel[k] is not None
            }
            item = relationship_variant_key(rel)
            # 番号は、番号を入れる前の ID が同じ項目の中で数える（ID と同じ値の扱いで比べるので、省いた欄と空文字の
            # 欄も同じに数える）。ID に入る欄の違う項目を並べ替えても、番号は変わらない。
            base = edge_id(subject_id, str(predicate), str(obj), rel.get("source"), rel.get("evidence"), here, item)
            ordinal = seen_items[base]
            seen_items[base] += 1
            same_base[base].append((index, _stable_json(rel)))
            push(make_edge(
                subject=subject_id,
                predicate=str(predicate),
                obj=str(obj),
                source=rel.get("source"),
                evidence=rel.get("evidence"),
                confidence=rel.get("confidence"),
                status=rel.get("status"),
                extraction_method="explicit_graph_frontmatter",
                page=p["rel_path"],
                page_id=here,
                variant=f"{item}#{ordinal}",
                extras=extras,
            ))

        # 中身の違う項目が同じ番号の列に並んだら（エッジの ID を書いた参照の欄か、未知の欄だけが違う）、
        # 並べ替えると ID が別の項目に移りうる。黙らずに診断に挙げる。
        for group in same_base.values():
            if len(group) > 1 and len({content for _index, content in group}) > 1:
                note("order_dependent_edge_ids", p, indexes=[index for index, _content in group])

        # 2. 本文の wikilink から作る mentions エッジ。
        seen_targets: set[str] = set()
        for link in p["links"]:
            target = link_target(link)
            if not target or target == here:
                continue
            target_id = page_node_ids.get(target)
            if not target_id or target_id == subject_id or target_id in seen_targets:
                continue
            seen_targets.add(target_id)
            push(make_edge(
                subject=subject_id,
                predicate="mentions",
                obj=target_id,
                source=None,
                evidence=None,
                confidence="low",
                status="current",
                extraction_method="body_wikilink",
                page=p["rel_path"],
                page_id=here,
            ))

        # 3. frontmatter の `sources:` から作る sourced_from エッジ（ソースページ自身ではスキップする）。
        # 値は、lint の unresolved_sources と同じく、スラッグ、パス、[[...]] のどれで書いてもよい。
        # 曖昧と未解決の診断は、ソースページでも挙げる。エッジを作らないのは、ソースページ自身の sources: だけである。
        # 分け方は lint の unresolved_sources と同じ wiki_pages.classify_source_ref に任せる。lint が source でない
        # ページを指すと挙げる値（not_source）も、ページが 1 つなら sourced_from を作る（lint がそれを挙げる）。
        for ref in ref_values(meta.get("sources")):
            _status, found, _wrong_type = classify_source_ref(ref, pages_by_path, pages_by_slug, type_by_key.get)
            if len(found) != 1:
                note("ambiguous_source" if found else "unresolved_source", p, source=ref, pages=sorted(found))
                continue
            src_id = page_node_ids.get(found[0])
            if meta.get("type") != "source" and src_id:
                push(make_edge(
                    subject=subject_id,
                    predicate="sourced_from",
                    obj=src_id,
                    source=ref,
                    evidence=None,
                    confidence="high",
                    status="current",
                    extraction_method="frontmatter_sources",
                    page=p["rel_path"],
                    page_id=here,
                ))

        # 4. ソースページの raw: フィールドから作る summarizes_raw エッジ。lint の missing_raw と同じく、
        # 配列で書いた raw: は値ごとに読む。
        if meta.get("type") == "source":
            for raw_path in ref_values(meta.get("raw")):
                push(make_edge(
                    subject=subject_id,
                    predicate="summarizes_raw",
                    obj=f"raw:{raw_path}",
                    source=None,
                    evidence=None,
                    confidence="high",
                    status="current",
                    extraction_method="frontmatter_raw",
                    page=p["rel_path"],
                    page_id=here,
                ))

    return edges


# ---------------------------------------------------------------------------
# 出力の書き込み
# ---------------------------------------------------------------------------


def _json_key(key) -> str:
    """辞書のキーを、json.dumps と同じ規則で文字列にする（True は "true"、None は "null"、2 は "2"）。"""
    if isinstance(key, str):
        return key
    if key is True or key is False or key is None or isinstance(key, (int, float)):
        return json.dumps(key)
    return str(key)


def _normalize_for_json(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_for_json(v) for v in value]
    if isinstance(value, dict):
        # キーは json.dumps と同じ規則で文字列にそろえる。数値と文字列のキーが混ざると、sort_keys で比べられずに
        # 止まるからである。文字列のキーとぶつかる数値などのキー（2 と "2"）には、型の名前を付けて値を消さない。
        text_keys = {k for k in value if isinstance(k, str)}
        out = {}
        for k, v in value.items():
            name = _json_key(k)
            if not isinstance(k, str) and name in text_keys:
                name = f"{name}#{type(k).__name__}"
            out[name] = _normalize_for_json(v)
        return out
    return value


OUTPUT_NAMES = ["nodes.jsonl", "edges.jsonl", "graph.sqlite", "graph.graphml"]
# 置き換える順序。読み手が開いていて置き換えに失敗しやすい graph.sqlite を最初にする。
# graph.sqlite の置き換えに失敗したときは、ほかの成果物も前のまま残る。
PUBLISH_ORDER = ["graph.sqlite", "nodes.jsonl", "edges.jsonl", "graph.graphml"]


def _write_lines(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(_normalize_for_json(row), sort_keys=True, ensure_ascii=False))
            f.write("\n")


def stage_outputs(out_dir: Path, formats: list[str], nodes: list[dict], aliases: list[dict],
                  edges: list[dict]) -> dict[str, Path]:
    """選んだ形式の成果物を、すべて一時ファイルに書く。{成果物の名前: 一時ファイル} を返す。

    途中で失敗したら、書いた一時ファイルを消して例外を送出する。元の成果物には触れない。
    """
    staged: dict[str, Path] = {}

    def temp(name: str) -> Path:
        path = common.temp_path_for(out_dir / name)
        common.remove_temp(path)
        staged[name] = path
        return path

    try:
        if "sqlite" in formats:
            build_sqlite(temp("graph.sqlite"), nodes, aliases, edges)
        if "jsonl" in formats:
            _write_lines(temp("nodes.jsonl"), sorted(nodes, key=lambda n: n["id"]))
            _write_lines(temp("edges.jsonl"), sorted(edges, key=lambda e: e["id"]))
        if "graphml" in formats:
            write_graphml_file(temp("graph.graphml"), nodes, edges)
    except BaseException:
        for path in staged.values():
            common.remove_temp(path)
        raise
    return staged


def publish_outputs(out_dir: Path, staged: dict[str, Path]) -> None:
    """一時ファイルを PUBLISH_ORDER の順に置き換える。失敗したら残りの一時ファイルを消して例外を送出する。"""
    names = [name for name in PUBLISH_ORDER if name in staged]
    for index, name in enumerate(names):
        try:
            common.replace_atomically(staged[name], out_dir / name)
        except BaseException:
            for rest in names[index:]:
                common.remove_temp(staged[rest])
            raise


def write_outputs(out_dir: Path, formats: list[str], nodes: list[dict], aliases: list[dict],
                  edges: list[dict]) -> None:
    """成果物を作り直す。すべて書き終えてから置き換えるので、書いている途中で止まっても前の成果物が残る。"""
    publish_outputs(out_dir, stage_outputs(out_dir, formats, nodes, aliases, edges))


def write_jsonl(out_dir: Path, nodes: list[dict], edges: list[dict]) -> None:
    write_outputs(out_dir, ["jsonl"], nodes, [], edges)


def write_sqlite(out_dir: Path, nodes: list[dict], aliases: list[dict], edges: list[dict]) -> None:
    """graph.sqlite だけを作り直す。一時ファイルに作り、接続を閉じてから置き換える。"""
    write_outputs(out_dir, ["sqlite"], nodes, aliases, edges)


def build_sqlite(db_path: Path, nodes: list[dict], aliases: list[dict], edges: list[dict]) -> None:
    """db_path に新しいグラフの DB を作る。db_path にはまだファイルがないこと。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE nodes (
              id TEXT PRIMARY KEY,
              slug TEXT NOT NULL,
              title TEXT NOT NULL,
              page_type TEXT NOT NULL,
              node_type TEXT NOT NULL,
              kind TEXT,
              path TEXT NOT NULL,
              created TEXT,
              updated TEXT,
              metadata_json TEXT NOT NULL
            );
            CREATE TABLE aliases (
              alias TEXT NOT NULL,
              node_id TEXT NOT NULL,
              PRIMARY KEY (alias, node_id),
              FOREIGN KEY (node_id) REFERENCES nodes(id)
            );
            CREATE TABLE edges (
              id TEXT PRIMARY KEY,
              subject TEXT NOT NULL,
              predicate TEXT NOT NULL,
              object TEXT NOT NULL,
              source TEXT,
              evidence TEXT,
              confidence TEXT,
              status TEXT,
              extraction_method TEXT NOT NULL,
              page TEXT NOT NULL,
              metadata_json TEXT NOT NULL
            );
            CREATE INDEX idx_edges_subject ON edges(subject);
            CREATE INDEX idx_edges_object ON edges(object);
            CREATE INDEX idx_edges_predicate ON edges(predicate);
            CREATE INDEX idx_edges_source ON edges(source);
        """)

        for n in sorted(nodes, key=lambda n: n["id"]):
            metadata_json = json.dumps(_normalize_for_json({
                "tags": n.get("tags", []),
                "aliases": n.get("aliases", []),
                "canonical": n.get("canonical", False),
            }), sort_keys=True, ensure_ascii=False)
            conn.execute(
                "INSERT INTO nodes (id, slug, title, page_type, node_type, kind, path, created, updated, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    n["id"], n["slug"], n["title"], n["page_type"], n["node_type"],
                    n.get("kind") or None, n["path"],
                    str(n.get("created") or "") or None,
                    str(n.get("updated") or "") or None,
                    metadata_json,
                ),
            )

        for a in sorted(aliases, key=lambda a: (a["alias"], a["node_id"])):
            conn.execute(
                "INSERT OR IGNORE INTO aliases (alias, node_id) VALUES (?, ?)",
                (a["alias"], a["node_id"]),
            )

        for e in sorted(edges, key=lambda e: e["id"]):
            metadata_json = json.dumps(_normalize_for_json(e.get("extras") or {}),
                                       sort_keys=True, ensure_ascii=False)
            conn.execute(
                "INSERT INTO edges (id, subject, predicate, object, source, evidence, "
                "confidence, status, extraction_method, page, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e["id"], e["subject"], e["predicate"], e["object"],
                    e.get("source") or None, e.get("evidence") or None,
                    e.get("confidence") or None, e.get("status") or None,
                    e["extraction_method"], e["page"], metadata_json,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def write_graphml_file(path: Path, nodes: list[dict], edges: list[dict]) -> None:
    """GraphML を path に書く。置き換えは呼び出す側が行う。"""
    ns ="http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}graphml")

    keys = [
        ("d_title", "node", "title", "string"),
        ("d_node_type", "node", "node_type", "string"),
        ("d_page_type", "node", "page_type", "string"),
        ("d_path", "node", "path", "string"),
        ("d_predicate", "edge", "predicate", "string"),
        ("d_confidence", "edge", "confidence", "string"),
        ("d_status", "edge", "status", "string"),
        ("d_source", "edge", "source", "string"),
        ("d_page", "edge", "page", "string"),
    ]
    for kid, kfor, kname, ktype in keys:
        k = ET.SubElement(root, f"{{{ns}}}key")
        k.set("id", kid)
        k.set("for", kfor)
        k.set("attr.name", kname)
        k.set("attr.type", ktype)

    graph = ET.SubElement(root, f"{{{ns}}}graph")
    graph.set("id", "wiki")
    graph.set("edgedefault", "directed")

    for n in sorted(nodes, key=lambda n: n["id"]):
        node_el = ET.SubElement(graph, f"{{{ns}}}node")
        node_el.set("id", n["id"])
        for kid, kfor, kname, _ in keys:
            if kfor != "node":
                continue
            data = ET.SubElement(node_el, f"{{{ns}}}data")
            data.set("key", kid)
            data.text = str(n.get(kname) or "")

    for e in sorted(edges, key=lambda e: e["id"]):
        edge_el = ET.SubElement(graph, f"{{{ns}}}edge")
        edge_el.set("id", e["id"])
        edge_el.set("source", e["subject"])
        edge_el.set("target", e["object"])
        for kid, kfor, kname, _ in keys:
            if kfor != "edge":
                continue
            data = ET.SubElement(edge_el, f"{{{ns}}}data")
            data.set("key", kid)
            data.text = str(e.get(kname) or "")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_graphml(out_dir: Path, nodes: list[dict], edges: list[dict]) -> None:
    write_outputs(out_dir, ["graphml"], nodes, [], edges)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki", type=Path, help="wiki のディレクトリ")
    parser.add_argument("--out", type=Path, help="出力ディレクトリ（既定: <wiki>/graph）")
    parser.add_argument("--formats", default=",".join(DEFAULT_FORMATS),
                        help="出力する形式。jsonl、sqlite、graphml をカンマで区切る（既定: 3 つすべて）")
    parser.add_argument("--ontology", type=Path, help="オントロジーのファイル（既定: <wiki>/graph/ontology.yaml）")
    localize_argparse(parser)
    args = parser.parse_args()

    if not args.wiki.exists():
        print(f"wiki のディレクトリが見つからない: {args.wiki}", file=sys.stderr)
        sys.exit(1)

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    unknown = [f for f in formats if f not in DEFAULT_FORMATS]
    if unknown:
        print(f"未知の形式: {', '.join(unknown)}。使える形式: {', '.join(DEFAULT_FORMATS)}", file=sys.stderr)
        sys.exit(1)

    # オントロジーがなければ、graph/ も生成物も作らずに止まる（lint、query と同じ扱い）。
    try:
        ontology_path = common.require_ontology(args.wiki, args.ontology)
    except common.GraphLayerDisabled as exc:
        common.report_disabled(exc)
        sys.exit(common.EXIT_GRAPH_DISABLED)
    ontology = load_ontology(ontology_path)

    out_dir = args.out or (args.wiki / "graph")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 同じ出力先の extract は 1 つずつ動かす。ロックを取ってからページを読むので、
    # 後から動いた extract の成果物が、先の extract の成果物と混ざらない。
    try:
        with common.extract_lock(out_dir):
            common.remove_stale_temps(out_dir, OUTPUT_NAMES)
            pages = collect_pages(args.wiki)
            diagnostics: list[dict] = []
            nodes, page_node_ids, aliases = build_nodes(pages, ontology, diagnostics)
            edges = build_edges(pages, page_node_ids, diagnostics)
            write_outputs(out_dir, formats, nodes, aliases, edges)
    except common.ExtractBusy as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except PermissionError as exc:
        print(f"成果物を置き換えられなかった（ほかのプロセスが開いている可能性がある）: {exc}", file=sys.stderr)
        print("前の成果物はそのまま残っている。開いているプロセスを閉じてから、もう一度実行する。", file=sys.stderr)
        sys.exit(1)

    print(f"ノード {len(nodes)} 個、エッジ {len(edges)} 本を抽出した → {out_dir}")
    breakdown = defaultdict(int)
    for e in edges:
        breakdown[e["predicate"]] += 1
    for pred, count in sorted(breakdown.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {ljust_display(pred, 20)} {count}")
    if diagnostics:
        # 黙って捨てない。エッジにしなかった参照と、重なった node_id を知らせる。
        kinds = defaultdict(int)
        for d in diagnostics:
            kinds[d["kind"]] += 1
        summary = "、".join(f"{DIAGNOSTIC_LABELS.get(k, k)} {n} 件" for k, n in sorted(kinds.items()))
        print(f"診断: {summary}。wiki_graph_lint.py で確かめる。", file=sys.stderr)


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
