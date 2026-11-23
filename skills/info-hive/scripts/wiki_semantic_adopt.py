"""P3 後半の定義採用・手動の意味採用。配布版の書込みは無効、親の隔離能力でだけ実行する。

候補は固定レコードと明示した head の更新。検査・採否・現行評価はこの層で生成し、
呼出側が渡す pass や ready を採用の根拠にしない。公開・再送・復旧は既存 writer を共有する。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import wiki_apply as publish
import wiki_live_apply as live
import wiki_public_save as save
import wiki_semantic_apply as shared
import wiki_semantic_definitions as fixed
import wiki_semantic_store as store
from wiki_fsguard import WriteGuard
from wiki_pages import article_path
from wiki_raw_roots import read_raw_roots
from wiki_ossie_contract import compile_contract, inspect_bundle
from wiki_semantic_model import (DefinitionRef, ModelError, Record, RoleBinding, RoleRef, activation_id, digest_id,
                                 canonical, fingerprint, load_json, relative_path, text)


ACTIVATE = "activate_definition"
ADOPT = "adopt_semantics"
KINDS = frozenset({ACTIVATE, ADOPT})
INPUT_KINDS = frozenset({"entity", "entity_revision", "claim", "claim_revision", "source_snapshot",
                         "evidence", "article_revision"})
PLAN_KEYS = frozenset({"operation", "rule", "plan_id", "wiki", "wiki_identity", "publication", "request",
    "proposal_sha256", "read_set", "before_semantic_snapshot_id", "semantic_snapshot_id", "invalidated_assessments",
    "heads", "rows", "definition", "activation_id"})


def approval(plan):
    return fingerprint("semantic-adoption-approval-v1", plan)


def _approved_plan(plan, operation, operation_id, guard, expected):
    if (not isinstance(plan, dict) or plan.keys() != PLAN_KEYS or plan.get("operation") != operation
            or plan.get("plan_id") != operation_id or plan.get("wiki") != str(guard.root)
            or plan.get("wiki_identity") != list(guard.root_id) or approval(plan) != expected
            or not isinstance(plan["publication"], dict)
            or plan["publication"].keys() != {"release_id", "definition_id"}
            or not isinstance(plan["request"], dict) or plan["request"].get("operation") != operation):
        raise publish.PublicationError("保存した意味採用の承認・計画・wikiが一致しません")
    for item in plan["publication"].values():
        digest_id(item)
    return plan


def _contract(definition, specification):
    if specification is None:
        raise ModelError("固定した公式仕様を指定してください")
    with TemporaryDirectory(prefix="info-hive-adoption-contract-") as name:
        target = Path(name) / definition.document
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(definition.content)
        contract = compile_contract(inspect_bundle(name, ontology=definition.document, specification=specification))
    if (contract.definition_snapshot_id, contract.processing_context_id) != (
            definition.definition_snapshot_id, definition.processing_context_id):
        raise ModelError("定義の内容・処理条件が現在の公式検査と一致しません")
    return contract


def _active_definition(value, base):
    """有効な保存版の定義と処理条件だけから、再検査用の固定入力を復元する。"""
    m = value.manifest
    if m["activation_id"] is None:
        raise ModelError("意味採用の定義が未設定です")
    spaces = store._named_ids(value.records)
    manifest = spaces["definition"][m["definition_snapshot_id"]].payload["manifest"]
    if len(manifest["documents"]) != 1:
        raise ModelError("単独 Ontology 以外は未対応です")
    doc = manifest["documents"][0]
    guard = WriteGuard(base.root, strict_root=True)
    content = guard.read_bytes(base.root / "ontology/definitions" / m["definition_snapshot_id"] / doc["path"])
    return fixed.FrozenDefinition(m["definition_snapshot_id"], m["processing_context_id"], doc["path"], content,
                                  canonical(manifest), canonical(spaces["context"][m["processing_context_id"]].payload["lock"]))


def _append(records, additions):
    result = {r.record_id: r for r in records}
    for row in additions:
        if row.record_id in result and result[row.record_id] != row:
            raise ModelError("同じ ID に異なる固定記録があります")
        result[row.record_id] = row
    return result


def _decision(wiki_id, operation_id, request, proposal, activation, context, targets, checks, basis):
    inputs = {"basis": basis, "request": request, "operation_id": operation_id}
    if fingerprint("semantic-proposal-v1", inputs) != proposal:
        raise ModelError("検査の対象マニフェストが proposal と一致しません")
    checks = {**checks, "input_manifest": {"state": "pass", "reason": canonical(inputs).decode("utf-8")}}
    report = Record.create(wiki_id, "validation_report", {
        "input_sha256": proposal, "processing_context_id": context, "checks": checks})
    decision = Record.create(wiki_id, "decision", {
        "targets": sorted(targets), "proposal_sha256": proposal, "activation_id": activation,
        "processing_context_id": context, "reports": [report.record_id], "disposition": "accept",
        "actor": request["actor"], "reason": request["reason"], "operation_id": operation_id,
        "recorded_at": request["recorded_at"]})
    return report, decision


def _raw_guard(base, raw_root):
    """登録済み原本の根を照合し、wiki・公開物をrawとして読む迂回を拒否する。"""
    root = Path(raw_root).absolute().resolve(strict=True)
    wiki = base.wiki.resolve(strict=True)
    if wiki.is_relative_to(root) or (root.is_relative_to(wiki) and not root.is_relative_to(wiki / "raw")):
        raise ModelError("原本の根をwiki・公開領域・記事ツリーと重ねられません")
    registered, failures = read_raw_roots(base.wiki)
    if failures:
        raise ModelError("登録された原本の根を確認できません")
    matches = False
    for path in registered:
        try:
            matches |= path.resolve(strict=True) == root
        except OSError:
            continue
    if not matches:
        raise ModelError("P3の意味採用には登録済みの原本の根を指定してください")
    return WriteGuard(Path(raw_root).absolute(), strict_root=True)


def _check_reads(reads, base):
    for entry in reads:
        guard = _raw_guard(base, entry["root"]) if entry["kind"] == "raw" else WriteGuard(Path(entry["root"]), strict_root=True)
        if entry["kind"] == "article":
            pinned = publish.resolve_publication(entry["wiki"], entry["release_id"])
            if pinned.root != guard.root or article_path(guard.root, entry["path"]).state != "ok":
                raise ModelError("根拠の記事版を読む口で照合できません")
        if list(guard.root_id) != entry["identity"]:
            raise ModelError("原本の根の実体が変わりました")
        data = guard.read_bytes(guard.root / entry["path"])
        if data is None or publish.digest(data) != entry["sha256"]:
            raise ModelError("原本または記事の固定入力が変わりました")


class EvidenceInputs:
    """同じ公開版と明示した raw 根から位置・来歴を検査する。支持の意味判断は人の固定値で受ける。"""

    def __init__(self, records, base, raw_guard, confirmations):
        self.records, self.base = records, base
        self.raw = raw_guard
        self.confirmations = {}
        for item in confirmations:
            if not isinstance(item, dict) or item.keys() != {"article_revision_id", "actor", "reason", "method"}:
                raise ModelError("原本性の確認に記事版・行為者・理由・方法を指定してください")
            digest_id(item["article_revision_id"])
            for name in ("actor", "reason", "method"):
                text(item[name], name)
            if item["article_revision_id"] in self.confirmations:
                raise ModelError("原本性の確認が重複しています")
            self.confirmations[item["article_revision_id"]] = item
        self.reads = {}

    def source(self, source):
        p = source.payload
        uri = p["reference"]["uri"]
        scheme, _, path = uri.partition(":")
        relative_path(path)
        if scheme == "raw" and "article_revision_id" not in p:
            guard = self.raw
        elif scheme == "article" and p.get("article_revision_id") in self.records:
            guard = WriteGuard(self.base.root, strict_root=True)
            if article_path(self.base.root, path).state != "ok" or save.snapshot_files(self.base).get(path) != p["sha256"]:
                raise ModelError("同じ公開版に含まれる記事だけを根拠にできます")
            article = self.records[p["article_revision_id"]]
            if article.kind != "article_revision" or (article.payload["page"], article.payload["sha256"]) != (path, p["sha256"]):
                raise ModelError("記事版と原本参照が一致しません")
        else:
            raise ModelError("根拠は raw または同じ公開版の記事への固定参照にしてください")
        if p["media_type"] not in {"text/plain; charset=utf-8", "text/markdown; charset=utf-8"}:
            raise ModelError("初期の根拠は UTF-8 テキストに限定します")
        data = guard.read_bytes(guard.root / path)
        if data is None or p["reference"]["version"] != "sha256:" + p["sha256"] or publish.digest(data) != p["sha256"]:
            raise ModelError("原本・記事のバイト指紋または版が一致しません")
        try:
            data.decode("utf-8")
        except UnicodeError as error:
            raise ModelError("原本は UTF-8 テキストにしてください") from error
        self.reads[(str(guard.root), path)] = {"root": str(guard.root), "identity": list(guard.root_id),
            "path": path, "sha256": p["sha256"], "kind": scheme,
            "wiki": str(self.base.wiki) if scheme == "article" else None,
            "release_id": self.base.release_id if scheme == "article" else None}
        return data

    def lineage(self, record_id, claim_id, active=None):
        active = set() if active is None else active
        if record_id in active or len(active) > 200:
            raise ModelError("根拠の来歴に循環があります")
        row = self.records.get(record_id)
        if row is None:
            raise ModelError("根拠の来歴を解決できません")
        p = row.payload
        active = active | {record_id}
        if row.kind == "source_snapshot":
            self.source(row)
            if p.get("article_revision_id") is None:
                return {record_id}
            links = [p["article_revision_id"]]
        elif row.kind == "evidence":
            self.position(row)
            links = [p["source_id"]]
        elif row.kind == "article_revision":
            self.source(Record.create(row.wiki_id, "source_snapshot", {
                "reference": {"uri": "article:" + p["page"], "version": "sha256:" + p["sha256"]},
                "sha256": p["sha256"], "media_type": "text/markdown; charset=utf-8", "article_revision_id": record_id}))
            if p["origin"] == "original":
                if p["derived_from"] or record_id not in self.confirmations:
                    raise ModelError("一次記事には明示した原本性の確認が必要です")
                return {record_id}
            if p["origin"] == "unknown" or not p["derived_from"]:
                raise ModelError("記事の独立した支持を来歴から確認できません")
            links = p["derived_from"]
        elif row.kind == "claim_revision":
            if p["claim_id"] == claim_id:
                raise ModelError("同じ主張から生成した記事を支持へ戻せません")
            links = [e["evidence_id"] for e in p["evidence_links"]]
        else:
            raise ModelError("対応していない根拠の来歴です")
        if not links:
            raise ModelError("独立した原本まで来歴が到達しません")
        result = set()
        for key in links:
            result |= self.lineage(key, claim_id, active)
        return result

    def position(self, row):
        p = row.payload
        source = self.records.get(p["source_id"])
        if source is None or source.kind != "source_snapshot":
            raise ModelError("根拠の原本参照がありません")
        data = self.source(source)
        locator = p["locator"]
        if (locator["text_sha256"] != publish.digest(data) or locator["end_byte"] > len(data)
                or data[locator["start_byte"]:locator["end_byte"]] != p["quote"].encode("utf-8")
                or p["extraction"].keys() != {"method", "version"}):
            raise ModelError("根拠の位置・引用・抽出表現が固定原本と一致しません")
        text(p["extraction"]["method"]); text(p["extraction"]["version"])

    def evidence(self, row, claim_id):
        self.position(row)
        return self.lineage(row.payload["source_id"], claim_id)


def _reachable(records, targets, spaces, fresh):
    """今回の改訂から参照されない新規レコードを、未検査のまま正本へ入れない。"""
    reached, pending = set(), list(targets)
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        row = records.get(key)
        if row is None:
            raise ModelError("候補から参照する記録がありません")
        reached.add(key)
        if key not in fresh:
            continue
        p = row.payload
        try:
            if row.kind == "entity_revision":
                pending.append(spaces["entity"][p["entity_id"]].record_id)
            elif row.kind == "claim_revision":
                pending.append(spaces["claim"][p["claim_id"]].record_id)
                pending.extend(x["evidence_id"] for x in p["evidence_links"])
            elif row.kind == "claim":
                pending.extend(spaces["entity"][x["entity_id"]].record_id for x in p["role_bindings"])
            elif row.kind == "evidence":
                pending.append(p["source_id"])
            elif row.kind == "source_snapshot" and p.get("article_revision_id"):
                pending.append(p["article_revision_id"])
            elif row.kind == "article_revision":
                pending.extend(p["derived_from"])
        except KeyError as error:
            raise ModelError("候補の実体・主張の参照を解決できません") from error
    if not fresh <= reached:
        raise ModelError("今回の改訂から到達しない候補レコードがあります")


def _adopt(value, base, operation_id, request, contract, proposal, public_request, raw_guard):
    m = value.manifest
    additions = [Record.from_dict(item) for item in request["records"]]
    if any(r.kind not in INPUT_KINDS or r.wiki_id != m["wiki_id"] for r in additions):
        raise ModelError("手動候補に採否・検査・別wikiのレコードは渡せません")
    if len({r.record_id for r in additions}) != len(additions):
        raise ModelError("候補の記録が重複しています")
    if any(r.kind == "entity" and r.record_id not in m["record_ids"] and r.payload["operation_id"] != operation_id
           for r in additions):
        raise ModelError("新しい実体の作成操作が採用計画と一致しません")
    records = _append(value.records, additions)
    heads = load_json(canonical(m["heads"]))
    updates = request["heads"]
    if not isinstance(updates, dict) or updates.keys() != heads.keys() or any(not isinstance(v, dict) for v in updates.values()):
        raise ModelError("更新する実体・主張 head を指定してください")
    targets = set()
    for space, owner, kind in (("entities", "entity_id", "entity_revision"), ("claims", "claim_id", "claim_revision")):
        for key, revision in updates[space].items():
            if not isinstance(key, str) or not isinstance(revision, str):
                raise ModelError("headの対象・改訂は固定IDにしてください")
            row = records.get(revision)
            if row is None or row.kind != kind or row.payload[owner] != key:
                raise ModelError("候補の head と対象改訂が一致しません")
            if revision in m["record_ids"] or row.payload["previous_id"] != heads[space].get(key):
                raise ModelError("候補の前改訂が現在の head と一致しません")
            heads[space][key] = revision
            targets.add(revision)
    if not targets or any(r.kind in {"entity_revision", "claim_revision"} and r.record_id not in targets
                          for r in additions if r.record_id not in m["record_ids"]):
        raise ModelError("新しい改訂と明示した head の更新を一致させてください")
    spaces = store._named_ids(records.values())
    _reachable(records, targets, spaces, set(records) - set(m["record_ids"]))
    types = {}
    concepts = {c.ref for c in contract.concepts}
    for entity, revision in heads["entities"].items():
        row = records[revision]
        refs = tuple(DefinitionRef(**ref) for ref in row.payload["type_refs"])
        if revision in targets and (not refs or len(refs) != len(set(refs)) or not set(refs) <= concepts):
            raise ModelError("新しい実体改訂の型を採用済み定義で解決できません")
        types[entity] = refs
    inputs = EvidenceInputs(records, base, raw_guard, request["original_confirmations"])
    if any(key not in records or records[key].kind != "article_revision" or records[key].payload["origin"] != "original"
           for key in inputs.confirmations):
        raise ModelError("原本性の確認は候補または保存版の一次記事に結び付けてください")
    judgments = {}
    for j in request["judgments"]:
        if not isinstance(j, dict) or j.keys() != {"claim_id", "evidence_id", "support", "actor", "reason", "method", "primary_sources"}:
            raise ModelError("支持判断には命題・根拠・行為者・理由・方法・一次原本を指定してください")
        for field in ("actor", "reason", "method"):
            text(j[field], field)
        digest_id(j["claim_id"]); digest_id(j["evidence_id"])
        key = (j["claim_id"], j["evidence_id"])
        if key in judgments or not isinstance(j["support"], str) or j["support"] not in {"supports", "opposes", "context"}:
            raise ModelError("支持判断が重複しているか未確認です")
        judgments[key] = j
    used, assessments = set(), []
    for cid, revision in updates["claims"].items():
        claim = spaces["claim"].get(cid)
        if claim is None:
            raise ModelError("候補の主張がありません")
        cp, rp = claim.payload, records[revision].payload
        relation = DefinitionRef(**cp["relationship"])
        bindings = [RoleBinding(RoleRef(relation, b["ordinal"]), b["entity_id"]) for b in cp["role_bindings"]]
        if contract.check_bindings(relation, bindings, types):
            raise ModelError("主張の関係・役割・端点型が採用済み定義に適合しません")
        supported, opposed = False, False
        for link in rp["evidence_links"]:
            evidence = records.get(link["evidence_id"])
            if evidence is None or evidence.kind != "evidence":
                raise ModelError("候補の根拠がありません")
            primary = inputs.evidence(evidence, cid)
            key = (cid, evidence.record_id)
            j = judgments.get(key)
            if j is None or j["support"] != link["kind"] or j["primary_sources"] != sorted(primary):
                raise ModelError("位置照合と独立した原本への支持判断が不足しています")
            used.add(key)
            supported |= j["support"] == "supports"
            opposed |= j["support"] == "opposes"
            assessments.append(Record.create(m["wiki_id"], "evidence_assessment", {
                "claim_id": cid, "evidence_id": evidence.record_id, "definition_snapshot_id": m["definition_snapshot_id"],
                "processing_context_id": m["processing_context_id"], "position": "pass", "support": j["support"]}))
        if not supported or (opposed and rp["lifecycle"] != "disputed"):
            raise ModelError("支持根拠がないか、反対根拠を disputed として提示していません")
    if used != judgments.keys():
        raise ModelError("対象候補にない支持判断が混ざっています")
    report, decision = _decision(m["wiki_id"], operation_id, public_request, proposal, m["activation_id"],
        m["processing_context_id"], targets, {"structure": {"state": "pass"}, "sources": {"state": "pass"},
        "manual_support": {"state": "pass", "reason": canonical(request["judgments"]).decode("utf-8")}}, base.release_id)
    records = _append(records.values(), [*assessments, report, decision])
    # 型・同一性の改訂に依存する旧評価は選択から外す。履歴と端点は変更しない。
    changed_entities = set(updates["entities"])
    replaced = {records[key].payload["previous_id"] for key in targets}
    selected = [key for key in m["current_assessments"] if records[key].payload["revision_id"] not in replaced
                and not any(records[r].payload["entity_id"] in changed_entities
                            for r in records[key].payload["dependencies"]["entities"])]
    for revision in sorted(targets):
        row = records[revision]
        is_claim = row.kind == "claim_revision"
        cp = spaces["claim"][row.payload["claim_id"]].payload if is_claim else None
        entities = {b["entity_id"] for b in cp["role_bindings"]} if is_claim else {row.payload["entity_id"]}
        current = Record.create(m["wiki_id"], "current_assessment", {
            "revision_id": revision, "activation_id": m["activation_id"], "processing_context_id": m["processing_context_id"],
            "decision_id": decision.record_id, "reports": [report.record_id],
            "dependencies": {"entities": sorted(heads["entities"][e] for e in entities),
                             "evidence": sorted({e["evidence_id"] for e in row.payload["evidence_links"]}) if is_claim else []},
            "compatibility": "compatible" if not is_claim or row.payload["lifecycle"] == "accepted" else "not_checked",
            "evidence_state": "verified" if is_claim else "not_checked"})
        records[current.record_id] = current
        selected.append(current.record_id)
    return store.snapshot(records.values(), wiki_id=m["wiki_id"], parent=m["semantic_snapshot_id"], heads=heads,
                          definition_snapshot_id=m["definition_snapshot_id"], activation_id=m["activation_id"],
                          processing_context_id=m["processing_context_id"], current_assessments=selected), list(inputs.reads.values())


def _make(guard, base, operation_id, request, specification):
    old = store.read_bundle(base.root / "ontology/ossie")
    fixed.verify_stored(WriteGuard(base.root, strict_root=True), base.root / "ontology", old)
    m = old.manifest
    operation = request.get("operation") if isinstance(request, dict) else None
    if not isinstance(operation, str) or operation not in KINDS:
        raise ModelError("未知の意味採用操作です")
    expected = {"operation", "actor", "reason", "recorded_at"}
    expected |= {"definition"} if operation == ACTIVATE else {
        "records", "heads", "judgments", "raw_root", "original_confirmations"}
    if request.keys() != expected:
        raise ModelError("採用操作の入力項目が一致しません")
    if operation == ADOPT and (not isinstance(request["records"], list) or not isinstance(request["judgments"], list)
            or not isinstance(request["original_confirmations"], list)):
        raise ModelError("候補・支持判断・原本性の確認には配列を指定してください")
    for field in ("actor", "reason", "recorded_at"):
        text(request[field], field)
    definition = fixed.FrozenDefinition.from_record(request["definition"]) if operation == ACTIVATE else _active_definition(old, base)
    contract = _contract(definition, specification)
    public_request = load_json(canonical(request))
    raw_guard = None
    if operation == ADOPT:
        raw_guard = _raw_guard(base, request["raw_root"])
        public_request.pop("raw_root")
        public_request["raw_root_id"] = fingerprint("registered-raw-root-v1", [m["wiki_id"], list(raw_guard.root_id)])
    proposal = fingerprint("semantic-proposal-v1", {"basis": base.release_id, "request": public_request, "operation_id": operation_id})
    reads = []
    if operation == ACTIVATE:
        aid = activation_id(m["wiki_id"], operation_id, m["activation_id"], definition.definition_snapshot_id)
        activation = Record.create(m["wiki_id"], "definition_activation", {"activation_id": aid,
            "definition_snapshot_id": definition.definition_snapshot_id, "previous_id": m["activation_id"], "operation_id": operation_id})
        report, decision = _decision(m["wiki_id"], operation_id, request, proposal, aid, definition.processing_context_id,
                                     [activation.record_id], {"official_and_capabilities": {"state": "pass"}}, base.release_id)
        records = _append(old.records, [*definition.records(m["wiki_id"]), activation, report, decision])
        value = store.snapshot(records.values(), wiki_id=m["wiki_id"], parent=m["semantic_snapshot_id"], heads=m["heads"],
            definition_snapshot_id=definition.definition_snapshot_id, activation_id=aid, processing_context_id=definition.processing_context_id)
    else:
        value, reads = _adopt(old, base, operation_id, request, contract, proposal, public_request, raw_guard)
    plan = {"operation": operation, "rule": "manual-adoption-v1", "plan_id": operation_id,
            "wiki": str(guard.root), "wiki_identity": list(guard.root_id),
            "publication": {"release_id": base.release_id, "definition_id": base.definition_id},
            "request": request, "proposal_sha256": proposal, "read_set": reads,
            "before_semantic_snapshot_id": m["semantic_snapshot_id"], "semantic_snapshot_id": value.manifest["semantic_snapshot_id"],
            "invalidated_assessments": sorted(set(m["current_assessments"]) - set(value.manifest["current_assessments"])),
            "heads": value.manifest["heads"], "rows": value.manifest["rows"],
            "definition": definition.plan_entry(), "activation_id": value.manifest["activation_id"]}
    return plan, value, definition


def _prepare(wiki, operation_id, request, capability, specification):
    publish.checked(operation_id, publish.PLAN_ID)
    request = load_json(canonical(request))
    guard = live.wiki_guard(wiki)
    base = publish.resolve_publication(guard.root)
    if base is None:
        raise ModelError("P3 の採用は形式2へ移行した公開版だけで準備できます")
    plan, _, _ = _make(guard, base, operation_id, request, specification)
    applicable, reason = True, None
    try:
        shared.require_capability(capability, operation=request["operation"], guard=guard, operation_id=operation_id)
        publish.check_publication_writer(guard, guard.root / publish.AREA)
        shared._unfinished(guard, operation_id)
    except (ModelError, publish.Conflict) as error:
        applicable, reason = False, str(error)
    return {"state": "prepared", "plan_id": operation_id, "plan": plan,
            "plan_sha256": approval(plan), "applicable": applicable, "reason": reason}


def prepare_activation(wiki, operation_id, definition, *, actor, reason, recorded_at, capability=None, specification=None):
    definition = definition if isinstance(definition, fixed.FrozenDefinition) else fixed.freeze(definition)
    return _prepare(wiki, operation_id, {"operation": ACTIVATE, "definition": definition.to_record(),
        "actor": actor, "reason": reason, "recorded_at": recorded_at}, capability, specification)


def prepare_adoption(wiki, operation_id, *, records, heads, judgments, raw_root, actor, reason, recorded_at,
                     original_confirmations=(), capability=None, specification=None):
    """準備時に割当済みの実体IDと固定レコードを受け、型・位置・手動判断を独立に照合する。"""
    return _prepare(wiki, operation_id, {"operation": ADOPT, "records": [r.to_dict() for r in records],
        "heads": heads, "judgments": judgments, "raw_root": str(Path(raw_root).absolute()),
        "original_confirmations": list(original_confirmations), "actor": actor, "reason": reason,
        "recorded_at": recorded_at}, capability, specification)


class AdoptionOperation:
    def __init__(self, kind, capability, specification):
        if not isinstance(kind, str) or kind not in KINDS:
            raise ModelError("未知の意味採用操作です")
        self.kind, self.capability, self.specification = kind, capability, specification

    def prepare(self, guard, record):
        plan = _approved_plan(record.get("plan"), self.kind, record["plan_id"], guard, record["approval"])
        shared.require_capability(self.capability, operation=self.kind, guard=guard, operation_id=record["plan_id"])
        base = save.base_view(guard, plan)
        active = publish.resolve_publication(guard.root)
        if active is None or active.release_id != base.release_id:
            raise publish.Conflict("承認時の基準公開版が進みました。意味採用は保留します")
        shared._unfinished(guard, record["plan_id"])
        try:
            computed, value, definition = _make(guard, base, record["plan_id"], plan["request"], self.specification)
        except (ModelError, OSError) as error:
            raise publish.Conflict(f"意味採用の入力を再検査してください: {error}") from error
        if computed != plan:
            raise publish.Conflict("意味採用の入力・処理条件が承認後に変わりました")
        files = save.snapshot_files(base)
        changed = {*shared.SOURCES.values(), *(f"ontology/definitions/{definition.definition_snapshot_id}/{name}"
                                               for name in definition.files())}
        result = {"plan_id": record["plan_id"], "approval": record["approval"], "attempt": record["attempt"],
                  "operation": self.kind, "files": sorted(changed), "log_entries": [],
                  "base_release_id": base.release_id, "semantic_snapshot_id": plan["semantic_snapshot_id"]}

        def verify_inputs():
            try:
                _check_reads(plan["read_set"], base)
                _contract(definition, self.specification)
            except (ModelError, OSError) as error:
                raise publish.Conflict(f"公開直前の意味採用入力が変わりました: {error}") from error

        def build(destination):
            save.copy_candidate(base, files, destination)
            with store.artifacts(value) as folder:
                for path in sorted(folder.iterdir()):
                    live.write(destination, destination.root / "ontology/ossie" / path.name, path.read_bytes(), replace=True)
                live.write(destination, destination.root / shared.CATALOG, (folder.parent / "definition.json").read_bytes(), replace=True)
            fixed.write(destination, destination.root / "ontology", definition)
            live.write(destination, destination.root / save.RESULT, publish.encoded(result), replace=True)
            if store.read_bundle(destination.root / "ontology/ossie") != value:
                raise ModelError("意味採用の候補を読み直せません")
            fixed.verify_stored(destination, destination.root / "ontology", value)

        transition = shared.Transition(self.capability, self.kind, record["plan_id"], plan["semantic_snapshot_id"],
                                       base.release_id, verify_inputs)
        return SimpleNamespace(base=base, active=base, semantic=transition, rebase=False, build=build,
                               before_journal=lambda: None)


def queue(wiki, operation, operation_id, expected, *, plan=None, capability=None, specification=None,
          timeout=180, checkpoint=lambda _: None):
    """入力は承認された計画。完了の照合を、新しい定義・根拠の検査より先に行う。"""
    publish.checked(operation_id, publish.PLAN_ID); publish.checked(expected)
    guard = live.wiki_guard(wiki)
    handler = AdoptionOperation(operation, capability, specification)
    shared.require_capability(capability, operation=operation, guard=guard, operation_id=operation_id)
    with save.save_lock(guard, operation_id, timeout):
        with save.pending_lock(guard, timeout):
            save.clear_record_temp(guard, save.record_path(guard, operation_id))
            record = save.record_for_retry(guard, operation_id, expected)
            new = record is None
            if new:
                if not isinstance(plan, dict) or plan.get("operation") != operation or plan.get("plan_id") != operation_id:
                    raise publish.Conflict("承認した意味採用の計画がありません")
                plan = load_json(canonical(plan))
                if plan.get("wiki") != str(guard.root) or plan.get("wiki_identity") != list(guard.root_id) or approval(plan) != expected:
                    raise publish.Conflict("意味採用の操作・wiki・承認が一致しません")
                _approved_plan(plan, operation, operation_id, guard, expected)
                publish.check_publication_writer(guard, guard.root / publish.AREA)
                shared._unfinished_locked(guard, operation_id)
                record = {"wiki": str(guard.root), "wiki_identity": list(guard.root_id), "plan_id": operation_id,
                          "approval": expected, "attempt": operation_id, "operation": operation, "plan": plan}
                save.write_record(guard, record, replace=False)
        if new:
            checkpoint("save_queued")
        return save.run_save(guard, record, timeout, checkpoint, operation=handler)


def resume(wiki, operation, operation_id, expected, *, capability=None, specification=None, timeout=180,
           checkpoint=lambda _: None):
    return queue(wiki, operation, operation_id, expected, capability=capability, specification=specification,
                 timeout=timeout, checkpoint=checkpoint)
