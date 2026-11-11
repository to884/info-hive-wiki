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
import hmac  # noqa: E402
import html  # noqa: E402
import http.client  # noqa: E402
import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import secrets  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
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
        # 目印を最後に消す。途中で失敗しても目印が残り、次の書き出しで世代として見つけて消せる。
        guard.rmtree(path, last=fs.GENERATION_MARKER)
    except OSError as e:
        say(f"消せなかったので残す（次の書き出しで消す）: {path.name}（{type(e).__name__}）")


def cleanup(guard: fs.Guard, out: Path, work: Path) -> None:
    """作業用のディレクトリを片付け、途中で止まった入れ替えから最後に公開した世代を戻す（設計の 7 の 3）。

    出力先に世代の目印があるときだけ、公開した世代がそこにあるとみなす。出力先がない、または空のディレクトリ
    のときは、退避した世代のうち published_seq のいちばん大きいものを戻す。
    """
    for tmp in _generation_dirs(work, "tmp-"):
        _try_rmtree(guard, tmp)
    olds = _generation_dirs(work, "old-")
    if out.exists() and fs.has_marker(out, fs.GENERATION_MARKER):
        for old in olds:
            _try_rmtree(guard, old)
        return
    candidates = [(m["published_seq"], o) for o in olds if (m := _read_marker(o)) is not None]
    if candidates:
        _seq, best = max(candidates)
        if out.exists():
            guard.rmdir_empty(out)   # 空でなければ失敗する（check_output_dir で空だと確かめてある）
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
    """出力のファイルを base の下に書く。名前は、base の下に収まる相対パスでなければ書かない。"""
    for name, content in sorted(files.items()):
        if not core.is_safe_relative(name):
            raise fs.Refused(f"出力の名前が相対パスでない: {name!r}")
        target = base / name
        if not fs.is_within(target, base) or fs.is_within(base, target):
            raise fs.Refused(f"出力の名前が書き出し先の外を指す: {name!r}")
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
        # 目印を最初に置く。組み立てが途中で失敗しても、作りかけの世代として見つけて消せる。
        guard.write_text(tmp / fs.GENERATION_MARKER,
                         json.dumps({"generation": snap.generation, "published_seq": seq}) + "\n")
        c = core.Core(snap)
        files = c.render_all(raw, generated_at)
        write_tree(guard, tmp, files)
        write_tree(guard, tmp, {name: core.asset_bytes(name) for name in core.ASSETS})
        needles = [str(root), str(Path.home()), str(Path(os.path.abspath(common.data_dir()))),
                   *(str(p) for p in snap.external)]
        leaks, warnings = core.find_leaks(files, needles, c.recorder)
    except BaseException:
        try:
            _try_rmtree(guard, tmp)
        except (fs.Refused, OSError):
            pass   # 片付けの失敗で、元の例外を隠さない。残った作りかけは次の書き出しで消す
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
# サーバー（設計の 8）
# ---------------------------------------------------------------------------

BUILD_WAIT = 30.0
RETRY_INTERVAL = 5.0
RECORD_WAIT = 10.0
STOP_WAIT = 10.0
DEFAULT_IDLE = 1800.0


def _quote_path(name: str) -> str:
    return "/".join(urllib.parse.quote(p, safe="") for p in name.split("/"))


class BrowseState:
    """サーバーが持つ中核と、作り直しの状態（設計の 8.3）。"""

    def __init__(self, root: Path, guard: fs.Guard, state_dir: Path, raw: str = "text"):
        self.root = root
        self.guard = guard
        self.state_dir = state_dir
        self.raw = raw
        self.current: "core.Core | None" = None
        self.failed: "str | None" = None
        self.build_lock = threading.Lock()
        self.attempts: dict = {}
        self.shutdown_requested = False

    def _should_retry(self) -> bool:
        cur = self.current
        return self.failed is not None or (cur is not None and cur.snapshot.unreadable)

    def _recently_tried(self, sig: str) -> bool:
        last = self.attempts.get(sig)
        return last is not None and time.monotonic() - last < RETRY_INTERVAL

    def get(self, wait: "float | None" = None) -> "tuple[core.Core | None, str | None]":
        """答えに使う中核と、古いときの理由（"failed" か "building"）を返す。中核がなければ (None, 理由)。"""
        wait = BUILD_WAIT if wait is None else wait
        request_sig = core.signature(self.root)
        cur = self.current
        if cur is not None and cur.snapshot.signature == request_sig and not self._should_retry():
            return cur, None
        if not self.build_lock.acquire(timeout=wait):
            return self._answer(self.current, request_sig, "building")
        try:
            sig = core.signature(self.root)
            cur = self.current
            if cur is not None and cur.snapshot.signature == sig and not self._should_retry():
                return self._answer(cur, request_sig, None)
            if self._recently_tried(sig):
                return self._answer(cur, request_sig, "failed" if self.failed else "building")
            self.attempts = {sig: time.monotonic()}
            try:
                snap = core.take_snapshot(self.root)
                self.guard.set_protected([self.root, *snap.external])
                if self.guard.overlapping(self.state_dir):
                    # 状態のディレクトリが守る場所と重なった。記録に触れずに終わる（設計の 6.1）。
                    self.shutdown_requested = True
                    return self._answer(cur, request_sig, "failed")
                new = core.Core(snap, record=False)
            except Exception as e:  # noqa: BLE001  前の中核を使い続ける
                self.failed = type(e).__name__
                return self._answer(cur, request_sig, "failed")
            self.current = new
            self.failed = None
            return self._answer(new, request_sig, None)
        finally:
            self.build_lock.release()

    def _answer(self, c, request_sig: str, reason: "str | None"):
        if c is None:
            return None, reason or "building"
        if c.snapshot.signature != request_sig:
            return c, reason or ("failed" if self.failed else "building")
        return c, None


def _same(given: str, token: str) -> bool:
    """トークンを、時間の一定な比較で確かめる。ASCII でない文字の要求でも例外にしない。"""
    return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "info-hive-browse"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # サーバーの記録には、要求のパスを書かない（トークンを残さない。設計の 8.1）。
    def log_message(self, fmt, *args):  # noqa: D401
        pass

    def log_request(self, code="-", size="-"):
        app = self.server.app
        if app.verbose:
            say(f"{self.command} {getattr(self, '_kind', '-')} {code}")

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra: "dict | None" = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", core.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, code: int, text: str, extra=None):
        self._send(code, (text + "\n").encode("utf-8"), extra=extra)

    def _json(self, code: int, value: dict):
        self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _route(self):
        try:
            self._route_inner()
        except Exception as e:  # noqa: BLE001  描画の例外などで接続を黙って閉じない。例外の文字列は出さない
            self.server.app.render_errors += 1
            say(f"要求の処理に失敗した（{type(e).__name__}）")
            try:
                self._text(500, "ページを描けない。lint で確かめる")
            except OSError:
                pass

    def send_error(self, code, message=None, explain=None):
        # 標準ライブラリの既定のエラーの応答は、要求の行（トークンを含む）を返し、CSP などを付けない。
        self.close_connection = True
        # 要求の行が不正だと、標準ライブラリは HTTP/0.9 とみなしてヘッダーを付けない。CSP などを付けて返す。
        self.request_version = "HTTP/1.1"
        try:
            self._text(code, "Bad Request" if code == 400 else "Error")
        except OSError:
            pass

    def _route_inner(self):
        app = self.server.app
        app.touch()
        self._kind = "-"
        if app.state.shutdown_requested:
            app.request_shutdown()
            return self._text(503, "終わるところである")
        if self.headers.get("Host") != f"127.0.0.1:{app.port}":
            return self._text(421, "Misdirected Request")
        raw_path = urllib.parse.urlsplit(self.path).path
        try:
            path = urllib.parse.unquote(raw_path, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._text(404, "Not Found")
        if not path.startswith("/"):
            return self._text(404, "Not Found")
        parts = path[1:].split("/")
        # デコードしたあとで分け、`..`、`.`、空の要素、`\`、NUL を含めば拒む（設計の 8.1）。
        if any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in parts):
            return self._text(404, "Not Found")
        if len(parts) >= 3 and _same(parts[0], app.control_token) and parts[1] == "control":
            self._kind = "control"
            return self._control(parts[2:])
        if len(parts) >= 3 and _same(parts[0], app.view_token) and parts[1] == "w" \
                and parts[2] == app.key:
            self._kind = "view"
            if self.command not in ("GET", "HEAD"):
                return self._text(405, "Method Not Allowed", {"Allow": "GET, HEAD"})
            return self._view("/".join(parts[3:]) or "index.html")
        return self._text(404, "Not Found")

    def _view(self, name: str):
        app = self.server.app
        if name in core.ASSETS:
            ctype = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
            return self._send(200, core.asset_bytes(name), ctype)
        c, stale = app.state.get()
        if app.state.shutdown_requested:
            app.request_shutdown()
            return self._text(503, "終わるところである")
        if c is None:
            return self._text(503, "Wiki を組み立てている。少し待ってから再読み込みする" if stale == "building"
                              else "Wiki を組み立てられない。lint で確かめる", {"Retry-After": "5"})
        if not c.knows(name):
            index = "index.html"
            body = ('<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>見つからない</title></head>'
                    '<body><p>ページが見つからない。Wiki が更新されたかもしれない。</p>'
                    f'<p><a href="{html.escape(core.rel_href(name, index))}">索引へ</a></p></body></html>\n')
            return self._send(404, body.encode("utf-8"), "text/html; charset=utf-8")
        reason = None if stale is None else ("組み立てに失敗した" if stale == "failed" else "組み立てている")
        text = c.render(name, app.state.raw, stale=reason)
        ctype = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/html; charset=utf-8"
        return self._send(200, text.encode("utf-8"), ctype)

    def _control(self, rest):
        app = self.server.app
        op = rest[0] if rest else ""
        if op == "status" and self.command in ("GET", "HEAD") and len(rest) == 1:
            return self._json(200, {"instance": app.instance, "wiki_key": app.key})
        if op == "stop" and self.command == "POST" and len(rest) == 1:
            self._json(200, {"stopping": True})
            app.request_shutdown()
            return None
        if op == "locate" and self.command in ("GET", "HEAD") and len(rest) >= 2:
            target = "/".join(rest[1:])
            c, _stale = app.state.get()
            if app.state.shutdown_requested:
                app.request_shutdown()
                return self._json(503, {"state": "unavailable"})
            if c is None:
                return self._json(200, {"state": "unavailable"})
            r = c.resolve(core.nfc(target))
            res = {"state": r["state"]}
            if r["state"] == "resolved":
                res["path"] = c.out_name[r["id"]]
                if r.get("anchor"):
                    res["anchor"] = r["anchor"]
            return self._json(200, res)
        return self._text(405 if op in ("status", "stop", "locate") else 404,
                          "Method Not Allowed" if op in ("status", "stop", "locate") else "Not Found")

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_OPTIONS(self):
        self._route()

    def do_TRACE(self):
        self._route()

    def do_CONNECT(self):
        self._route()


class App:
    def __init__(self, root: Path, guard: fs.Guard, state_dir: Path, idle: float, verbose: bool = False):
        self.root = root
        self.key = common.wiki_key(root)
        self.guard = guard
        self.state_dir = state_dir
        self.idle = idle
        self.verbose = verbose
        self.view_token = secrets.token_urlsafe(24)
        self.control_token = secrets.token_urlsafe(24)
        self.instance = secrets.token_hex(12)
        self.state = BrowseState(root, guard, state_dir)
        self.last = time.monotonic()
        self.httpd = None
        self.port = 0
        self.render_errors = 0
        self._stopping = threading.Event()

    def touch(self):
        self.last = time.monotonic()

    def request_shutdown(self):
        if not self._stopping.is_set():
            self._stopping.set()
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def url(self, name: str = "index.html") -> str:
        return f"http://127.0.0.1:{self.port}/{self.view_token}/w/{self.key}/{_quote_path(name)}"

    def record(self) -> dict:
        return {"port": self.port, "view_token": self.view_token, "control_token": self.control_token,
                "instance": self.instance, "wiki_key": self.key, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def idle_watch(self):
        while not self._stopping.wait(1.0):
            if time.monotonic() - self.last > self.idle:
                self.request_shutdown()


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 既定は、スクリプトの絶対パスを含むトレースバックを標準エラーに出す。例外の種類だけを出す。
        say(f"接続の処理に失敗した（{type(sys.exc_info()[1]).__name__}）")


def _http(port: int, method: str, path: str, timeout: float = 3.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read()
        return res.status, body
    finally:
        conn.close()


def _status(rec: dict) -> "str | None":
    try:
        code, body = _http(int(rec["port"]), "GET", f"/{rec['control_token']}/control/status")
        if code != 200:
            return None
        return json.loads(body).get("instance")
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return None


def _read_record(state_dir: Path) -> "dict | None":
    try:
        rec = json.loads((state_dir / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not all(k in rec for k in ("port", "view_token", "control_token", "instance")):
        return None
    return rec


def _prepare_state(root: Path) -> "tuple[fs.Guard, Path]":
    bdir = browse_dir()
    state_dir = bdir / f"{common.wiki_key(root)}.server"
    guard = fs.Guard([root, *core.external_inputs(root)])
    guard.check(bdir)
    guard.check(state_dir)
    fs.check_server_dir(state_dir)
    ensure_dir(guard, bdir)
    if not state_dir.exists():
        guard.make_marked_dir(state_dir, fs.SERVER_MARKER, mode=0o700)
    fs.check_server_dir(state_dir)
    if os.name == "nt" and os.environ.get(common.DATA_DIR_ENV, "").strip():
        say("警告: INFO_HIVE_DATA_DIR を指定している。ほかの利用者が読める場所なら、サーバーの記録のトークンを読まれる")
    return guard, state_dir


def _locate(port: int, control_token: str, target: str) -> dict:
    try:
        code, body = _http(port, "GET", f"/{control_token}/control/locate/{urllib.parse.quote(target, safe='')}",
                           timeout=BUILD_WAIT + 5)
        return json.loads(body) if code == 200 else {"state": "unavailable"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"state": "unavailable"}


def _emit(url: str, port: int, reused: bool, page_state: "str | None", open_browser: bool) -> None:
    print(json.dumps({"url": url, "port": port, "reused": reused, "page": page_state}, ensure_ascii=False), flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)


def _page_url(base_url_for, port, token, target):
    if not target:
        return base_url_for("index.html"), None
    found = _locate(port, token, target)
    if found.get("state") == "resolved":
        name = found["path"]
        url = base_url_for(name)
        if found.get("anchor"):
            url += "#" + urllib.parse.quote(found["anchor"], safe="")
        return url, "resolved"
    return base_url_for("index.html"), found.get("state")


def serve(root: Path, page: "str | None", open_browser: bool, idle: float, verbose: bool = False) -> int:
    guard, state_dir = _prepare_state(root)
    lock = fs.Lock(guard, state_dir / "server.lock")
    deadline = time.monotonic() + RECORD_WAIT
    while not lock.try_acquire():
        rec = _read_record(state_dir)
        if rec is not None and _status(rec) == rec["instance"]:
            key = rec.get("wiki_key") or common.wiki_key(root)

            def url_for(name, rec=rec, key=key):
                return f"http://127.0.0.1:{rec['port']}/{rec['view_token']}/w/{key}/{_quote_path(name)}"
            url, state = _page_url(url_for, int(rec["port"]), rec["control_token"], page)
            _emit(url, int(rec["port"]), True, state, open_browser)
            return EXIT_OK
        if time.monotonic() >= deadline:
            raise Failed("動いているサーバーを使い回せない。/wiki:browse --stop で止めてからやり直す")
        time.sleep(0.2)
    try:
        guard.unlink(state_dir / "server.json")
        app = App(root, guard, state_dir, idle, verbose)
        httpd = _Server(("127.0.0.1", 0), Handler)
        httpd.app = app
        app.httpd = httpd
        app.port = httpd.server_address[1]
        guard.write_text(state_dir / "server.json", json.dumps(app.record(), ensure_ascii=False) + "\n", mode=0o600)
        threading.Thread(target=app.idle_watch, daemon=True).start()
        server_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        server_thread.start()
        url, state = _page_url(app.url, app.port, app.control_token, page)
        _emit(url, app.port, False, state, open_browser)
        server_thread.join()
        httpd.server_close()
        return _finish(root, app, guard, state_dir)
    finally:
        lock.release()


def _finish(root: Path, app: "App", guard: fs.Guard, state_dir: Path) -> int:
    """終わるときの後片付け。記録を消すのは、守る場所と重ならないことを、その時点で確かめてからにする。

    組み立ての途中で終わると、組み立てが読んだ入力（記録を指すページなど）を守る場所に入れる前に、記録を消して
    しまう。そこで、組み立てのロックを取って組み立てが走っていないことを確かめ、守る場所を今の入力から
    決め直す（設計の 6.1）。重なれば、記録に触れずに終わる。
    """
    got = app.state.build_lock.acquire(timeout=BUILD_WAIT)
    try:
        external = core.external_inputs(root)
        guard.set_protected([root, *external])
        if not got or app.state.shutdown_requested or guard.overlapping(state_dir) or \
                any(fs.is_within(x, state_dir) for x in external):
            say("サーバーの状態の場所が、Wiki か Wiki が読む入力と重なったか、確かめられなかったので、記録を残して終わる")
            return EXIT_FAILED
        rec = _read_record(state_dir)
        if rec is not None and rec.get("instance") == app.instance:
            try:
                guard.unlink(state_dir / "server.json")
            except (OSError, fs.Refused):
                pass
        return EXIT_OK
    finally:
        if got:
            app.state.build_lock.release()


def stop(root: Path) -> int:
    guard, state_dir = _prepare_state(root)
    lock = fs.Lock(guard, state_dir / "server.lock")
    if lock.try_acquire():
        try:
            guard.unlink(state_dir / "server.json")
        finally:
            lock.release()
        print(json.dumps({"stopped": False, "running": False}), flush=True)
        return EXIT_OK
    deadline = time.monotonic() + RECORD_WAIT
    rec = _read_record(state_dir)
    while rec is None or _status(rec) != rec["instance"]:
        if lock.try_acquire():
            lock.release()
            print(json.dumps({"stopped": False, "running": False}), flush=True)
            return EXIT_OK
        if time.monotonic() >= deadline:
            raise Failed("サーバーが応答しないので、止められない。操作がなければ、いずれ終わる")
        time.sleep(0.2)
        rec = _read_record(state_dir)
    try:
        _http(int(rec["port"]), "POST", f"/{rec['control_token']}/control/stop")
    except (OSError, http.client.HTTPException):
        pass
    if not lock.acquire(STOP_WAIT):
        raise Failed("サーバーが終わらないので、止められなかった")
    try:
        guard.set_protected([root, *core.external_inputs(root)])
        latest = _read_record(state_dir)
        if latest is not None and latest.get("instance") == rec["instance"]:
            guard.unlink(state_dir / "server.json")   # 記録を指すページがあれば、守りが拒む
    except fs.Refused:
        pass
    finally:
        lock.release()
    print(json.dumps({"stopped": True, "running": False}), flush=True)
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
    s = sub.add_parser("serve", help="127.0.0.1 のサーバーを起動して開く（動いていれば使い回す）")
    s.add_argument("wiki")
    s.add_argument("--page", default=None, help="開くページ（[[slug]] と同じ規則で探す）")
    s.add_argument("--no-open", action="store_true", help="ブラウザで開かない（URL を出すだけ）")
    s.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE, help="操作がないときに終わるまでの秒数（既定 1800）")
    s.add_argument("--verbose", action="store_true", help="要求の方法、口の種類、状態の番号を標準エラーに出す")
    t = sub.add_parser("stop", help="動いているサーバーを止める")
    t.add_argument("wiki")


def _run_server_command(args) -> int:
    if args.cmd == "serve":
        return serve(wiki_root(args.wiki), args.page, not args.no_open, args.idle_timeout, args.verbose)
    if args.cmd == "stop":
        return stop(wiki_root(args.wiki))
    raise Failed(f"未対応のコマンド: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
