"""wiki_graph_common.py — グラフのスクリプト（extract、lint、query）が共有する決まり。

標準ライブラリだけを使う。PyYAML を持たない `python wiki_graph_query.py` からも import できる。

- グラフ層が有効かどうかは、オントロジー（既定は `<wiki>/graph/ontology.yaml`）があるかで決める。
  ないときは、3 つの入口とも何も書き出さずに終了コード 1 で止まる（`EXIT_GRAPH_DISABLED`）。
- 生成物は、選んだ形式をすべて同じディレクトリの一時ファイルに書き終えてから置き換える
  （`replace_atomically`）。書いている途中で止まっても、前の生成物が残る。
- 同じ出力先で extract が同時に走らないように、`extract_lock` で排他する。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 終了コード。1 は「必要な入力がない」（wiki のディレクトリ、DB、ノード）、
# 2 は「環境や入力の形式の問題」（PyYAML がない、オントロジーを読めない）に使う。
EXIT_GRAPH_DISABLED = 1
EXIT_BAD_ONTOLOGY = 2

TEMP_SUFFIX = ".tmp"

# Windows では、ほかのプロセスが開いているファイルを置き換えると PermissionError になる。
# 問い合わせの接続はすぐ閉じるので、少し待ってやり直す。
REPLACE_RETRIES = 10
REPLACE_WAIT_SECONDS = 0.1


class GraphLayerDisabled(Exception):
    """オントロジーがないので、グラフ層を使えない。"""

    def __init__(self, ontology_path: Path):
        self.ontology_path = Path(ontology_path)
        super().__init__(f"オントロジーが見つからない: {self.ontology_path}")


def default_ontology_path(wiki: Path) -> Path:
    return Path(wiki) / "graph" / "ontology.yaml"


def require_ontology(wiki: Path, override: Path | None = None) -> Path:
    """使うオントロジーのパスを返す。ファイルがなければ GraphLayerDisabled を送出する。"""
    path = Path(override) if override else default_ontology_path(wiki)
    if not path.is_file():
        raise GraphLayerDisabled(path)
    return path


def report_disabled(exc: GraphLayerDisabled) -> None:
    """グラフ層がないことを、3 つの入口で同じ文面で標準エラーに書く。"""
    print(str(exc), file=sys.stderr)
    print(
        "グラフ層を導入する前の wiki なので、何もしない。グラフを使うには、"
        "assets/ontology.yaml.template を <wiki>/graph/ontology.yaml に置く"
        "（/wiki:upgrade でも置ける）。",
        file=sys.stderr,
    )


def temp_path_for(target: Path) -> Path:
    """target と同じディレクトリに置く一時ファイルのパス。同じボリュームなので置き換えが不可分になる。"""
    target = Path(target)
    return target.with_name(f"{target.name}.{os.getpid()}{TEMP_SUFFIX}")


def replace_atomically(temp: Path, target: Path) -> None:
    """temp を target に置き換える。失敗したら temp を消して例外を送出し、target は元のまま残す。"""
    last_error: OSError | None = None
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(temp, target)
            return
        except PermissionError as exc:  # Windows で target が開かれている
            last_error = exc
            time.sleep(REPLACE_WAIT_SECONDS * (attempt + 1))
        except OSError:
            remove_quietly(temp)
            raise
    remove_quietly(temp)
    assert last_error is not None
    raise last_error


def remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def remove_temp(path: Path) -> None:
    """一時ファイルと、SQLite がその隣に作る付随ファイル（-journal など）を消す。"""
    path = Path(path)
    for suffix in ("", "-journal", "-wal", "-shm"):
        remove_quietly(path.with_name(path.name + suffix))


def remove_stale_temps(out_dir: Path, names: list[str]) -> None:
    """前の実行が途中で止まって残した一時ファイル（<name>.<pid>.tmp と付随ファイル）を消す。

    extract_lock を持っているあいだに呼ぶ。ロックを持つのは 1 つの extract だけなので、
    残っている一時ファイルは、どれも止まった実行のものである。
    """
    out_dir = Path(out_dir)
    for name in names:
        for pattern in (f"{name}.*{TEMP_SUFFIX}", f"{name}.*{TEMP_SUFFIX}-*"):
            for stale in out_dir.glob(pattern):
                remove_quietly(stale)


# ---------------------------------------------------------------------------
# 同時に走る extract の排他
# ---------------------------------------------------------------------------

LOCK_NAME = ".extract.lock"
LOCK_WAIT_SECONDS = 120


class ExtractBusy(Exception):
    """ほかの extract が同じ出力先で動いていて、待っても終わらなかった。"""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        super().__init__(f"ほかの extract が動いている（{self.lock_path}）。終わってからやり直す。")


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class extract_lock:
    """出力先ごとの排他ロック。OS のファイルロックなので、プロセスが止まれば自動で外れる。

    ロックのファイル（<out>/.extract.lock）は消さずに残す。消すと、別のプロセスが
    作り直したファイルとのあいだで、2 つの extract が同時にロックを持ててしまう。
    """

    def __init__(self, out_dir: Path, wait_seconds: float | None = None):
        self.path = Path(out_dir) / LOCK_NAME
        self.wait_seconds = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
        self.fd: int | None = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.wait_seconds
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise ExtractBusy(self.path)
            time.sleep(0.2)
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            _unlock(self.fd)
            os.close(self.fd)
            self.fd = None
        return False
