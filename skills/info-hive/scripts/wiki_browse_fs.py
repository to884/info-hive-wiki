"""
wiki_browse_fs.py — 閲覧（browse）のファイルの入出力の層。

browse のファイルの入出力は、すべてこのモジュールを通す。wiki_browse.py と wiki_browse_core.py は、入出力を
呼ばない。設計は `docs/plans/wiki-browse-structure-2026-11-11.md`（3、6、7.1）にある。

- 口（Os）: OS を呼ぶ決まった口。呼ぶたびに os から引いて呼ぶ。試験は、口の呼び出しの N 回目に手を入れる。
- 失敗の読み替え（x）: 「ない」（Absent）と「分からない」（Unknown）に分ける。読み替えはここだけで行う。
- 書き込みの口（w）: どれも、書き込む場所が Wiki のルートと重ならないことを確かめる（Guard。設計の 6.1）。
  名前そのものに働き、最後の名前のリンクをたどらない。
- 領分（設計の 2.3、6.2）: <data_dir>/browse/ と、--out の書き出し先の隣の作業用のディレクトリ。browse は、
  領分の外では、自分が作ったものだけを置き換え、消す（守ること 1。ADR-0017）。
- 静的の書き出し（build。設計の 6.3、6.4）と、サーバーの記録（設計の 7.1）。

標準ライブラリと wiki_pages（走査と識別子の関数）だけで書き、単独では実行しない。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import time

import wiki_pages

WORK_MARKER = ".browse-work"
SERVER_MARKER = ".browse-server"
MANIFEST = ".browse-generation"
MARKERS = (WORK_MARKER, SERVER_MARKER, MANIFEST)
LEGACY = ("seq", "build-report.json")       # 0.10.0 が作業用のディレクトリに書いたもの。消す
STATE_KNOWN = ("server.lock", "server.json", "server.json.new", SERVER_MARKER + ".new")
O_BINARY = getattr(os, "O_BINARY", 0)
NT = os.name == "nt"
COMMON_LOCK_WAIT = 60.0

# 対照（設計の 9.5。わざと破った規則）の名前。試験だけが足す。空なら、どの対照も働かない。
MUTANTS: set = set()
# 単位の中で出した警告。呼ぶ側が、単位の終わりに標準エラーへ出す。
WARNINGS: list = []


class Unknown(Exception):
    """分からない。ないこととして扱わない（P1）。"""


class Absent(Exception):
    """ない。"""


class Stop(Exception):
    """何も変えずに止まり、利用者に知らせる。"""


def failure(cls, message, path=None, os_error=None):
    """失敗の例外に、対象のパスと、元の OSError の型を持たせる。警告が、どの項目の失敗かを名指せるようにする（設計の 6.2）。"""
    e = cls(message)
    e.path = path
    e.os_error = os_error
    return e


def warn(message: str) -> None:
    WARNINGS.append(message)


# ---------------------------------------------------------------------------
# 口（設計の 3.1）
# ---------------------------------------------------------------------------

_CREATE_NEW, _OPEN_EXISTING, _OPEN_ALWAYS = 1, 3, 4
_WIN = None


def _win():
    """CreateFileW と GetFileInformationByHandle（ctypes）。1 度だけ用意する。"""
    global _WIN
    if _WIN is None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        class Info(ctypes.Structure):
            _fields_ = [("attrs", wintypes.DWORD), ("c", wintypes.FILETIME), ("a", wintypes.FILETIME),
                        ("w", wintypes.FILETIME), ("vol", wintypes.DWORD), ("sh", wintypes.DWORD),
                        ("sl", wintypes.DWORD), ("links", wintypes.DWORD), ("ih", wintypes.DWORD),
                        ("il", wintypes.DWORD)]
        k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
        k32.GetFileInformationByHandle.restype = wintypes.BOOL
        _WIN = (ctypes, msvcrt, wintypes, k32, Info)
    return _WIN


def _win_open(path, disposition, flags):
    """Windows で、最後の名前の再解析点をたどらずに、ファイルを作るか開く。開いたものが通常のファイルでなければ失敗する。

    CreateFileW に FILE_FLAG_OPEN_REPARSE_POINT を付ける。名前がリンクなら、リンクそのものを開くので、先は作らない
    （os.open の O_CREAT は、O_EXCL を付けても、先のないリンクをたどって先を作る。設計の 3.1）。開いたハンドルの属性で、
    再解析点とディレクトリを拒む（名前を調べてから開く形にしない。設計の 3.2）。
    """
    ctypes, msvcrt, wintypes, k32, Info = _win()
    access = 0x40000000 | (0x80000000 if flags == os.O_RDWR else 0)      # GENERIC_WRITE と GENERIC_READ
    # FILE_ATTRIBUTE_NORMAL、FILE_FLAG_OPEN_REPARSE_POINT
    handle = k32.CreateFileW(os.fspath(path), access, 0x7, None, disposition, 0x80 | 0x00200000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        code = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(code).strip(), os.fspath(path), code)
    try:
        info = Info()
        if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            code = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(code).strip(), os.fspath(path), code)
        if info.attrs & (0x400 | 0x10):       # 再解析点、ディレクトリ
            raise OSError(errno.EPERM, "開いた名前が、通常のファイルでない", os.fspath(path))
        return msvcrt.open_osfhandle(handle, flags | O_BINARY)
    except BaseException:
        k32.CloseHandle(handle)
        raise


class Os:
    """OS を呼ぶ口。どの口も、呼ぶたびに os から引く。試験は、この口の呼び出しの N 回目に手を入れる。"""

    def lstat(self, path):
        return os.lstat(path)

    def stat(self, path):
        return os.stat(path)

    def file_id(self, path):
        """識別子（走査の計画と同じ関数）。リンクの先をたどる。Windows は win_file_id、POSIX は st_dev と st_ino。"""
        if NT:
            return wiki_pages.win_file_id(path)
        st = os.stat(path)
        return st.st_dev, st.st_ino

    def realpath(self, path):
        return os.path.realpath(path, strict=True)

    def scandir_open(self, path):
        return os.scandir(path)

    def scandir_next(self, it):
        """項目を 1 つ取り出す。終わりなら None。閉じる操作は、失敗が見えないので口に入れない（設計の 3.1）。"""
        try:
            return next(it).name
        except StopIteration:
            return None

    def mkdir(self, path, mode=0o777):
        os.mkdir(path, mode)

    def open_excl(self, path, mode=0o644):
        """排他で作る。最後の名前がリンクでも、たどらない（設計の 3.1）。mode は POSIX の権限（Windows では使わない）。"""
        if "follow_on_open" in MUTANTS:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, mode)    # 対照: 6 版目の形
        if NT:
            return _win_open(path, _CREATE_NEW, os.O_WRONLY)
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)

    def open_read(self, path):
        """読むために開く。入力の実体はリンクの先でも読む（設計の 5）。"""
        return os.open(path, os.O_RDONLY | O_BINARY)

    def open_lock(self, path, create=True):
        """ロックのファイルを開く。create なら、なければ作る。最後の名前がリンクなら、たどらずに失敗する。"""
        if "follow_on_open" in MUTANTS:
            return os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | O_BINARY, 0o600)  # 対照: 6 版目の形
        if NT:
            return _win_open(path, _OPEN_ALWAYS if create else _OPEN_EXISTING, os.O_RDWR)
        fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EPERM, "ロックのファイルが通常のファイルでない", path)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def write(self, fd, data):
        return os.write(fd, data)

    def read(self, fd, size):
        return os.read(fd, size)

    def close(self, fd):
        os.close(fd)

    def rename(self, src, dst):
        os.rename(src, dst)

    def replace(self, src, dst):
        os.replace(src, dst)

    def unlink(self, path):
        os.unlink(path)

    def rmdir(self, path):
        os.rmdir(path)

    def stdout_write(self, text):
        """知らせの出力（通信）。確定のあとの失敗は、終了コードを変えない（設計の 7）。"""
        sys.stdout.write(text)
        sys.stdout.flush()
        return len(text)

    def chmod(self, path, mode):      # 対照だけが使う。規則では、属性を変えない
        os.chmod(path, mode)

    def try_lock(self, fd):
        """取れたら真、ほかが持っていれば偽。ロックの操作そのものの失敗は、OSError のまま出す（設計の 3.1）。"""
        if NT:
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as e:
                if e.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", 36)):   # ほかが持っている
                    return False
                raise
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def unlock(self, fd):
        if NT:
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)


X = Os()

# 失敗の分け方（設計の 3.2。走査の計画と同じ）。解けないリンク（ELOOP、Windows のエラー 1921）と、正しくない名前
# （Windows のエラー 123）は「ない」。権限、識別子がない、準備のできていないドライブ、そのほかは「分からない」。
_ABSENT_ERRNOS = (errno.ELOOP,)
_ABSENT_WINERRORS = (1921, 123)


def classify(exc: OSError) -> type:
    """OSError を Absent か Unknown に分ける。読み替えは、x() とこの関数だけで行う（設計の 3.2）。"""
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return Absent
    winerror = getattr(exc, "winerror", None)
    if winerror in _ABSENT_WINERRORS or (winerror is None and exc.errno in _ABSENT_ERRNOS):
        return Absent
    return Unknown


def x(name, *args):
    """口を呼ぶ。失敗の読み替えは、ここだけで行う（設計の 3.2）。例外は、失敗した口と、その対象のパスを持つ。"""
    path = os.fspath(args[0]) if args and isinstance(args[0], (str, os.PathLike)) else None
    try:
        return getattr(X, name)(*args)
    except OSError as e:
        raise failure(classify(e), f"{name}: {type(e).__name__}", path, type(e)) from None


def listdir(path):
    """項目の名前を、最後まで取り出してから返す（並べ替える）。"""
    it = x("scandir_open", path)
    try:
        names = []
        while True:
            name = x("scandir_next", it)
            if name is None:
                return sorted(names)
            names.append(name)
    finally:
        it.close()


def kind(path):
    """'absent'、'file'、'dir'、'link'（シンボリックリンクと、ジャンクションなどの再解析点）、'other'。

    lstat から使うのは種類だけである（識別子は使わない。設計の 3.1）。Windows で種類がブロックデバイスなら、
    属性を取れなかったものとして「分からない」とする。
    """
    try:
        st = x("lstat", path)
    except Absent:
        return "absent"
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        if "follow_links" not in MUTANTS:
            return "link"
        try:
            st = X.stat(path)           # 対照: リンクの先を見る
        except OSError:
            return "link"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    if NT and stat.S_ISBLK(st.st_mode):
        raise failure(Unknown, "lstat: 属性を取れない（S_IFBLK）", os.fspath(path))
    return "other"


def ident(path):
    """識別子。取れなければ Unknown（識別子が 0 のときを含む）。"""
    value = x("file_id", path)
    if NT:
        if not any(value[1]):
            raise failure(Unknown, "file_id: 識別子が 0", os.fspath(path))
    elif value[1] == 0:
        raise failure(Unknown, "file_id: 識別子が 0", os.fspath(path))
    return value


def owner_of(root) -> str:
    """Wiki のルートの識別子を文字にしたもの。目印と記録に書き、触れるたびに照らす（設計の 7.1）。"""
    dev, fid = ident(x("realpath", os.fspath(root)))
    return f"{dev}:{fid.hex() if isinstance(fid, bytes) else fid}"


# ---------------------------------------------------------------------------
# Wiki のルートと重ならないことの確かめ（設計の 6.1）。書き込みの口が、どれもこれを通す。
# ---------------------------------------------------------------------------

class Guard:
    """1 つの仕事の単位のあいだ、Wiki のルートとその祖先の識別子と、確かめの結論を覚えておく。

    ほかのディレクトリの識別子は覚えない。browse は、単位の中で、自分で名前を消し、付け替え、作り直す
    （tmp-N、old-N、del-N、書き出し先）ので、名前の識別子は単位の中でも変わる（設計の 6.1）。
    結論（その名前か、その解いたパスが、Wiki のルートでもその下でもない）は、名前を作り直しても変わらない。
    作り直す名前は、領分の根より下で、リンクを通らずに届き、親が同じだからである（設計の 3.2）。
    """

    def __init__(self, root):
        self.root = None if root is None else os.fspath(root)
        self.root_id = None
        self.root_chain = None
        self.ok = set()          # 確かめた名前
        self.ok_real = set()     # 確かめた解いたパス（そのパスも、その親も、Wiki のルートでない）
        self.ids = {}

    def ident(self, path):
        if "cache_ids_by_name" in MUTANTS:
            # 対照: 8 版目の初めの形（7 版目まで）。名前ごとに識別子を覚え、取り直さない
            if path not in self.ids:
                self.ids[path] = ident(path)
            return self.ids[path]
        return ident(path)

    def _root(self):
        if self.root_chain is None:
            p = x("realpath", self.root)
            ids = []
            while True:
                ids.append(self.ident(p))
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
            self.root_id = ids[0]
            self.root_chain = set(ids)

    def _under_root(self, r, rid):
        """r（解いたパス。識別子は rid）か、その親のどれかが、Wiki のルートなら True。確かめた解いたパスに着けば止める。"""
        seen, p, pid = [], r, rid
        while p not in self.ok_real:
            if pid == self.root_id:
                return True
            seen.append(p)
            parent = os.path.dirname(p)
            if parent == p or parent in self.ok_real:
                break
            p, pid = parent, self.ident(parent)
        self.ok_real.update(seen)
        return False

    def place(self, directory):
        """directory（今あるディレクトリ）の中に名前を足す、変える、消すことができるか。Wiki のルートかその下なら Stop。

        directory そのものを見て、祖先にはさかのぼらない。書き込みの口は、どれも親がなければ成り立たないので、
        「ない」の答えは、そのまま失敗にする。「ない」という答えから、あとの書き込みが安全だと決めない（設計の 6.1）。
        覚えるのは、あると見て、識別子を取ったディレクトリだけである。
        """
        if "no_root_guard" in MUTANTS or directory in self.ok:
            return
        self._root()
        p = directory
        if "climb_on_absent" in MUTANTS:
            # 対照: 6 版目の初めの形。「ない」なら祖先にさかのぼり、祖先で確かめた結論を、ないと答えた名前に残す
            while kind(p) == "absent":
                parent = os.path.dirname(p)
                if parent == p:
                    raise failure(Unknown, "今ある祖先がない", directory)
                p = parent
        else:
            k = kind(p)
            if k == "absent":
                raise failure(Absent, "lstat: 書き込む場所がない", p)
            if k not in ("dir", "link"):
                raise failure(Unknown, "lstat: 書き込む場所がディレクトリでない", p)
        r = x("realpath", p)
        rid = self.ident(r)
        if rid != self.ident(p):
            raise failure(Unknown, "realpath: 解いたパスの識別子が違う", p)
        if self._under_root(r, rid):
            raise Stop(f"Wiki のルートの下には書かない: {os.path.basename(directory)}")
        self.ok.add(directory)

    def not_root_ancestor(self, path):
        """path（今あるディレクトリ）が、Wiki のルートか、その祖先なら Stop。"""
        if "no_root_guard" in MUTANTS:
            return
        self._root()
        if self.ident(x("realpath", path)) in self.root_chain:
            raise Stop(f"Wiki のルートか、その祖先は動かさない: {os.path.basename(path)}")


G = Guard(None)


def w(name, *args):
    """書き込みの口。対象の場所を確かめてから、x() を呼ぶ（設計の 6.1）。"""
    if name in ("mkdir", "open_excl", "open_lock", "unlink"):
        G.place(os.path.dirname(args[0]))
    elif name == "rmdir":
        G.place(os.path.dirname(args[0]))
        if kind(args[0]) == "dir":
            G.not_root_ancestor(args[0])
    elif name in ("rename", "replace"):
        src, dst = args[:2]
        G.place(os.path.dirname(src))
        G.place(os.path.dirname(dst))
        if kind(src) == "dir":
            G.not_root_ancestor(src)
    return x(name, *args)


# ---------------------------------------------------------------------------
# 読む、作る、消す
# ---------------------------------------------------------------------------

def _close(fd):
    """読み終えたファイルとロックのファイルを閉じる。失敗は捨てる（閉じれば OS が外し、結果を変えない。設計の 9.2）。"""
    try:
        X.close(fd)
    except OSError:
        pass


def read_file(path) -> bytes:
    """ファイルの中身を読む。ないなら Absent、読めなければ Unknown。"""
    fd = x("open_read", path)
    try:
        chunks = []
        while True:
            chunk = x("read", fd, 1 << 20)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        _close(fd)


def create_file(path, data: bytes, mode: int = 0o644) -> None:
    """排他で作る。今あるファイルは開かない。短い書き込みは、書き終わるまで続ける。"""
    fd = w("open_excl", path, mode)
    try:
        view = memoryview(data)
        while view:
            written = x("write", fd, view)
            view = view[written:]
    except BaseException:
        _close(fd)
        raise
    if "swallow_close" in MUTANTS:
        _close(fd)
        return
    x("close", fd)      # 書いたあとの close の失敗は、書けなかったこととして扱う（設計の 3.2）


def remove_tree(path) -> None:
    """領分の中のものを消す。リンクはたどらず、リンクそのものを外す。属性は変えない。分からなければ止まる。"""
    k = kind(path)
    if k == "absent":
        return
    if k == "link":
        if "fallback_rmdir" in MUTANTS:
            try:
                w("unlink", path)
            except Unknown:
                w("rmdir", path)      # 対照: 0.10.0 と 6 版目の初めの規則の形（どの失敗も rmdir で試し直す）
            return
        # os.unlink は、Windows でもジャンクションとディレクトリのリンクを外す。失敗しても、別の口で試し直さない（設計の 3.2）
        w("unlink", path)
        return
    if k == "dir":
        # 世代の一覧は最後に消す。途中で止まった世代が一覧を持ったまま残る形を、del-N に付け替える規則が
        # 戻す候補から外す（設計の 6.2）
        for name in sorted(listdir(path), key=lambda n: n == MANIFEST):
            remove_tree(os.path.join(path, name))
        w("rmdir", path)
        return
    try:
        w("unlink", path)
    except Unknown:
        if "chmod_on_delete" not in MUTANTS:
            raise
        X.chmod(path, 0o666)          # 対照: 属性を変えて消す
        w("unlink", path)


def move(src, dst) -> None:
    """名前を付け替える。先があれば、付け替えない。"""
    if kind(dst) != "absent":
        raise failure(Unknown, "rename: 付け替える先がある", dst)
    w("rename", src, dst)


def replace_file(path, data: bytes, mode: int = 0o644) -> None:
    """領分の中のファイルを置き換える。前の残りの .new を消し、.new を排他で作り、置き換える。"""
    new = path + ".new"
    remove_tree(new)
    create_file(new, data, mode)
    w("replace", new, path)


def read_text(path) -> "str | None":
    """領分の中の通常のファイルを読む。ないか、通常のファイルでないか、UTF-8 でなければ None。読めなければ Unknown。

    名前がリンクなら読まない（リンクの先の値を、持ち主の証や記録にしない。設計の 6.2）。
    """
    if kind(path) != "file":
        return None
    try:
        return read_file(path).decode("utf-8")
    except Absent:
        return None
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# 領分（設計の 2.3、6.2）
# ---------------------------------------------------------------------------

def use_territory(path, marker, owner=None, lock_name=None, mode=0o777):
    """目印があるか、空なら、領分として使う。目印がなく、空でもないディレクトリは、使わない。

    owner を渡すと、目印に Wiki のルートの識別子を書き、使うたびに照らす。違えば、領分のロックが空いて
    いるときだけ、警告を出して引き継ぐ。ロックが取られていれば（別の Wiki が使っている）、止まる。
    目印の名前がリンクなら、読まずに、持ち主が違うものとして扱う（持ち主を照らさない --out の作業用でも、
    リンクを目印のファイルに置き換えて警告する。設計の 6.2）。
    """
    k = kind(path)
    if k == "absent":
        _mkdir_shared(path, mode)
        k = "dir"
    if k != "dir":
        raise Stop(f"ディレクトリでない（リンクを含む）: {os.path.basename(path)}")
    entries = listdir(path)
    marker_path = os.path.join(path, marker)
    content = (owner or "").encode("utf-8")
    if marker not in entries:
        if entries:
            raise Stop(f"目印がなく、空でもないので使わない: {os.path.basename(path)}")
        try:
            _put_marker(marker_path, content)
            return
        except Unknown as e:
            # ほかの browse が同時に目印を置いた（排他で作る口が「もうある」で失敗した）。目印のある領分として続ける
            if e.os_error is not FileExistsError:
                raise
        entries = listdir(path)
    _check_marker(path, marker_path, marker, entries, content, owner, lock_name)


def _put_marker(marker_path, content):
    """目印を置く。"""
    if "swallow_marker_failure" in MUTANTS:
        try:
            create_file(marker_path, content)
        except Unknown:
            if kind(marker_path) != "file":     # 5 版目の試作の形: ファイルがあれば失敗を捨てる
                raise
    elif "warn_on_marker_close" in MUTANTS:
        try:
            create_file(marker_path, content)
        except Unknown as e:
            warn(f"目印を書けなかった（{e}）")      # 対照: 確定の前の失敗を、警告して続ける
    else:
        create_file(marker_path, content)


def _check_marker(path, marker_path, marker, entries, content, owner, lock_name):
    """目印のある領分を使う前の確かめ（持ち主と、目印の名前のリンク）。"""
    if "refuse_unknown_entries" in MUTANTS:
        # 3 版目の規則: 記録と、ロックと、目印のほかの項目があれば使わない（置き換えの一時ファイルも拒む）
        extra = [e for e in entries if e != marker and e not in STATE_KNOWN and e != "build.lock"
                 or e.endswith(".new")]
        if extra:
            raise Stop(f"決まった名前のほかの項目がある: {extra[0]}")
    marker_kind = kind(marker_path)
    if owner is None or "skip_owner" in MUTANTS:
        if marker_kind == "link" and lock_name is not None and "skip_owner" not in MUTANTS:
            _replace_marker(path, marker_path, content, lock_name, "目印がリンクだったので、ファイルに置き換えた")
        return
    current = read_text(marker_path) if marker_kind == "file" else None
    if current != owner:
        _replace_marker(path, marker_path, content, lock_name,
                        "目印がリンクだったので、ファイルに置き換えた" if marker_kind == "link"
                        else "別の Wiki が使っていた場所を引き継いだ")


def _replace_marker(path, marker_path, content, lock_name, message):
    probe = Lock(os.path.join(path, lock_name))
    if not probe.try_acquire():
        raise Stop("別の Wiki が、同じ場所を使っている")
    try:
        replace_file(marker_path, content)
        warn(message)
    finally:
        probe.release()


class Lock:
    """OS のファイルのロック。プロセスが止まれば OS が外す。ロックのファイルは消さない。"""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def try_acquire(self) -> bool:
        if self.fd is not None:
            return True
        # 領分の中のロックの名前に、利用者が置いたリンクがあれば、たどらずに外してから作る（設計の 6.2）。
        # 外すかどうかは、見た種類で決める。種類の答えが誤っていても、ロックの口がリンクをたどらずに失敗する。
        if kind(self.path) == "link":
            remove_tree(self.path)
        fd = w("open_lock", self.path)
        try:
            got = x("try_lock", fd)
        except BaseException:
            _close(fd)
            raise
        if not got:
            _close(fd)
            return False
        self.fd = fd
        return True

    def acquire(self, wait_seconds: float, interval: float = 0.2) -> bool:
        deadline = time.monotonic() + wait_seconds
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                X.unlock(self.fd)
            except OSError:
                pass          # 閉じれば、OS が外す（設計の 3.1）
            _close(self.fd)
            self.fd = None


def lock_held(path) -> bool:
    """名前が通常のファイルのロックを、作らずに開いて、ほかが持っているかを確かめる。ないかリンクなら偽。

    stop と動いているかの確かめは、何も作らない（設計の 6.2、7.1）。動いているサーバーは、通常のファイルの
    ロックを持つ。分からなければ Unknown。
    """
    if kind(path) != "file":
        return False
    try:
        fd = x("open_lock", path, False)
    except Absent:
        return False
    try:
        got = x("try_lock", fd)
        if got:
            try:
                X.unlock(fd)
            except OSError:
                pass
        return not got
    finally:
        _close(fd)


def enter_root(root_dir, create) -> bool:
    """領分の根（<data_dir>/browse）に入る。根があれば True、なければ False（create なら作って True）。

    <data_dir> は、どうたどって届いても、利用者が browse にデータを置かせる場所である（指定。設計の 2.3）。
    領分は、そこにある実の browse ディレクトリと、その下でリンクを通らずに届くものである。根の名前そのものが
    リンクか、ディレクトリでなければ、その先は利用者が指した場所でないので、使わずに止まる。
    根より上（<data_dir> とその祖先）は、利用者が指した場所への道で、リンクでもよい。ないものだけを作る。
    """
    if "enter_root_unchecked" in MUTANTS:
        # 対照: 7 版目の形。今ある根は、種類を見ずに使う（ジャンクションの先を領分として扱う）
        if kind(root_dir) == "absent" and not create:
            return False
        if create:
            ensure_dirs(root_dir)
        return True
    k = kind(root_dir)
    if k == "absent":
        if not create:
            return False
        ensure_dirs(os.path.dirname(root_dir))
        _mkdir_shared(root_dir)
        return True
    if k != "dir":
        raise Stop(f"browse のデータの場所がディレクトリでない（リンクを含む）。使わずに止める: {root_dir}")
    return True


def ensure_dirs(path) -> None:
    """ない祖先を、上から順に作る（<data_dir> とその上）。今ある祖先は、リンクでもそのまま通る。"""
    missing = []
    p = path
    while kind(p) == "absent":
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            raise failure(Unknown, "今ある祖先がない", path)
        p = parent
    for d in reversed(missing):
        _mkdir_shared(d)


def _mkdir_shared(path, mode=0o777) -> None:
    """ほかの browse も同時に作りうるディレクトリ（<data_dir> とその祖先、領分の根）を作る。

    作る口が「もうある」で失敗し、名前そのものが実のディレクトリになっていれば、ほかの browse が先に作ったものとして
    使う（0.10.0 と同じ。初めての 2 つの書き出しが同時に走る場合）。種類を見るだけで、別の口で作り直さない。
    """
    try:
        w("mkdir", path, mode)
    except Unknown as e:
        if e.os_error is not FileExistsError or kind(path) != "dir":
            raise


# ---------------------------------------------------------------------------
# 世代と門（設計の 6.3）
# ---------------------------------------------------------------------------

def make_manifest(files: dict, seq: int) -> bytes:
    """一覧。その世代のファイルとディレクトリの名前、ファイルの大きさと SHA-256、記録のための通し番号。"""
    entries = {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
               for name, data in files.items()}
    dirs = sorted({"/".join(name.split("/")[:i]) for name in files for i in range(1, name.count("/") + 1)})
    return json.dumps({"published_seq": seq, "files": entries, "dirs": dirs}, sort_keys=True).encode("utf-8")


def read_manifest(directory) -> "dict | None":
    """一覧を読む。ないか、形が違えば None。読めなければ Unknown。"""
    text = read_text(os.path.join(directory, MANIFEST))
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("published_seq"), int):
        return None
    return data


def verify_generation(directory, manifest) -> "str | None":
    """世代の全体を、読んである一覧と照らす。合えば None、合わなければ理由を返す。調べられなければ Unknown。"""
    if manifest is None:
        return "一覧がない"
    files, dirs = manifest.get("files"), manifest.get("dirs")
    if not isinstance(files, dict) or not isinstance(dirs, list):
        return "一覧に名前がない（0.10.0 の書き出し。消してからやり直す）"
    seen = set()

    def walk(rel):
        for name in listdir(os.path.join(directory, *rel.split("/")) if rel else directory):
            child = f"{rel}/{name}" if rel else name
            k = kind(os.path.join(directory, *child.split("/")))
            if child == MANIFEST:
                continue
            if k == "dir" and child in dirs:
                seen.add(child)
                reason = walk(child)
                if reason:
                    return reason
            elif k == "file" and child in files and isinstance(files[child], dict):
                data = read_file(os.path.join(directory, *child.split("/")))
                if hashlib.sha256(data).hexdigest() != files[child].get("sha256"):
                    return f"中身が違う: {child}"
                seen.add(child)
            else:
                return f"一覧にない、または種類が違う: {child}"
        return None

    reason = walk("")
    if reason:
        return reason
    missing = (set(files) | set(dirs)) - seen
    return f"一覧にあるものがない: {sorted(missing)[0]}" if missing else None


def out_state(out, verify):
    """('absent' | 'empty' | 'generation' | 'foreign' | 'present', 理由, 通し番号)。

    verify が偽（既定の書き出し先）なら、前の世代を読まない。'present' を返す。
    一覧は 1 度だけ読み、判定にも通し番号にも、その 1 度の結果を使う。
    """
    k = kind(out)
    if k == "absent":
        return "absent", None, 0
    if k != "dir" and (verify or "accept_out_link" not in MUTANTS):
        # 名前そのものがディレクトリでない（リンクを含む）。--out も既定も、門で止まる（設計の 6.2、6.3）。
        # 書き出しの始めの確かめと同じ決まりである。始めの答えが誤っても、ここで同じ決まりが止める
        return "foreign", "ディレクトリでない（リンクを含む）", 0
    if not verify and "default_verifies" not in MUTANTS:
        if k == "dir" and not listdir(out):
            return "empty", None, 0
        return "present", None, 0
    if not listdir(out):
        return "empty", None, 0
    manifest = read_manifest(out)
    seq = manifest["published_seq"] if manifest is not None else 0
    reason = verify_generation(out, manifest)
    return ("generation", None, seq) if reason is None else ("foreign", reason, seq)


def _number(name) -> int:
    try:
        return int(name.split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _existing_ancestor(path):
    """path か、その祖先のうち、今あるもの（種類が「ない」でないもの）。"""
    p = path
    while kind(p) == "absent":
        parent = os.path.dirname(p)
        if parent == p:
            raise failure(Unknown, "今ある祖先がない", path)
        p = parent
    return p


def _check_nesting(out, browse_root) -> None:
    """--out の書き出し先を、ほかの browse のディレクトリ（目印を持つディレクトリ）と、browse のデータの場所の中に
    作らない。browse のデータの場所も、--out の中に置かない（設計の 6.1 の入れ子。0.10.0 と同じ）。

    守りではない。入れ子にすると、外側の世代が「一覧にない項目がある」として次の書き出しで止まるので、先に知らせる。
    何も作る前に呼ぶ。browse のデータの場所がまだなければ、今ある祖先で照らす（作ってから拒むと、拒んだ書き出し先が
    変わる。実装の 1 回目のレビューの重大 1）。
    """
    data_id = ident(x("realpath", browse_root)) if kind(browse_root) == "dir" else None
    p = x("realpath", os.path.dirname(out))
    while True:
        for marker in MARKERS:
            if kind(os.path.join(p, marker)) != "absent":
                raise Stop(f"ほかの browse のディレクトリの中には書き出さない: {os.path.dirname(out)}")
        if data_id is not None and ident(p) == data_id:
            raise Stop(f"--out は、browse のデータの場所（{browse_root}）と重ならない場所にする")
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if kind(out) == "dir":
        q = x("realpath", _existing_ancestor(browse_root))
        out_id = ident(x("realpath", out))
        while True:
            if ident(q) == out_id:
                raise Stop(f"--out は、browse のデータの場所（{browse_root}）と重ならない場所にする")
            parent = os.path.dirname(q)
            if parent == q:
                return
            q = parent


def _check_out(out) -> None:
    """書き出し先そのものとその親を確かめる（設計の 6.1、6.4 の 1）。書き込みの口も確かめるが、ここで止めれば作業用も
    作らない。書き出し先の名前そのものが、ないか実のディレクトリでなければ止まる。"""
    G.place(os.path.dirname(out))
    k = kind(out)
    if k == "dir":
        G.not_root_ancestor(out)
    elif k != "absent" and "accept_out_link" not in MUTANTS:
        raise Stop(f"書き出し先がディレクトリでない（リンクを含む）: {out}")


def build(root, out, files: dict, default: bool, owner: str, browse_root, report_text: str = "",
          lock_wait: float = COMMON_LOCK_WAIT) -> int:
    """静的の書き出し（設計の 6.4）。0 を返せば、書き出し先は新しい世代である。Stop と Unknown は、前のままで止まる。

    files は {出力の / 区切りの相対パス: 中身のバイト列}。report_text は、確定のあとに標準出力へ出す知らせである。

    拒むかどうかの判定（書き出し先とその親、入れ子、browse のデータの場所との重なり）は、何かを作る前に済ませる。
    拒む書き出し先と、Wiki のルートと重なる書き出し先では、砂場の全体が変わらない（設計の 9.1）。既定の書き出し先は、
    親が領分の根なので、根に入ってから確かめる（根を作る口も、Wiki のルートとの重なりを確かめる）。
    """
    global G
    G = Guard(root)
    out, browse_root = os.fspath(out), os.fspath(browse_root)
    work = out + ".browse-work"
    if default or "enter_root_first" in MUTANTS:     # 対照: 実装の 1 回目の形（--out も、根に入ってから確かめる）
        enter_root(browse_root, create=True)
        _check_out(out)
        if not default:
            _check_nesting(out, browse_root)
    else:
        _check_out(out)
        _check_nesting(out, browse_root)
        # --out の書き出しも、根の共通のロックを取るので、根に入る（設計の 6.4 の 1）
        enter_root(browse_root, create=True)
    common = Lock(os.path.join(browse_root, "build.lock"))
    if not common.acquire(lock_wait):
        raise Stop("ほかの書き出しが終わらない。終わってからやり直す")
    try:
        use_territory(work, WORK_MARKER, owner=owner if default else None, lock_name="build.lock")
        lock = Lock(os.path.join(work, "build.lock"))
        if not lock.try_acquire():
            raise Stop("同じ書き出し先に、ほかの書き出しが動いている")
        try:
            return _build_locked(out, work, files, default, report_text)
        finally:
            lock.release()
    finally:
        common.release()


def _discard(work, names) -> None:
    """領分の残りを消す。消せなければ、警告して残す。"""
    for name in names:
        path = os.path.join(work, name)
        try:
            if name.startswith("old-") and "delete_in_place" not in MUTANTS:
                target = os.path.join(work, "del-" + name[4:])
                move(path, target)
                path = target
            remove_tree(path)
        except (Unknown, Absent) as e:
            if "coarse_warning" in MUTANTS:
                warn(f"消せなかったので残す: {name}（{e}）")      # 対照: 6 版目の初めの形（付け替える前の名前だけ）
            elif "silent_post_commit" not in MUTANTS:
                # 残した項目の今の名前（付け替えたあとなら del-N）と、失敗した口とその対象を書く（設計の 6.2）
                failed = getattr(e, "path", None)
                target = os.path.relpath(failed, work).replace(os.sep, "/") if failed else "?"
                warn(f"消せなかったので残す（次の書き出しで消す）: {os.path.basename(path)}（{target} の {e}）")


def _build_locked(out, work, files, default, report_text) -> int:
    # 2. 作業用の残りを消す
    entries = listdir(work)
    leftover = ("tmp-", "del-", "old-") if "discard_old_first" in MUTANTS else ("tmp-", "del-")   # 対照: 退避の世代も消す
    _discard(work, [n for n in entries if n.startswith(leftover) or n in LEGACY])
    olds = [n for n in listdir(work) if n.startswith("old-")]
    state, reason, _seq = out_state(out, verify=not default)

    # 3. 途中で止まった入れ替えから、最後に退避した世代を戻す。番号は、作業用にあるどの名前よりも大きく取るので、
    # 作業用にある old-N のあいだでは、番号の大きいほうが新しい。一覧（published_seq）には頼らない。
    if state in ("absent", "empty") and olds:
        if "recover_by_published_seq" in MUTANTS:
            seqs = {n: (read_manifest(os.path.join(work, n)) or {}).get("published_seq", 0) for n in olds}
            best = max(olds, key=lambda n: seqs[n])
        else:
            best = max(olds, key=_number)
        restored = _restorable(os.path.join(work, best), default)
        if restored is not None:
            if state == "empty":
                w("rmdir", out)
            move(os.path.join(work, best), out)
            warn(f"前の書き出しが途中で止まっていたので、最後に公開した世代を戻した（{best}）")
            if default or restored[0] == "unchecked":
                state, reason, _seq = out_state(out, verify=not default)
            else:
                state, reason, _seq = restored      # 門の読み取りは、戻す前の 1 度だけ（設計の 6.3）

    # 4. 門: 書き出し先が browse の書き出しと確かめられなければ、止まる。既定の書き出し先は領分なので、通さない。
    if state == "foreign" and "skip_gate" not in MUTANTS:
        raise Stop(f"書き出し先に、browse が作っていないものがある（{reason}）。書き出し先を確かめる: {out}")

    # 5. 通し番号は、作業用にある名前の番号だけで決める
    used = [_number(n) for n in listdir(work) if n.startswith(("tmp-", "old-", "del-"))]
    seq = max(used, default=0) + 1

    # 6. 世代を作る。7. 入れ替える
    tmp = os.path.join(work, f"tmp-{seq}")
    try:
        w("mkdir", tmp)
        create_file(os.path.join(tmp, MANIFEST), make_manifest(files, seq))
        for name in sorted(files):
            parts = name.split("/")
            for i in range(1, len(parts)):
                sub = os.path.join(tmp, *parts[:i])
                if kind(sub) == "absent":
                    w("mkdir", sub)
            create_file(os.path.join(tmp, *parts), files[name])
        old = None
        if state == "empty":
            w("rmdir", out)
        elif state != "absent":
            old = os.path.join(work, f"old-{seq}")
            move(out, old)
        try:
            move(tmp, out)              # 確定の点
        except (Unknown, Absent):
            if old is not None:
                try:
                    move(old, out)
                except (Unknown, Absent):
                    pass                # 次の書き出しが戻す
            raise
    except (Unknown, Absent, Stop):
        try:
            remove_tree(tmp)
        except (Unknown, Absent, Stop):
            pass                        # 片付けの失敗で、元の失敗を隠さない。残りは次の書き出しが消す
        raise

    # 8. 確定のあと。失敗しても、単位を失敗にしない（設計の 7）。
    leftovers = [n for n in _listdir_after_commit(work) if n.startswith("old-")]
    before = len(WARNINGS)
    _discard(work, leftovers)
    if "commit_then_fail" in MUTANTS and len(WARNINGS) > before:
        raise Unknown("確定のあとの片付けに失敗した")
    # 9. 知らせ
    report(report_text)
    return 0


def _restorable(path, default):
    """途中で止まった入れ替えから戻せる世代か（設計の 6.4 の 3）。戻せれば out_state の形の結果、戻せなければ None。

    戻すのは、名前そのものが実のディレクトリで、--out の書き出し先なら、その場で門（6.3）を通るものだけである。
    戻せないものは、戻さずに残し、確定のあとの片付けで、ほかの old-N と一緒に消す（リンクは、たどらずに外す）。
    既定の書き出し先は、前の世代を読まないので、種類だけを見る。
    """
    if "recover_unchecked" in MUTANTS:
        return ("unchecked", None, 0)       # 対照: 8 版目の初めの形。確かめずに戻し、付け替えたあとで門を通す
    if kind(path) != "dir":
        return None
    if default:
        return ("present", None, 0)
    result = out_state(path, verify=True)
    return result if result[0] == "generation" else None


def report(text: str) -> None:
    """知らせを出す。確定のあとなので、失敗しても終了コードを変えない（設計の 7）。"""
    if not text:
        return
    try:
        x("stdout_write", text)
    except (Unknown, Absent) as e:
        if "report_fails_unit" in MUTANTS:
            raise
        warn(f"知らせを出せなかった（{e}）")


def _listdir_after_commit(work):
    """確定のあとの一覧。作れなければ、片付けをあきらめて警告する（単位は失敗にしない）。"""
    if "raise_post_listdir" in MUTANTS:
        return listdir(work)        # 対照: 確定のあとの一覧の失敗を、単位の失敗にする
    try:
        return listdir(work)
    except (Unknown, Absent) as e:
        warn(f"作業用のディレクトリの一覧を作れない（{e}）。残りは次の書き出しで消す")
        return []


# ---------------------------------------------------------------------------
# サーバーの記録（設計の 7.1）
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, state_dir, lock, owner, root):
        self.state_dir, self.lock, self.owner, self.root = state_dir, lock, owner, root


def _enter_state(root, browse_root, state_dir, owner, create=False) -> bool:
    """状態のディレクトリに触れる前の確かめ（設計の 6.2、7.1）。使えば True、使う状態のディレクトリがなければ False。

    起動（create）だけは、ない祖先と領分の根を上から作り、目印を置く。stop と動いているかの確かめは、何も作らない。
    根か状態のディレクトリがないか、状態のディレクトリが目印のない空のディレクトリなら「動いていない」（False）。
    目印がなく、空でもなければ止まる。根か状態のディレクトリの名前がリンクなら、どの操作も止まる。
    """
    global G
    G = Guard(root)
    if not enter_root(browse_root, create):
        return False
    if create:
        # 状態のディレクトリと記録は、ほかの利用者に読ませない（トークンを持つ。POSIX の権限。0.10.0 と同じ）
        use_territory(state_dir, SERVER_MARKER, owner=owner, lock_name="server.lock", mode=0o700)
        return True
    k = kind(state_dir)
    if k == "absent":
        return False
    if k != "dir":
        raise Stop(f"サーバーの状態のディレクトリがディレクトリでない（リンクを含む）: {state_dir}")
    entries = listdir(state_dir)
    if SERVER_MARKER not in entries:
        if entries:
            raise Stop(f"目印がなく、空でもないので使わない: {state_dir}")
        return False
    return True


def read_record(state_dir) -> "dict | None":
    """記録を読む。ないか、形が違えば None。読めなければ Unknown。"""
    text = read_text(os.path.join(state_dir, "server.json"))
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def serve_start(root, browse_root, state_dir, owner, make_record):
    """起動。('started', Server) か、('reuse', 記録か None) を返す。別の Wiki のものなら Stop。

    ロックを取る、待ち受けを始める（make_record。記録の中身を返す）、記録を置き換える、の順にする。記録を
    書けなければ、ロックを離して失敗する（呼ぶ側が待ち受けを閉じる）。動いているサーバーがあれば、記録を返す
    （読めないか、まだないなら None。呼ぶ側が待つ）。
    """
    _enter_state(root, browse_root, state_dir, owner, create=True)
    lock = Lock(os.path.join(state_dir, "server.lock"))
    if not lock.try_acquire():
        try:
            rec = read_record(state_dir)
        except Unknown:
            # 記録を読めない（分からない）。ロックがあるので「動いているが、届かない」。使い回さず、呼ぶ側が待ち、
            # 時間が切れたら知らせて失敗で終わる（設計の 7.1）
            rec = None
        if rec is not None and rec.get("owner") != owner and "skip_owner" not in MUTANTS:
            raise Stop("動いているのは、別の Wiki のサーバーである。使い回さない")
        return "reuse", rec
    try:
        record = make_record()
        record = dict(record, owner=owner)
        replace_file(os.path.join(state_dir, "server.json"), json.dumps(record, ensure_ascii=False).encode("utf-8"),
                     0o600)
    except Unknown as e:
        if "warn_on_record_failure" not in MUTANTS:
            lock.release()
            raise
        warn(f"記録を書けなかった（{e}）")              # 対照: 起動の失敗を、警告して続ける
    except BaseException:
        lock.release()
        raise
    return "started", Server(state_dir, lock, owner, root)


def serve_finish(server) -> None:
    """終了。待ち受けを閉じたあとに呼ぶ。記録を消し、ロックを離す。記録を消せなくても、ロックは離す。"""
    global G
    G = Guard(server.root)
    try:
        w("unlink", os.path.join(server.state_dir, "server.json"))
    except Absent:
        pass
    except (Unknown, Stop) as e:
        warn(f"記録を消せなかった（{e}）。次の起動が置き換える")
    finally:
        server.lock.release()


def probe_server(root, browse_root, state_dir, owner):
    """動いているか。('not-running', None) か、('running', 記録か None) を返す。何も作らない（設計の 6.2、7.1）。

    真実はロックである。記録は手がかりで、Wiki のルートの識別子（owner）が今の Wiki と違えば、止める相手に
    しないで Stop にする。目印の持ち主が違い、ロックが取られていれば、別の Wiki が今使っている（Stop）。
    """
    if not _enter_state(root, browse_root, state_dir, owner):
        return "not-running", None
    if not lock_held(os.path.join(state_dir, "server.lock")):
        return "not-running", None
    marker = os.path.join(state_dir, SERVER_MARKER)
    current = read_text(marker) if kind(marker) == "file" else None
    if current != owner and "skip_owner" not in MUTANTS:
        raise Stop("動いているのは、別の Wiki のサーバーである。止めない")
    rec = read_record(state_dir)
    if rec is not None and rec.get("owner") != owner and "skip_owner" not in MUTANTS:
        raise Stop("動いているのは、別の Wiki のサーバーである。止めない")
    return "running", rec


def remove_record(root, browse_root, state_dir, owner, instance) -> None:
    """stop が止めたサーバーの記録を消す（設計の 7.1。終了と stop は、記録を消す）。

    サーバーが終わり（ロックが空き）、記録がその instance のもので、持ち主が今の Wiki のときだけ消す。サーバー
    自身の終了が消せなかった記録（Windows で、読んでいるほかのプロセスがいると消せない）を残さないためである。
    何も作らない。消せなければ、警告を出して続ける（次の起動が置き換える）。
    """
    if not _enter_state(root, browse_root, state_dir, owner):
        return
    if lock_held(os.path.join(state_dir, "server.lock")):
        return
    try:
        rec = read_record(state_dir)
    except Unknown as e:
        warn(f"記録を読めなかった（{e}）。次の起動が置き換える")
        return
    if rec is None or rec.get("instance") != instance or rec.get("owner") != owner:
        return
    try:
        w("unlink", os.path.join(state_dir, "server.json"))
    except Absent:
        pass
    except (Unknown, Stop) as e:
        warn(f"記録を消せなかった（{e}）。次の起動が置き換える")


# ---------------------------------------------------------------------------
# 読む側（設計の 4、5）
# ---------------------------------------------------------------------------

def walk_inputs(root):
    """入力の一覧（設計の 4）。(entries, problem) を返す。

    entries は (種類, ディスク上の / 区切りの相対パス, 大きさ, 更新時刻) の並びで、ページ、索引、オントロジーの順。
    ページと索引の集合は、lint と同じ走査の関数（wiki_pages.walk_pages、walk_index_files）が決める。オントロジーは、
    層の stat で種類、大きさ、更新時刻を得る（ないなら入れない。調べられなければ、大きさと更新時刻を None にする）。
    problem は、名前の集合が分からないとき（走査が送出した、一覧を作れない場所、種類を調べられない項目）に、
    その理由の短い文字。そのときは全体を採らない（設計の 4.1）。
    """
    root = os.fspath(root)
    try:
        pages = wiki_pages.walk_pages(root)
        indexes = wiki_pages.walk_index_files(root)
    except OSError as e:
        return [], f"Wiki をたどれない（{type(e).__name__}）"
    if "drop_unlistable" not in MUTANTS:
        if pages.unlistable or indexes.unlistable:
            return [], "一覧を作れないディレクトリがある"
        if pages.unknown or indexes.unknown:
            return [], "種類を調べられない項目がある"
    entries = [("page", f.rel, f.size, f.mtime_ns) for f in pages.files]
    entries += [("index", f.rel, f.size, f.mtime_ns) for f in indexes.files]
    if "drop_ontology" not in MUTANTS:
        onto = wiki_pages.ONTOLOGY_REL if hasattr(wiki_pages, "ONTOLOGY_REL") else "graph/ontology.yaml"
        try:
            st = x("stat", os.path.join(root, *onto.split("/")))
        except Absent:
            st = None
        except Unknown:
            entries.append(("ontology", onto, None, None))
            st = None
        else:
            if NT and stat.S_ISBLK(st.st_mode):
                entries.append(("ontology", onto, None, None))
                st = None
        if st is not None and stat.S_ISREG(st.st_mode):
            entries.append(("ontology", onto, st.st_size, st.st_mtime_ns))
    return entries, None


def read_input(root, rel) -> bytes:
    """入力の実体の中身を読む。実体がどこにあっても、Wiki の内容として読む（設計の 5）。"""
    return read_file(os.path.join(os.fspath(root), *rel.split("/")))


def resolve(path) -> str:
    """解いたパス（公開の前の検査で、Wiki の外の入力を探すため）。分からなければ Unknown、ないなら Absent。"""
    return x("realpath", os.fspath(path))


def server_tokens(browse_root) -> "tuple[list, list]":
    """<data_dir>/browse/ の下のすべての状態のディレクトリの記録から、トークンを集める（設計の 5）。

    (トークンの並び, 読めなかった記録の名前の並び) を返す。何も作らない。根がないか、ディレクトリでなければ空。
    """
    tokens, unreadable = [], []
    try:
        if kind(browse_root) != "dir":
            return tokens, unreadable
        names = listdir(browse_root)
    except (Unknown, Absent):
        return tokens, ["<data_dir>/browse"]
    for name in names:
        if not name.endswith(".server"):
            continue
        state_dir = os.path.join(browse_root, name)
        try:
            if kind(state_dir) != "dir":
                continue
            rec = read_record(state_dir)
        except (Unknown, Absent):
            unreadable.append(name)
            continue
        if rec is None:
            continue
        for key in ("view_token", "control_token"):
            value = rec.get(key)
            if isinstance(value, str) and len(value) >= 8:
                tokens.append(value)
    return tokens, unreadable
