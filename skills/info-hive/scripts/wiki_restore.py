"""別ボリュームの完了記録と取り下げを使って公開 wiki の復元を照合する。"""

from pathlib import Path
import json
import os
import time
import uuid

import wiki_apply as publication
import wiki_restore_content as content
import wiki_restore_raw as raw_archive
from wiki_fsguard import Refused, external_raw_guard
from wiki_raw_roots import raw_root_pair, register_upgrade_roots


CONFIG = "restore-ledger.json"
LEDGER = "ledger.json"
LOCK = "ledger.lock"
LEDGER_ENV = "INFO_HIVE_RESTORE_LEDGER"
KINDS = {"publication": "receipts", "live": "live-receipts", "return_live": "returns"}
JOURNALS = (publication.JOURNAL, publication.RETURN_JOURNAL, publication.LIVE_JOURNAL,
            publication.LIVE_TRANSACTION, publication.LIVE_OWNER)


def temporary(path):
    return path.with_name(f".wiki-restore-{path.name}.tmp")


def write(guard, path, value):
    staged = temporary(path)
    guard.clear_exclusive_temp(staged, path)
    guard.write_bytes(path, publication.encoded(value), replace=True, write_through=True,
                      delays=publication.REPLACE_DELAYS, mode=0o600, temporary=staged)
    publication.sync_directory(path.parent)


def binding(guard, path, token):
    return {"ledger": str(path), "id": token, "wiki": str(guard.root.resolve(strict=True)),
            "wiki_identity": list(guard.root_id)}


def ledger_guard(guard, path):
    try:
        ledger = external_raw_guard(guard.root, Path(path))
    except OSError as error:
        raise publication.PublicationError(f"独立した復元記録のディレクトリを開けません: {error}") from error
    if not guard.root_id[0] or not ledger.root_id[0] or ledger.root_id[0] == guard.root_id[0]:
        raise publication.PublicationError("復元記録には wiki と異なるボリュームを指定してください")
    return ledger


def open_bound(guard, area):
    data = guard.read_bytes(area / CONFIG)
    independent = os.environ.get(LEDGER_ENV)
    if data is None and not independent:
        return None
    if data is None:
        # 配信側に設定した記録は、設定前のバックアップを戻しても残る。
        ledger = ledger_guard(guard, independent)
        config = load(ledger)["binding"]
    else:
        config = json.loads(data)
    if not isinstance(config, dict) or not isinstance(config.get("ledger"), str):
        raise publication.PublicationError("復元記録の設定が不正です")
    path = Path(config["ledger"])
    token = publication.checked(config.get("id"), publication.PLAN_ID)
    if not path.is_absolute() or config != binding(guard, path, token):
        raise publication.PublicationError("復元記録の設定と wiki の実体が一致しません")
    ledger = ledger_guard(guard, path)
    if independent and Path(independent).resolve() != ledger.root.resolve(strict=True):
        raise publication.PublicationError("配信側と wiki に指定された独立した復元記録が一致しません")
    return ledger, config


def load(ledger, config=None):
    for delay in (*publication.REPLACE_DELAYS, None):
        try:
            data = ledger.read_bytes(ledger.root / LEDGER)
            break
        except Refused as error:
            if delay is None or error.kind != "changed":
                raise
            time.sleep(delay)
    if data is None:
        raise publication.PublicationError("独立した復元記録がありません")
    envelope = json.loads(data)
    if not isinstance(envelope, dict) or envelope.keys() != {"data", "sha256"}:
        raise publication.PublicationError("独立した復元記録の形式が不正です")
    state = envelope["data"]
    base = {"format", "binding", "completions", "retired"}
    formats = {1: base, 2: base | {"snapshots"}, 3: base | {"snapshots", "raw"}}
    if (publication.digest(publication.encoded(state)) != envelope["sha256"] or not isinstance(state, dict)
            or type(state.get("format")) is not int or state.keys() != formats.get(state["format"])
            or not isinstance(state["binding"], dict) or (config is not None and state["binding"] != config)
            or not isinstance(state["completions"], dict)
            or not isinstance(state["retired"], list)):
        raise publication.PublicationError("独立した復元記録の指紋または wiki が一致しません")
    for key, entry in state["completions"].items():
        if (not isinstance(entry, dict) or not {"kind", "plan_id", "plan_sha256", "receipt_sha256"} <= entry.keys()
                or not isinstance(entry["kind"], str) or entry["kind"] not in KINDS or key != completion_key(entry)):
            raise publication.PublicationError("独立した完了記録の形式が不正です")
        publication.checked(entry["plan_id"], publication.PLAN_ID)
        publication.checked(entry["plan_sha256"])
        publication.checked(entry["receipt_sha256"])
        if "save_plan_id" in entry:
            publication.checked(entry["save_plan_id"], publication.PLAN_ID)
            publication.checked(entry.get("save_approval"))
    for release_id in state["retired"]:
        publication.checked(release_id)
    if len(state["retired"]) != len(set(state["retired"])):
        raise publication.PublicationError("独立した取り下げ記録が重複しています")
    if state["format"] >= 2:
        if not isinstance(state["snapshots"], list) or not state["snapshots"]:
            raise publication.PublicationError("復元用の版の記録が不正です")
        for checksum in state["snapshots"]:
            publication.checked(checksum)
    if "raw" in state:
        raw_archive.verify_record(state["raw"])
    return state


def store(ledger, state):
    write(ledger, ledger.root / LEDGER, {"data": state, "sha256": publication.digest(publication.encoded(state))})


def completion_key(entry):
    return f"{entry['kind']}:{entry['plan_id']}"


def completion(guard, area, kind, result):
    entry = {"kind": kind, "plan_id": publication.checked(result["plan_id"], publication.PLAN_ID),
             "plan_sha256": publication.checked(result["plan_sha256"]),
             "receipt_sha256": publication.digest(publication.encoded(result))}
    if "release_id" in result:
        entry["release_id"] = publication.checked(result["release_id"])
    if kind == "publication":
        # 公開の試行 ID を付け直しても、人が承認した保存の ID で追跡できるようにする。
        version = entry["release_id"]
        path = area / "releases" / version / "wiki" / "ontology" / "wiki-save.json"
        data = guard.read_bytes(path)
        if data is not None:
            saved = json.loads(data)
            if isinstance(saved, dict) and saved.get("attempt") == entry["plan_id"]:
                entry["save_plan_id"] = publication.checked(saved.get("plan_id"), publication.PLAN_ID)
                entry["save_approval"] = publication.checked(saved.get("approval"))
    return entry


def receipt_paths(area, entry):
    if entry["kind"] == "return_live":
        return (publication.archived_return(area, entry["receipt_sha256"]), area / publication.RETURN_RECEIPT)
    return (area / KINDS[entry["kind"]] / f"{entry['plan_id']}.json",)


def receipt_state(guard, area, entry):
    mismatch = False
    for path in receipt_paths(area, entry):
        data = guard.read_bytes(path)
        if data is None:
            continue
        try:
            record = json.loads(data)
        except ValueError:
            mismatch = True
            continue
        if publication.digest(publication.encoded(record)) == entry["receipt_sha256"]:
            return None
        if entry["kind"] != "return_live" or path.name != publication.RETURN_RECEIPT or (
                isinstance(record, dict) and record.get("plan_id") == entry["plan_id"]):
            mismatch = True
    return "mismatch" if mismatch else "missing"


def retirement_path(area, release_id):
    return area / "retired" / f"{publication.checked(release_id)}.json"


def differences(guard, area, state):
    incomplete = []
    for entry in state["completions"].values():
        reason = receipt_state(guard, area, entry)
        if reason:
            incomplete.append({**entry, "reason": reason})
    tombstones = [release_id for release_id in state["retired"]
                  if guard.read_bytes(retirement_path(area, release_id)) != publication.encoded({"release_id": release_id})]
    return incomplete, tombstones


def check(guard, area, release_id=None, *, live_payload=False):
    target = open_bound(guard, area)
    if target is None:
        return
    state = load(*target)
    if release_id in state["retired"]:
        raise publication.RetiredPublication("この公開版は取り下げられています")
    content.refuse_pending(guard, area)
    incomplete, tombstones = differences(guard, area, state)
    if incomplete or tombstones:
        raise publication.PublicationError("独立した完了記録または取り下げと一致しません。reconcile-restore で照合してください")
    if live_payload:
        verify_live(guard, area, state)
    return state


def verify_live(guard, area, state):
    from wiki_live_apply import LIVE_HEAD, load as load_live, verify_latest_live

    verify_latest_live(guard, area)
    latest = load_live(guard, area / LIVE_HEAD)
    if latest is not None:
        require_completion(guard, area, "live", latest, state=state)


def require_completion(guard, area, kind, result, *, state=None):
    """確認済みの記録を返す経路から、独立記録を飛び越えて成功を返さない。"""
    if state is None:
        target = open_bound(guard, area)
        if target is None:
            return
        state = load(*target)
    entry = completion(guard, area, kind, result)
    if state["completions"].get(completion_key(entry)) != entry:
        raise publication.PublicationError("この操作の独立した完了記録がありません。日誌を recover で確認してください")


def acknowledge(guard, area, *results):
    """wiki の適用ロックを持って、成功応答の前に完了記録を永続化する。"""
    target = open_bound(guard, area)
    if target is None:
        return
    ledger, config = target
    with ledger.hold_lock(ledger.root / LOCK, timeout=180):
        state = load(ledger, config)
        content.refuse_pending(guard, area)
        if any(differences(guard, area, state)):
            raise publication.PublicationError("独立した復元記録と一致しないため、保存を完了できません")
        changed = False
        for kind, result in results:
            entry = completion(guard, area, kind, result)
            key = completion_key(entry)
            prior = state["completions"].get(key)
            if (prior is not None and prior != entry) or receipt_state(guard, area, entry) is not None:
                raise publication.PublicationError("成功応答を記録する完了記録が一致しません")
            if prior is None:
                state["completions"][key] = entry
                changed = True
        if changed:
            if "raw" in state:
                raw_archive.archive(guard, ledger, state["raw"])
            if state["format"] >= 2:
                content.capture(guard, area, ledger, state)
            store(ledger, state)


def enable_raw(guard, ledger, state, root):
    if root is None:
        return False
    raw = raw_archive.root_guard(guard, ledger, root)
    if "raw" in state:
        if Path(state["raw"]["root"]) != raw.root:
            raise publication.PublicationError("別の raw の根が独立した復元記録に設定済みです")
        return False
    record = {"root": str(raw.root), "paths": {}}
    raw_archive.archive(guard, ledger, record)
    state.update(format=3, raw=record)
    return True


def record_restore_root(guard, state):
    if "raw" in state:
        register_upgrade_roots(guard, [raw_root_pair(Path(state["raw"]["root"]))])


def enroll(wiki, path, *, raw_root=None):
    from wiki_live_apply import live_receipts

    guard, area = publication.writing_area(wiki)
    ledger = ledger_guard(guard, path)
    independent = os.environ.get(LEDGER_ENV)
    if independent and Path(independent).resolve() != ledger.root.resolve(strict=True):
        raise publication.PublicationError("配信側と設定先の独立した復元記録が一致しません")
    with guard.hold_lock(area / "apply.lock", timeout=180), ledger.hold_lock(ledger.root / LOCK, timeout=180):
        content.refuse_pending(guard, area)
        if (publication.mode_marker(guard)[0] != "publication"
                or any(guard.file_info(area / name) is not None for name in JOURNALS)):
            raise publication.PublicationError("確定済みの公開版で独立した復元記録を有効にしてください")
        active = publication.verified_active(guard, area)
        configured = open_bound(guard, area) if guard.file_info(area / CONFIG) is not None else None
        if configured:
            if configured[0].root != ledger.root:
                raise publication.PublicationError("別の独立した復元記録がすでに設定されています")
            state = check(guard, area)
            changed = state["format"] == 1
            if changed:
                state.update(format=2, snapshots=[])
                content.capture(guard, area, ledger, state)
            if enable_raw(guard, ledger, state, raw_root) or changed:
                store(ledger, state)
                changed = True
            record_restore_root(guard, state)
            return {"state": "enrolled" if changed else "already_enrolled", "release_id": active[0]}
        ledger_file = ledger.root / LEDGER
        if {p.name for p in ledger.root.iterdir()} - {LOCK, LEDGER, content.OBJECTS, temporary(ledger_file).name}:
            raise publication.PublicationError("独立した復元記録には試験または運用専用の空のディレクトリを指定してください")
        records = []
        for version, entry in publication.history(guard, area, active):
            records.append(completion(guard, area, "publication", publication.expected_receipt(guard, area, version, entry)))
            if "return_sha256" in entry:
                returned = json.loads(publication.read(guard, publication.archived_return(area, entry["return_sha256"])))
                records.append(completion(guard, area, "return_live", returned))
        records.extend(completion(guard, area, "live", result) for result in live_receipts(guard, area))
        retired = []
        for path, _ in guard.tree_entries(area / "retired"):
            release_id = publication.checked(path.stem)
            if path != retirement_path(area, release_id) or json.loads(publication.read(guard, path)) != {"release_id": release_id}:
                raise publication.PublicationError("既存の取り下げ記録が不正です")
            retired.append(release_id)
        previous = load(ledger) if ledger.file_info(ledger_file) is not None else None
        token = (publication.checked(previous["binding"].get("id"), publication.PLAN_ID)
                 if previous is not None else uuid.uuid4().hex)
        config = binding(guard, ledger.root, token)
        state = {"format": 1, "binding": config, "completions": {completion_key(row): row for row in records},
                 "retired": sorted(retired)}
        if previous is not None and any(previous[key] != state[key] for key in ("binding", "completions", "retired")):
            raise publication.PublicationError("独立した復元記録は別の状態です。再初期化せず復元元を確認してください")
        if previous is None or previous["format"] == 1:
            state.update(format=2, snapshots=[])
            content.capture(guard, area, ledger, state)
            enable_raw(guard, ledger, state, raw_root)
            store(ledger, state)
        elif content.inventory(guard, area) != content.read_snapshot(ledger, previous["snapshots"][-1]):
            raise publication.PublicationError("独立した復元記録と現在の本文が一致しません")
        else:
            state = previous
            if enable_raw(guard, ledger, state, raw_root):
                store(ledger, state)
        write(guard, area / CONFIG, config)
        record_restore_root(guard, state)
        return {"state": "enrolled", "release_id": active[0]}


def retire(guard, area, release_id):
    """独立記録を先に確定させ、どちらの記録からでも取り下げを優先する。"""
    target = open_bound(guard, area)
    if target is None:
        return False
    ledger, config = target
    with ledger.hold_lock(ledger.root / LOCK, timeout=180):
        state = load(ledger, config)
        content.refuse_pending(guard, area)
        if release_id not in state["retired"]:
            publication.resolve_publication(guard.root, release_id)
            state["retired"] = sorted([*state["retired"], release_id])
            store(ledger, state)
        write(guard, retirement_path(area, release_id), {"release_id": release_id})
    return True


def reconcile(wiki, *, restore_content=False, checkpoint=lambda _: None):
    guard, area = publication.writing_area(wiki)
    with guard.hold_lock(area / "apply.lock", timeout=180):
        target = open_bound(guard, area)
        if target is None:
            raise publication.PublicationError("独立した復元記録を設定済みのバックアップを指定してください")
        ledger, config = target
        with ledger.hold_lock(ledger.root / LOCK, timeout=180):
            state = load(ledger, config)
            if not restore_content:
                content.refuse_pending(guard, area)
            incomplete, tombstones = differences(guard, area, state)
            if guard.file_info(area / CONFIG) is None:
                write(guard, area / CONFIG, config)
            for release_id in tombstones:
                write(guard, retirement_path(area, release_id), {"release_id": release_id})
            restored = 0
            restored_raw = 0
            raw_missing, raw_conflicts = [], []
            raw_record = state.get("raw")
            if raw_record is not None:
                raw = raw_archive.root_guard(guard, ledger, raw_record["root"])
                paths = raw_record["paths"]
                raw_missing, raw_conflicts = raw_archive.compare(raw, paths)

            def restore_raw():
                nonlocal restored_raw
                restored_raw = raw_archive.restore(raw, ledger, paths, raw_missing, checkpoint)

            if restore_content:
                if any(guard.file_info(area / name) is not None for name in JOURNALS):
                    raise publication.PublicationError("復元した wiki に未完了の日誌があります。recover で確認してください")
                if not raw_conflicts:
                    if raw_missing:
                        raw_archive.preflight(ledger, paths, raw_missing)
                    restored = content.install(guard, area, ledger, state, checkpoint,
                                               restore_raw=restore_raw if raw_missing else None)
                    incomplete, remaining = differences(guard, area, state)
                    if incomplete or remaining:
                        raise publication.PublicationError("復元した本文と独立した完了記録・取り下げが一致しません")
                    raw_missing = []
                    record_restore_root(guard, state)
            active = publication.verified_active(guard, area) if not incomplete else None
            if not incomplete and active is None:
                raise publication.PublicationError("復元した wiki の公開版を確認できません")
            if active:
                if any(guard.file_info(area / name) is not None for name in JOURNALS):
                    raise publication.PublicationError("復元した wiki に未完了の日誌があります。recover で確認してください")
                for version, entry in publication.history(guard, area, active):
                    receipt = publication.verify_release(guard, area, version, entry, payload=version != active[0])
                    require_completion(guard, area, "publication", receipt, state=state)
                mode, proof = publication.mode_marker(guard)
                if mode == "live":
                    from wiki_live_return import verify_result

                    verify_live(guard, area, state)
                    returned = verify_result(guard, area, active[0], proof)
                    require_completion(guard, area, "return_live", returned, state=state)
            current_retired = bool(active and active[0] in state["retired"])
            result = {"state": "blocked" if incomplete or current_retired or raw_missing or raw_conflicts else "reconciled",
                      "incomplete_receipts": incomplete, "retirements_reapplied": tombstones,
                      "current_retired": current_retired, "checked_receipts": len(state["completions"])}
            if raw_record is not None:
                result.update(raw_missing=raw_missing, raw_conflicts=raw_conflicts)
            if restore_content:
                result["restored_files"] = restored
                if raw_record is not None:
                    result["restored_raw_files"] = restored_raw
                journal = area / content.REPLAY
                if guard.file_info(journal) is not None and result["state"] == "reconciled":
                    guard.remove_file(journal)
                    publication.sync_directory(area)
            return result
