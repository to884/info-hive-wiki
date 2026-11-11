"""
wiki_browse_fs.py — 閲覧（browse）の書き込みの守り。

browse のすべての書き込み（ディレクトリを作る、ファイルを書く、名前を変える、消す、ロックのファイルを作る）は、
このモジュールの Guard を通る。書く直前に、対象が守る場所（Wiki のルートと、Wiki の外にある入力の実体）と
重ならないことを、パスの文字ではなく実体（st_dev と st_ino）で確かめる。設計は
`docs/plans/wiki-browse-stage1-2026-11-09.md` の 6 にある。

標準ライブラリと wiki_graph_common（ロック）だけで書き、単独では実行しない。
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import wiki_graph_common

GENERATION_MARKER = ".browse-generation"
WORK_MARKER = ".browse-work"
SERVER_MARKER = ".browse-server"
MARKERS = (GENERATION_MARKER, WORK_MARKER, SERVER_MARKER)


class Refused(Exception):
    """守る場所と重なる、または browse のものでない場所に書こうとしたので、何もせずに止まった。"""


# ---------------------------------------------------------------------------
# 実体の識別
# ---------------------------------------------------------------------------

def _resolved(path) -> Path:
    """シンボリックリンクとジャンクションを解いた、絶対のパス。まだないところは、そのまま足す。"""
    return Path(os.path.realpath(os.path.abspath(str(path))))


def identity(path) -> "tuple | None":
    """パスの実体の識別。ないものは None。

    st_ino が 0 のファイルシステム（識別を返さないもの）では、大文字と小文字をそろえたパスで代える。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_ino:
        return ("id", st.st_dev, st.st_ino)
    return ("path", os.path.normcase(str(_resolved(path))))


def _existing_chain(path) -> list:
    """パスを解き、あるところまでの祖先から根までの、実体の識別の並び（近い順）。"""
    p = _resolved(path)
    chain = []
    for candidate in (p, *p.parents):
        ident = identity(candidate)
        if ident is not None:
            chain.append(ident)
    return chain


def is_within(path, other) -> bool:
    """path が other の中にあるか、同じ場所なら真。実体で比べる。"""
    target = identity(other)
    if target is None:
        return False
    return target in _existing_chain(path)


def overlaps(a, b) -> bool:
    """a と b のどちらかが、もう一方の中にあるか、同じ場所なら真。"""
    return is_within(a, b) or is_within(b, a)


def _is_link(path: Path) -> bool:
    """シンボリックリンクか、Windows のジャンクションなどの再解析点なら真。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


# ---------------------------------------------------------------------------
# 守り
# ---------------------------------------------------------------------------

class Guard:
    """守る場所を持ち、書き込みのたびに確かめる。

    protected は、Wiki のルートと、Wiki の外にある入力の実体のパス。set_protected で決め直せる。
    """

    def __init__(self, protected):
        self.set_protected(protected)

    def set_protected(self, protected) -> None:
        self._ids = set()
        self._chains = set()
        self._paths = []
        for p in protected:
            ident = identity(p)
            if ident is None:
                continue
            self._ids.add(ident)
            self._chains.update(_existing_chain(p))
            self._paths.append(_resolved(p))

    def overlapping(self, path) -> bool:
        """path が守る場所の中にあるか、同じか、守る場所を含むなら真。"""
        if any(ident in self._ids for ident in _existing_chain(path)):
            return True
        ident = identity(path)
        return ident is not None and ident in self._chains

    def check(self, path, contains_ok: bool = False) -> None:
        """書き込みの対象を確かめる。守る場所の中にあるか、同じなら止まる。

        contains_ok が偽なら、対象が守る場所を含むときも止まる（名前を変える、消す操作で、守る場所ごと動かさない）。
        対象の親が守る場所を含むことは拒まない。
        """
        if any(ident in self._ids for ident in _existing_chain(path)):
            raise Refused(f"守る場所（Wiki か、Wiki が読む入力）の中には書かない: {path}")
        if not contains_ok:
            ident = identity(path)
            if ident is not None and ident in self._chains:
                raise Refused(f"守る場所（Wiki か、Wiki が読む入力）を含む場所は動かさない: {path}")

    # --- 書き込みの操作 ---

    def mkdir(self, path, mode: int = 0o755) -> Path:
        path = Path(path)
        self.check(path)
        os.mkdir(path, mode)
        return path

    def write_bytes(self, path, data: bytes, mode: int = 0o644) -> None:
        """同じディレクトリに新しく作った一時のファイルに書いてから、置き換える。あるファイルは開かない。"""
        path = Path(path)
        self.check(path.parent, contains_ok=True)
        self.check(path)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        self.check(temporary)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            self.check(path)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def write_text(self, path, text: str, mode: int = 0o644) -> None:
        self.write_bytes(path, text.encode("utf-8"), mode)

    def rename(self, src, dst) -> None:
        """名前を変える。dst がすでにあれば失敗する（置き換えない）。"""
        src, dst = Path(src), Path(dst)
        self.check(src)
        self.check(dst)
        if os.path.lexists(dst):
            raise FileExistsError(str(dst))
        os.rename(src, dst)

    def rmtree(self, path, last: "str | None" = None) -> None:
        """ディレクトリを消す。シンボリックリンクとジャンクションはたどらず、そのものだけを消す。

        last の名前のファイル（世代の目印）は最後に消す。途中で失敗しても、目印が残り、次の書き出しで消せる。
        """
        path = Path(path)
        if _is_link(path):
            # リンクそのものを消す。先は消さない。
            self.check(path.parent, contains_ok=True)
            _remove_link(path)
            return
        self.check(path)
        _rmtree_nofollow(path, last)

    def unlink(self, path) -> None:
        """ファイルを消す。ないときは何もしない。"""
        path = Path(path)
        if not os.path.lexists(path):
            return
        self.check(path.parent, contains_ok=True)
        if not _is_link(path):
            self.check(path)
        os.unlink(path)

    def rmdir_empty(self, path) -> None:
        """空のディレクトリを取り除く。空でなければ失敗する。"""
        path = Path(path)
        self.check(path)
        os.rmdir(path)

    def make_marked_dir(self, path, marker: str, marker_text: str = "", mode: int = 0o755) -> bool:
        """目印を入れた一時の名前のディレクトリを作ってから、名前を変えて置く。

        目印のないディレクトリが見える途中の状態を作らない。すでにあれば何もせず偽を返す（中身は呼ぶ側が確かめる）。
        """
        path = Path(path)
        if os.path.lexists(path):
            return False
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        self.mkdir(temporary, mode)
        try:
            self.write_text(temporary / marker, marker_text)
            self.check(path)
            if os.path.lexists(path):
                raise FileExistsError(str(path))
            os.rename(temporary, path)
        except FileExistsError:
            self.rmtree(temporary)
            return False
        except OSError:
            self.rmtree(temporary)
            if os.path.isdir(path):
                return False
            raise
        return True

    def open_lock(self, path) -> int:
        """ロックのファイルを開く（なければ作る）。中身は書かず、消さない。開いた fd を返す。"""
        path = Path(path)
        self.check(path.parent, contains_ok=True)
        self.check(path)
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _remove_link(path: Path) -> None:
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError, OSError):
        # Windows のジャンクションとディレクトリのシンボリックリンクは rmdir で外す（先は消えない）。
        os.rmdir(path)


def _rmtree_nofollow(path: Path, last: "str | None" = None) -> None:
    """消せなければ、そのまま失敗する。属性（読み取り専用など）は変えない。ハードリンクの先が Wiki のページなら、
    属性を変えるとページの属性も変わるからである。last の名前のファイルは、最上位で最後に消す（目印を最後まで残す）。"""
    with os.scandir(path) as it:
        entries = sorted(it, key=lambda e: e.name == last)
    for entry in entries:
        child = Path(entry.path)
        if _is_link(child):
            _remove_link(child)
        elif entry.is_dir(follow_symlinks=False):
            _rmtree_nofollow(child)
        else:
            os.unlink(child)
    os.rmdir(path)


# ---------------------------------------------------------------------------
# ロック
# ---------------------------------------------------------------------------

class Lock:
    """OS のファイルのロック。プロセスが止まれば OS が外す。ロックのファイルは消さない。"""

    def __init__(self, guard: Guard, path):
        self.guard = guard
        self.path = Path(path)
        self.fd: "int | None" = None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        fd = self.guard.open_lock(self.path)
        if wiki_graph_common._try_lock(fd):
            self.fd = fd
            return True
        os.close(fd)
        return False

    def acquire(self, wait_seconds: float, interval: float = 0.2) -> bool:
        import time
        deadline = time.monotonic() + wait_seconds
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)
        return True

    def release(self) -> None:
        if self.fd is not None:
            wiki_graph_common._unlock(self.fd)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ---------------------------------------------------------------------------
# browse のディレクトリと目印（設計の 6.4）
# ---------------------------------------------------------------------------

def has_marker(path, marker: str) -> bool:
    return os.path.isfile(Path(path) / marker) and not _is_link(Path(path) / marker)


def markers_above(path) -> list:
    """path の祖先（path そのものは含めない）のうち、browse の目印があるものを返す。"""
    p = _resolved(path)
    found = []
    for ancestor in p.parents:
        for marker in MARKERS:
            if os.path.isfile(ancestor / marker):
                found.append(ancestor / marker)
    return found


def markers_below(path) -> list:
    """path の中（リンクをたどらない）にある browse の目印の、path からの相対パス（/ 区切り）を返す。"""
    root = Path(path)
    found = []
    if not root.is_dir() or _is_link(root):
        return found

    def walk(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            return
        for entry in entries:
            rel = prefix + entry.name
            child = Path(entry.path)
            if _is_link(child):
                continue
            if entry.is_dir(follow_symlinks=False):
                walk(child, rel + "/")
            elif entry.name in MARKERS:
                found.append(rel)

    walk(root, "")
    return found


def check_output_dir(path) -> None:
    """静的の書き出し先として使えるかを確かめる。空か、世代の目印だけを持つディレクトリでなければ止まる。"""
    path = Path(path)
    if markers_above(path):
        raise Refused(f"ほかの browse のディレクトリの中には書き出さない: {path}")
    if not os.path.lexists(path):
        return
    if _is_link(path) or not path.is_dir():
        raise Refused(f"書き出し先がディレクトリでない: {path}")
    below = markers_below(path)
    if below and below != [GENERATION_MARKER]:
        raise Refused(f"書き出し先の中に、ほかの browse のディレクトリがある: {path}")
    if not below and any(path.iterdir()):
        raise Refused(f"書き出し先が空でなく、browse の書き出しでもない: {path}")


def check_work_dir(path) -> None:
    """作業用のディレクトリとして使えるかを確かめる。ないか、作業用の目印と、その直下の世代の目印だけを持つこと。"""
    path = Path(path)
    if markers_above(path):
        raise Refused(f"ほかの browse のディレクトリの中には作業用のディレクトリを置かない: {path}")
    if not os.path.lexists(path):
        return
    if _is_link(path) or not path.is_dir():
        raise Refused(f"作業用のディレクトリがディレクトリでない: {path}")
    if not has_marker(path, WORK_MARKER):
        raise Refused(f"目印のない既存のディレクトリは、作業用のディレクトリに使わない: {path}")
    for rel in markers_below(path):
        if rel == WORK_MARKER:
            continue
        parts = rel.split("/")
        if len(parts) == 2 and parts[1] == GENERATION_MARKER and parts[0].startswith(("tmp-", "old-")):
            continue
        raise Refused(f"作業用のディレクトリの中に、ほかの browse のディレクトリがある: {path}")


def check_server_dir(path) -> None:
    """サーバーの状態のディレクトリとして使えるかを確かめる。ないか、サーバーの目印だけを持つこと。"""
    path = Path(path)
    if markers_above(path):
        raise Refused(f"ほかの browse のディレクトリの中には、サーバーの状態を置かない: {path}")
    if not os.path.lexists(path):
        return
    if _is_link(path) or not path.is_dir():
        raise Refused(f"サーバーの状態のディレクトリがディレクトリでない: {path}")
    if markers_below(path) != [SERVER_MARKER]:
        raise Refused(f"目印のない、またはほかの browse の目印を持つディレクトリは、サーバーの状態に使わない: {path}")
