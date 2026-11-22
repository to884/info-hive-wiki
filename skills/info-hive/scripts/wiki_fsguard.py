"""設定された根の下に書くための共通部品（ADR-0029）。"""

from __future__ import annotations

import ntpath
import os
import secrets
import shutil
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, Callable

from wiki_links import FILE_ATTRIBUTE_REPARSE_POINT, link_kind
from wiki_pages import absolute_user_path
from wiki_raw import within


class Refused(OSError):
    """リンク、別名、型の不一致などで経路を使えない。"""

    def __init__(self, path: Path, kind: str, reason: str):
        super().__init__(f"{reason}: {path}（{kind}）")
        self.path = path
        self.kind = kind


class ExclusiveUnsupported(OSError):
    """重ねて書かない形で置けないファイルシステム。"""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_link(path: Path, info: os.stat_result, *, allow_hardlink: bool = False) -> None:
    kind = link_kind(info)
    if kind is None and getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        kind = "reparse"
    if kind and not (allow_hardlink and kind == "hardlink"):
        raise Refused(path, kind, "リンクかリパースポイントを通って書き込めない")


def _reserved_windows_name(name: str) -> bool:
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(name)
    return ":" in name or name.endswith((" ", ".")) or PureWindowsPath(name).is_reserved()


def _mount_points(root: Path, *, required: bool = False) -> set[Path]:
    """Linux では同じファイルシステムのマウントも、マウント表で確かめる。"""
    if sys.platform != "linux":
        return set()
    try:
        contents = Path("/proc/self/mountinfo").read_bytes()
    except OSError:
        if required:
            raise
        return set()  # マウント表がない環境でも実体の輪と st_dev は検査する。
    real_root = root.resolve(strict=True)
    points = set()
    for entry in contents.split(b"\n"):
        if not entry:
            continue
        spelling = entry.split(b" ", 5)[4]
        for escaped, byte in ((b"\\040", b" "), (b"\\011", b"\t"), (b"\\012", b"\n"), (b"\\134", b"\\")):
            spelling = spelling.replace(escaped, byte)
        mount = Path(os.fsdecode(spelling))
        if mount != real_root and mount.is_relative_to(real_root):
            points.add(mount.relative_to(real_root))
    return points


def publish_exclusive(tmp: Path, target: Path) -> None:
    """同じディレクトリ内の一時ファイルを、既存の名前を置き換えずに置く。"""
    if os.name == "nt":
        os.rename(tmp, target)
        return
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ExclusiveUnsupported(str(exc)) from exc
    tmp.unlink()


class WriteGuard:
    """根の子から行き先を検査する。操作ごとに、根の配置を確かめ直す。"""

    def __init__(self, root: Path, *, strict_root: bool = False):
        self.root = absolute_user_path(root)
        self.strict_root = strict_root
        self.root_id = _identity(self._root_stat())
        self.mounts = _mount_points(self.root)

    def _root_stat(self) -> os.stat_result:
        if self.strict_root:
            for path in reversed((self.root, *self.root.parents)):
                _check_link(path, path.lstat())
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise Refused(self.root, "not_directory", "根がディレクトリではない")
        return info

    def _parts(self, path: Path) -> tuple[Path, tuple[str, ...]]:
        path = absolute_user_path(path)
        if path == self.root or not path.is_relative_to(self.root):
            raise Refused(path, "outside", "行き先は根の下に指定する")
        parts = path.relative_to(self.root).parts
        if os.name == "nt" and any(_reserved_windows_name(part) for part in parts):
            raise Refused(path, "alias", "Windows の別名になる綴りは使えない")
        return path, parts

    def _check(self, path: Path, *, directory: bool | None, create: bool = False,
               allow_linked_leaf: bool = False) -> os.stat_result | None:
        path, parts = self._parts(path)
        root_info = self._root_stat()
        if _identity(root_info) != self.root_id:
            raise Refused(self.root, "changed", "根の実体が変わった")
        seen = {_identity(p.stat()) for p in self.root.parents}
        seen.add(self.root_id)
        current = self.root
        for index, part in enumerate(parts):
            current /= part
            want_dir = directory is True or index < len(parts) - 1
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    return None
                current.mkdir(exist_ok=True)
                info = current.lstat()
            _check_link(current, info, allow_hardlink=allow_linked_leaf and not want_dir)
            if info.st_dev != root_info.st_dev or current.relative_to(self.root) in self.mounts:
                raise Refused(current, "mount", "根の下のマウントを通って書き込めない")
            if want_dir or (directory is None and stat.S_ISDIR(info.st_mode)):
                if not stat.S_ISDIR(info.st_mode):
                    raise Refused(current, "not_directory", "行き先までの道がディレクトリではない")
                identity = _identity(info)
                if info.st_ino and identity in seen:
                    raise Refused(current, "cycle", "経路が同じディレクトリを再び指している")
                seen.add(identity)
            elif not stat.S_ISREG(info.st_mode):
                raise Refused(current, "not_file", "行き先がふつうのファイルではない")
        return info

    def file_info(self, path: Path) -> os.stat_result | None:
        return self._check(path, directory=False)

    def entry_info(self, path: Path) -> os.stat_result | None:
        """道と最後の名前を調べ、リンクをたどらずに通常のファイルかディレクトリの属性を得る。"""
        return self._check(path, directory=None)

    def directory_info(self, path: Path) -> os.stat_result | None:
        return self._check(path, directory=True)

    def ensure_directory(self, path: Path) -> None:
        self._check(path, directory=True, create=True)

    def tree_entries(self, path: Path) -> list[tuple[Path, os.stat_result]]:
        """根の下の木を、各ディレクトリに降りる前に検査する。親の項目を先に返す。"""
        entries = []
        pending = [absolute_user_path(path)]
        while pending:
            directory = pending.pop()
            if self.directory_info(directory) is None:
                continue
            for child in directory.iterdir():
                info = self.entry_info(child)
                if info is None:
                    continue
                entries.append((child, info))
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child)
        return entries

    def check_sqlite(self, path: Path) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            self.file_info(path.with_name(path.name + suffix))

    def temporary_directory(self, parent: Path, *, prefix: str, suffix: str) -> Path:
        path = parent / f"{prefix}{secrets.token_hex(16)}{suffix}"
        self.directory_info(path)
        path.mkdir()
        self.directory_info(path)
        return path

    def replace_file(self, src: Path, dst: Path, *, delays: tuple[float, ...] = (),
                     expected: os.stat_result | None = None, before_replace: Callable[[], None] | None = None,
                     write_through: bool = False) -> None:
        """再試行のたびに、移動元と移動先の名前を確かめる。"""
        for delay in (*delays, None):
            current = self.file_info(src)
            if current is None:
                raise FileNotFoundError(src)
            if expected is not None and not os.path.samestat(current, expected):
                raise Refused(src, "changed", "一時ファイルの実体が変わった")
            self.file_info(dst)
            if before_replace is not None:
                before_replace()
            try:
                if write_through and os.name == "nt":
                    from wiki_browse_fs import X

                    X.move_write_through(src, dst, replace=True)
                else:
                    os.replace(src, dst)
                return
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)

    def remove_file(self, path: Path) -> None:
        if self.file_info(path) is not None:
            path.unlink()

    def _check_open_file(self, path: Path, fd: int, *, previous: os.stat_result | None = None) -> None:
        opened = os.fstat(fd)
        _check_link(path, opened)
        current = self.file_info(path)
        if (current is None or _identity(current) != _identity(opened)
                or previous is not None and _identity(previous) != _identity(opened)):
            raise Refused(path, "changed", "開いたファイルの実体が変わった")

    @contextmanager
    def open_read(self, path: Path, *, missing_ok=False):
        from wiki_browse_fs import X

        previous = self.file_info(path)
        if previous is None:
            if missing_ok:
                yield None
                return
            raise FileNotFoundError(path)
        with os.fdopen(X.open_plain_read(path), "rb") as handle:
            self._check_open_file(path, handle.fileno(), previous=previous)
            yield handle

    def read_bytes(self, path: Path) -> bytes | None:
        with self.open_read(path, missing_ok=True) as handle:
            return handle.read() if handle is not None else None

    def open_new(self, path: Path, *, mode: int = 0o644) -> BinaryIO:
        """検査した場所に、既にない通常のファイルを開く。"""
        from wiki_browse_fs import X

        self.file_info(path)
        handle = os.fdopen(X.open_excl(path, mode), "wb")
        try:
            self._check_open_file(path, handle.fileno())
        except BaseException:
            handle.close()
            raise
        return handle

    def open_append(self, path: Path, *, mode: int = 0o644) -> BinaryIO:
        from wiki_browse_fs import X

        previous = self.file_info(path)
        handle = os.fdopen(X.open_append(path, mode), "ab")
        try:
            self._check_open_file(path, handle.fileno(), previous=previous)
        except BaseException:
            handle.close()
            raise
        return handle

    def move_directory(self, src: Path, dst: Path) -> None:
        if self.directory_info(src) is None:
            raise FileNotFoundError(src)
        if self.directory_info(dst) is not None:
            raise FileExistsError(dst)
        os.rename(src, dst)

    @contextmanager
    def hold_lock(self, path: Path, *, timeout: float = 0):
        """ロックのファイルは残し、解放してからハンドルを閉じる。"""
        from wiki_browse_fs import X
        self.file_info(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        # Windows ではハンドルを持つ間にロック名が変わらないよう、削除を共有しない。
        fd = X.open_lock(path, share_delete=False) if os.name == "nt" else os.open(path, flags, 0o644)
        try:
            deadline = time.monotonic() + timeout
            while True:
                self.check_lock_file(path, fd)
                if X.try_lock(fd):
                    break
                if time.monotonic() >= deadline:
                    raise Refused(path, "busy", "同じ対象の処理が動いている。終了後にやり直す")
                time.sleep(0.1)
            try:
                self.check_lock_file(path, fd)
                yield fd
            finally:
                X.unlock(fd)
        finally:
            os.close(fd)

    def check_lock_file(self, path: Path, fd: int) -> None:
        current = self.file_info(path)
        if current is None or _identity(current) != _identity(os.fstat(fd)):
            raise Refused(path, "changed", "ロックのファイルの実体が変わった")

    def make_read_only(self, path: Path) -> None:
        info = self.file_info(path)
        if info is None:
            raise FileNotFoundError(path)
        if info.st_mode & 0o222:
            os.chmod(absolute_user_path(path), stat.S_IMODE(info.st_mode) & ~0o222)

    def write_bytes(self, path: Path, data: bytes, *, replace: bool = False,
                    delays: tuple[float, ...] = (), prefix: str = ".wiki-write-", before_publish=None,
                    mode: int = 0o644, temporary: Path | None = None, write_through: bool = False) -> None:
        """一時名に書き、排他公開する。replace のときは検査済みの名前を置き換える。"""
        self._write(path, lambda fh: fh.write(data), replace=replace, delays=delays,
                    prefix=prefix, before_publish=before_publish, mode=mode, temporary=temporary,
                    write_through=write_through)

    def write_stream(self, path: Path, source: BinaryIO, *, mode=0o644, temporary=None,
                     write_through=False, before_publish=None) -> None:
        """読み取り中のストリームを、一時名に写して排他公開する。"""
        self._write(path, lambda fh: shutil.copyfileobj(source, fh), mode=mode, temporary=temporary,
                    write_through=write_through, before_publish=before_publish)

    def _write(self, path: Path, write, *, replace: bool = False, delays: tuple[float, ...] = (),
               prefix: str = ".wiki-write-", before_publish=None, mode: int = 0o644,
               temporary: Path | None = None, write_through: bool = False) -> None:
        from wiki_browse_fs import X

        path = absolute_user_path(path)
        existing = self.file_info(path)
        if existing is not None and not replace:
            raise FileExistsError(path)
        tmp = temporary if temporary is not None else path.with_name(f"{prefix}{secrets.token_hex(16)}.tmp")
        if tmp.parent != path.parent or tmp == path:
            raise Refused(tmp, "temporary", "一時ファイルは行き先と同じディレクトリの別名にする")
        self.file_info(tmp)
        fd = X.open_excl(tmp, mode)
        temp_id = None
        try:
            with os.fdopen(fd, "wb") as fh:
                temp_id = _identity(os.fstat(fh.fileno()))
                write(fh)
                fh.flush()
                os.fsync(fh.fileno())
            if before_publish is not None:
                before_publish()
            present = self.file_info(tmp)
            if present is None or _identity(present) != temp_id:
                raise Refused(tmp, "changed", "一時ファイルの実体が変わった")
            existing = self.file_info(path)
            if replace:
                self.replace_file(tmp, path, delays=delays, expected=present, write_through=write_through)
            elif existing is not None:
                raise FileExistsError(path)
            elif write_through and os.name == "nt":
                X.move_write_through(tmp, path)
            else:
                publish_exclusive(tmp, path)
        finally:
            # 自分が作った名前と確かめられる場合にだけ消す。配置が変わったときは触れない。
            if temp_id is not None:
                self._clean_temp(tmp, temp_id)

    def _clean_temp(self, tmp: Path, temp_id: tuple[int, int]) -> None:
        try:
            # 排他公開で名前を増やしたあとに失敗しても、同じ実体なら一時名を消せる。
            info = self._check(tmp, directory=False, allow_linked_leaf=True)
            if info is not None and _identity(info) == temp_id:
                tmp.unlink()
        except (FileNotFoundError, Refused):
            pass

    def clear_exclusive_temp(self, tmp: Path, target: Path) -> bool:
        """新規公開の link と unlink の間で止まった場合も、一時名だけを外す。"""
        info = self._check(tmp, directory=False, allow_linked_leaf=True)
        if info is None:
            return False
        if info.st_nlink > 1:
            peer = self._check(target, directory=False, allow_linked_leaf=True)
            if (tmp.parent != target.parent or info.st_nlink != 2 or peer is None or
                    _identity(info) != _identity(peer) or not stat.S_ISREG(info.st_mode)):
                raise Refused(tmp, "hardlink", "一時名と公開したファイルの組が一致しない")
            tmp.unlink()
        else:
            self.remove_file(tmp)
        return True


def external_raw_guard(wiki: Path, raw_root: Path) -> WriteGuard:
    """セッションと字幕の原本は、別名のない、wiki から独立した根に置く。"""
    guard = WriteGuard(raw_root, strict_root=True)
    raw_path = guard.root.resolve(strict=True)
    wiki_path = absolute_user_path(wiki).resolve(strict=True)
    if within(raw_path, wiki_path) or within(wiki_path, raw_path):
        raise Refused(raw_path, "overlap", f"raw と wiki の根の一方が他方に含まれる（wiki は {wiki_path}）")
    return guard
