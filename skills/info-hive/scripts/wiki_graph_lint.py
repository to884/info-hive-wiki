#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml==6.0.3",
# ]
# ///
"""
wiki_graph_lint.py — wiki の型付きグラフのメタデータを検証する。

すべてのページの `graph:` の frontmatter を読み、オントロジー（`wiki/graph/ontology.yaml`）と
照らし合わせ、問題を報告する。保守的な設計で、報告するだけで編集はしない。

PyYAML の固定した依存を持つので、`uv run --script` で実行する。

オントロジーがないときは、グラフ層を導入する前の wiki として扱い、終了コード 1 で止まる
（extract、query と同じ）。手順の正本は references/graph-workflow.md にある。

チェック項目:
- `graph.node_id` の値がページ全体で一意である。
- 関係の `object` の ID がすべて既知のノードに解決される（または、object_types に "*" を
  含む述語について、許可された文字列リテラルの対象である）。
- すべての述語が `graph/ontology.yaml` にある。
- 述語の主語と目的語の型がオントロジーと一致する。
- 型付きの意味のエッジ（mentions、sourced_from、summarizes_raw と、
  `requires_evidence: false` の述語を除くすべて）が `source` と `evidence` を持つ。
- `source` の参照が既存のソースページに解決される。
- `confidence` が high|medium|low のいずれか、`status` が
  current|historical|proposed|disputed|superseded のいずれかである。
- 同じノード ID について、正本のノードが重複していない。
- 別名が、異なる正本のノードの間で衝突していない。
- `contradicts` と `supersedes` の参照が、既知のノード ID、ソースページのスラッグ、抽出したエッジの ID の
  どれかに解決される。
- frontmatter の `graph:` を読めなかったページ（YAML として読めない、マッピングでない）と、lint のパーサーが
  読めない書き方の最上位の欄（`type`、`kind`、`sources`、`raw`）がない。
- 生成したグラフに、孤立した型付きノード（型付きのエッジが入ってくることも出ていくこともないノード）がない。
  ただし `source` のノードは除く（ソースとしてだけ使われることを許可する）。
- 任意（--decide）: 証拠の引用がソースページにない型付きのエッジを、判定モデルがチェックする。
  検出結果を報告するだけである。

使い方:
    uv run --script wiki_graph_lint.py [<wiki-dir>] [--json] [--decide [--decide-threshold X]]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "wiki_graph_lint.py には PyYAML が必要である。"
        "`uv run --script wiki_graph_lint.py ...` で実行する。",
        file=sys.stderr,
    )
    sys.exit(2)

# extract と同じモジュールを import し、その build_nodes と build_edges を再利用して、
# extract が出力するものとまったく同じものを lint が見るようにする。
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import wiki_graph_extract as _extract  # noqa: E402
import wiki_graph_common as common  # noqa: E402
from wiki_pages import kind_value, text_value, type_value  # noqa: E402
from wiki_text import localize_argparse, nfc  # noqa: E402
import wiki_initialized  # noqa: E402
import wiki_workspace  # noqa: E402


ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_STATUS = {"current", "historical", "proposed", "disputed", "superseded"}
IMPLICIT_PREDICATES = {"mentions", "sourced_from", "summarizes_raw"}
# unreadable_graph の reason の説明。
UNREADABLE_REASONS = {
    "yaml_error": "frontmatter か graph: の値を YAML として読めない",
    "not_mapping": "graph: の値がマッピングでない",
}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """extract と同じ規則で frontmatter を読む。最上位の欄は wiki の lint と同じ値で、graph だけを PyYAML から取る。"""
    return _extract.parse_frontmatter(text)


def collect_pages(wiki_root: Path) -> list[dict]:
    """extract と同じ規則でページを集める。スラッグ、パス、参照は NFC にそろっている。

    extract と lint が別々にページを読むと、正規化の規則がずれて結果が食い違うので、
    extract の実装をそのまま使う。
    """
    return _extract.collect_pages(wiki_root)


def derive_node_type(meta: dict, ontology: dict) -> str | None:
    page_type = type_value(meta.get("type"))
    page_kind = kind_value(meta.get("kind"))
    explicit = (meta.get("graph") or {}).get("node_type") if isinstance(meta.get("graph"), dict) else None
    if explicit:
        return explicit
    type_kind_match = None
    type_only_match = None
    for nt_name, nt_def in ontology.get("node_types", {}).items():
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


def derive_node_id(meta: dict, slug: str, ontology: dict) -> str:
    """extract の page_node_id と同じ。extract と lint で、ページのノード ID の規則を食い違わせない。"""
    return _extract.page_node_id(meta, slug, ontology)


def types_match(allowed: list[str] | None, actual: str | None) -> bool:
    if not allowed:
        return True
    if "*" in allowed:
        return True
    return actual in allowed


def lint(pages: list[dict], ontology: dict) -> dict:
    findings = {
        "duplicate_node_ids": [],
        "unknown_predicates": [],
        "broken_object_refs": [],
        "subject_type_mismatch": [],
        "object_type_mismatch": [],
        "missing_evidence": [],
        "missing_source_field": [],
        "broken_source_refs": [],
        "invalid_confidence": [],
        "invalid_status": [],
        "duplicate_canonical": [],
        "alias_collisions": [],
        "broken_contradicts": [],
        "broken_supersedes": [],
        "orphan_typed_nodes": [],
        "ambiguous_object_refs": [],
        "ambiguous_sources": [],
        "unresolved_sources": [],
        "order_dependent_edge_ids": [],
        "unreadable_graph": [],
        "unread_fields": [],
        "summary": {},
    }

    predicates = ontology.get("predicates", {})
    node_types = ontology.get("node_types", {})

    # ノードのインデックスを作る
    node_by_id: dict[str, dict] = {}
    duplicates: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        nid = derive_node_id(p["meta"], p["slug"], ontology)
        if nid in node_by_id:
            # 報告にはディスク上の実際のパスを使う。NFC にそろえたパスでは、NFD と NFC の
            # ファイル名の違いだけで重複した2つのページが、同じ1つのパスに見えてしまう。
            duplicates[nid].append(p.get("disk_rel_path", p["rel_path"]))
            duplicates[nid].append(node_by_id[nid]["disk_rel_path"])
            continue
        node_type = derive_node_type(p["meta"], ontology) or "concept"
        graph_meta = p["meta"].get("graph") if isinstance(p["meta"].get("graph"), dict) else {}
        node_by_id[nid] = {
            "id": nid,
            "node_type": node_type,
            "rel_path": p["rel_path"],
            "disk_rel_path": p.get("disk_rel_path", p["rel_path"]),
            "slug": p["slug"],
            "page_type": type_value(p["meta"].get("type")),
            "canonical": bool(graph_meta.get("canonical", False)),
            "aliases": list(graph_meta.get("aliases") or []),
            "graph": graph_meta,
        }

    for nid, paths in duplicates.items():
        unique_paths = sorted(set(paths))
        finding = {"node_id": nid, "paths": unique_paths}
        # NFC にそろえると同じになるパスがあれば、正規化の違いによる重複だと分かるように印を付ける。
        if len({nfc(path) for path in unique_paths}) < len(unique_paths):
            finding["unicode_normalization"] = True
        findings["duplicate_node_ids"].append(finding)

    # スラッグをキーにしたソースページ。エッジの `source:` の参照を検証するのに使う。
    source_slugs = {p["slug"] for p in pages if type_value(p["meta"].get("type")) == "source"}

    # 別名
    alias_to_canonicals: dict[str, set[str]] = defaultdict(set)
    canonical_by_id: dict[str, list[str]] = defaultdict(list)
    for n in node_by_id.values():
        if n["canonical"]:
            canonical_by_id[n["id"]].append(n["rel_path"])
        for alias in n["aliases"]:
            alias_to_canonicals[str(alias)].add(n["id"])

    for nid, paths in canonical_by_id.items():
        if len(paths) > 1:
            findings["duplicate_canonical"].append({"node_id": nid, "paths": paths})

    for alias, owners in alias_to_canonicals.items():
        if len(owners) > 1:
            findings["alias_collisions"].append({"alias": alias, "owners": sorted(owners)})

    # extract と同じ build_nodes と build_edges でエッジを作る。主語はページで引く（スラッグで引くと、別の
    # ディレクトリの同じスラッグのページのノードになる）。エッジにしなかった参照は、下で診断として挙げる。
    # contradicts と supersedes は別のページのエッジの ID も指すので、関係を調べる前に、すべてのエッジを作っておく。
    diagnostics: list[dict] = []
    _nodes, page_node_ids, _aliases = _extract.build_nodes(pages, ontology, diagnostics)
    extracted_edges = _extract.build_edges(pages, page_node_ids, diagnostics)
    # 参照先として認めるエッジの ID。extract が edges.jsonl に書くエッジと同じものである。
    # 16 進 24 桁の形だけでは認めない。形が合っても、実在しないエッジの ID は挙げる。
    edge_ids = {e["id"] for e in extracted_edges}

    # 関係を順に調べる
    for p in pages:
        graph_meta = p["meta"].get("graph") if isinstance(p["meta"].get("graph"), dict) else {}
        subject_id = derive_node_id(p["meta"], p["slug"], ontology)
        subject_type = node_by_id.get(subject_id, {}).get("node_type")

        for idx, rel in enumerate(graph_meta.get("relationships") or []):
            if not isinstance(rel, dict):
                continue
            predicate = rel.get("predicate")
            obj = rel.get("object")
            here = {"page": p["rel_path"], "predicate": predicate,
                    "object": obj, "index": idx}

            if not predicate or predicate not in predicates:
                findings["unknown_predicates"].append({**here})
                continue
            pdef = predicates[predicate] or {}

            # 目的語の解決。文字列リテラルの目的語は、オントロジーの object_types に
            # "*" がある場合だけ許可する（例: summarizes_raw）。
            object_types = pdef.get("object_types") or []
            allows_wildcard_obj = "*" in object_types
            # 抽出と同じく、目的語は文字列にしてノード ID と比べる（object: 1 は "1" のノードを指す）。
            obj_key = str(obj) if obj else None
            if obj_key and obj_key not in node_by_id:
                if not allows_wildcard_obj:
                    findings["broken_object_refs"].append({**here})

            # 主語の型のチェック
            if not types_match(pdef.get("subject_types"), subject_type):
                findings["subject_type_mismatch"].append({
                    **here,
                    "subject": subject_id,
                    "subject_type": subject_type,
                    "allowed": pdef.get("subject_types"),
                })
            # 目的語の型のチェック（目的語がノードに解決される場合だけ）
            obj_node = node_by_id.get(obj_key) if obj_key else None
            obj_type = obj_node["node_type"] if obj_node else None
            if obj_node and not types_match(pdef.get("object_types"), obj_type):
                findings["object_type_mismatch"].append({
                    **here,
                    "object_type": obj_type,
                    "allowed": pdef.get("object_types"),
                })

            requires_evidence = pdef.get("requires_evidence", True)
            if requires_evidence:
                if not rel.get("evidence"):
                    findings["missing_evidence"].append({**here})
                if not rel.get("source"):
                    findings["missing_source_field"].append({**here})

            # source フィールドは、既存のソースページのスラッグを参照していなければならない
            src = rel.get("source")
            if src and str(src) not in source_slugs:
                findings["broken_source_refs"].append({**here, "source": src})

            confidence = rel.get("confidence")
            if confidence and confidence not in ALLOWED_CONFIDENCE:
                findings["invalid_confidence"].append({**here, "confidence": confidence})

            status = rel.get("status")
            if status and status not in ALLOWED_STATUS:
                findings["invalid_status"].append({**here, "status": status})

            # contradicts と supersedes の解決。ノード ID、ソースのスラッグ、エッジの ID のどれかに当たればよい。
            for ref_field, bucket in (("contradicts", "broken_contradicts"),
                                       ("supersedes", "broken_supersedes")):
                ref = rel.get(ref_field)
                if ref:
                    ref_str = str(ref)
                    if ref_str not in node_by_id and ref_str not in source_slugs and ref_str not in edge_ids:
                        findings[bucket].append({**here, ref_field: ref_str})

    # 孤立した型付きノード。`graph:` の frontmatter を宣言しているのに、
    # 抽出後に、型付きの（暗黙でない）エッジが1本もつながっていないページ。
    # ソースのノードは対象外である（暗黙のエッジでつながるため）。
    for d in diagnostics:
        # duplicate_node_id は、上で duplicate_node_ids に挙げている。
        bucket = {"ambiguous_object": "ambiguous_object_refs", "ambiguous_source": "ambiguous_sources",
                  "unresolved_source": "unresolved_sources",
                  "order_dependent_edge_ids": "order_dependent_edge_ids",
                  "unreadable_graph": "unreadable_graph", "unread_field": "unread_fields"}.get(d["kind"])
        if bucket:
            findings[bucket].append({k: v for k, v in d.items() if k != "kind"})
    typed_node_refs: set[str] = set()
    for e in extracted_edges:
        if e["predicate"] in IMPLICIT_PREDICATES:
            continue
        typed_node_refs.add(e["subject"])
        if e["object"] in node_by_id:
            typed_node_refs.add(e["object"])

    for n in node_by_id.values():
        if n["node_type"] == "source":
            continue
        graph_meta = n.get("graph") or {}
        if not graph_meta:
            continue  # グラフのメタデータがないページも有効で、テキストだけのノードになる。
        if n["id"] in typed_node_refs:
            continue
        findings["orphan_typed_nodes"].append({
            "node_id": n["id"],
            "path": n["rel_path"],
        })

    # 集計
    findings["summary"] = {
        "pages_scanned": len(pages),
        "nodes": len(node_by_id),
        **{k: len(v) for k, v in findings.items() if isinstance(v, list)},
    }
    return findings


def decide_evidence(pages: list[dict], ontology: dict, threshold: float | None) -> tuple[list[dict], dict]:
    """D2: 証拠がソースページからの引用になっていない型付きのエッジについて、
    そのエッジを述べているソースの文がどれかを判定モデルに聞く。(裏づけのない検出結果, ブロック) を返す。"""
    import wiki_decide as wd
    from concurrent.futures import ThreadPoolExecutor
    import time

    if threshold is None:
        threshold = wd.QUESTIONS["evidence"]["default_threshold"]
    started = time.monotonic()
    try:
        client = wd.client_from_config()
    except wd.DecisionError as exc:
        wd.note(str(exc))
        client = None
    predicates = ontology.get("predicates", {})
    sources = {p["slug"]: p for p in pages if type_value(p["meta"].get("type")) == "source"}
    titles = {derive_node_id(p["meta"], p["slug"], ontology): text_value(p["meta"].get("title")) or p["slug"]
              for p in pages}
    items = []
    for p in pages:
        graph_meta = p["meta"].get("graph") if isinstance(p["meta"].get("graph"), dict) else {}
        subject_id = derive_node_id(p["meta"], p["slug"], ontology)
        for idx, rel in enumerate(graph_meta.get("relationships") or []):
            if not isinstance(rel, dict):
                continue
            predicate, evidence, src = rel.get("predicate"), rel.get("evidence"), rel.get("source")
            if not predicate or predicate in IMPLICIT_PREDICATES or predicate not in predicates:
                continue
            if not (predicates[predicate] or {}).get("requires_evidence", True):
                continue
            source_page = sources.get(str(src)) if src else None
            if not evidence or source_page is None or wd.evidence_in_text(str(evidence), source_page["body"]):
                continue
            obj = rel.get("object")
            items.append({
                "page": p["rel_path"],
                "predicate": predicate,
                "object": obj,
                "index": idx,
                "source": str(src),
                "evidence": str(evidence),
                "claim": f"{titles.get(subject_id, subject_id)} {predicate.replace('_', ' ')} {titles.get(obj, obj)}",
                "body": source_page["body"],
            })

    def check(item):
        try:
            return wd.check_evidence(client, item["claim"], item["evidence"], item["body"], threshold), None
        except wd.DecisionError as exc:
            return None, exc

    findings, errors = [], []
    if items and client is not None:
        with ThreadPoolExecutor(max_workers=min(wd.MAX_WORKERS, len(items))) as pool:
            results = list(pool.map(check, items))
        for item, (verdict, exc) in zip(items, results):
            if exc is not None:
                errors.append(exc.code)
                continue
            if not verdict["supported"]:
                findings.append({
                    **{k: item[k] for k in ("page", "predicate", "object", "index", "source", "evidence")},
                    "choice": verdict["choice"],
                    "confidence": verdict["confidence"],
                    "best_sentence": verdict["sentence"],
                })
    failed_open = bool(items) and (client is None or len(errors) == len(items))
    if errors:
        wd.note(f"証拠のチェックが {len(items)} 本のエッジのうち {len(errors)} 本で失敗した（{errors[0]}）。失敗したエッジは報告しない")
    elif items and client is None:
        wd.note("証拠のチェックを飛ばした。バックエンドが設定されていない（INFO_HIVE_DECISIONS で選ぶ）")
    block = {
        "backend": client.backend if client else "off",
        "model": client.model if client else None,
        "threshold": threshold,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "checked": len(items) - len(errors) if client else 0,
        "candidates": len(items),
        "errors": len(errors),
        "failed_open": failed_open,
    }
    return findings, block


def render_text(findings: dict) -> str:
    out = []
    s = findings["summary"]
    out.append("=" * 60)
    out.append("wiki のグラフの lint の報告")
    out.append("=" * 60)
    out.append(f"調べたページ数: {s['pages_scanned']}    ノード数: {s['nodes']}")
    out.append("")

    sections = [
        ("duplicate_node_ids", "重複したノード ID",
         lambda f: f"  - {f['node_id']}: {', '.join(f['paths'])}"
                   + ("  （Unicode の NFC 正規化をすると同じファイル名になる）"
                      if f.get("unicode_normalization") else "")),
        ("unknown_predicates", "未知の述語（オントロジーにない）",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  predicate={f['predicate']!r}"),
        ("broken_object_refs", "目的語の参照を解決できない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']!r}"),
        ("subject_type_mismatch", "主語の型がオントロジーと一致しない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']}: 主語の型={f['subject_type']}（許可: {f['allowed']}）"),
        ("object_type_mismatch", "目的語の型がオントロジーと一致しない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']}: 目的語の型={f['object_type']}（許可: {f['allowed']}）"),
        ("missing_evidence", "型付きのエッジに evidence がない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}"),
        ("missing_source_field", "型付きのエッジに source がない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}"),
        ("broken_source_refs", "source: がどのソースページにも一致しない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  source={f['source']!r}"),
        ("invalid_confidence", "confidence の値が無効",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  confidence={f['confidence']!r}"),
        ("invalid_status", "status の値が無効",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  status={f['status']!r}"),
        ("duplicate_canonical", "重複した正本のノード",
         lambda f: f"  - {f['node_id']}: {', '.join(f['paths'])}"),
        ("alias_collisions", "複数の正本のノードが同じ別名を使っている",
         lambda f: f"  - {f['alias']!r}: {', '.join(f['owners'])}"),
        ("broken_contradicts", "contradicts の参照を解決できない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  contradicts={f.get('contradicts')}"),
        ("broken_supersedes", "supersedes の参照を解決できない",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  supersedes={f.get('supersedes')}"),
        ("orphan_typed_nodes", "孤立した型付きノード（型付きのエッジが入ってくることも出ていくこともない）",
         lambda f: f"  - {f['node_id']}  （{f['path']}）"),
        ("ambiguous_object_refs", "目的語の node_id を持つページが複数あり、エッジにしなかった関係",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}  （{', '.join(f['pages'])}）"),
        ("ambiguous_sources", "sources: の値に当たるページが複数あり、sourced_from を作らなかった",
         lambda f: f"  - {f['page']}  {f['source']}  （{', '.join(f['pages'])}）"),
        ("unresolved_sources", "sources: の値に当たるページがなく、sourced_from を作らなかった",
         lambda f: f"  - {f['page']}  {f['source']}"),
        ("order_dependent_edge_ids", "並べ替えるとエッジの ID が入れ替わりうる関係の項目（エッジの ID の参照か未知の欄だけが違う）",
         lambda f: f"  - {f['page']}#rel{f['indexes']}"),
        ("unreadable_graph", "frontmatter の graph: を読めなかったページ（型付きの関係を作らなかった）",
         lambda f: f"  - {f['page']}  （{UNREADABLE_REASONS.get(f['reason'], f['reason'])}）"),
        ("unread_fields", "wiki の lint のパーサーが読めない書き方の欄（lint と同じく、グラフも読まない）",
         lambda f: f"  - {f['page']}  {f['field']}:"),
        ("evidence_unsupported", "ソースページが証拠を裏づけていない（判定モデル）",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}  "
                   f"（source={f['source']}、choice={f['choice']}、confidence={f['confidence']}）"),
    ]

    healthy = True
    for key, label, formatter in sections:
        items = findings.get(key)
        if not items:
            continue
        healthy = False
        out.append(f"{label}（{len(items)} 件）:")
        for item in items[:50]:
            out.append(formatter(item))
        if len(items) > 50:
            out.append(f"  ……ほか {len(items) - 50} 件")
        out.append("")

    if "evidence_check" in findings:
        check = findings["evidence_check"]
        state = ("実行できなかったので、処理を通した（fail-open）" if check["failed_open"]
                 else f"{check['candidates']} 本のエッジのうち {check['checked']} 本をチェックした")
        out.append(f"証拠のチェック（{check['backend']}）: {state}。")
        out.append("")

    if healthy:
        out.append("グラフの問題は見つからなかった。")
    return "\n".join(out)


def decision_default_threshold(check: str) -> str:
    """判定チェックの既定の閾値。decision-questions.json から読む。"""
    path = SCRIPT_DIR.parent / "assets" / "decision-questions.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["checks"][check]["default_threshold"])
    except (OSError, ValueError, KeyError):
        return f"{path.name} を参照"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki", nargs="?", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    parser.add_argument("--ontology", type=Path, help="オントロジーのファイル（既定: <wiki>/graph/ontology.yaml）")
    parser.add_argument("--json", action="store_true", help="テキストの代わりに JSON を出力する。キーは英語のまま")
    parser.add_argument("--decide", action="store_true",
                        help="ソースページからの引用になっていないエッジの証拠を、判定モデルでチェックする。"
                             "使うには INFO_HIVE_DECISIONS の設定が必要")
    parser.add_argument("--decide-threshold", type=float, default=None,
                        help="裏づけの文として認めるのに必要な確信度"
                             f"（既定: {decision_default_threshold('evidence')}）")
    localize_argparse(parser)
    args = parser.parse_args()
    if args.wiki is None:
        args.wiki = wiki_workspace.default_wiki_or_exit()
    # SCHEMA.md のないディレクトリは、初期化された wiki とみなさない（ADR-0019）。
    wiki_initialized.require_or_exit(args.wiki)

    # オントロジーの有無と読み方は extract、query と同じ規則に従う。
    try:
        ontology_path = common.require_ontology(args.wiki, args.ontology)
    except common.GraphLayerDisabled as exc:
        common.report_disabled(exc)
        sys.exit(common.EXIT_GRAPH_DISABLED)
    ontology = _extract.load_ontology(ontology_path)

    pages = collect_pages(args.wiki)
    findings = lint(pages, ontology)
    if args.decide:
        unsupported, check = decide_evidence(pages, ontology, args.decide_threshold)
        findings["evidence_unsupported"] = unsupported
        findings["evidence_check"] = check
        findings["summary"]["evidence_unsupported"] = len(unsupported)

    if args.json:
        print(json.dumps(findings, indent=2, default=str, ensure_ascii=False))
    else:
        print(render_text(findings))


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
