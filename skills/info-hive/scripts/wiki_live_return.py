"""承認された公開版の内容とライブ専用ファイルを、日誌でライブに戻す。"""

from pathlib import Path
import uuid

import wiki_apply as publish
import wiki_live_apply as live
from wiki_pages import absolute_user_path, is_page_path


def approval(plan):
    return publish.digest(publish.encoded(plan))


def returning_marker(mode, checksum):
    return f"wiki-{mode}/1 {checksum}\n".encode("ascii")


def verify_result(guard, area, release_id, checksum, *, stored_path=None):
    result = live.load(guard, stored_path or (area / publish.RETURN_RECEIPT))
    if (not isinstance(result, dict) or approval(result) != checksum or result.get("release_id") != release_id
            or result.get("wiki_identity") != list(guard.root_id)):
        raise publish.PublicationError("ライブへの切り戻しの完了記録と wiki の状態が一致しません")
    plan_id = publish.checked(result.get("plan_id"), publish.PLAN_ID)
    publish.checked(result.get("plan_sha256"))
    bridge = result.get("live_receipt")
    if bridge is not None:
        if (not isinstance(bridge, dict) or bridge.get("plan_id") != plan_id
                or bridge.get("plan_sha256") != result["plan_sha256"]
                or live.load(guard, area / "live-receipts" / f"{plan_id}.json") != bridge):
            raise publish.PublicationError("切り戻しのライブ完了記録が一致しません")
    return result


def initial_entry(guard, area, active):
    for _, entry in publish.history(guard, area, active):
        if entry.get("live_base"):
            return entry
    raise publish.Conflict("公開を開始したライブの基準がないため、切り戻しを照合できません")


def target_files(before, initial, published):
    result = dict(before)
    for name in sorted(initial.keys() | published.keys()):
        old, first, last = before.get(name), initial.get(name), published.get(name)
        if first is None and old is not None and old != last:
            raise publish.Conflict(f"公開後の追加先がライブ専用ファイルと重なります: {name}")
        if last is None:
            result.pop(name, None)
        else:
            result[name] = last
    # 大文字小文字の異なる別名と、ファイルからディレクトリへの置換を止める。
    publish.verify_file_names(before | result)
    return result


def describe(guard, area, raw_root, plan_id, active):
    initial = initial_entry(guard, area, active)
    before = publish.live_files(guard)
    publish.verify_file_names(before)
    receipts = publish.live_receipt_fingerprints(guard)
    if publish.live_fingerprint(guard, before, receipts) != initial["live_base"]:
        raise publish.Conflict("初回公開の基準からライブ wiki が変わりました。差分を書き戻す前に照合してください")
    live.verify_latest_live(guard, area)
    live.protect_roots(guard, raw_root)
    after = target_files(before, initial["files"], active[1]["files"])
    return {"format": 1, "wiki": str(guard.root), "wiki_identity": list(guard.root_id),
            "plan_id": plan_id, "release_id": active[0], "raw_root": str(raw_root), "before": before, "after": after}


def build_plan(wiki, raw_root):
    guard = live.wiki_guard(wiki)
    area = guard.root / publish.AREA
    active = publish.read_active(guard, area)
    publish.check_publication_writer(guard, area)
    if active is None:
        raise publish.Conflict("切り戻す公開版がありません")
    return describe(guard, area, absolute_user_path(raw_root), uuid.uuid4().hex, active)


def load_plan(guard, plan, checksum):
    if (not isinstance(plan, dict) or plan.keys() != {
            "format", "wiki", "wiki_identity", "plan_id", "release_id", "raw_root", "before", "after"}
            or plan["format"] != 1 or approval(plan) != publish.checked(checksum)
            or plan["wiki"] != str(guard.root) or plan["wiki_identity"] != list(guard.root_id)
            or not isinstance(plan["before"], dict) or not isinstance(plan["after"], dict)
            or not isinstance(plan["raw_root"], str) or not Path(plan["raw_root"]).is_absolute()):
        raise publish.PublicationError("切り戻しの承認済み計画と wiki が一致しません")
    publish.checked(plan["plan_id"], publish.PLAN_ID)
    publish.checked(plan["release_id"])
    publish.verify_file_names(plan["before"])
    publish.verify_file_names(plan["after"])
    publish.verify_file_names(plan["before"] | plan["after"])
    return plan


def confirm_pending(guard):
    from wiki_public_save import confirmed, load_record, pending_public_locked

    outstanding = []
    for pending in pending_public_locked(guard):
        record = load_record(guard, pending["plan_id"])
        # 保存側の排他を待たず、共通ロックで固定した公開履歴を照合する。
        if not confirmed(guard, record, compact=False):
            outstanding.append(pending["plan_id"])
    if outstanding:
        raise publish.Conflict("未適用の承認済み保存を再試行するか、取り消してください: " + "、".join(outstanding))


def paths_for(guard, plan):
    before, after = plan["before"], plan["after"]
    names = [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
    for i, name in enumerate(names):
        path = guard.root / publish.relative(name)
        temporary = path.with_name(f".wiki-return-{plan['plan_id']}-{i}.tmp")
        yield name, path, temporary


def preimage_files(plan, initial):
    # 初回の公開で採用した変更が、元のライブの内容を上書きする場合に限って保全する。
    return {name: value for name, value in plan["before"].items()
            if name in initial and value != initial[name] and value != plan["after"].get(name)}


def protect_preimages(guard, area, plan, preimages, *, create=False):
    root = area / "return-preimages" / plan["plan_id"]
    for name, expected in preimages.items():
        relative = publish.relative(name)
        backup = root / relative
        temporary = metadata_temporary(backup, plan["plan_id"])
        if create:
            guard.clear_exclusive_temp(temporary, backup)
        saved = guard.read_bytes(backup)
        if saved is None and create:
            saved = publish.read(guard, guard.root / relative)
            if publish.digest(saved) == expected:
                live.write(guard, backup, saved, temporary=temporary)
        if saved is None or publish.digest(saved) != expected:
            raise publish.PublicationError(f"上書き前のライブの内容を保全できません: {name}")


def metadata_temporary(path, plan_id):
    key = publish.digest(path.name.encode("utf-8"))[:24]
    return path.with_name(f".wiki-return-{plan_id}-{key}.tmp")


def write_metadata(guard, path, value, plan, checkpoint, point, *, replace=False):
    live.write(guard, path, value, replace=replace, temporary=metadata_temporary(path, plan["plan_id"]),
               before_publish=lambda: checkpoint(point))


def check_raw(guard, plan, archive, published, paths):
    for _, path, _ in paths:
        guard.file_info(path)
    targets = [path for _, file, temporary in paths for path in (file, temporary)]
    contents = []
    for name in sorted(plan["before"].keys() | plan["after"].keys()):
        path = guard.root / publish.relative(name)
        if is_page_path(name):
            contents.append((path, guard.read_bytes(path)))
            if name in published:
                contents.append((path, publish.read(guard, archive / publish.relative(name))))
    roots = live.check_raw_refs(guard, contents, targets=targets)
    live.protect_roots(guard, plan["raw_root"], roots)


def verify_position(guard, plan, *, complete=False):
    before, after = plan["before"], plan["after"]
    current = publish.live_files(guard)
    if complete:
        if current != after:
            raise publish.PublicationError("ライブに書き戻した成果物が切り戻し計画と一致しません")
        return
    for name in before.keys() | after.keys() | current.keys():
        if current.get(name) not in (before.get(name), after.get(name)):
            raise publish.Conflict(f"切り戻し中のファイルが計画と異なります: {name}")


def write_files(guard, plan, paths, archive, checkpoint):
    for i, (name, path, temporary) in enumerate(paths):
        data = guard.read_bytes(path)
        current = publish.digest(data) if data is not None else None
        expected = plan["after"].get(name)
        if current == expected:
            continue
        if current != plan["before"].get(name):
            raise publish.Conflict(f"切り戻し中にファイルが変わりました: {name}")
        if expected is None:
            live.remove(guard, path)
        else:
            content = publish.read(guard, archive / publish.relative(name))
            if publish.digest(content) != expected:
                raise publish.PublicationError(f"書き戻す公開ファイルの指紋が一致しません: {name}")
            live.write(guard, path, content, replace=data is not None, temporary=temporary,
                       before_publish=lambda: checkpoint(f"return_before_write:{i}"))
        checkpoint(f"return_after_write:{i}")


def make_result(guard, area, plan, checksum, paths, preimages):
    receipts = live.ordered_receipts(guard, area)
    prior = next((entry for entry in receipts if entry["plan_id"] == plan["plan_id"]), None)
    other = [entry for entry in receipts if entry is not prior]
    head = live.load(guard, area / live.LIVE_HEAD)
    previous = other[-1] if other else None
    if (prior and receipts[-1] != prior) or (head is not None and head != previous and head != prior):
        raise publish.PublicationError("切り戻し前のライブの完了記録を照合できません")
    if head is None and previous is not None:
        raise publish.PublicationError("切り戻し前のライブの先頭記録がありません")
    # ライブの通常保存が管理する記事・索引・ログの世代を引き継ぐ。
    # グラフなどの派生成果物は切り替え時に照合し、ライブ側では再生成できる。
    managed = [name for name, _, _ in paths if is_page_path(name) or name in ("index.md", "log.md")
               or name.startswith("indexes/") and name.endswith(".md")]
    bridge = None
    if managed:
        bridge = {"state": "applied", "plan_id": plan["plan_id"], "plan_sha256": checksum,
                  "generation": len(other) + 1, "files": managed, "log_entries": [],
                  "after_sha256": {name: plan["after"].get(name) for name in managed}}
    stored = live.load(guard, area / "live-receipts" / f"{plan['plan_id']}.json")
    if stored is not None and stored != bridge:
        raise publish.PublicationError("切り戻しの計画 ID がライブの完了記録と重複しています")
    return {"state": "returned_live", "plan_id": plan["plan_id"], "plan_sha256": checksum,
            "release_id": plan["release_id"], "wiki_identity": list(guard.root_id), "live_receipt": bridge,
            "preimages": preimages, "live_generation": len(other) + (bridge is not None),
            "written": sum(name in plan["after"] for name, _, _ in paths),
            "removed": sum(name not in plan["after"] for name, _, _ in paths)}


def finish(guard, area, plan, checksum, checkpoint):
    active = publish.verified_active(guard, area)
    if active is None or active[0] != plan["release_id"]:
        raise publish.PublicationError("切り戻しの日誌と公開版が一致しません")
    initial = initial_entry(guard, area, active)
    paths = list(paths_for(guard, plan))
    marker = guard.root / publish.MARKER
    stored_path = area / publish.RETURN_RECEIPT
    bridge_path = area / "live-receipts" / f"{plan['plan_id']}.json"
    for path in (marker, stored_path, bridge_path):
        guard.clear_exclusive_temp(metadata_temporary(path, plan["plan_id"]), path)
    preimages = preimage_files(plan, initial["files"])
    protect_preimages(guard, area, plan, preimages)
    result = make_result(guard, area, plan, checksum, paths, preimages)
    result["private_paths"] = sorted(plan["after"].keys() - active[1]["files"].keys())
    plan_receipt = f"{plan['plan_id']}.json" if result["live_receipt"] else None
    receipt_hashes = publish.live_receipt_fingerprints(guard, exclude=plan_receipt)
    if (publish.live_fingerprint(guard, plan["before"], receipt_hashes) != initial["live_base"]
            or target_files(plan["before"], initial["files"], active[1]["files"]) != plan["after"]):
        raise publish.PublicationError("切り戻しの日誌の内容を初回と現在の公開版から照合できません")
    mode, fingerprint = publish.mode_marker(guard)
    archived = area / "releases" / active[0] / "wiki"
    stored = live.load(guard, stored_path)
    if isinstance(stored, dict):
        for field in ("live_generation", "private_paths"):
            if field not in stored:
                result.pop(field)
    if stored is not None and stored != result:
        raise publish.PublicationError("切り戻しの完了記録と日誌が一致しません")
    bridge = result["live_receipt"]
    committed = bridge is not None and live.load(guard, bridge_path) == bridge
    if mode == "live":
        if fingerprint != approval(result):
            raise publish.PublicationError("ライブへ切り戻した記録と日誌が一致しません")
        verify_position(guard, plan, complete=True)
    else:
        # 最初のライブファイルを変える前に、切り戻し中の印を同期する。
        if mode == "publication":
            if stored is not None or committed:
                raise publish.PublicationError("切り戻し中の印がありません")
            if publish.live_files(guard) != plan["before"]:
                raise publish.Conflict("切り戻し前のライブ wiki と日誌が一致しません")
            write_metadata(guard, marker, returning_marker("returning", checksum), plan, checkpoint,
                           "return_write_intent", replace=True)
            checkpoint("return_marked")
        elif mode != "returning" or fingerprint != checksum:
            raise publish.PublicationError("切り戻し中の印と日誌が一致しません")
        for _, path, temporary in paths:
            guard.clear_exclusive_temp(temporary, path)
        verify_position(guard, plan, complete=stored is not None or committed)
        check_raw(guard, plan, archived, active[1]["files"], paths)
        write_files(guard, plan, paths, archived, checkpoint)
        verify_position(guard, plan, complete=True)
        if bridge is not None:
            if not committed:
                write_metadata(guard, bridge_path, publish.encoded(bridge), plan, checkpoint, "return_write_live_receipt")
            live.commit_live_head(guard, area, bridge)
        if stored is None:
            write_metadata(guard, stored_path, publish.encoded(result), plan, checkpoint, "return_write_receipt")
        verify_result(guard, area, active[0], approval(result))
        live.verify_latest_live(guard, area)
        checkpoint("return_before_switch")
        verify_position(guard, plan, complete=True)
        write_metadata(guard, marker, returning_marker("live", approval(result)), plan, checkpoint,
                       "return_write_mode", replace=True)
        checkpoint("return_switched")
    live.verify_latest_live(guard, area)
    live.remove(guard, area / publish.RETURN_JOURNAL)
    return result


def resume(guard, area, checkpoint=lambda _: None, *, expected=None):
    journal = live.load(guard, area / publish.RETURN_JOURNAL)
    if not isinstance(journal, dict) or journal.keys() != {"plan", "plan_sha256"}:
        raise publish.PublicationError("ライブへの切り戻しの日誌がありません、または壊れています")
    checksum = journal["plan_sha256"]
    plan = load_plan(guard, journal["plan"], checksum)
    if expected is not None and checksum != expected:
        raise publish.Conflict("中断した切り戻しは別の承認で開始されています")
    return finish(guard, area, plan, checksum, checkpoint)


def apply(wiki, plan, checksum, *, timeout=180, checkpoint=lambda _: None):
    guard, area = publish.writing_area(wiki)
    load_plan(guard, plan, checksum)
    with guard.hold_lock(area / "apply.lock", timeout=timeout):
        publish.check_live_writer(guard, area)
        mode, fingerprint = publish.mode_marker(guard)
        if guard.file_info(area / publish.RETURN_JOURNAL) is not None or mode == "returning":
            return resume(guard, area, checkpoint, expected=checksum)
        if mode == "live":
            active = publish.verified_active(guard, area)
            result = verify_result(guard, area, active[0], fingerprint,
                                   stored_path=publish.live_return_record_path(guard, area, fingerprint))
            if result["plan_sha256"] != checksum:
                raise publish.Conflict("別の承認でライブへの切り戻しが済んでいます")
            protect_preimages(guard, area, plan, result["preimages"])
            return {**result, "state": "already_returned"}
        publish.finish_journal(guard, area)
        active = publish.verified_active(guard, area)
        if active:
            for _, entry in publish.history(guard, area, active):
                proof = entry.get("return_sha256")
                if proof and entry["parent"] == plan["release_id"]:
                    result = verify_result(guard, area, entry["parent"], proof,
                                           stored_path=publish.archived_return(area, proof))
                    if result["plan_sha256"] == checksum:
                        protect_preimages(guard, area, plan, result["preimages"])
                        return {**result, "state": "already_returned"}
        if active is None or active[0] != plan["release_id"]:
            raise publish.Conflict("公開版が変わりました。切り戻しの計画を作り直してください")
        if live.load(guard, area / "live-receipts" / f"{plan['plan_id']}.json") is not None:
            raise publish.Conflict("すでに使われているライブ計画 ID です")
        with guard.hold_lock(area / "pending.lock", timeout=timeout):
            confirm_pending(guard)
            current = describe(guard, area, Path(plan["raw_root"]), plan["plan_id"], active)
            if current != plan:
                raise publish.Conflict("ライブへの切り戻しの計画と現在の入力が一致しません")
            archive = area / "releases" / active[0] / "wiki"
            check_raw(guard, plan, archive, active[1]["files"], list(paths_for(guard, plan)))
            initial = initial_entry(guard, area, active)
            preimages = preimage_files(plan, initial["files"])
            protect_preimages(guard, area, plan, preimages, create=True)
            live.write(guard, area / publish.RETURN_JOURNAL, publish.encoded({"plan": plan, "plan_sha256": checksum}))
        checkpoint("return_queued")
        return finish(guard, area, plan, checksum, checkpoint)
