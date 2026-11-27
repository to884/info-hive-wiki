"""形式2の通常保存で記事版を記録し、現行評価の根拠を再照合する。"""

from dataclasses import dataclass
from pathlib import Path

import wiki_apply as publish
import wiki_live_apply as live
import wiki_semantic_definitions as fixed
import wiki_semantic_store as store
from wiki_fsguard import WriteGuard, external_raw_guard
from wiki_lint import parse_frontmatter
from wiki_pages import is_page_path
from wiki_raw_roots import read_raw_roots
from wiki_semantic_adopt import ADOPT, EvidenceInputs
from wiki_semantic_format import detect
from wiki_semantic_gate import REPUBLISH, RETURN_LIVE, SAVE_LIVE, SAVE_PAGES, require_isolated
from wiki_semantic_model import ModelError, Record, fingerprint, load_json, relative_path
CATALOG = "ontology/definition.json"
BUNDLE = "ontology/ossie"


def require_capability(capability, guard, operation_id, *, operation=SAVE_PAGES):
    try:
        require_isolated(capability, operation=operation, guard=guard, operation_id=operation_id)
    except ModelError as error:
        raise publish.Conflict(str(error)) from error


def archive_view(guard, active):
    release_id, entry = active
    root = guard.root / publish.AREA / "releases" / release_id / "wiki"
    return publish.Publication(guard.root, root, release_id, entry["definition_id"])


def cutback_format(guard, active, operation, operation_id, capability):
    version = detect(WriteGuard(archive_view(guard, active).root, strict_root=True))
    if version == 2:
        require_capability(capability, guard, operation_id, operation=operation)
    return version


class EvidenceChanged(ValueError):
    """固定した原本または記事の内容が変わったか、確認できる場所からなくなった。"""


class SourceUnavailable(ValueError):
    """到達できる登録済みルートに、採用時の実体が見つからない。"""


class PageSaveInputs(EvidenceInputs):
    def __init__(self, records, base, raw_guard, confirmations, articles):
        super().__init__(records, base, raw_guard, confirmations)
        self.articles = articles
        self.sources = {}

    def source(self, source):
        payload = source.payload
        scheme, _, path = payload["reference"]["uri"].partition(":")
        if source.record_id not in self.sources:
            if scheme == "article":
                data = super().source(source)
                if self.articles.get(path) != payload["sha256"]:
                    reason = "article_missing" if path not in self.articles else "article_changed"
                    raise EvidenceChanged(reason, path)
            elif scheme == "raw" and "article_revision_id" not in payload:
                relative_path(path)
                if (payload["reference"]["version"] != "sha256:" + payload["sha256"]
                        or payload["media_type"] not in {"text/plain; charset=utf-8", "text/markdown; charset=utf-8"}):
                    raise ModelError("採用済み原本の参照形式が不正です")
                if self.raw is None:
                    raise SourceUnavailable(path)
                try:
                    data = self.raw.read_bytes(self.raw.root / path)
                except OSError as error:
                    raise publish.Conflict("採用時の原本を読めません。原本を確認して保存を再試行してください") from error
                if data is None or publish.digest(data) != payload["sha256"]:
                    reason = "raw_missing" if data is None else "raw_changed"
                    raise EvidenceChanged(reason, path)
            else:
                raise ModelError("採用済み根拠に未対応の原本参照があります")
            self.sources[source.record_id] = data
        return self.sources[source.record_id]


def raw_guards(base, wiki_id):
    roots, failures = read_raw_roots(base.wiki)
    if failures:
        raise publish.Conflict("原本の根の登録を確認できません。登録を確認して保存を再試行してください")
    guards = {}
    for path in roots:
        try:
            guard = external_raw_guard(base.wiki, path)
        except (OSError, ValueError) as error:
            raise publish.Conflict("登録された原本の根を開けません。根を確認して保存を再試行してください") from error
        key = fingerprint("registered-raw-root-v1", [wiki_id, list(guard.root_id)])
        guards[key] = guard
    return guards


def adoption_request(records, assessment):
    """採用時に公開された入力から、私的な絶対パスを使わず根と原本性の確認を得る。"""
    decision = records[assessment["decision_id"]].payload
    for key in assessment["reports"]:
        report = records[key].payload
        check = report["checks"].get("input_manifest", {})
        if check.get("state") != "pass" or key not in decision["reports"]:
            continue
        inputs = load_json(check.get("reason", ""))
        if (not isinstance(inputs, dict) or not isinstance(inputs.get("request"), dict)
                or fingerprint("semantic-proposal-v1", inputs) != decision["proposal_sha256"]
                or report["input_sha256"] != decision["proposal_sha256"]):
            continue
        request = inputs["request"]
        if (request.get("operation") == ADOPT and isinstance(request.get("raw_root_id"), str)
                and isinstance(request.get("original_confirmations"), list)):
            return request
    raise ModelError("根拠の採用時入力を確認できません")


def current_assessments(value, records, base, articles):
    previous = value.manifest["current_assessments"]
    guards = None
    selected = []
    for key in previous:
        assessment = records[key].payload
        revision = records[assessment["revision_id"]]
        if revision.kind != "claim_revision" or assessment["evidence_state"] != "verified":
            selected.append(key)
            continue
        if guards is None:
            guards = raw_guards(base, value.manifest["wiki_id"])
        request = adoption_request(records, assessment)
        inputs = PageSaveInputs(records, base, guards.get(request["raw_root_id"]),
                                request["original_confirmations"], articles)
        findings = {}
        for evidence_id in assessment["dependencies"]["evidence"]:
            try:
                inputs.evidence(records[evidence_id], revision.payload["claim_id"])
            except EvidenceChanged as error:
                reason, path = error.args
                findings[evidence_id] = {"state": "fail", "reason": reason, "location": path}
            except SourceUnavailable as error:
                findings[evidence_id] = {"state": "not_run", "reason": "raw_root_unavailable", "location": str(error)}
        if findings:
            state = "affected" if any(item["state"] == "fail" for item in findings.values()) else "unavailable"
            report = Record.create(revision.wiki_id, "validation_report", {
                "input_sha256": fingerprint("page-save-evidence-v1", [base.release_id, key, findings]),
                "processing_context_id": assessment["processing_context_id"], "checks": findings})
            updated = Record.create(revision.wiki_id, "current_assessment", {
                **assessment, "evidence_state": state, "reports": [*assessment["reports"], report.record_id]})
            records.update({report.record_id: report, updated.record_id: updated})
            key = updated.record_id
        selected.append(key)
    return sorted(selected)


def page_snapshot(value, base, changes, articles):
    manifest = value.manifest
    records = {row.record_id: row for row in value.records}
    selected = current_assessments(value, records, base, articles)
    known = {(row.payload["page"], row.payload["sha256"]) for row in value.records
             if row.kind == "article_revision"}
    for path, (_, after) in changes.items():
        if not is_page_path(path) or after is None:
            continue
        checksum = publish.digest(after)
        if (path, checksum) in known:
            continue
        meta, _, _ = parse_frontmatter(after.decode("utf-8-sig"))
        title = meta.get("title")
        if not isinstance(title, str) or not title.strip():
            title = Path(path).stem
        row = Record.create(manifest["wiki_id"], "article_revision", {
            "page": path, "sha256": checksum, "title": title, "origin": "unknown", "derived_from": []})
        records[row.record_id] = row
    if len(records) == len(value.records) and selected == manifest["current_assessments"]:
        return value
    return store.snapshot(records.values(), wiki_id=manifest["wiki_id"], parent=manifest["semantic_snapshot_id"],
                          heads=manifest["heads"], definition_snapshot_id=manifest["definition_snapshot_id"],
                          activation_id=manifest["activation_id"], processing_context_id=manifest["processing_context_id"],
                          current_assessments=selected)


def write_page_snapshot(destination, value):
    ontology = destination.root / "ontology"
    with store.artifacts(value) as folder:
        for name in ("records.jsonl", "release.json", "wiki-data.sqlite"):
            live.write(destination, ontology / "ossie" / name, (folder / name).read_bytes(), replace=True)
        live.write(destination, ontology / "definition.json", value.catalog_json, replace=True)


@dataclass(frozen=True)
class PageTransition:
    capability: object
    operation_id: str
    base_release_id: str
    value: store.Snapshot
    files: tuple
    verify_evidence: object


def prepare_page_snapshot(base, files, changes, operation_id, capability):
    previous = store.read_bundle(base.root / BUNDLE)
    approved = {path: publish.digest(after) for path, (_, after) in changes.items()}
    articles = {path: checksum for path, checksum in (files | approved).items() if is_page_path(path)}
    value = page_snapshot(previous, base, changes, articles)
    def verify_evidence():
        # 日誌を確定する前に、根拠の変化で候補の選択が変わらないことを確かめる。
        if page_snapshot(previous, base, changes, articles) != value:
            raise publish.Conflict("公開候補の作成後に意味評価の根拠が変わりました。保存を再試行してください")

    transition = PageTransition(capability, operation_id, base.release_id, value, tuple(approved.items()), verify_evidence)
    return transition, value


def verify_transition(transition, *, wiki, candidate, base, base_release_id, verify_inputs):
    from wiki_graph_extract import OUTPUT_NAMES

    require_capability(transition.capability, WriteGuard(Path(wiki).absolute(), strict_root=True), transition.operation_id)
    if base is None or base_release_id != transition.base_release_id:
        raise publish.Conflict("形式2の通常保存の基準版が変わりました")
    base_guard = WriteGuard(base, strict_root=True)
    destination = WriteGuard(candidate, strict_root=True)
    if detect(base_guard) != 2 or detect(destination) != 2:
        raise ModelError("形式2の通常保存では基準版と候補の形式が一致する必要があります")
    value = store.read_bundle(destination.root / BUNDLE)
    if value != transition.value:
        raise ModelError("形式2の候補が承認差分から計算した意味スナップショットと一致しません")
    fixed.verify_stored(destination, destination.root / "ontology", value)
    before, after = publish.file_checksums(base_guard), publish.file_checksums(destination)
    approved = dict(transition.files)
    if not approved or any(after.get(path) != checksum for path, checksum in approved.items()):
        raise ModelError("ページ保存候補が承認済みの差分と一致しません")
    for path in approved:
        if path == "index.md" or path.startswith("indexes/"):
            live.index_path(path)
        elif path != "log.md":
            live.page_path(path)
    allowed = {*approved, CATALOG, "ontology/wiki-save.json",
               *(f"{BUNDLE}/{name}" for name in ("records.jsonl", "release.json", "wiki-data.sqlite")),
               *("graph/" + name for name in OUTPUT_NAMES)}
    if before.keys() - after.keys() or any(
            before.get(path) != checksum and path not in allowed for path, checksum in after.items()):
        raise ModelError("ページ保存候補が承認差分と派生物の範囲を越えています")
    if verify_inputs:
        transition.verify_evidence()
