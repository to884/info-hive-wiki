"""公開版を使う wiki に、承認済みの記事・索引・ログを保存する。"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid

import wiki_apply as publish
import wiki_live_apply as live
from wiki_fsguard import Refused, WriteGuard
from wiki_lint import parse_frontmatter
from wiki_pages import clean_link_target, extract_wikilinks, is_page_path, link_candidates, ref_link, ref_values
from wiki_raw import resolve_raw
from wiki_raw_roots import read_raw_roots, record_raw_root


SAVES = publish.AREA / "saves"
RESULT = Path("ontology/wiki-save.json")
ATTEMPTS = 3


def base_view(guard, plan):
    selection = plan.get("publication")
    if not isinstance(selection, dict) or selection.keys() != {"release_id", "definition_id"}:
        raise publish.PublicationError("基準の公開版を指定してください")
    try:
        base = publish.resolve_publication(guard.root, selection["release_id"])
    except publish.RetiredPublication as error:
        raise publish.Conflict("承認時の公開版は取り下げられました。現在の版で承認を取り直してください") from error
    if base.definition_id != selection["definition_id"]:
        raise publish.Conflict("計画の定義版が一致しません")
    return base


def ensure_draft(guard, plan):
    if guard.file_info(record_path(guard, plan["plan_id"])) is not None:
        raise publish.Conflict("承認済みの保存です。再試行するか、新しい計画を begin で用意してください")


def register_dependency(guard, plan, name):
    publish.relative(name)
    base = base_view(guard, plan)
    if name not in snapshot_files(base):
        raise publish.Conflict("基準の公開版のマニフェストにある相対パスを指定してください")
    if name not in plan["dependencies"]:
        plan["dependencies"].append(name)
    return {"state": "registered", "path": name}


def public_preview(work, plan, guard):
    drafts, checksum = live.snapshot(work, plan)
    base = base_view(guard, plan)
    live.protect_drafts(guard, plan, drafts, view=base)
    changes, _ = live.calculate_view(WriteGuard(base.root), plan, drafts, apply=True)
    return {"plan_sha256": checksum, "diff": live.diff_text(changes), "release_id": base.release_id}


def record_path(guard, plan_id):
    return guard.root / SAVES / f"{publish.checked(plan_id, publish.PLAN_ID)}.json"


def record_temp(path):
    return path.with_name(f".wiki-save-{path.stem}.tmp")


def clear_record_temp(guard, path):
    """保留受付の共通ロック内で、公開途中の一時名を回収する。"""
    if guard.clear_exclusive_temp(record_temp(path), path):
        publish.sync_directory(path.parent)
        publish.sync_directory(path.parent.parent)


def save_lock(guard, plan_id, timeout):
    path = record_path(guard, plan_id)
    guard.ensure_directory(path.parent)
    return guard.hold_lock(path.with_suffix(".lock"), timeout=timeout)


def pending_lock(guard, timeout=live.WAIT_SECONDS):
    return guard.hold_lock(guard.root / publish.AREA / "pending.lock", timeout=timeout)


def load_record(guard, plan_id, checksum=None):
    record = live.load(guard, record_path(guard, plan_id))
    if record is not None:
        if (not isinstance(record, dict) or record.get("plan_id") != plan_id
                or record.get("wiki") != str(guard.root) or record.get("wiki_identity") != list(guard.root_id)):
            raise publish.PublicationError("保留記録がこの wiki の保存と一致しません")
        publish.checked(record.get("attempt"), publish.PLAN_ID)
        publish.checked(record.get("approval"))
        if checksum is not None and record["approval"] != checksum:
            raise publish.Conflict("この保存の承認ハッシュと一致しません")
    return record


def write_record(guard, record, *, replace=True):
    path = record_path(guard, record["plan_id"])
    guard.write_bytes(path, publish.encoded(record), replace=replace, delays=live.DELAYS, mode=0o600,
                      write_through=True, temporary=None if replace else record_temp(path))
    publish.sync_directory(path.parent)
    if not replace:
        # ロックの確保時に saves/ ができる場合も、最初の受付より先に名前を確定させる。
        publish.sync_directory(path.parent.parent)


def freeze(work, plan, guard, expected):
    drafts, checksum = live.snapshot(work, plan)
    if checksum != expected:
        raise publish.Conflict("承認した計画の指紋が一致しません")
    record = {"wiki": str(guard.root), "wiki_identity": list(guard.root_id), "plan_id": plan["plan_id"],
              "approval": checksum, "attempt": plan["plan_id"], "plan": plan,
              "drafts": {name: [live.pack(before), live.pack(after)] for name, (before, after) in drafts.items()}}
    write_record(guard, record, replace=False)
    return record


def approved_drafts(record):
    plan = record["plan"]
    if (not isinstance(plan, dict) or plan.get("plan_id") != record["plan_id"]
            or plan.get("wiki") != record["wiki"] or plan.get("wiki_identity") != record["wiki_identity"]
            or plan["pages"].keys() != record["drafts"].keys()):
        raise publish.PublicationError("承認済みの下書きと計画が一致しません")
    drafts, fingerprints = {}, {}
    for name, pair in record["drafts"].items():
        live.page_path(name)
        before, after = map(live.unpack, pair)
        entry = plan["pages"][name]
        if (after is None or not after.strip() or entry["kind"] not in {"new", "edit"}
                or (entry["kind"] == "edit") != (before is not None)
                or (publish.digest(before) if before is not None else None) != entry["before"]):
            raise publish.PublicationError(f"下書きの基準または内容が一致しません: {name}")
        after.decode("utf-8-sig")
        drafts[name] = before, after
        fingerprints[name] = publish.digest(after)
    if publish.digest(publish.encoded({"plan": plan, "drafts": fingerprints})) != record["approval"]:
        raise publish.PublicationError("保留記録の内容が承認ハッシュと一致しません")
    return plan, drafts


def snapshot_files(view):
    guard = WriteGuard(view.wiki)
    return publish.manifest(guard, guard.root / publish.AREA, view.release_id)["files"]


def referenced_pages(names, files):
    by_path = defaultdict(list)
    by_slug = defaultdict(list)
    for name, checksum in files.items():
        if name.lower().endswith(".md"):
            path = clean_link_target(name)
            by_path[path].append((name, checksum))
            by_slug[path.rsplit("/", 1)[-1]].append((name, checksum))
    return {name: sorted(link_candidates(name, by_path, by_slug)) for name in names}


def verify_basis(plan, drafts, base, active, files):
    if active.definition_id != base.definition_id:
        raise publish.Conflict("定義版が変わりました。変更を読み直して承認を取り直してください")
    if active.release_id == base.release_id:
        return
    original = snapshot_files(base)
    dependencies = set(plan["dependencies"])
    for op in plan["operations"]:
        if op["kind"] == "log-sub":
            dependencies.add("log.md")
        elif op["kind"] == "index-add" and op["nth"] is not None:
            dependencies.add(op["file"])
    if any(original.get(path) is None or original[path] != files.get(path) for path in dependencies):
        raise publish.Conflict("読んだページか操作の対象が変わりました。承認を取り直してください")
    from wiki_graph_extract import parse_frontmatter as parse_graph_frontmatter

    refs = set()
    for _, after in drafts.values():
        meta, body = parse_graph_frontmatter(after.decode("utf-8-sig"))
        refs.update(extract_wikilinks(body))
        for key in ("sources", "sources_consulted", "entities", "concepts"):
            refs.update(ref_link(ref) for ref in ref_values(meta.get(key)))
        relationships = meta.get("graph", {}).get("relationships")
        if isinstance(relationships, list):
            for relation in relationships:
                if isinstance(relation, dict) and isinstance(relation.get("source"), str):
                    refs.add(ref_link(relation["source"]))
    if referenced_pages(refs, original) != referenced_pages(refs, files):
        raise publish.Conflict("下書きから参照したページが変わりました。承認を取り直してください")


def verify_new_slugs(drafts, files):
    occupied = {clean_link_target(name).rsplit("/", 1)[-1].casefold() for name in files if is_page_path(name)}
    for name, (before, _) in drafts.items():
        if before is None:
            key = clean_link_target(name).rsplit("/", 1)[-1].casefold()
            if key in occupied:
                raise publish.Conflict(f"新規ページのスラッグがすでに使われています: {name}")
            occupied.add(key)


def verify_raw_versions(guard, plan, drafts):
    historical, failures = read_raw_roots(guard.root)
    if failures:
        raise publish.PublicationError("原本の根を確認できません")
    for name, (_, after) in drafts.items():
        meta, _, _ = parse_frontmatter(after.decode("utf-8-sig"))
        if not meta.get("raw_sha256"):
            continue
        hashes = ref_values(meta["raw_sha256"])
        if len(hashes) != len(ref_values(meta.get("raw"))) or not all(publish.ID.fullmatch(h.lower()) for h in hashes):
            raise publish.Conflict(f"下書きの原本と SHA-256 の組が一致しません: {name}")
        results = resolve_raw(meta, guard.root / name, guard.root, raw_roots=(Path(plan["raw_root"]),),
                              recorded_raw_roots=historical)
        # RAG が預かった範囲外の原本は従来の境界に従う。ジョブの承認済み指紋を記事に保持する。
        if not results or any(result.state != "resolved" or result.version_unverified for result in results):
            raise publish.Conflict(f"下書きの原本の版を確かめられません: {name}")
    if Path(plan["raw_root"]) not in historical:
        record_raw_root(guard.root, Path(plan["raw_root"]), writer="wiki_apply")


def copy_candidate(active, files, destination):
    source = WriteGuard(active.root, strict_root=True)
    for name, checksum in files.items():
        path = publish.relative(name)
        content = publish.read(source, source.root / path)
        if publish.digest(content) != checksum:
            raise publish.PublicationError(f"基準の公開版のファイルが変わりました: {name}")
        live.write(destination, destination.root / path, content)


def build_candidate(active, files, changes, receipt, destination):
    from wiki_public_projection import update_projections

    copy_candidate(active, files, destination)
    for name, (before, after) in changes.items():
        live.write(destination, destination.root / publish.relative(name), after, replace=before is not None)
    update_projections(destination, changes)
    live.write(destination, destination.root / RESULT, publish.encoded(receipt), replace=RESULT.as_posix() in files)


def confirmed(guard, record, *, compact=True, expected_operation=None):
    applied = publish.read_receipt(guard.root, record["attempt"])
    if applied is None:
        if record.get("completed"):
            raise publish.PublicationError("公開した保存の完了記録がありません")
        return None
    # 完了記録は公開履歴と成果物を照合済み。配信可否に関係なく、ここでは完了の情報だけを得る。
    area = guard.root / publish.AREA
    version = applied["release_id"]
    entry = publish.manifest(guard, area, version)
    result_bytes = publish.read(guard, area / "releases" / version / "wiki" / RESULT)
    if publish.digest(result_bytes) != entry["files"].get(RESULT.as_posix()):
        raise publish.PublicationError("公開した保存の結果の指紋が一致しません")
    result = json.loads(result_bytes)
    if (not isinstance(result, dict) or result.get("attempt") != record["attempt"]
            or result.get("plan_id") != record["plan_id"] or result.get("approval") != record["approval"]):
        raise publish.PublicationError("公開した保存の記録と承認が一致しません")
    if expected_operation is not None and result.get("operation", "pages") != expected_operation:
        raise publish.PublicationError("完了した保存の操作種別が再開の入口と一致しません")
    if compact and not record.get("completed"):
        compact = {key: record[key] for key in ("wiki", "wiki_identity", "plan_id", "approval", "attempt")}
        write_record(guard, {**compact, "completed": True})
    return {"state": "already_applied", "release_id": applied["release_id"], "plan_id": record["plan_id"],
            "plan_sha256": record["approval"], "files": result["files"], "log_entries": result["log_entries"]}


class PagesOperation:
    """記事・索引・ログの保存。操作ごとの入力検査・前提確認・候補作成だけを持ち、公開と復旧は run_save が共有する。"""
    kind = "pages"

    def prepare(self, guard, record):
        plan, drafts = approved_drafts(record)
        base = base_view(guard, plan)
        active = publish.resolve_publication(guard.root)
        if active is None:
            raise publish.PublicationError("公開中の版がありません")
        files = snapshot_files(active)
        live.protect_drafts(guard, plan, drafts, view=active)
        verify_basis(plan, drafts, base, active, files)
        verify_new_slugs(drafts, files)
        changes, logs = live.calculate_view(WriteGuard(active.root), plan, drafts, apply=True)
        if not changes:
            raise publish.Conflict("適用する差分がありません")
        result_info = {"plan_id": record["plan_id"], "approval": record["approval"], "attempt": record["attempt"],
                       "files": list(changes), "log_entries": logs,
                       "article_versions": {name: {"before": publish.digest(before) if before is not None else None,
                                                   "after": publish.digest(after)}
                                            for name, (before, after) in changes.items() if is_page_path(name)}}
        return SimpleNamespace(base=base, active=active, semantic=None, rebase=True,
                               build=lambda destination: build_candidate(active, files, changes, result_info, destination),
                               before_journal=lambda: verify_raw_versions(guard, plan, drafts))


def operation_for(record, operation):
    """記録の種類と再開の入口を突き合わせる。意味操作の記録は、通常の入口では再開しない。"""
    kind = record.get("operation", "pages")
    if record.get("completed"):
        # 完了の照合だけを行う。新しい書込みはしない。
        return operation or PagesOperation()
    if operation is None:
        if kind != "pages":
            raise publish.PublicationError("意味操作の承認済み保存は、この入口では再開できません")
        return PagesOperation()
    if operation.kind != kind:
        raise publish.PublicationError("承認済み保存の種類と再開の入口が一致しません")
    return operation


def run_save(guard, record, timeout, checkpoint, operation=None):
    if record.get("discarded"):
        raise publish.Conflict("この承認済み保存は破棄されています")
    operation = operation_for(record, operation)
    expected_operation = None if operation.kind == "pages" else operation.kind
    area = guard.root / publish.AREA
    cause = None
    for _ in range(ATTEMPTS):
        try:
            publish.recover(guard.root, timeout=timeout)
            result = confirmed(guard, record, expected_operation=expected_operation)
            if result:
                return result
            if guard.file_info(area / "rejected" / f"{record['attempt']}.json") is not None:
                record["attempt"] = uuid.uuid4().hex
                write_record(guard, record)
            prepared = operation.prepare(guard, record)
            parent = area / "save-work"
            guard.ensure_directory(parent)
            with TemporaryDirectory(dir=parent, prefix="candidate-", ignore_cleanup_errors=True) as temp:
                destination = WriteGuard(Path(temp), strict_root=True)
                prepared.build(destination)
                candidate = publish.build_plan(guard.root, destination.root, base=prepared.active,
                                               plan_id=record["attempt"], semantic=prepared.semantic)
                checkpoint("save_prepared")

                def check(point):
                    if point == "before_journal":
                        if guard.file_info(area / "retired" / f"{prepared.base.release_id}.json") is not None:
                            raise publish.Conflict("承認時の公開版は取り下げられました。現在の版で承認を取り直してください")
                        prepared.before_journal()
                    checkpoint(point)

                try:
                    publish.apply_plan(guard.root, destination.root, candidate, publish.digest(publish.encoded(candidate)),
                                       checkpoint=check, timeout=timeout, semantic=prepared.semantic)
                except publish.Conflict:
                    latest = publish.resolve_publication(guard.root)
                    if prepared.rebase and latest and latest.release_id != prepared.active.release_id:
                        cause = "並行する公開によって基準版が変わりました"
                        continue
                    raise
            result = confirmed(guard, record, expected_operation=expected_operation)
            if result is None:
                raise publish.PublicationError("保存の公開結果を照合できません")
            return {**result, "state": "applied"}
        except publish.Conflict as error:
            cause = str(error)
            break
        except Refused as error:
            if error.kind != "busy":
                raise
            cause = str(error)
            break
    if record.get("reason") != cause:
        record["reason"] = cause
        write_record(guard, record)
    return {"state": "pending", "plan_id": record["plan_id"], "plan_sha256": record["approval"], "reason": cause}


def record_for_retry(guard, plan_id, expected):
    from wiki_restore_content import completed_public_save

    record = load_record(guard, plan_id, expected)
    if record is None or not (record.get("completed") or record.get("discarded")):
        completed = completed_public_save(guard, plan_id, expected)
        if completed:
            write_record(guard, completed)
            return completed
    return record


def queue_save(directory, expected, *, timeout=180, checkpoint=lambda _: None):
    publish.checked(expected)
    work = live.work_directory(directory)
    with work.hold_lock(work.root / "plan.lock", timeout=timeout):
        plan = live.read_plan(work)
        guard = live.plan_wiki(plan)
        plan_id = plan["plan_id"]
        with save_lock(guard, plan_id, timeout):
            with pending_lock(guard, timeout):
                clear_record_temp(guard, record_path(guard, plan_id))
                record = record_for_retry(guard, plan_id, expected)
                new = record is None
                if new:
                    publish.check_publication_writer(guard, guard.root / publish.AREA)
                    record = freeze(work, plan, guard, expected)
            if new:
                checkpoint("save_queued")
            return run_save(guard, record, timeout, checkpoint)


def require_queued_record(guard, plan_id, expected, timeout):
    with pending_lock(guard, timeout):
        clear_record_temp(guard, record_path(guard, plan_id))
        record = record_for_retry(guard, plan_id, expected)
    if record is None:
        raise publish.PublicationError("承認済みの保存の記録がありません")
    return record


def resume(wiki, plan_id, expected, *, timeout=180, checkpoint=lambda _: None):
    guard = live.wiki_guard(wiki)
    publish.checked(expected)
    with save_lock(guard, plan_id, timeout):
        record = require_queued_record(guard, plan_id, expected, timeout)
        return run_save(guard, record, timeout, checkpoint)


def discard(wiki, plan_id, expected, *, timeout=180):
    guard = live.wiki_guard(wiki)
    publish.checked(expected)
    with save_lock(guard, plan_id, timeout):
        record = require_queued_record(guard, plan_id, expected, timeout)
        if not record.get("discarded"):
            area = guard.root / publish.AREA
            with guard.hold_lock(area / "apply.lock", timeout=timeout):
                if publish.read_publication_journal(guard, area) is not None:
                    raise publish.Conflict("未処理の公開日誌があるため、先に公開状態を復旧してください")
                if confirmed(guard, record):
                    raise publish.Conflict("公開済みの保存は破棄できません")
                audit = {key: record[key] for key in ("wiki", "wiki_identity", "plan_id", "approval", "attempt")}
                write_record(guard, {**audit, "discarded": True, "reason": record.get("reason")})
        return {"state": "discarded", "plan_id": plan_id, "plan_sha256": expected}


def pending_public(wiki):
    guard = live.wiki_guard(wiki)
    if guard.directory_info(guard.root / SAVES) is None:
        return []
    with pending_lock(guard):
        return pending_public_locked(guard)


def pending_public_locked(guard):
    """保留受付の共通ロック内で一時名を回収し、承認済みの記録を列挙する。"""
    folder = guard.root / SAVES
    if guard.directory_info(folder) is None:
        return []
    for path in folder.iterdir():
        match = re.fullmatch(rf"\.wiki-save-({publish.PLAN_ID.pattern})\.tmp", path.name)
        if match:
            clear_record_temp(guard, record_path(guard, match[1]))
    result = []
    for path, _ in guard.tree_entries(folder):
        if path.suffix != ".json" or not re.fullmatch(publish.PLAN_ID, path.stem):
            continue
        record = load_record(guard, path.stem)
        if record and not record.get("completed") and not record.get("discarded"):
            result.append({"mode": "publication", "plan_id": record["plan_id"], "plan_sha256": record["approval"],
                           "reason": record.get("reason")})
    return result
