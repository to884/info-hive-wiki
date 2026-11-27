"""固定した形式2の記録を、既存のページグラフに一方向で投影する。"""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import wiki_apply as publish
import wiki_semantic_definitions as definitions
import wiki_semantic_store as store
from wiki_fsguard import WriteGuard
from wiki_graph_extract import page_key
from wiki_pages import build_page_index, classify_source_ref, type_value
from wiki_raw_roots import read_raw_roots
from wiki_semantic_format import detect
from wiki_semantic_gate import EXTRACT as EXTRACT_OPERATION
from wiki_semantic_model import fingerprint
from wiki_semantic_projection import require_capability


def read_snapshot(guard):
    if detect(guard) == 1:
        return None
    value = store.read_bundle(guard.root / "ontology/ossie")
    definitions.verify_stored(guard, guard.root / "ontology", value)
    return value


@dataclass(frozen=True)
class StandaloneSource:
    owner: WriteGuard
    source: WriteGuard
    output: WriteGuard
    files: dict
    publication: publish.Publication

    def verify(self):
        publish.verify_payload(self.owner, self.source.root, self.files)
        if publish.resolve_publication(self.owner.root, self.publication.release_id) != self.publication:
            raise publish.Conflict("抽出中に復元の照合状態が変わりました")


def standalone_scope(wiki, output, override, capability):
    """形式2では公開版を固定し、読み手が所有する隔離出力先を照合する。"""
    view = publish.resolve_publication(wiki)
    source = WriteGuard(view.root if view else wiki)
    if detect(source) == 1:
        return None
    if view is None or output is None or override is not None:
        raise publish.Conflict("形式2では公開版と既存の隔離出力先を指定してください")
    source = WriteGuard(view.root, strict_root=True)
    owner = WriteGuard(view.wiki, strict_root=True)
    require_capability(capability, owner, view.release_id, operation=EXTRACT_OPERATION)
    destination = WriteGuard(Path(output).absolute(), strict_root=True)
    require_capability(capability, destination, view.release_id, operation=EXTRACT_OPERATION)
    roots, failures = read_raw_roots(view.wiki)
    if failures:
        raise publish.Conflict("出力先と登録された原本の根が重ならないことを確かめられません")
    target = destination.root.resolve(strict=True)
    for other in (view.wiki, *roots):
        other = other.resolve()
        if target.is_relative_to(other) or other.is_relative_to(target):
            raise publish.Conflict("形式2の出力先は wiki と原本の根から独立させてください")
    files = publish.manifest(owner, owner.root / publish.AREA, view.release_id)["files"]
    scope = StandaloneSource(owner, source, destination, files, view)
    scope.verify()
    return scope


def eligibility(claim, revision, assessment, manifest):
    if len(claim["role_bindings"]) != 2:
        return "unsupported_arity"
    if claim["polarity"] != "positive" or claim["temporal_scope"]["kind"] != "unknown" or claim["qualifiers"]:
        return "unsupported_claim_scope"
    if revision["lifecycle"] != "accepted":
        return "claim_not_accepted"
    if claim["relationship"]["definition_snapshot_id"] != manifest["definition_snapshot_id"]:
        return "definition_not_current"
    if assessment is None:
        return "assessment_missing"
    if assessment["evidence_state"] != "verified":
        return "evidence_" + assessment["evidence_state"]
    if assessment["compatibility"] != "compatible":
        return "claim_not_compatible"
    return None


class GraphPages:
    def __init__(self, guard, pages, node_ids):
        self.guard = guard
        self.node_ids = node_ids
        self.pages = {page_key(p): p for p in pages}
        self.node_counts = Counter(node_ids.values())
        self.paths, self.slugs = build_page_index(pages, page_key)
        self.link_warnings = getattr(pages, "link_warnings", ())
        self.checksums = {}
        self.mappings = {}
        self.unavailable = {}
        self.entities_by_node = {}

    def map_entities(self, records, heads):
        for entity_id, revision_id in sorted(heads.items()):
            article_id = records[revision_id].payload["display"].get("graph_article_revision_id")
            if article_id is None:
                continue
            path = self.article_page(records[article_id])
            if path is None:
                self.unavailable[entity_id] = article_id
                continue
            node = self.node_ids[path]
            self.mappings[entity_id] = {"page": path, "article_revision_id": article_id, "node_id": node}
            self.entities_by_node.setdefault(node, []).append(entity_id)

    def endpoint(self, path):
        if not isinstance(path, str) or path not in self.pages:
            return None
        node = self.node_ids.get(path)
        if node is None or self.node_counts[node] != 1:
            return None
        return node

    def page_type(self, key):
        return type_value(self.pages[key]["meta"].get("type"))

    def article_page(self, article):
        path = article.payload["page"]
        if self.endpoint(path) is None:
            return None
        if path not in self.checksums:
            data = self.guard.read_bytes(self.guard.root / path)
            self.checksums[path] = publish.digest(data) if data is not None else None
        return path if self.checksums[path] == article.payload["sha256"] else None

    def source_page(self, source, article):
        if article is None or article.kind != "article_revision":
            return None
        path = article.payload["page"]
        checksum = article.payload["sha256"]
        if (source["reference"] != {"uri": "article:" + path, "version": "sha256:" + checksum}
                or source["sha256"] != checksum or self.article_page(article) is None or self.page_type(path) != "source"):
            return None
        status, keys, _ = classify_source_ref(path, self.paths, self.slugs, self.page_type,
                                             link_warnings=self.link_warnings)
        return path if status == "ok" and keys == [path] else None


def ordered_roles(claim):
    return sorted(claim["role_bindings"], key=lambda binding: binding["ordinal"])


def edge_for_claim(claim_id, claim, revision, assessment, records, graph):
    endpoints = []
    for binding in ordered_roles(claim):
        entity = binding["entity_id"]
        if entity in graph.unavailable:
            return None, {"reason": "endpoint_article_unavailable", **binding,
                          "article_revision_id": graph.unavailable[entity]}
        if entity not in graph.mappings:
            return None, {"reason": "endpoint_unmapped", "entity_id": entity, "ordinal": binding["ordinal"]}
        endpoint = graph.mappings[entity]
        others = graph.entities_by_node[endpoint["node_id"]]
        if len(others) > 1:
            return None, {"reason": "endpoint_shared_page", **binding, **endpoint, "entity_ids": others}
        endpoints.append({**binding, **endpoint})

    candidates = []
    support_ids = sorted({item["evidence_id"] for item in revision["evidence_links"] if item["kind"] == "supports"})
    for key in support_ids:
        evidence = records[key].payload
        source = records[evidence["source_id"]].payload
        path = graph.source_page(source, records.get(source.get("article_revision_id")))
        if path is not None:
            candidates.append((path, key, evidence["quote"]))
    if not candidates:
        return None, {"reason": "support_source_unmapped", "evidence_ids": support_ids}
    path, key, quote = min(candidates)
    relation = claim["relationship"]
    return {
        "id": fingerprint("ossie-graph-edge-v1", claim_id),
        "subject": endpoints[0]["node_id"], "object": endpoints[1]["node_id"],
        "predicate": "ossie:" + fingerprint("ossie-graph-predicate-v1", relation),
        "source": path, "page": graph.pages[path]["rel_path"], "evidence": quote,
        "confidence": "", "status": "", "extraction_method": "ossie_semantic",
        "extras": {"claim_id": claim_id, "assessment": assessment, "relationship": relation,
                   "role_bindings": endpoints, "evidence_id": key, "support_evidence_ids": support_ids,
                   "evidence_state": "verified"},
    }, None


def relation_roles(claim):
    return fingerprint("ossie-graph-relation-roles-v1", [claim["relationship"], ordered_roles(claim)])


def project(guard, value, pages, node_ids, *, invalidated=frozenset()):
    manifest = value.manifest
    records = {row.record_id: row for row in value.records}
    claims = {row.payload["claim_id"]: row.payload for row in value.records if row.kind == "claim"}
    assessments = {records[key].payload["revision_id"]: key for key in manifest["current_assessments"]}
    graph = GraphPages(guard, pages, node_ids)
    graph.map_entities(records, manifest["heads"]["entities"])
    heads = manifest["heads"]["claims"]
    opposed = set()
    for claim_id, revision_id in heads.items():
        key = assessments.get(revision_id)
        if (key is not None and key not in invalidated and claims[claim_id]["polarity"] == "negative"
                and records[revision_id].payload["lifecycle"] == "accepted"):
            assessment = records[key].payload
            if assessment["compatibility"] == "compatible" and assessment["evidence_state"] == "verified":
                opposed.add(relation_roles(claims[claim_id]))
    edges, omitted = [], []
    for claim_id, revision_id in sorted(heads.items()):
        claim, revision = claims[claim_id], records[revision_id].payload
        assessment = assessments.get(revision_id)
        payload = records[assessment].payload if assessment is not None else None
        reason = eligibility(claim, revision, payload, manifest)
        if assessment in invalidated and reason is None:
            reason = "restore_reassessment_required"
        if reason is None and relation_roles(claim) in opposed:
            reason = "opposing_claim_present"
        if reason is None:
            edge, diagnostic = edge_for_claim(claim_id, claim, revision, assessment, records, graph)
            if edge is not None:
                edges.append(edge)
                continue
        else:
            diagnostic = {"reason": reason}
        omitted.append({"claim_id": claim_id, "claim_revision_id": revision_id, **diagnostic})
    report = {"format": 1, "semantic_snapshot_id": manifest["semantic_snapshot_id"],
              "projected": len(edges), "omitted": omitted}
    return edges, report
