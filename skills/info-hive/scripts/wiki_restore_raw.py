"""独立記録に保全した raw 原本を、元のパスへ排他的に復元する。"""

from contextlib import contextmanager
from hashlib import file_digest, sha256
import os
from pathlib import Path, PurePosixPath
import stat
import unicodedata

import wiki_apply as publication
from wiki_fsguard import WriteGuard, external_raw_guard
from wiki_raw import raw_digest, within
from wiki_restore_content import object_path, temporary


def relative(name):
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or path.as_posix() != name
            or any(part.startswith(".") for part in path.parts) or any(ch in name for ch in "\\:\0")):
        raise publication.PublicationError("独立記録の raw の相対パスが不正です")
    return Path(*path.parts)


def verify_paths(paths):
    if not isinstance(paths, dict):
        raise publication.PublicationError("独立記録の raw の一覧が不正です")
    if len({unicodedata.normalize("NFC", name.casefold()) for name in paths}) != len(paths):
        raise publication.PublicationError("独立記録の raw の名前が衝突しています")
    for name, checksum in paths.items():
        path = relative(name)
        if checksum is not None:
            publication.checked(checksum)
        if any(parent.as_posix() not in paths or paths[parent.as_posix()] is not None
               for parent in path.parents if parent != Path(".")):
            raise publication.PublicationError(f"独立記録の raw の親ディレクトリが不正です: {name}")


def verify_record(record):
    if (not isinstance(record, dict) or record.keys() not in ({"root", "paths"}, {"root", "paths", "observed"})
            or not isinstance(record["root"], str) or not Path(record["root"]).is_absolute()):
        raise publication.PublicationError("独立記録の raw の根が不正です")
    verify_paths(record["paths"])
    observed = record.get("observed", {})
    if (not isinstance(observed, dict) or any(name not in record["paths"] or record["paths"][name] is None
            or not isinstance(info, list) or len(info) != 5 or any(type(value) is not int for value in info)
            for name, info in observed.items())):
        raise publication.PublicationError("独立記録の raw のファイル属性が不正です")


def root_guard(guard, ledger, root):
    try:
        raw = external_raw_guard(guard.root, Path(root))
        resolved = raw.root.resolve(strict=True)
    except OSError as error:
        raise publication.PublicationError(f"設定した raw の根を開けません: {error}") from error
    if raw.root_id[0] == ledger.root_id[0] or not raw.root_id[0]:
        raise publication.PublicationError("raw と独立記録は異なるボリュームに置いてください")
    if raw.root != resolved:
        raise publication.PublicationError("raw の根には実体の絶対パスを指定してください")
    return raw


def fingerprint(guard, path):
    with guard.open_read(path) as stream:
        return file_digest(stream, "sha256").hexdigest()


def paths_below(raw, target):
    if target != raw.root:
        name = target.relative_to(raw.root).as_posix()
        relative(name)
        for parent in target.parents:
            if parent == raw.root:
                break
            yield parent.relative_to(raw.root).as_posix(), None
    pending = list(raw.root.iterdir()) if target == raw.root else [target]
    while pending:
        path = pending.pop()
        if path.name.startswith("."):
            continue
        info = raw.entry_info(path)
        if info is None:
            raise FileNotFoundError(path)
        name = path.relative_to(raw.root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
            yield name, None
        else:
            yield name, [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class CheckedStream:
    def __init__(self, source, checksum):
        self.source = source
        self.expected = checksum
        self.digest = sha256()

    def read(self, size):
        data = self.source.read(size)
        self.digest.update(data)
        return data

    def verify(self):
        if self.digest.hexdigest() != self.expected:
            raise publication.PublicationError("コピーする raw 原本と記録された SHA-256 が一致しません")


def copy(source, original, destination, path, name, checksum):
    staged = temporary(path, name)
    if destination.clear_exclusive_temp(staged, path):
        publication.sync_directory(path.parent)
    with source.open_read(original) as stream:
        checked = CheckedStream(stream, checksum)
        destination.write_stream(path, checked, mode=0o600, write_through=True,
                                 temporary=staged, before_publish=checked.verify)
    publication.sync_directory(path.parent)


def compatible_paths(known, current):
    """既知の原本を上書きせず、記録済みの種類・名前と両立するパスを選ぶ。"""
    identities = {unicodedata.normalize("NFC", name.casefold()) for name in known}
    accepted = {}
    for name in sorted(current):
        checksum = current[name]
        if name in known:
            if checksum != known[name]:
                continue
        elif unicodedata.normalize("NFC", name.casefold()) in identities:
            continue
        parent = PurePosixPath(name).parent.as_posix()
        if parent == "." or (parent in accepted and accepted[parent] is None):
            accepted[name] = checksum
    return accepted


def archive(guard, ledger, record, *, target=None, expected=None, preserve_recorded=False):
    raw = root_guard(guard, ledger, record["root"])
    if target is not None and raw_digest(target, origin=raw.root) != expected:
        raise publication.PublicationError("保存した raw 原本の SHA-256 が変わりました")
    known = record["paths"]
    observed = record.get("observed", {})
    present = dict(paths_below(raw, target or raw.root))
    current = {}
    for name, info in present.items():
        if info is None:
            current[name] = None
        elif target is None and info == observed.get(name) and info[1]:
            current[name] = known[name]
        else:
            current[name] = fingerprint(raw, raw.root / relative(name))
    relevant = known if target is None else {name: value for name, value in known.items()
                                             if name in current or within(raw.root / relative(name), target)}
    if preserve_recorded:
        current = compatible_paths(known, current)
    elif any(name not in current or current[name] != value for name, value in relevant.items()):
        raise publication.PublicationError("記録済みの raw 原本が欠落または変更されています。復元するか原本を確認してください")
    verify_paths(known | current)
    for name, checksum in current.items():
        if checksum is None:
            continue
        obj = object_path(ledger, checksum)
        if ledger.file_info(obj) is None:
            if ledger.directory_info(obj.parent) is None:
                ledger.ensure_directory(obj.parent)
                publication.sync_directory(ledger.root)
            copy(raw, raw.root / relative(name), ledger, obj, checksum, checksum)
        elif (target is not None or name not in known) and fingerprint(ledger, obj) != checksum:
            raise publication.PublicationError(f"別ディスクにある raw 原本の指紋が一致しません: {name}")
    if target is not None and raw_digest(target, origin=raw.root) != expected:
        raise publication.PublicationError("別ディスクへの保存中に raw 原本の SHA-256 が変わりました")
    # 復元後の既存原本を指定した再試行も、記録済みのバイト列を参照する。
    refreshed = {name: info for name, info in present.items()
                 if info is not None and name in current and (target is None or name not in known)}
    if refreshed:
        record["observed"] = observed | refreshed
    known.update(current)


def compare(raw, paths):
    present = dict(paths_below(raw, raw.root))
    missing = sorted(paths.keys() - present.keys())
    # 余分なファイルもディレクトリ原本の SHA-256 を変える。
    conflicts = set(present.keys() - paths.keys())
    for name in paths.keys() & present.keys():
        checksum = paths[name]
        info = present[name]
        if (checksum is None) != (info is None) or (
                checksum is not None and fingerprint(raw, raw.root / relative(name)) != checksum):
            conflicts.add(name)
    return missing, sorted(conflicts)


def preflight(ledger, paths, missing):
    for checksum in {paths[name] for name in missing if paths[name] is not None}:
        obj = object_path(ledger, checksum)
        if ledger.file_info(obj) is None or fingerprint(ledger, obj) != checksum:
            raise publication.PublicationError(f"別ディスクにある raw 原本が欠落しているか指紋が一致しません: {checksum}")


def restore(raw, ledger, paths, missing, checkpoint):
    restored_files = 0
    for name in missing:
        path = raw.root / relative(name)
        checksum = paths[name]
        if checksum is None:
            if raw.directory_info(path) is None:
                raw.ensure_directory(path)
                publication.sync_directory(path.parent)
            continue
        if raw.file_info(path) is not None:
            if fingerprint(raw, path) != checksum:
                raise publication.PublicationError(f"復元中に raw 原本が追加されました: {name}")
            continue
        copy(ledger, object_path(ledger, checksum), raw, path, name, checksum)
        restored_files += 1
        checkpoint(f"restore_after_raw:{restored_files}")
    if any(compare(raw, paths)):
        raise publication.PublicationError("復元した raw 原本と独立記録が一致しません")
    return restored_files


@contextmanager
def writing_raw(wiki, root):
    """通常の raw 保存は、保護対象のときだけ同じ適用ロックと完了順序を使う。"""
    from wiki_restore import CONFIG, LEDGER_ENV, LOCK, check, load, open_bound, store

    if not (os.path.lexists(wiki / publication.AREA / CONFIG) or os.environ.get(LEDGER_ENV)):
        yield lambda _: None
        return
    guard = WriteGuard(wiki, strict_root=True)
    area = guard.root / publication.AREA
    with guard.hold_lock(area / "apply.lock", timeout=180):
        target = open_bound(guard, area)
        if target is None:
            raise publication.PublicationError("独立した復元記録の設定が失われました")
        ledger, config = target
        with ledger.hold_lock(ledger.root / LOCK, timeout=180):
            state = load(ledger, config)
            record = state.get("raw")
            if record is None or not within(root, Path(record["root"])):
                yield lambda _: None
                return
            check(guard, area)
            root_guard(guard, ledger, record["root"])

            def remember(result):
                if "raw_path" in result:
                    archive(guard, ledger, record, target=Path(result["raw_path"]), expected=result["raw_sha256"])
                    store(ledger, state)

            yield remember
