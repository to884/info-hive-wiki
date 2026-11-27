"""復元した固定公開版の根拠を照合し、旧評価の失効を独立記録に渡す。"""

import wiki_apply as publish
from wiki_fsguard import WriteGuard
from wiki_semantic_graph import read_snapshot
from wiki_semantic_model import ModelError, fingerprint
from wiki_semantic_projection import EvidenceChanged, PageSaveInputs, SourceUnavailable, adoption_request


def recheck(owner, area, releases, raw):
    invalidated = set()
    counts = {"releases": 0, "matched": 0, "affected": 0, "unavailable": 0}
    findings = []
    for version, entry in releases:
        root = area / "releases" / version / "wiki"
        value = read_snapshot(WriteGuard(root, strict_root=True))
        if value is None:
            continue
        counts["releases"] += 1
        view = publish.Publication(owner.root, root, version, entry["definition_id"])
        records = {row.record_id: row for row in value.records}
        raw_id = None if raw is None else fingerprint(
            "registered-raw-root-v1", [value.manifest["wiki_id"], list(raw.root_id)])
        for key in value.manifest["current_assessments"]:
            assessment = records[key].payload
            if assessment["evidence_state"] != "verified":
                continue
            revision = records[assessment["revision_id"]]
            if revision.kind != "claim_revision":
                raise ModelError("復元した検査済み評価が主張を指していません")
            request = adoption_request(records, assessment)
            inputs = PageSaveInputs(records, view, raw if raw_id == request["raw_root_id"] else None,
                                    request["original_confirmations"], entry["files"])
            claim_id = revision.payload["claim_id"]
            checks = {}
            for evidence_id in assessment["dependencies"]["evidence"]:
                try:
                    inputs.evidence(records[evidence_id], claim_id)
                except EvidenceChanged as error:
                    checks[evidence_id] = {"state": "affected", "reason": error.args[0]}
                except SourceUnavailable:
                    checks[evidence_id] = {"state": "unavailable", "reason": "raw_root_unavailable"}
            outcome = "matched"
            if checks:
                outcome = "affected" if any(check["state"] == "affected" for check in checks.values()) else "unavailable"
                findings.append({"release_id": version, "claim_id": claim_id, "assessment_id": key, "evidence": checks})
            counts[outcome] += 1
            invalidated.add(key)
    if not counts["releases"]:
        raise publish.PublicationError("復元した形式2の固定公開版を確認できません")
    return invalidated, {**counts, "findings": findings}
