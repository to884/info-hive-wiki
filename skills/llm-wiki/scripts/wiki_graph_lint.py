#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyyaml==6.0.3",
# ]
# ///
"""
wiki_graph_lint.py — wiki の型付きグラフのメタデータを検証する。

すべてのページの `graph:` の frontmatter を読み、オントロジー（`wiki/graph/ontology.yaml`）と
照らし合わせ、問題を報告する。保守的な設計で、報告するだけで編集はしない。

PyYAML の固定した依存を持つので、`uv run --script` で実行する。

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
- `contradicts` と `supersedes` の参照が、既知のノードかエッジの ID に解決される。
- 生成したグラフに、孤立した型付きノード（型付きのエッジが入ってくることも出ていくこともないノード）がない。
  ただし `source` のノードは除く（ソースとしてだけ使われることを許可する）。
- 任意（--decide）: 証拠の引用がソースページにない型付きのエッジを、判定モデルがチェックする。
  検出結果を報告するだけである。

使い方:
    uv run --script wiki_graph_lint.py [<wiki-dir>] [--json] [--decide [--decide-threshold X]]
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "wiki_graph_lint.py requires PyYAML. "
        "Run it with `uv run --script wiki_graph_lint.py ...`.",
        file=sys.stderr,
    )
    sys.exit(2)

# extract と同じモジュールを import し、その build_nodes と build_edges を再利用して、
# extract が出力するものとまったく同じものを lint が見るようにする。
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import wiki_graph_extract as _extract  # noqa: E402


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

SKIP_TOP_LEVEL_FILES = {"SCHEMA.md", "index.md", "log.md", "README.md"}
SKIP_TOP_LEVEL_DIRS = {"indexes", "graph", "raw", ".evolution"}

ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_STATUS = {"current", "historical", "proposed", "disputed", "superseded"}
IMPLICIT_PREDICATES = {"mentions", "sourced_from", "summarizes_raw"}


def parse_frontmatter(text: str) -> tuple[dict, str]:
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
    return meta, body


def collect_pages(wiki_root: Path) -> list[dict]:
    pages = []
    for md_path in sorted(wiki_root.rglob("*.md")):
        rel = md_path.relative_to(wiki_root)
        if rel.parts[0] in SKIP_TOP_LEVEL_FILES or rel.parts[0] in SKIP_TOP_LEVEL_DIRS:
            continue
        if rel.name.startswith("."):
            continue
        try:
            text = md_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        meta, body = parse_frontmatter(text)
        pages.append({
            "path": str(md_path),
            "rel_path": str(rel).replace("\\", "/"),
            "slug": md_path.stem,
            "meta": meta,
            "body": body,
            "links": [m.group(1).strip() for m in WIKILINK_RE.finditer(body)],
        })
    return pages


def derive_node_type(meta: dict, ontology: dict) -> str | None:
    page_type = meta.get("type")
    page_kind = meta.get("kind")
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
    graph_meta = meta.get("graph") if isinstance(meta.get("graph"), dict) else {}
    explicit = graph_meta.get("node_id")
    if explicit:
        return str(explicit)
    node_type = derive_node_type(meta, ontology) or "concept"
    return f"{node_type}:{slug}"


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
            duplicates[nid].append(p["rel_path"])
            duplicates[nid].append(node_by_id[nid]["rel_path"])
            continue
        node_type = derive_node_type(p["meta"], ontology) or "concept"
        graph_meta = p["meta"].get("graph") if isinstance(p["meta"].get("graph"), dict) else {}
        node_by_id[nid] = {
            "id": nid,
            "node_type": node_type,
            "rel_path": p["rel_path"],
            "slug": p["slug"],
            "page_type": p["meta"].get("type"),
            "canonical": bool(graph_meta.get("canonical", False)),
            "aliases": list(graph_meta.get("aliases") or []),
            "graph": graph_meta,
        }

    for nid, paths in duplicates.items():
        findings["duplicate_node_ids"].append({"node_id": nid, "paths": sorted(set(paths))})

    # スラッグをキーにしたソースページ。エッジの `source:` の参照を検証するのに使う。
    source_slugs = {p["slug"] for p in pages if p["meta"].get("type") == "source"}

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
            if obj and obj not in node_by_id:
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
            obj_node = node_by_id.get(obj) if obj else None
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

            # contradicts と supersedes の解決
            for ref_field, bucket in (("contradicts", "broken_contradicts"),
                                       ("supersedes", "broken_supersedes")):
                ref = rel.get(ref_field)
                if ref:
                    ref_str = str(ref)
                    if ref_str not in node_by_id and ref_str not in source_slugs:
                        findings[bucket].append({**here, ref_field: ref_str})

    # 孤立した型付きノード。`graph:` の frontmatter を宣言しているのに、
    # 抽出後に、型付きの（暗黙でない）エッジが1本もつながっていないページ。
    # ソースのノードは対象外である（暗黙のエッジでつながるため）。
    extracted_edges = _extract.build_edges(pages, {n["slug"]: n["id"] for n in node_by_id.values()})
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
    sources = {p["slug"]: p for p in pages if p["meta"].get("type") == "source"}
    titles = {derive_node_id(p["meta"], p["slug"], ontology): p["meta"].get("title") or p["slug"] for p in pages}
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
        wd.note(f"evidence check failed for {len(errors)} of {len(items)} edges ({errors[0]}); those edges are not flagged")
    elif items and client is None:
        wd.note("evidence check skipped: no backend configured (set LLM_WIKI_DECISIONS)")
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
    out.append("Wiki Graph Lint Report")
    out.append("=" * 60)
    out.append(f"Pages scanned: {s['pages_scanned']}    Nodes: {s['nodes']}")
    out.append("")

    sections = [
        ("duplicate_node_ids", "Duplicate node ids",
         lambda f: f"  - {f['node_id']}: {', '.join(f['paths'])}"),
        ("unknown_predicates", "Unknown predicates (not in ontology)",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  predicate={f['predicate']!r}"),
        ("broken_object_refs", "Broken object references",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']!r}"),
        ("subject_type_mismatch", "Subject type does not match ontology",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']}: subject={f['subject_type']} (allowed: {f['allowed']})"),
        ("object_type_mismatch", "Object type does not match ontology",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']}: object={f['object_type']} (allowed: {f['allowed']})"),
        ("missing_evidence", "Missing evidence on typed edge",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}"),
        ("missing_source_field", "Missing source on typed edge",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}"),
        ("broken_source_refs", "source: does not match any source page",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  source={f['source']!r}"),
        ("invalid_confidence", "Invalid confidence value",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  confidence={f['confidence']!r}"),
        ("invalid_status", "Invalid status value",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  status={f['status']!r}"),
        ("duplicate_canonical", "Duplicate canonical nodes",
         lambda f: f"  - {f['node_id']}: {', '.join(f['paths'])}"),
        ("alias_collisions", "Alias used by multiple canonical nodes",
         lambda f: f"  - {f['alias']!r}: {', '.join(f['owners'])}"),
        ("broken_contradicts", "Broken contradicts reference",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  contradicts={f.get('contradicts')}"),
        ("broken_supersedes", "Broken supersedes reference",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  supersedes={f.get('supersedes')}"),
        ("orphan_typed_nodes", "Orphan typed nodes (no inbound or outbound typed edges)",
         lambda f: f"  - {f['node_id']}  ({f['path']})"),
        ("evidence_unsupported", "Evidence not supported by the source page (decision model)",
         lambda f: f"  - {f['page']}#rel[{f['index']}]  {f['predicate']} → {f['object']}  "
                   f"(source={f['source']}, choice={f['choice']}, confidence={f['confidence']})"),
    ]

    healthy = True
    for key, label, formatter in sections:
        items = findings.get(key)
        if not items:
            continue
        healthy = False
        out.append(f"{label} ({len(items)}):")
        for item in items[:50]:
            out.append(formatter(item))
        if len(items) > 50:
            out.append(f"  ... and {len(items) - 50} more")
        out.append("")

    if "evidence_check" in findings:
        check = findings["evidence_check"]
        state = "failed open" if check["failed_open"] else f"checked {check['checked']} of {check['candidates']} edges"
        out.append(f"Evidence check ({check['backend']}): {state}.")
        out.append("")

    if healthy:
        out.append("No graph issues found.")
    return "\n".join(out)


def decision_default_threshold(check: str) -> str:
    """判定チェックの既定の閾値。decision-questions.json から読む。"""
    path = SCRIPT_DIR.parent / "assets" / "decision-questions.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["checks"][check]["default_threshold"])
    except (OSError, ValueError, KeyError):
        return f"see {path.name}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki", nargs="?", type=Path, default=Path("wiki"))
    parser.add_argument("--ontology", type=Path, help="Ontology file (default: <wiki>/graph/ontology.yaml)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--decide", action="store_true",
                        help="Check unquoted edge evidence with the decision model. Needs LLM_WIKI_DECISIONS.")
    parser.add_argument("--decide-threshold", type=float, default=None,
                        help="Confidence needed to accept a supporting sentence "
                             f"(default: {decision_default_threshold('evidence')}).")
    args = parser.parse_args()

    if not args.wiki.exists():
        print(f"Wiki directory not found: {args.wiki}", file=sys.stderr)
        sys.exit(1)

    ontology_path = args.ontology or (args.wiki / "graph" / "ontology.yaml")
    if not ontology_path.exists():
        print(f"Ontology not found: {ontology_path}", file=sys.stderr)
        print("Did you forget to seed wiki/graph/ontology.yaml? See assets/ontology.yaml.template.",
              file=sys.stderr)
        sys.exit(1)
    try:
        ontology = yaml.safe_load(ontology_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"Ontology parse error: {e}", file=sys.stderr)
        sys.exit(2)

    pages = collect_pages(args.wiki)
    findings = lint(pages, ontology)
    if args.decide:
        unsupported, check = decide_evidence(pages, ontology, args.decide_threshold)
        findings["evidence_unsupported"] = unsupported
        findings["evidence_check"] = check
        findings["summary"]["evidence_unsupported"] = len(unsupported)

    if args.json:
        print(json.dumps(findings, indent=2, default=str))
    else:
        print(render_text(findings))


if __name__ == "__main__":
    main()
