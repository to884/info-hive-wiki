"""独立した復元記録に、確定済みの wiki と管理記録のバイト列を保存する。"""

import json
from pathlib import Path
import stat

import wiki_apply as publication


OBJECTS = "objects"
REPLAY = "restore-content.journal.json"
METADATA = ("current", publication.HEAD, "live-head.json", publication.RETURN_RECEIPT)
TREES = ("receipts", "live-receipts", "returns", "return-preimages")
SWITCH = {publication.MARKER, *(f"{publication.AREA.as_posix()}/{name}" for name in METADATA)}
REQUIRED = {publication.MARKER, *(f"{publication.AREA.as_posix()}/{name}" for name in METADATA[:2])}


def sha(data):
    return publication.digest(data) if data is not None else None


def temporary(path, name):
    token = publication.digest(name.encode("utf-8"))
    return path.with_name(f".wiki-restore-data-{token}.tmp")


def replace(guard, path, data, name, *, existing):
    from wiki_live_apply import remove

    if data is None:
        remove(guard, path)
        return
    parents = []
    parent = path.parent
    while parent != guard.root and guard.directory_info(parent) is None:
        parents.append(parent)
        parent = parent.parent
    if path.parent != guard.root:
        guard.ensure_directory(path.parent)
    for folder in reversed(parents):
        publication.sync_directory(folder.parent)
    guard.write_bytes(path, data, replace=existing, write_through=True, delays=publication.REPLACE_DELAYS,
                      mode=0o600, temporary=temporary(path, name))
    publication.sync_directory(path.parent)


def object_path(ledger, checksum):
    return ledger.root / OBJECTS / publication.checked(checksum)


def read_object(ledger, checksum):
    data = ledger.read_bytes(object_path(ledger, checksum))
    if sha(data) != checksum:
        raise publication.PublicationError(f"別ディスクの復元データが欠落しているか指紋が一致しません: {checksum}")
    return data


def save_object(ledger, checksum, data):
    path = object_path(ledger, checksum)
    ledger.clear_exclusive_temp(temporary(path, checksum), path)
    if ledger.file_info(path) is None:
        replace(ledger, path, data, checksum, existing=False)
    else:
        read_object(ledger, checksum)


def relative(name):
    area = publication.AREA.as_posix()
    if name == publication.MARKER:
        return Path(name)
    if name in SWITCH:
        return publication.AREA / name.removeprefix(f"{area}/")
    if name.startswith(f"{area}/"):
        group, _, suffix = name.removeprefix(f"{area}/").partition("/")
        if group in TREES or group == "releases":
            return publication.AREA / group / publication.relative(suffix)
    return publication.relative(name)


def inventory(guard, area, *, sealed=False):
    """公開時は確定したマニフェストを使い、復元時は実際に戻したファイルを照合する。"""
    files = publication.live_files(guard)
    publication.verify_file_names(files)

    def put(path):
        data = guard.read_bytes(path)
        if data is not None:
            files[path.relative_to(guard.root).as_posix()] = sha(data)

    put(guard.root / publication.MARKER)
    for name in METADATA:
        put(area / name)
    for directory in TREES if sealed else (*TREES, "releases"):
        for path, info in guard.tree_entries(area / directory):
            if not stat.S_ISDIR(info.st_mode):
                put(path)
    if sealed:
        tip = publication.current(guard, area)
        if tip is None:
            raise publication.PublicationError("確定済みの公開版がありません")
        for version, entry in publication.history(guard, area, (tip, publication.manifest(guard, area, tip))):
            base = (publication.AREA / "releases" / version).as_posix()
            files[f"{base}/manifest.json"] = version
            files.update({f"{base}/wiki/{name}": checksum for name, checksum in entry["files"].items()})
    return files


def read_snapshot(ledger, checksum):
    files = json.loads(read_object(ledger, checksum))
    if not isinstance(files, dict) or not REQUIRED <= files.keys():
        raise publication.PublicationError("復元する版のファイル一覧が不正です")
    for name, value in files.items():
        relative(name)
        publication.checked(value)
    return files


def capture(guard, area, ledger, state):
    files = inventory(guard, area, sealed=True)
    previous = set(read_snapshot(ledger, state["snapshots"][-1]).values()) if state["snapshots"] else set()
    available = set()
    for name, checksum in files.items():
        if checksum in available:
            continue
        if checksum not in previous or ledger.file_info(object_path(ledger, checksum)) is None:
            data = guard.read_bytes(guard.root / relative(name))
            if sha(data) != checksum:
                raise publication.PublicationError(f"別ディスクに保存する内容が変わりました: {name}")
            save_object(ledger, checksum, data)
        available.add(checksum)
    encoded = publication.encoded(files)
    checksum = sha(encoded)
    if not state["snapshots"] or state["snapshots"][-1] != checksum:
        save_object(ledger, checksum, encoded)
        state["snapshots"].append(checksum)


def pending(guard, area):
    return guard.file_info(area / REPLAY) is not None


def refuse_pending(guard, area):
    if pending(guard, area):
        raise publication.PublicationError("本文の復元が途中です。reconcile-restore --restore-content を再試行してください")


def changed_paths(before, after):
    return sorted((name for name in before.keys() | after.keys() if before.get(name) != after.get(name)),
                  key=lambda name: (name in SWITCH, name == publication.MARKER, name))


def install(guard, area, ledger, state, checkpoint, *, restore_raw=None):
    """検証済みのバックアップにだけ適用し、各ファイルを同じ位置で再試行できるようにする。"""
    from wiki_restore import write

    snapshots = state.get("snapshots")
    if not snapshots:
        raise publication.PublicationError("この独立記録には本文がありません。本文のあるバックアップを用意してください")
    journal = area / REPLAY
    saved = guard.read_bytes(journal)
    target = snapshots[-1]
    after = read_snapshot(ledger, target)
    if saved is None:
        before = inventory(guard, area)
        base = sha(publication.encoded(before))
        if base not in snapshots:
            raise publication.PublicationError("復元した wiki が別ディスクに記録したいずれの基準版とも一致しません")
        if base == target and restore_raw is None:
            return 0
        read_snapshot(ledger, base)
        if publication.verified_active(guard, area) is None:
            raise publication.PublicationError("復元の基準にできる公開版がありません")
    else:
        intent = json.loads(saved)
        if (not isinstance(intent, dict) or intent.keys() != {"base", "target"}
                or intent["base"] not in snapshots or intent["target"] != target):
            raise publication.PublicationError("本文の復元日誌と別ディスクの版が一致しません")
        base = intent["base"]
        before = read_snapshot(ledger, base)
    names = changed_paths(before, after)
    live = {name: checksum for name, checksum in (before | after).items()
            if not name.startswith(f"{publication.AREA.as_posix()}/") and name != publication.MARKER}
    publication.verify_file_names(live)
    for checksum in {after[name] for name in names if name in after}:
        read_object(ledger, checksum)
    if saved is None:
        write(guard, journal, {"base": base, "target": target})
    for name in names:
        path = guard.root / relative(name)
        guard.clear_exclusive_temp(temporary(path, name), path)
    present = inventory(guard, area)
    if any(present.get(name) not in (before.get(name), after.get(name)) for name in present.keys() | before.keys() | after.keys()):
        raise publication.PublicationError("本文の復元中に記録の範囲外の変更を検出しました")
    if restore_raw is not None:
        restore_raw()
    restored = 0
    for name in names:
        path = guard.root / relative(name)
        current = sha(guard.read_bytes(path))
        expected = after.get(name)
        if current == expected:
            continue
        if current != before.get(name):
            raise publication.PublicationError(f"本文の復元中にファイルが変わりました: {name}")
        data = read_object(ledger, expected) if expected else None
        replace(guard, path, data, name, existing=current is not None)
        restored += 1
        checkpoint(f"restore_after_file:{restored}")
    if inventory(guard, area) != after:
        raise publication.PublicationError("復元した本文・成果物と別ディスクの版が一致しません")
    return restored


def completed_public_save(guard, plan_id, checksum):
    """バックアップに受付ファイルがない場合も、確定した保存の ID を再試行できる。"""
    from wiki_restore import check

    state = check(guard, guard.root / publication.AREA)
    if state is None or not state.get("snapshots"):
        return None
    matches = [entry for entry in state["completions"].values() if entry.get("save_plan_id") == plan_id]
    if not matches:
        return None
    if len(matches) != 1 or matches[0]["save_approval"] != checksum:
        raise publication.Conflict("この保存の承認ハッシュと独立した完了記録が一致しません")
    return {"wiki": str(guard.root), "wiki_identity": list(guard.root_id), "plan_id": plan_id,
            "approval": checksum, "attempt": matches[0]["plan_id"], "completed": True}
