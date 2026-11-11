#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyyaml==6.0.3",
# ]
# ///
"""
wiki_browse.py — wiki の閲覧（browse）の入口。

    uv run --script wiki_browse.py build <WIKI_ROOT> [--out DIR] [--raw text|link|basename|none]
    uv run --script wiki_browse.py serve <WIKI_ROOT> [--page SLUG] [--no-open] [--idle-timeout 秒]
    uv run --script wiki_browse.py stop <WIKI_ROOT>
    uv run --script wiki_browse.py core          （標準入力から 1 行に 1 つの JSON の要求を読む）

build は静的の HTML を書き出す（既定の書き出し先は <data_dir>/browse/<wiki_key>/）。serve は 127.0.0.1 だけで
待ち受けるサーバーを起動し、動いていれば使い回す。stop はサーバーを止める。core は、中核を Python 以外から
呼ぶための JSON の入出力である。どれも Wiki のファイルを変えない。設計は
`docs/plans/wiki-browse-stage1-2026-11-09.md` にある。
"""

import sys

# 標準ライブラリ以外を読み込む前に、バイトコードを書かないようにする。プラグインが Wiki の中にあっても、
# import だけで Wiki の中にファイルを作らないため（設計の 6）。
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import wiki_browse_core as core  # noqa: E402
import wiki_browse_fs as fs  # noqa: E402
import wiki_mcp_common as common  # noqa: E402
from wiki_text import localize_argparse  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
COMMON_LOCK_WAIT = 60.0


class Failed(Exception):
    """書き出しや起動を、理由を知らせて止める。"""


def say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def wiki_root(arg: str) -> Path:
    root = Path(arg).expanduser()
    if not root.is_dir():
        raise Failed(f"Wiki のルートがない: {arg}")
    return root.resolve()


def browse_dir() -> Path:
    return Path(os.path.abspath(common.data_dir())) / "browse"


def ensure_dir(guard: fs.Guard, path: Path) -> None:
    """ない祖先のディレクトリを、上から順に守りを通して作る。"""
    path = Path(path)
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            guard.mkdir(d)
        except FileExistsError:
            pass


# ---------------------------------------------------------------------------
# 静的の書き出し（設計の 7）
# ---------------------------------------------------------------------------

def _read_marker(directory: Path) -> "dict | None":
    try:
        data = json.loads((directory / fs.GENERATION_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("published_seq"), int):
        return None
    return data


def _generation_dirs(work: Path, prefix: str) -> list:
    if not work.is_dir():
        return []
    return sorted(p for p in work.iterdir()
                  if p.name.startswith(prefix) and p.is_dir() and fs.has_marker(p, fs.GENERATION_MARKER))


def _try_rmtree(guard: fs.Guard, path: Path) -> None:
    if fs.markers_below(path) != [fs.GENERATION_MARKER]:
        # ほかの browse のディレクトリが中にあるものは消さない。
        raise fs.Refused(f"消そうとした世代の中に、ほかの browse のディレクトリがある: {path}")
    try:
        guard.rmtree(path)
    except OSError as e:
        say(f"消せなかったので残す（次の書き出しで消す）: {path.name}（{type(e).__name__}）")


def cleanup(guard: fs.Guard, out: Path, work: Path) -> None:
    """作業用のディレクトリを片付け、途中で止まった入れ替えから最後に公開した世代を戻す（設計の 7 の 3）。"""
    for tmp in _generation_dirs(work, "tmp-"):
        _try_rmtree(guard, tmp)
    olds = _generation_dirs(work, "old-")
    if out.exists():
        for old in olds:
            _try_rmtree(guard, old)
        return
    candidates = [(m["published_seq"], o) for o in olds if (m := _read_marker(o)) is not None]
    if candidates:
        _seq, best = max(candidates)
        guard.rename(best, out)
        say(f"前の書き出しが途中で止まっていたので、最後に公開した世代を戻した（{best.name}）")
        olds = [o for o in olds if o != best]
    for old in olds:
        _try_rmtree(guard, old)


def next_seq(guard: fs.Guard, out: Path, work: Path) -> int:
    seq_file = work / "seq"
    try:
        current = int(seq_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        seen = [m["published_seq"] for d in [out, *_generation_dirs(work, "old-")]
                if (m := _read_marker(d)) is not None]
        current = max(seen, default=0)
    new = current + 1
    guard.write_text(seq_file, f"{new}\n")
    return new


def write_tree(guard: fs.Guard, base: Path, files: dict) -> None:
    for name, content in sorted(files.items()):
        target = base / name
        ensure_dir(guard, target.parent)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        guard.write_bytes(target, data)


def build(root: Path, out_arg: "str | None", raw: "str | None") -> dict:
    root = Path(root)
    key = common.wiki_key(root)
    bdir = browse_dir()
    default = out_arg is None
    out = bdir / key if default else Path(os.path.abspath(os.path.expanduser(out_arg)))
    raw = raw or ("link" if default else "basename")
    if not default and not out.parent.is_dir():
        raise Failed(f"書き出し先の親のディレクトリがない: {out.parent}")
    work = out.with_name(out.name + ".browse-work")

    # 始める前に: 守る場所を決め、どこにも書く前に確かめる（設計の 6.1）。
    guard = fs.Guard([root, *core.external_inputs(root)])
    for p in (bdir, out, work):
        guard.check(p)
    if not default and (fs.overlaps(out, bdir) or fs.overlaps(work, bdir)):
        raise Failed(f"--out は、browse のデータの場所（{bdir}）と重ならない場所にする")
    fs.check_output_dir(out)
    fs.check_work_dir(work)

    ensure_dir(guard, bdir)
    with fs.Lock(guard, bdir / "build.lock") as common_lock:
        if not common_lock.acquire(COMMON_LOCK_WAIT):
            raise Failed("ほかの書き出しが終わらない。終わってからやり直す")
        fs.check_output_dir(out)
        fs.check_work_dir(work)
        if not work.exists():
            guard.make_marked_dir(work, fs.WORK_MARKER)
            fs.check_work_dir(work)
        with fs.Lock(guard, work / "build.lock") as work_lock:
            if not work_lock.try_acquire():
                raise Failed("同じ書き出し先に、ほかの書き出しが動いている")
            return _build_locked(guard, root, out, work, raw)


def _build_locked(guard: fs.Guard, root: Path, out: Path, work: Path, raw: str) -> dict:
    started = time.perf_counter()
    fs.check_output_dir(out)
    fs.check_work_dir(work)
    snap = core.take_snapshot(root)
    guard.set_protected([root, *snap.external])
    for p in (out, work):
        guard.check(p)
    cleanup(guard, out, work)
    seq = next_seq(guard, out, work)
    tmp = work / f"tmp-{seq}"
    guard.mkdir(tmp)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        c = core.Core(snap)
        files = c.render_all(raw, generated_at)
        guard.write_text(tmp / fs.GENERATION_MARKER,
                         json.dumps({"generation": c.generation, "published_seq": seq}) + "\n")
        write_tree(guard, tmp, files)
        write_tree(guard, tmp, {name: core.asset_bytes(name) for name in core.ASSETS})
        needles = [str(root), str(Path.home()), str(Path(os.path.abspath(common.data_dir()))),
                   *(str(p) for p in snap.external)]
        leaks, warnings = core.find_leaks(files, needles, c.recorder)
    except BaseException:
        _try_rmtree(guard, tmp)
        raise
    report = {"wiki": root.as_posix(), "out": out.as_posix(), "generation": c.generation,
              "pages": len(c.drawn), "raw": raw, "leaks": leaks, "warnings": warnings,
              "diagnostics": len(c.diagnostics)}
    if leaks:
        _try_rmtree(guard, tmp)
        guard.write_text(work / "build-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        raise Failed("生成した出力に、生成器が入れた絶対パスがある。前の世代を残して止める: " + ", ".join(leaks[:5]))
    for name in warnings:
        say(f"警告: このページの本文に絶対パスがある（{name}）" + ("。持ち出す書き出しに含まれる" if raw == "basename" else ""))
    fs.check_output_dir(out)
    old = None
    if out.exists():
        if fs.has_marker(out, fs.GENERATION_MARKER):
            old = work / f"old-{seq}"
            guard.rename(out, old)
        else:
            guard.rmdir_empty(out)
    try:
        guard.rename(tmp, out)
    except OSError:
        if old is not None:
            guard.rename(old, out)
        raise
    if old is not None:
        _try_rmtree(guard, old)
    report["seconds"] = round(time.perf_counter() - started, 3)
    guard.write_text(work / "build-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


# ---------------------------------------------------------------------------
# 中核の子プロセスの入出力（設計の 9）
# ---------------------------------------------------------------------------

def core_loop(stdin, stdout) -> int:
    cache = {}

    def get(wiki: str):
        root = wiki_root(wiki)
        cached = cache.get(root)
        sig = core.signature(root)
        if cached and cached.snapshot.signature == sig and not cached.snapshot.unreadable:
            return cached
        c = core.Core(core.take_snapshot(root), record=False)
        cache[root] = c
        return c

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.get("op")
            c = get(str(req.get("wiki", "")))
            if op == "manifest":
                res = {"ok": True, "protocol_version": core.PROTOCOL_VERSION, "generation": c.generation,
                       "manifest": c.manifest(), "nav": c._nav_json(), "diagnostics": c.diagnostics,
                       "pages": [{"id": pid, "path": c.out_name[pid], "title": c.title(pid)}
                                 for pid in sorted(c.drawn)]}
            elif op == "page":
                pid = str(req.get("id", ""))
                raw = req.get("raw") or "text"
                if pid not in c.drawn or raw not in core.RAW_MODES:
                    res = {"ok": False, "error": "unknown_page" if pid not in c.drawn else "bad_raw"}
                else:
                    res = {"ok": True, "protocol_version": core.PROTOCOL_VERSION, "generation": c.generation,
                           "id": pid, "path": c.out_name[pid], "html": c.render_page(pid, raw)}
            else:
                res = {"ok": False, "error": "unknown_op"}
        except Failed:
            res = {"ok": False, "error": "no_wiki"}
        except (ValueError, AttributeError):
            res = {"ok": False, "error": "bad_request"}
        except Exception as e:  # noqa: BLE001  例外の文字列（絶対パスを含む）は返さない
            res = {"ok": False, "error": "internal", "type": type(e).__name__}
        stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
        stdout.flush()
    return EXIT_OK


# ---------------------------------------------------------------------------
# コマンドライン
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="wiki を閲覧する（静的の書き出し、サーバー、中核の入出力）。")
    localize_argparse(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="静的の HTML を書き出す")
    b.add_argument("wiki")
    b.add_argument("--out", default=None, help="書き出し先（既定は <data_dir>/browse/<wiki_key>/）")
    b.add_argument("--raw", choices=core.RAW_MODES, default=None,
                   help="raw: の出し方（既定は、既定の書き出し先で link、--out で basename）")
    sub.add_parser("core", help="中核を、標準入力と標準出力の JSON で呼ぶ")
    _add_server_commands(sub)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            report = build(wiki_root(args.wiki), args.out, args.raw)
            print(json.dumps({"out": report["out"], "generation": report["generation"], "pages": report["pages"],
                              "warnings": len(report["warnings"])}, ensure_ascii=False), flush=True)
            return EXIT_OK
        if args.cmd == "core":
            sys.stdin.reconfigure(encoding="utf-8")
            return core_loop(sys.stdin, sys.stdout)
        return _run_server_command(args)
    except (Failed, fs.Refused, core.NameCollision) as e:
        say(str(e))
        return EXIT_FAILED


def _add_server_commands(sub) -> None:
    """serve と stop（設計の 8）。"""


def _run_server_command(args) -> int:
    raise Failed(f"未対応のコマンド: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
