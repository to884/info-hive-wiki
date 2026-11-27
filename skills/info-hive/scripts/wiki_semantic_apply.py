"""P3 の migrate_format の適用。共通の公開保存（wiki_public_save と wiki_apply）の記録・ロック・日誌・完了記録・復旧に載せる。

独立した公開トランザクションや、CLI・MCP・環境変数・設定ファイルからの有効化は持たない。
配布版の SEMANTIC_WRITES_ENABLED は False に固定し、親リポジトリの隔離試験が渡す能力がない限り、
承認・受付・候補の作成・公開の各段で拒否する。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import stat
from types import SimpleNamespace

import wiki_apply as publish
import wiki_live_apply as live
import wiki_public_save as save
import wiki_semantic_definitions as fixed
import wiki_semantic_migrate as migrate
import wiki_semantic_store as store
from wiki_fsguard import WriteGuard
from wiki_semantic_format import detect
from wiki_semantic_gate import require_distributed_writes, require_isolated
from wiki_semantic_model import ModelError


OPERATION = "migrate_format"
RULE = "migration-preview-v2"
SUPPORTED = frozenset({OPERATION, "activate_definition", "adopt_semantics"})
SEMANTIC_DIR = "ontology/ossie/"
CATALOG = "ontology/definition.json"
# プレビューが読む元ファイル（報告書の sources のキー）と、公開版の中の位置。
SOURCES = {"records.jsonl": "ontology/ossie/records.jsonl", "release.json": "ontology/ossie/release.json",
           "wiki-data.sqlite": "ontology/ossie/wiki-data.sqlite",
           "wiki.ontology.json": "ontology/ossie/wiki.ontology.json",
           "wiki.semantic_model.json": "ontology/ossie/wiki.semantic_model.json",
           "wiki.mapping.json": "ontology/ossie/wiki.mapping.json", CATALOG: CATALOG}
# 移行で変わってよいファイル。記事・索引・グラフ等は基準のまま写す。
CHANGEABLE = frozenset({CATALOG, save.RESULT.as_posix()})
CHANGEABLE_PREFIXES = (SEMANTIC_DIR, "ontology/" + fixed.FOLDER + "/")
WRITER_CHECKS = ("publication_mode", "no_publication_journal", "no_live_writer", "no_live_return",
                 "no_other_pending_saves", "no_live_pending")


def require_capability(capability, *, operation, guard, operation_id):
    """配布版では常に拒否する。隔離試験の能力が、この wiki の実体・操作・操作 ID を許可した場合だけ通す。"""
    if operation not in SUPPORTED:
        raise ModelError(f"扱わない意味操作です: {operation}")
    if capability is None:
        require_distributed_writes()
    require_isolated(capability, operation=operation, guard=guard, operation_id=operation_id)


@dataclass(frozen=True)
class Transition:
    """公開計画とは別に渡す、形式1→2の遷移の照合条件。計画・マニフェスト・日誌のキーは変えない。"""
    capability: object
    operation: str
    operation_id: str
    semantic_snapshot_id: str
    base_release_id: str
    validate_inputs: object = None


def approval(plan):
    return publish.digest(publish.encoded({"operation": OPERATION, "plan": plan}))


def _freeze_all(values):
    frozen = []
    for value in values:
        frozen.append(value if isinstance(value, fixed.FrozenDefinition) else fixed.freeze(value))
    ids = [f.definition_snapshot_id for f in frozen]
    if len(ids) != len(set(ids)):
        raise ModelError("同じ定義内容を重ねて保存できません")
    return tuple(sorted(frozen, key=lambda f: f.definition_snapshot_id))


def _plan(guard, base, operation_id, frozen):
    """基準公開版の固定入力から、決定的に移行計画と意味スナップショットを作る。書込みはしない。"""
    files = save.snapshot_files(base)
    unknown = sorted(name for name in files if (name.startswith(SEMANTIC_DIR) and name not in SOURCES.values())
                     or name.startswith(CHANGEABLE_PREFIXES[1]))
    if unknown:
        raise ModelError(f"移行元に未対応の意味ファイルがあります: {unknown[0]}")
    preview = migrate.preview_legacy(base.root / "ontology/ossie", operation_id=operation_id,
                                     basis={"mode": "publication", "release_id": base.release_id})
    report = preview.report
    if report["sources"].keys() != SOURCES.keys() or any(
            files.get(SOURCES[key]) != checksum for key, checksum in report["sources"].items()):
        raise ModelError("プレビューが読んだ入力と基準公開版のマニフェストの指紋が一致しません")
    value = preview.value
    if frozen:
        manifest = value.manifest
        records = {r.record_id: r for r in value.records}
        for definition in frozen:
            for record in definition.records(manifest["wiki_id"]):
                if record.record_id in records and records[record.record_id] != record:
                    raise ModelError("同じ ID に異なる記録を保存できません")
                records[record.record_id] = record
        value = store.snapshot(records.values(), wiki_id=manifest["wiki_id"], heads=manifest["heads"])
    plan = {"operation": OPERATION, "rule": RULE, "plan_id": operation_id, "wiki": str(guard.root),
            "wiki_identity": list(guard.root_id),
            "publication": {"release_id": base.release_id, "definition_id": base.definition_id},
            "inputs": {path: files[path] for path in sorted(SOURCES.values())},
            "preview_sha256": report["preview_sha256"], "preview_snapshot_id": report["semantic_snapshot_id"],
            "semantic_snapshot_id": value.manifest["semantic_snapshot_id"], "rows": value.manifest["rows"],
            "legacy_rows": report["jsonl_rows"], "diagnostics": report["diagnostics"],
            "domain_definition": "not_configured", "activation": None,
            "definitions": [f.plan_entry() for f in frozen], "writer_checks": list(WRITER_CHECKS)}
    return plan, value, files


def _unfinished_locked(guard, own_plan_id):
    others = [item["plan_id"] for item in save.pending_public_locked(guard) if item["plan_id"] != own_plan_id]
    if others:
        raise publish.Conflict("未完了の承認済み保存があります。完了か破棄を決めてから移行してください")
    if live.pending_blockers(guard):
        raise publish.Conflict("保留されたライブの承認済み差分があります。処理してから移行してください")


def _unfinished(guard, own_plan_id):
    with save.pending_lock(guard):
        _unfinished_locked(guard, own_plan_id)


@contextmanager
def commit_gate(transition, guard):
    """公開ロックの内側で受付を止め、検査と日誌確定の間を不可分にする。"""
    if type(transition) is not Transition:
        raise ModelError("移行の照合条件が不正です")
    require_capability(transition.capability, operation=transition.operation, guard=guard,
                       operation_id=transition.operation_id)
    with save.pending_lock(guard):
        _unfinished_locked(guard, transition.operation_id)
        yield lambda: _unfinished_locked(guard, transition.operation_id)


def prepare_migration(wiki, operation_id, *, definitions=(), capability=None):
    """移行の候補と承認用の指紋を返す。wiki と公開状態は変更しない。"""
    publish.checked(operation_id, publish.PLAN_ID)
    guard = live.wiki_guard(wiki)
    frozen = _freeze_all(definitions)
    base = publish.resolve_publication(guard.root)
    if base is None:
        preview = migrate.preview_legacy(guard.root / "ontology/ossie", operation_id=operation_id,
                                         basis={"mode": "live", "live_base_sha256": publish.capture_live_base(guard.root)})
        return {"state": "preview_only", "applicable": False, "reason": "publication_required",
                "plan_id": operation_id, "report": preview.report}
    plan, _, _ = _plan(guard, base, operation_id, frozen)
    result = {"state": "prepared", "applicable": True, "reason": None, "plan_id": operation_id,
              "plan_sha256": approval(plan), "plan": plan}
    try:
        require_capability(capability, operation=OPERATION, guard=guard, operation_id=operation_id)
        publish.check_publication_writer(guard, guard.root / publish.AREA)
        _unfinished(guard, operation_id)
    except (ModelError, publish.Conflict) as error:
        # 準備は読むだけ。受付・適用の段で同じ条件を改めて照合する。
        result.update(applicable=False, reason=str(error))
    return result


def _approved(record):
    plan = record.get("plan")
    stored = record.get("definition_files")
    if (record.get("operation") != OPERATION or not isinstance(plan, dict) or plan.get("operation") != OPERATION
            or plan.get("plan_id") != record["plan_id"] or plan.get("wiki") != record["wiki"]
            or plan.get("wiki_identity") != record["wiki_identity"] or not isinstance(stored, list)):
        raise publish.PublicationError("承認済みの移行の記録と計画が一致しません")
    frozen = _freeze_all(fixed.FrozenDefinition.from_record(item) for item in stored)
    if [f.plan_entry() for f in frozen] != plan["definitions"] or approval(plan) != record["approval"]:
        raise publish.PublicationError("保留記録の移行計画が承認ハッシュと一致しません")
    return plan, frozen


class MigrationOperation:
    """wiki_public_save.run_save に渡す、移行専用の入力検査・前提確認・候補作成。"""
    kind = OPERATION

    def __init__(self, capability, specification=None):
        self.capability = capability
        self.specification = specification

    def prepare(self, guard, record):
        plan, frozen = _approved(record)
        plan_id = record["plan_id"]
        require_capability(self.capability, operation=OPERATION, guard=guard, operation_id=plan_id)
        area = guard.root / publish.AREA
        base = save.base_view(guard, plan)
        active = publish.resolve_publication(guard.root)
        if active is None or active.release_id != base.release_id:
            # 初期の意味操作は当て直さない。新しい基準でプレビューと承認を取り直す。
            raise publish.Conflict("承認時の基準公開版から公開版が進みました。移行は保留します")
        publish.check_publication_writer(guard, area)
        _unfinished(guard, plan_id)
        recomputed, value, files = _plan(guard, active, plan_id, frozen)
        if recomputed != plan:
            raise publish.Conflict("基準公開版から作り直した移行計画が承認と一致しません。承認を取り直してください")
        for item in frozen:
            if fixed.reinspect(item, self.specification) != item.processing_context_id:
                raise publish.Conflict("定義内容は同じですが、処理条件が承認時から変わりました。承認を取り直してください")
        changed = sorted({*SOURCES.values(), *(f"ontology/{fixed.FOLDER}/{item.definition_snapshot_id}/{name}"
                                               for item in frozen for name in item.files())})
        result_info = {"plan_id": plan_id, "approval": record["approval"], "attempt": record["attempt"],
                       "operation": OPERATION, "files": changed, "log_entries": [],
                       "base_release_id": base.release_id, "semantic_snapshot_id": plan["semantic_snapshot_id"]}
        transition = Transition(self.capability, OPERATION, plan_id, plan["semantic_snapshot_id"], base.release_id)

        def build(destination):
            save.copy_candidate(active, files, destination)
            ontology = destination.root / "ontology"
            with store.artifacts(value) as folder:
                for path in sorted(folder.iterdir()):
                    target = ontology / "ossie" / path.name
                    live.write(destination, target, path.read_bytes(), replace=destination.file_info(target) is not None)
                live.write(destination, ontology / "definition.json", (folder.parent / "definition.json").read_bytes(),
                           replace=True)
            for item in frozen:
                fixed.write(destination, ontology, item)
            live.write(destination, destination.root / save.RESULT, publish.encoded(result_info),
                       replace=save.RESULT.as_posix() in files)
            if store.read_bundle(ontology / "ossie") != value:
                raise publish.PublicationError("作成した移行候補を読み直せません")
            fixed.verify_stored(destination, ontology, value)

        def before_journal():
            require_capability(self.capability, operation=OPERATION, guard=guard, operation_id=plan_id)

        return SimpleNamespace(base=base, active=active, semantic=transition, rebase=False,
                               build=build, before_journal=before_journal)


def verify_transition(transition, *, wiki, candidate, base, base_release_id):
    """wiki_apply から呼び、操作に応じた成果物の差分の範囲を照合する。"""
    if type(transition) is not Transition:
        raise ModelError("形式の遷移には移行で作った照合条件を指定してください")
    guard = WriteGuard(Path(wiki).absolute(), strict_root=True)
    require_capability(transition.capability, operation=transition.operation, guard=guard,
                       operation_id=transition.operation_id)
    if base is None or base_release_id != transition.base_release_id:
        raise publish.Conflict("移行の基準公開版が変わりました。移行は保留します")
    base_guard = WriteGuard(Path(base), strict_root=True)
    candidate_guard = WriteGuard(Path(candidate), strict_root=True)
    expected_format = 1 if transition.operation == OPERATION else 2
    if detect(base_guard) != expected_format or detect(candidate_guard) != 2:
        raise ModelError("P3 の意味操作の基準形式と候補形式が一致しません")
    value = store.read_bundle(candidate_guard.root / "ontology/ossie")
    if value.manifest["semantic_snapshot_id"] != transition.semantic_snapshot_id:
        raise ModelError("公開候補の意味スナップショットが承認と一致しません")
    folder = candidate_guard.root / "ontology/ossie"
    documents = store.exchange_documents()
    expected = {"records.jsonl", "release.json", "wiki-data.sqlite", *documents}
    actual = {path.relative_to(folder).as_posix() for path, info in candidate_guard.tree_entries(folder)
              if not stat.S_ISDIR(info.st_mode)}
    if actual != expected:
        raise ModelError("移行候補の意味成果物のファイル集合が生成物と一致しません")
    for name, content in documents.items():
        if candidate_guard.read_bytes(folder / name) != content:
            raise ModelError(f"移行候補の交換用文書が正規の生成物と一致しません: {name}")
    fixed.verify_stored(candidate_guard, candidate_guard.root / "ontology", value)
    if transition.operation != OPERATION:
        previous = store.read_bundle(base_guard.root / "ontology/ossie")
        if value.manifest["parent"] != previous.manifest["semantic_snapshot_id"]:
            raise ModelError("意味版の親が基準版と一致しません")
        if not set(previous.records) <= set(value.records):
            raise ModelError("意味操作で過去の固定レコードを削除・変更できません")
        if not callable(transition.validate_inputs):
            raise ModelError("意味採用には承認入力の最終照合が必要です")
        transition.validate_inputs()
    before, after = publish.file_checksums(base_guard), publish.file_checksums(candidate_guard)
    changeable = CHANGEABLE
    if transition.operation != OPERATION:
        from wiki_graph_extract import OUTPUT_NAMES, SEMANTIC_REPORT

        graph_files = {"graph/" + name for name in OUTPUT_NAMES}
        if "graph/" + SEMANTIC_REPORT not in after or (before.keys() & graph_files) - after.keys():
            raise ModelError("公開候補の互換グラフの成果物がそろっていません")
        changeable = changeable | graph_files
    for name in before.keys() | after.keys():
        if before.get(name) != after.get(name) and name not in changeable and not name.startswith(CHANGEABLE_PREFIXES):
            raise ModelError(f"候補が承認した操作の成果物以外を変えています: {name}")
    if any(name.startswith(SEMANTIC_DIR) for name in before.keys() - after.keys()):
        raise ModelError("移行の候補に旧形式の成果物の置換漏れがあります")


def queue_migration(wiki, operation_id, expected, *, capability, definitions=(), specification=None,
                    timeout=180, checkpoint=lambda _: None):
    """承認した移行を共通の保存記録に固定し、共通の公開経路で適用する。同じ操作の再送は既存の結果を返す。"""
    publish.checked(expected)
    publish.checked(operation_id, publish.PLAN_ID)
    guard = live.wiki_guard(wiki)
    require_capability(capability, operation=OPERATION, guard=guard, operation_id=operation_id)
    with save.save_lock(guard, operation_id, timeout):
        with save.pending_lock(guard, timeout):
            save.clear_record_temp(guard, save.record_path(guard, operation_id))
            record = save.record_for_retry(guard, operation_id, expected)
            new = record is None
            if new:
                # 過去の承認・完了の照合を、現在の入力や処理条件の検査より先に行う。
                frozen = _freeze_all(definitions)
                area = guard.root / publish.AREA
                publish.check_publication_writer(guard, area)
                others = [item for item in save.pending_public_locked(guard) if item["plan_id"] != operation_id]
                if others or live.pending_blockers(guard):
                    raise publish.Conflict("未完了の承認済み保存があります。完了か破棄を決めてから移行してください")
                base = publish.resolve_publication(guard.root)
                if base is None:
                    raise publish.Conflict("公開版がない wiki では移行候補の準備までを扱います")
                plan, _, _ = _plan(guard, base, operation_id, frozen)
                if approval(plan) != expected:
                    raise publish.Conflict("承認した移行計画の指紋が一致しません。プレビューを取り直してください")
                record = {"wiki": str(guard.root), "wiki_identity": list(guard.root_id), "plan_id": operation_id,
                          "approval": expected, "attempt": operation_id, "operation": OPERATION, "plan": plan,
                          "definition_files": [f.to_record() for f in frozen]}
                save.write_record(guard, record, replace=False)
        if new:
            checkpoint("save_queued")
        return save.run_save(guard, record, timeout, checkpoint, operation=MigrationOperation(capability, specification))


def resume_migration(wiki, operation_id, expected, *, capability, specification=None, timeout=180,
                     checkpoint=lambda _: None):
    guard = live.wiki_guard(wiki)
    publish.checked(expected)
    require_capability(capability, operation=OPERATION, guard=guard, operation_id=operation_id)
    with save.save_lock(guard, operation_id, timeout):
        record = save.require_queued_record(guard, operation_id, expected, timeout)
        return save.run_save(guard, record, timeout, checkpoint, operation=MigrationOperation(capability, specification))
