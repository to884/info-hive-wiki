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
from wiki_text import canonical_meta, ljust_display, localize_argparse, nfc, posix_rel  # noqa: E402
import wiki_graph_common as common  # noqa: E402


# ページの集合とリンクの規則は wiki_pages の 1 か所に置く（ADR-0010 の決定の 1）。
from wiki_pages import (  # noqa: E402
    SKIP_TOP_LEVEL_DIRS,
    SKIP_TOP_LEVEL_FILES,
    WIKILINK_RE,
    clean_link_target,
    extract_wikilinks,
    iter_page_files,
    link_lookup,
)
from wiki_pages import resolve_link as resolve_link_slug  # noqa: E402

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

DEFAULT_FORMATS = ["jsonl", "sqlite", "graphml"]


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
        for key in ("node_id", "aliases"):
            if key in graph_meta:
                graph_meta[key] = _nfc_value(graph_meta[key])
        rels = graph_meta.get("relationships")
        if isinstance(rels, list):
            new_rels = []
            for rel in rels:
                if isinstance(rel, dict):
                    rel = dict(rel)
                    for key in ("object", "source", "contradicts", "supersedes"):
                        if key in rel:
                            rel[key] = _nfc_value(rel[key])
                new_rels.append(rel)
            graph_meta["relationships"] = new_rels
        out["graph"] = graph_meta
    return out


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """PyYAML で YAML の frontmatter を取り出す。(meta, body) を返す。

    トップレベルのキーと type・kind の値の日本語の別名は英語に直す（`graph:` の中のキーは
    直さない）。ノードやページを指す値は NFC にそろえる。
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():]
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta, _conflicts = canonical_meta(meta)
    return normalize_refs(meta), body


def collect_pages(wiki_root: Path) -> list[dict]:
    pages = []
    for md_path in iter_page_files(wiki_root):
        rel = md_path.relative_to(wiki_root)
        try:
            # BOM 付きの UTF-8 でも frontmatter を読めるように utf-8-sig で読む。
            text = md_path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        meta, body = parse_frontmatter(text)
        # コードフェンスとインラインのコードの中の [[...]] からは mentions のエッジを作らない。
        links = extract_wikilinks(body)
        pages.append({
            "path": str(md_path),
            # スラッグとパスは NFC にそろえる。lint が重複を報告するときのために、
            # ディスク上の実際のパスも disk_rel_path に残しておく。
            "rel_path": posix_rel(str(rel)),
            "disk_rel_path": str(rel).replace("\\", "/"),
            "slug": nfc(md_path.stem),
            "meta": meta,
            "body": body,
            "links": links,
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
    extract と lint は、この関数で同じように読む。
    """
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        raise OntologyError(f"オントロジーを読めない（{path}）: {e}") from e
    if data is None:  # 空のファイルは、空のオントロジーとして扱う
        data = {}
    # []、false、0 などを空のマッピングとして通さない。
    if not isinstance(data, dict):
        raise OntologyError(f"オントロジーの最上位がマッピングでない（{path}）")
    for key in ("node_types", "predicates"):
        if data.get(key) is None:
            data[key] = {}
    return data


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


def page_key(p: dict) -> str:
    """ページを 1 つに決めるキー。ディスク上の相対パスを使う。

    スラッグは別のディレクトリのページと重なり、NFC にそろえた rel_path も、NFC と NFD の名前の違いだけの
    2 つのファイルで重なるので、どちらもページを 1 つに決めない。
    """
    return p.get("disk_rel_path") or p["rel_path"]


def build_nodes(pages: list[dict], ontology: dict) -> tuple[list[dict], dict, list[dict]]:
    """ノードのリスト、ページから node_id を引くインデックス、別名の行を作る。(nodes, page_node_ids, aliases) を返す。

    page_node_ids のキーは page_key(p) である。node_id が前のページと重なってノードにならなかったページは
    含まない。スラッグでは引かない。別のディレクトリに同じスラッグのページがあると、別のページのノードを
    引いてしまうからである。
    """
    nodes: list[dict] = []
    page_node_ids: dict[str, str] = {}
    aliases: list[dict] = []
    seen_ids: set[str] = set()

    for p in pages:
        meta = p["meta"]
        graph_meta = meta.get("graph") if isinstance(meta.get("graph"), dict) else {}
        node_type = derive_node_type(meta, ontology) or "concept"
        explicit_id = graph_meta.get("node_id")
        node_id = explicit_id or f"{node_type}:{p['slug']}"

        # 重複はスキップする。最初のものを使い、lint がこれを指摘する。
        if node_id in seen_ids:
            continue
        seen_ids.add(node_id)

        node = {
            "id": node_id,
            "slug": p["slug"],
            "title": meta.get("title") or p["slug"],
            "page_type": meta.get("type") or "",
            "node_type": node_type,
            "kind": meta.get("kind") or "",
            "tags": list(meta.get("tags") or []),
            "aliases": list(graph_meta.get("aliases") or []),
            "path": p["rel_path"],
            "created": meta.get("created") or "",
            "updated": meta.get("updated") or "",
            "canonical": bool(graph_meta.get("canonical", False)),
        }
        nodes.append(node)
        page_node_ids[page_key(p)] = node_id
        for alias in node["aliases"]:
            aliases.append({"alias": str(alias), "node_id": node_id})

    return nodes, page_node_ids, aliases


def edge_id(subject: str, predicate: str, obj: str, source: str | None, evidence: str | None) -> str:
    # 96 ビットに切り詰める。現実的な wiki の規模なら衝突のおそれは無視でき、
    # ID が短いほうが JSONL、sqlite、graphml の出力が読みやすい。
    h = hashlib.sha256()
    parts = [subject or "", predicate or "", obj or "", source or "", evidence or ""]
    h.update("\x1f".join(parts).encode("utf-8"))
    return h.hexdigest()[:24]


def make_edge(*, subject, predicate, obj, source, evidence, confidence, status,
              extraction_method, page, extras: dict | None = None) -> dict:
    return {
        "id": edge_id(subject, predicate, obj, source, evidence),
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


def build_edges(pages: list[dict], page_node_ids: dict[str, str]) -> list[dict]:
    """エッジを作る。page_node_ids は build_nodes が返す、page_key(p) から node_id を引くインデックス。

    主語は、そのページ自身のノードである。リンクと `sources:` の行き先は、ページを 1 つに決められるときだけ
    エッジにする。パスのない形が、同じスラッグの複数のページに当たるときは、どれにも寄せない（lint の
    duplicate_slugs がその重なりを挙げる）。
    """
    edges: list[dict] = []
    seen_ids: set[str] = set()

    # 行き先のページを引くインデックス。パスは .md を除いた wiki からの相対パス（NFC、大文字と小文字を区別）、
    # スラッグはファイル名の stem である。どちらも、当たるページのキーをすべて並べる。
    pages_by_path: dict[str, list[str]] = defaultdict(list)
    pages_by_slug: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        rel = p["rel_path"].replace("\\", "/")
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        pages_by_path[rel].append(page_key(p))
        pages_by_slug[p["slug"]].append(page_key(p))

    def only(keys: list[str] | None) -> str | None:
        """ページがちょうど 1 つのときだけ、そのキーを返す。"""
        return keys[0] if keys and len(keys) == 1 else None

    def link_target(link: str) -> str | None:
        lookup = link_lookup(link)
        if lookup is None:
            return None
        kind, target = lookup
        return only((pages_by_path if kind == "path" else pages_by_slug).get(target))

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

        # 1. graph.relationships[] から作る型付きの意味のエッジ。
        for rel in graph_meta.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            obj = rel.get("object")
            predicate = rel.get("predicate")
            if not (obj and predicate):
                continue
            extras = {
                k: rel[k] for k in ("valid_from", "valid_to", "notes", "raw_ref",
                                    "contradicts", "supersedes")
                if k in rel and rel[k] is not None
            }
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
                extras=extras,
            ))

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
            ))

        # 3. frontmatter の `sources:` から作る sourced_from エッジ（ソースページ自身ではスキップする）。
        if meta.get("type") != "source":
            for src_slug in meta.get("sources") or []:
                src_key = only(pages_by_slug.get(str(src_slug)))
                src_id = page_node_ids.get(src_key) if src_key else None
                if not src_id:
                    continue
                push(make_edge(
                    subject=subject_id,
                    predicate="sourced_from",
                    obj=src_id,
                    source=str(src_slug),
                    evidence=None,
                    confidence="high",
                    status="current",
                    extraction_method="frontmatter_sources",
                    page=p["rel_path"],
                ))

        # 4. ソースページの raw: フィールドから作る summarizes_raw エッジ。
        if meta.get("type") == "source":
            raw_path = meta.get("raw")
            if raw_path:
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
                ))

    return edges


# ---------------------------------------------------------------------------
# 出力の書き込み
# ---------------------------------------------------------------------------


def _normalize_for_json(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_for_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_for_json(v) for k, v in value.items()}
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
            nodes, page_node_ids, aliases = build_nodes(pages, ontology)
            edges = build_edges(pages, page_node_ids)
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


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
