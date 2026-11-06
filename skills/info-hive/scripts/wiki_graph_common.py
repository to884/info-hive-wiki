"""wiki_graph_common.py — グラフのスクリプト（extract、lint、query）が共有する決まり。

標準ライブラリだけを使う。PyYAML を持たない `python wiki_graph_query.py` からも import できる。

- グラフ層が有効かどうかは、オントロジー（既定は `<wiki>/graph/ontology.yaml`）があるかで決める。
  ないときは、3 つの入口とも何も書き出さずに終了コード 1 で止まる（`EXIT_GRAPH_DISABLED`）。
- 生成物は、同じディレクトリの一時ファイルに書いてから置き換える（`replace_atomically`）。
  途中で止まっても、前の生成物が残る。
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


STALE_TEMP_SECONDS = 600


def remove_stale_temps(out_dir: Path, names: list[str]) -> None:
    """前の実行が途中で止まって残した一時ファイル（<name>.<pid>.tmp）を消す。

    同時に動いているほかの extract の一時ファイルを消さないように、
    更新から STALE_TEMP_SECONDS 秒より経ったものだけを消す。
    """
    out_dir = Path(out_dir)
    now = time.time()
    for name in names:
        for stale in out_dir.glob(f"{name}.*{TEMP_SUFFIX}"):
            try:
                if now - stale.stat().st_mtime < STALE_TEMP_SECONDS:
                    continue
            except OSError:
                continue
            remove_quietly(stale)
