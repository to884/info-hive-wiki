"""wiki ごとに、raw を保存した根の綴りと実体のパスを記録する。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from wiki_fsguard import WriteGuard
from wiki_pages import absolute_user_path


RECORDS = Path(".wiki-cache/raw-roots.d")


def raw_root_pair(raw_root: Path) -> tuple[Path, Path]:
    """指定された根の綴りと、存在するディレクトリの実体を得る。"""
    given = absolute_user_path(raw_root)
    resolved = given.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(f"raw の根がディレクトリではない: {given}")
    return given, resolved


def _write_record(guard: WriteGuard, path: Path, pair: tuple[Path, Path], writer: str) -> None:
    given, resolved = pair
    record = {
        "given": str(given),
        "resolved": str(resolved),
        "writer": writer,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    guard.write_bytes(path, data)


def record_raw_root(wiki: Path, raw_root: Path, *, writer: str) -> None:
    """他の書き手の記録を読まずに、一件を排他公開する。"""
    pair = raw_root_pair(raw_root)
    guard = WriteGuard(wiki)
    directory = guard.root / RECORDS
    guard.ensure_directory(directory)
    _write_record(guard, directory / f"{secrets.token_hex(16)}.json", pair, writer)


def register_upgrade_roots(guard: WriteGuard, pairs: list[tuple[Path, Path]]) -> None:
    """アップグレードの記録は同じ組なら再利用し、取り込みの記録はそのまま残す。"""
    directory = guard.root / RECORDS
    guard.ensure_directory(directory)
    with guard.hold_lock(directory / ".upgrade.lock", timeout=5):
        for pair in dict.fromkeys(pairs):
            key = hashlib.sha256(json.dumps([str(path) for path in pair]).encode("ascii")).hexdigest()
            path = directory / f"upgrade-{key}.json"
            if guard.file_info(path) is None:
                _write_record(guard, path, pair, "init_wiki")
            elif _record_paths(path, writer="init_wiki") != list(pair):
                raise ValueError(f"アップグレードの記録が raw の根と一致しない: {path}")


def _record_paths(path: Path, *, writer: str | None = None) -> list[Path]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("raw の根の記録が JSON オブジェクトではない")
    if writer is not None and data.get("writer") != writer:
        raise ValueError(f"アップグレードの記録の書き手が一致しない: {path}")
    paths = []
    for key in ("given", "resolved"):
        value = data.get(key)
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"raw の根の記録の {key} がパスではない")
        if Path(value).is_absolute():
            paths.append(absolute_user_path(value))
        elif PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
            raise ValueError(f"raw の根の記録の {key} はこの OS では参照できない")
        else:
            raise ValueError(f"raw の根の記録の {key} が絶対パスではない")
    return paths


def read_raw_roots(wiki: Path) -> tuple[list[Path], list[dict]]:
    """記録にある根を、その時点の綴りのまま返す。検査不能な記録も返す。"""
    try:
        root = absolute_user_path(wiki)
        directory = root / RECORDS
        guard = WriteGuard(root)
        if guard.directory_info(directory) is None:
            return [], []
        entries = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except (OSError, ValueError) as exc:
        return [], [{"path": RECORDS.as_posix(), "reason": str(exc)}]

    roots = {}
    unchecked = []
    for entry in entries:
        try:
            if guard.file_info(entry) is None:
                raise FileNotFoundError("一覧の取得後に記録がなくなった")
            roots.update(dict.fromkeys(_record_paths(entry)))
        except (OSError, ValueError, RecursionError) as exc:
            unchecked.append({"path": entry.relative_to(root).as_posix(), "reason": str(exc)})
    return list(roots), unchecked
