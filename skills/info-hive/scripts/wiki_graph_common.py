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
import re
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path

from wiki_fsguard import Refused, WriteGuard
from wiki_pages import absolute_user_path

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


DISABLED_HINT = (
    "グラフ層を導入する前の wiki なので、何もしない。グラフを使うには、"
    "assets/ontology.yaml.template を <wiki>/graph/ontology.yaml に置く"
    "（/wiki:upgrade でも置ける）。"
)


def disabled_message(exc: GraphLayerDisabled) -> str:
    """グラフ層がないことを伝える文面。CLI の 3 つの入口と MCP のツールで同じものを使う。"""
    return f"{exc}\n{DISABLED_HINT}"


def report_disabled(exc: GraphLayerDisabled) -> None:
    """グラフ層がないことを、3 つの入口で同じ文面で標準エラーに書く。"""
    print(disabled_message(exc), file=sys.stderr)


def temp_path_for(target: Path) -> Path:
    """target と同じディレクトリに置く一時ファイルのパス。同じボリュームなので置き換えが不可分になる。"""
    target = Path(target)
    return target.with_name(f"{target.name}.{os.getpid()}-{secrets.token_hex(16)}{TEMP_SUFFIX}")


def replace_atomically(temp: Path, target: Path, *, guard: WriteGuard | None = None,
                       expected: os.stat_result | None = None) -> None:
    """閉じた一時ファイルを検査し、Windows の共有違反を待って置き換える。"""
    guard = guard or WriteGuard(target.parent)
    delays = tuple(REPLACE_WAIT_SECONDS * attempt for attempt in range(1, REPLACE_RETRIES))

    def check_sqlite():
        check_sqlite_output(guard, temp)
        check_sqlite_output(guard, target)

    guard.replace_file(temp, target, delays=delays, expected=expected,
                       before_replace=check_sqlite if target.name == "graph.sqlite" else None)


def sqlite_sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm")]


def check_sqlite_output(guard: WriteGuard, path: Path) -> None:
    guard.file_info(path)
    for sidecar in sqlite_sidecars(path):
        if guard.file_info(sidecar) is not None:
            raise Refused(sidecar, "sqlite_sidecar", "SQLite の付随ファイルが残っている")


def remove_temp(guard: WriteGuard, path: Path, created: os.stat_result) -> None:
    """今回作ったファイルだけを、SQLite の付随ファイルとともに片付ける。"""
    current = guard.file_info(path)
    if current is None:
        return
    if not os.path.samestat(current, created):
        raise Refused(path, "changed", "一時ファイルの実体が変わった")
    sidecars = sqlite_sidecars(path)
    for sidecar in sidecars:
        guard.file_info(sidecar)
    for item in (*sidecars, path):
        guard.remove_file(item)


def remove_stale_temps(guard: WriteGuard, out_dir: Path, names: list[str]) -> None:
    """排他ロックを持っている間に、既知の一時名を検査して片付ける。"""
    outputs = "|".join(re.escape(name) for name in names)
    pattern = re.compile(rf"(?:{outputs})\.[0-9]+(?:-[0-9a-f]{{32}})?\.tmp(?:-(?:journal|wal|shm))?")
    stale = [path for path in out_dir.iterdir() if pattern.fullmatch(path.name)]
    for path in stale:
        guard.file_info(path)
    for path in stale:
        guard.remove_file(path)


def output_guard(wiki: Path, out_dir: Path) -> WriteGuard:
    """wiki の内側を指定した場合は、wiki の根から出力先までを検査する。"""
    wiki = absolute_user_path(wiki)
    out_dir = absolute_user_path(out_dir)
    real_wiki = wiki.resolve(strict=True)
    for root in (wiki, real_wiki):
        if out_dir.is_relative_to(root):
            guard = WriteGuard(root)
            if out_dir != root:
                guard.ensure_directory(out_dir)
            return guard
    # 最終パスだけ解くと、途中で wiki に入り、別のリンクで再び外へ出る道を見落とす。
    for part in (*reversed(out_dir.parents), out_dir):
        try:
            real_part = part.resolve(strict=True)
        except FileNotFoundError:
            break
        if real_part.is_relative_to(real_wiki):
            raise Refused(part, "alias", f"wiki 内の出力先は wiki の根 {wiki} から指定する")
    out_dir.mkdir(parents=True, exist_ok=True)
    return WriteGuard(out_dir)

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


@contextmanager
def extract_lock(out_dir: Path, wait_seconds: float | None = None, *, guard: WriteGuard | None = None):
    """出力先ごとの排他ロック。OS のファイルロックなので、プロセスが止まれば自動で外れる。

    ロックのファイル（<out>/.extract.lock）は消さずに残す。消すと、別のプロセスが
    作り直したファイルとのあいだで、2 つの extract が同時にロックを持ててしまう。
    """

    path = absolute_user_path(Path(out_dir) / LOCK_NAME)
    guard = guard or WriteGuard(out_dir)
    try:
        with guard.hold_lock(path, timeout=LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds):
            yield
    except Refused as exc:
        if exc.path == path and exc.kind == "busy":
            raise ExtractBusy(path) from exc
        raise
