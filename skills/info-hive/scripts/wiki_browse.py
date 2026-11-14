#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml==6.0.3",
# ]
# ///
"""
wiki_browse.py — wiki の閲覧（browse）の入口。

    uv run --script wiki_browse.py build <WIKI_ROOT> [--out DIR] [--raw text|link|basename|none] [--scope user|project]
    uv run --script wiki_browse.py serve <WIKI_ROOT> [--page SLUG] [--no-open] [--idle-timeout 秒] [--scope user|project]
    uv run --script wiki_browse.py stop <WIKI_ROOT>
    uv run --script wiki_browse.py core          （標準入力から 1 行に 1 つの JSON の要求を読む）

build は静的の HTML を書き出す（既定の書き出し先は <data_dir>/browse/<wiki_key>/）。serve は 127.0.0.1 だけで
待ち受けるサーバーを起動し、動いていれば使い回す。stop はサーバーを止める。core は、中核を Python 以外から
呼ぶための JSON の入出力である。どれも Wiki のファイルを変えず、Wiki のルートの下には何も作らない。browse は、
領分（<data_dir>/browse/ と、--out の書き出し先の隣の作業用のディレクトリ）の外では、自分が作ったものだけを
置き換え、消す。設計は `docs/plans/wiki-browse-stage1-2026-11-09.md` と
`docs/plans/wiki-browse-structure-2026-11-11.md` にある。

このモジュールは、ファイルの入出力を呼ばない。入出力は wiki_browse_fs.py の口を通す（構造の直しの設計の 3）。
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 入出力をするモジュール（wiki_pages は読み込みのときに同梱のファイルを読む）は、コマンドの出口の境界の中で
# 読み込む（構造の直しの設計の 3.3）。試験は _load() を呼んでから使う。
core = fs = initialized = common = None

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def _load() -> None:
    global core, fs, initialized, common
    if core is None:
        import wiki_browse_core as _core  # noqa: PLC0415
        import wiki_browse_fs as _fs  # noqa: PLC0415
        import wiki_initialized as _initialized  # noqa: PLC0415
        import wiki_mcp_common as _common  # noqa: PLC0415
        core, fs, initialized, common = _core, _fs, _initialized, _common


class Failed(Exception):
    """書き出しや起動を、理由を知らせて止める。"""


def say(message: str) -> None:
    """標準エラーへの知らせ（通信）。書けなくても、終了コードを変えない（設計の 7）。"""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def flush_warnings() -> None:
    """層が単位の中で出した警告を、標準エラーに出す。"""
    for message in fs.WARNINGS:
        say(f"警告: {message}")
    fs.WARNINGS.clear()


def wiki_root(arg: str) -> Path:
    """初期化された wiki のルート。SCHEMA.md のないディレクトリは断る（ADR-0019）。build、serve、stop、core の
    どれも通る。stop も通すのは、未初期化のディレクトリのためにデータの場所に状態のディレクトリを作らないためである。
    判定は、パスを解決するより前に行う。解決の失敗も、同じ理由の文と案内で止める。"""
    try:
        return initialized.resolve(Path(arg).expanduser())
    except initialized.NotInitialized as e:
        raise Failed(str(e)) from e


def browse_dir() -> str:
    """領分の根 <data_dir>/browse（設計の 2.3）。"""
    return os.path.join(os.path.abspath(common.data_dir()), "browse")


def _unknown_text(e) -> str:
    path = getattr(e, "path", None)
    return f"{e}" + (f"（{path}）" if path else "")


# ---------------------------------------------------------------------------
# 静的の書き出し（設計の 7。構造の直しの設計の 6.4）
# ---------------------------------------------------------------------------

READ_ATTEMPTS = 3


def _read_and_render(root: Path, raw: str, scope: "str | None", generated_at: str):
    """写しを採り、すべてのページを描く。読んでいるあいだに Wiki が変われば、3 回までやり直す（設計の 4.2）。"""
    for attempt in range(READ_ATTEMPTS):
        try:
            snap = core.take_snapshot(root)
        except core.InputsChanged:
            if attempt + 1 == READ_ATTEMPTS:
                raise Failed("読んでいるあいだに Wiki が変わり続けた。前の世代を残して止める。少し待ってからやり直す")
            continue
        c = core.Core(snap, scope=scope)
        return snap, c, c.render_all(raw, generated_at)
    raise AssertionError("unreachable")


def _machine_needles(root: Path, snap) -> "tuple[list, int]":
    """公開の前の検査で探す絶対パス（Wiki のルート、ホーム、data_dir、Wiki の外にある入力の実体）。
    (並び, 実体の場所を確かめられなかった入力の数) を返す。"""
    needles = [str(root), str(Path.home()), os.path.abspath(common.data_dir())]
    try:
        real_root = os.path.normcase(fs.resolve(root))
    except (fs.Unknown, fs.Absent):
        return needles, len(snap.entries)
    unknown = 0
    for _kind, rel, _size, _mtime in snap.entries:
        try:
            real = fs.resolve(os.path.join(str(root), *rel.split("/")))
        except (fs.Unknown, fs.Absent):
            unknown += 1
            continue
        folded = os.path.normcase(real)
        if folded != real_root and not folded.startswith(real_root.rstrip(os.sep) + os.sep):
            needles.append(real)
    return needles, unknown


def build(root: Path, out_arg: "str | None", raw: "str | None", scope: "str | None" = None) -> dict:
    root = Path(root)
    key = common.wiki_key(root)
    bdir = browse_dir()
    default = out_arg is None
    out = os.path.join(bdir, key) if default else os.path.abspath(os.path.expanduser(out_arg))
    raw = raw or ("link" if default else "basename")
    started = time.perf_counter()
    assets = core.load_assets()
    owner = fs.owner_of(root)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    snap, c, files = _read_and_render(root, raw, scope, generated_at)
    files = {name: (content if isinstance(content, bytes) else content.encode("utf-8"))
             for name, content in files.items()}
    files.update(assets)
    for name in files:
        if not core.is_safe_relative(name):
            raise Failed(f"出力の名前が相対パスでない: {name!r}")

    # 公開の前の検査（構造の直しの設計の 8）。見つかれば警告を出す。書き出しは止めない。
    needles, unresolved = _machine_needles(root, snap)
    tokens, unreadable_records = fs.server_tokens(bdir)
    findings = core.scan_machine_info(files, needles, tokens)
    warnings = []
    for name, what in findings:
        warnings.append(f"この出力に、{'絶対パス' if what == 'path' else 'サーバーの操作のトークン'}がある"
                        f"（{name}）。入力の中身にあるもので、持ち出すと渡る")
    if unresolved:
        warnings.append(f"Wiki の外にある入力の実体かを、{unresolved} 件確かめられなかった（絶対パスの検査が不完全）")
    for name in unreadable_records:
        warnings.append(f"サーバーの記録を読めないので、トークンが含まれていないかを確かめられなかった（{name}）")
    for pid in c.render_failures:
        warnings.append(f"このページを描けない。そのページに「描けない」と出した（{pid}）")
    for kind, rel in snap.unreadable:
        warnings.append(f"この入力を読めない。ない Wiki と同じに描き、「読めない」と示した（{kind}: {rel}）")

    report = {"wiki": root.as_posix(), "out": Path(out).as_posix(), "generation": c.generation,
              "pages": len(c.drawn), "raw": raw, "warnings": list(warnings), "diagnostics": len(c.diagnostics)}
    fs.WARNINGS.clear()
    for message in warnings:
        fs.warn(message)
    report_text = json.dumps({"out": report["out"], "generation": report["generation"], "pages": report["pages"],
                              "warnings": len(warnings)}, ensure_ascii=False) + "\n"
    try:
        fs.build(root, out, files, default, owner, bdir, report_text=report_text)
    finally:
        flush_warnings()
    report["seconds"] = round(time.perf_counter() - started, 3)
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
        if cached and cached.snapshot.signature == sig and not cached.snapshot.transient:
            return cached
        c = core.Core(core.take_snapshot(root))
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
                           "id": pid, "path": c.out_name[pid], "html": c.render(c.out_name[pid], raw)}
            else:
                res = {"ok": False, "error": "unknown_op"}
        except Failed:
            res = {"ok": False, "error": "no_wiki"}
        except (core.InputsUnknown, core.InputsChanged):
            res = {"ok": False, "error": "unavailable"}
        except (ValueError, AttributeError):
            res = {"ok": False, "error": "bad_request"}
        except Exception as e:  # noqa: BLE001  例外の文字列（絶対パスを含む）は返さない
            res = {"ok": False, "error": "internal", "type": type(e).__name__}
        stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
        stdout.flush()
    return EXIT_OK


# ---------------------------------------------------------------------------
# サーバー（設計の 8。構造の直しの設計の 7.1）
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

    def __init__(self, root: Path, raw: str = "text", scope: "str | None" = None):
        self.root = root
        self.raw = raw
        # このサーバーが wiki のページ（サイドバーの先頭とタブの題名）に描くスコープ。サーバーは描くスコープを
        # ここにだけ持つ。組み立てるどの中核にも同じものを渡し、操作の口の status と起動の出力もこれを書く。
        # wiki を組み立てられないあいだは 503 を返してページを描かないが、この値は変わらない。
        self.scope = core.normalize_scope(scope)
        self.current: "core.Core | None" = None
        self.failed: "str | None" = None
        self.build_lock = threading.Lock()
        self.attempts: dict = {}
        # 最後に未初期化と決まった理由の種類（wiki_initialized.REASONS のキー）。応答の文に使う。
        self.uninitialized_kind = "no_marker"

    def _should_retry(self) -> bool:
        cur = self.current
        # 開けなかった入力は、ファイルを変えずに読めるようになりうるので、やり直す（UTF-8 でないものは変わらない）
        return self.failed is not None or (cur is not None and bool(cur.snapshot.transient))

    def _recently_tried(self, sig: str) -> bool:
        last = self.attempts.get(sig)
        return last is not None and time.monotonic() - last < RETRY_INTERVAL

    def get(self, wait: "float | None" = None) -> "tuple[core.Core | None, str | None]":
        """答えに使う中核と、古いときの理由（"failed" か "building"）を返す。中核がなければ (None, 理由)。

        要求のたびに wiki を読み直すので、要求のたびに初期化を確かめる（ADR-0019）。起動のあとで SCHEMA.md が
        なくなったら（調べたうえで未初期化と決まったら）、前の中核も使わずに (None, "uninitialized") を返し、理由の
        種類を uninitialized_kind に置く。中核は捨てない。SCHEMA.md が戻れば、入力の印が同じならそのまま使う。
        調べられなかったとき（アクセス権、読み取りの障害、循環するリンクなど）は、初期化されていると確かめられて
        いないので、新しい中核を組み立てない。前の中核があれば古いと示して使い、なければ組み立てられないと答える。
        入力の名前の集合が分からないときと、読んでいるあいだに Wiki が変わったときも、新しい中核を採らない
        （構造の直しの設計の 4.1、4.2）。次の要求で、またやり直す。
        """
        found = initialized.check(self.root)
        if found is not None:
            if found.certain:
                self.uninitialized_kind = found.kind
                return None, "uninitialized"
            return self._answer(self.current, None, "failed")
        wait = BUILD_WAIT if wait is None else wait
        request_sig = self._signature()
        cur = self.current
        if cur is not None and cur.snapshot.signature == request_sig and not self._should_retry():
            return cur, None
        if not self.build_lock.acquire(timeout=wait):
            return self._answer(self.current, request_sig, "building")
        try:
            sig = self._signature()
            cur = self.current
            if cur is not None and cur.snapshot.signature == sig and not self._should_retry():
                return self._answer(cur, request_sig, None)
            if self._recently_tried(sig):
                return self._answer(cur, request_sig, "failed" if self.failed else "building")
            self.attempts = {sig: time.monotonic()}
            if sig is None:
                # 入力の名前の集合が分からない。前の中核を古いと示して使う（なければ 503）。
                self.failed = self.failed or "InputsUnknown"
                return self._answer(cur, request_sig, "failed")
            try:
                snap = core.take_snapshot(self.root)
                new = core.Core(snap, scope=self.scope)
            except Exception as e:  # noqa: BLE001  前の中核を使い続ける（InputsUnknown、InputsChanged を含む）
                self.failed = type(e).__name__
                return self._answer(cur, request_sig, "failed")
            # 生成日時は中核に持たせる。答えに使う中核の世代と、いつも同じ組になる。
            new.generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.current = new
            self.failed = None
            return self._answer(new, request_sig, None)
        finally:
            self.build_lock.release()

    def _signature(self) -> "str | None":
        """入力の印。入力の名前の集合が分からなければ None（どの中核の印とも一致しない）。"""
        try:
            return core.signature(self.root)
        except Exception:  # noqa: BLE001  InputsUnknown のほか、思わない例外でも新しい中核を採らない
            return None

    def _answer(self, c, request_sig: "str | None", reason: "str | None"):
        if c is None:
            return None, reason or "building"
        if c.snapshot.signature != request_sig:
            return c, reason or ("failed" if self.failed else "building")
        if reason == "failed" and self.failed is not None:
            # 印が同じでも、組み立て直しに失敗しているあいだは、今の中核が最新とは言えない（実装の 1 回目のレビュー）
            return c, "failed"
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
        # 要求の境界（構造の直しの設計の 7.2）。1 つの要求の全体を囲み、ヘッダーのそろった 500 を返す。
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
        app.mark_activity()
        self._kind = "-"
        if self.headers.get("Host") != f"127.0.0.1:{app.port}":
            return self._text(421, "Misdirected Request")
        raw_path = urllib.parse.urlsplit(self.path).path
        # 操作の口の locate は、行き先（wikilink の中身）を、符号化した 1 つの要素で受け取る。行き先は
        # ファイルのパスとして使わず、中核の解決に渡すだけなので、下のパスの確かめ（`/` や `..` を拒む）を
        # 通さない。`[[concepts/a]]` と同じに、先頭の `/` や区切りの違う行き先も解決できる。
        raw_parts = raw_path[1:].split("/") if raw_path.startswith("/") else []
        if len(raw_parts) == 4 and raw_parts[1] == "control" and raw_parts[2] == "locate" \
                and _same(raw_parts[0], app.control_token):
            self._kind = "control"
            if self.command not in ("GET", "HEAD"):
                return self._text(405, "Method Not Allowed", {"Allow": "GET, HEAD"})
            try:
                target = urllib.parse.unquote(raw_parts[3], encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                return self._text(404, "Not Found")
            return self._control_locate(target)
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
            return self._send(200, app.assets[name], ctype)
        c, stale = app.state.get()
        if c is None and stale == "uninitialized":
            # 応答にパスを出さない（設計の 8.1）。理由はパスを含まない短い文にする。
            reason = initialized.REASONS.get(app.state.uninitialized_kind, initialized.REASONS["no_marker"])
            return self._text(503, f"{reason}。{initialized.INIT_HINT}", {"Retry-After": "5"})
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
        text = c.render(name, app.state.raw, generated_at=getattr(c, "generated_at", ""), stale=reason)
        ctype = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/html; charset=utf-8"
        return self._send(200, text.encode("utf-8"), ctype)

    def _control_locate(self, target: str):
        """行き先を wikilink と同じ規則で解決し、状態と、解決したページの出力の名前を返す。"""
        app = self.server.app
        c, _stale = app.state.get()
        if c is None:
            return self._json(200, {"state": "unavailable"})
        r = c.resolve(core.nfc(target))
        res = {"state": r["state"]}
        if r["state"] == "resolved":
            res["path"] = c.out_name[r["id"]]
            if r.get("anchor"):
                res["anchor"] = r["anchor"]
        return self._json(200, res)

    def _control(self, rest):
        app = self.server.app
        op = rest[0] if rest else ""
        if op == "status" and self.command in ("GET", "HEAD") and len(rest) == 1:
            # scope は BrowseState.scope である。使い回すときの起動の出力は、これを書く。
            return self._json(200, {"instance": app.instance, "wiki_key": app.key, "scope": app.state.scope})
        if op == "stop" and self.command == "POST" and len(rest) == 1:
            self._json(200, {"stopping": True})
            app.request_shutdown()
            return None
        if op == "locate" and self.command in ("GET", "HEAD") and len(rest) >= 2:
            return self._control_locate("/".join(rest[1:]))   # 符号化していない `concepts/a` の形
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
    def __init__(self, root: Path, idle: float, verbose: bool = False, scope: "str | None" = None,
                 assets: "dict | None" = None):
        self.root = root
        self.key = common.wiki_key(root)
        self.idle = idle
        self.verbose = verbose
        self.assets = assets or {}
        self.view_token = secrets.token_urlsafe(24)
        self.control_token = secrets.token_urlsafe(24)
        self.instance = secrets.token_hex(12)
        self.state = BrowseState(root, scope=scope)
        self.last = time.monotonic()
        self.httpd = None
        self.port = 0
        self.render_errors = 0
        self._stopping = threading.Event()

    def mark_activity(self):
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


def _live_status(rec: dict) -> "dict | None":
    """動いているサーバーに、操作の口で状態を聞く。答えなければ None。"""
    try:
        code, body = _http(int(rec["port"]), "GET", f"/{rec['control_token']}/control/status")
        if code != 200:
            return None
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return None


def _valid_record(rec) -> "dict | None":
    """記録の形を確かめる。正しくなければ None。記録は手がかりで、真実はロックである（設計の 7.1）。"""
    if not isinstance(rec, dict):
        return None
    if not isinstance(rec.get("port"), int) or not 0 < rec["port"] < 65536:
        return None
    if not all(isinstance(rec.get(k), str) and rec[k] for k in ("view_token", "control_token", "instance")):
        return None
    return rec


def _answering(rec: "dict | None") -> "dict | None":
    """記録の instance が、その口で答えているサーバーと同じなら、その答え。違うか答えなければ None。"""
    rec = _valid_record(rec)
    if rec is None:
        return None
    live = _live_status(rec)
    return live if live is not None and live.get("instance") == rec["instance"] else None


def _drawn_scope(live: dict) -> "str | None":
    """使い回すサーバーが操作の口で答えた、そのサーバーの BrowseState.scope。

    答えにないもの（スコープなしで起動したサーバーと、スコープを出さない前の版のサーバー）と、正しくないものは
    None とする。記録のファイル（server.json）からは読まない。記録は画面を描かないからである。
    """
    return core.normalize_scope(live.get("scope"))


def _state_dir(root: Path) -> str:
    return os.path.join(browse_dir(), f"{common.wiki_key(root)}.server")


def _locate(port: int, control_token: str, target: str) -> dict:
    try:
        code, body = _http(port, "GET", f"/{control_token}/control/locate/{urllib.parse.quote(target, safe='')}",
                           timeout=BUILD_WAIT + 5)
        return json.loads(body) if code == 200 else {"state": "unavailable"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"state": "unavailable"}


def _emit(url: str, port: int, reused: bool, page_state: "str | None", open_browser: bool,
          drawn: "str | None") -> None:
    """起動の出力（通信）。scope には、URL の先のサーバーの BrowseState.scope を書く。"""
    try:
        print(json.dumps({"url": url, "port": port, "reused": reused, "page": page_state, "scope": drawn},
                         ensure_ascii=False), flush=True)
    except (OSError, ValueError):
        say("起動の知らせを標準出力に出せなかった")
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


def serve(root: Path, page: "str | None", open_browser: bool, idle: float, verbose: bool = False,
          scope: "str | None" = None) -> int:
    bdir = browse_dir()
    state_dir = _state_dir(root)
    if os.name == "nt" and os.environ.get(common.DATA_DIR_ENV, "").strip():
        say("警告: INFO_HIVE_DATA_DIR を指定している。ほかの利用者が読める場所なら、サーバーの記録のトークンを読まれる")
    assets = core.load_assets()
    owner = fs.owner_of(root)
    app = App(root, idle, verbose, scope, assets)

    def make_record():
        # ロックを取ったあとに待ち受けを始め、記録を置き換える（設計の 7.1）
        httpd = _Server(("127.0.0.1", 0), Handler)
        httpd.app = app
        app.httpd = httpd
        app.port = httpd.server_address[1]
        return app.record()

    deadline = time.monotonic() + RECORD_WAIT
    while True:
        try:
            outcome, value = fs.serve_start(root, bdir, state_dir, owner, make_record)
        except BaseException:
            if app.httpd is not None:
                app.httpd.server_close()
            raise
        finally:
            flush_warnings()
        if outcome == "started":
            server = value
            break
        live = _answering(value)
        if live is not None:
            rec = value
            key = rec.get("wiki_key") or app.key

            def url_for(name, rec=rec, key=key):
                return f"http://127.0.0.1:{rec['port']}/{rec['view_token']}/w/{key}/{_quote_path(name)}"
            url, state = _page_url(url_for, int(rec["port"]), rec["control_token"], page)
            # 今回の --scope ではなく、動いているサーバー自身が操作の口で答えたものを書く。
            _emit(url, int(rec["port"]), True, state, open_browser, _drawn_scope(live))
            return EXIT_OK
        if time.monotonic() >= deadline:
            raise Failed("動いているサーバーに届かないので、使い回せない。/wiki:browse --stop で止めてからやり直す")
        time.sleep(0.2)
    try:
        threading.Thread(target=app.idle_watch, daemon=True).start()
        server_thread = threading.Thread(target=app.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        server_thread.start()
        url, state = _page_url(app.url, app.port, app.control_token, page)
        _emit(url, app.port, False, state, open_browser, app.state.scope)
        server_thread.join()
    finally:
        # 終了: 待ち受けを閉じる、記録を消す、ロックを離す、の順にする（設計の 7.1）
        app.httpd.server_close()
        fs.serve_finish(server)
        flush_warnings()
    return EXIT_OK


def stop(root: Path) -> int:
    """動いているサーバーを止める。何も作らない（構造の直しの設計の 6.2、7.1）。"""
    bdir = browse_dir()
    state_dir = _state_dir(root)
    owner = fs.owner_of(root)
    deadline = time.monotonic() + RECORD_WAIT
    while True:
        state, rec = fs.probe_server(root, bdir, state_dir, owner)
        if state == "not-running":
            _print_json({"stopped": False, "running": False})
            return EXIT_OK
        if _answering(rec) is not None:
            break
        if time.monotonic() >= deadline:
            raise Failed("サーバーが応答しないので、止められない。操作がなければ、いずれ終わる")
        time.sleep(0.2)
    try:
        _http(int(rec["port"]), "POST", f"/{rec['control_token']}/control/stop")
    except (OSError, http.client.HTTPException):
        pass
    deadline = time.monotonic() + STOP_WAIT
    while fs.probe_server(root, bdir, state_dir, owner)[0] != "not-running":
        if time.monotonic() >= deadline:
            raise Failed("サーバーが終わらないので、止められなかった")
        time.sleep(0.2)
    try:
        fs.remove_record(root, bdir, state_dir, owner, rec["instance"])
    finally:
        flush_warnings()
    _print_json({"stopped": True, "running": False})
    return EXIT_OK


def _print_json(value: dict) -> None:
    try:
        print(json.dumps(value), flush=True)
    except (OSError, ValueError):
        say("結果を標準出力に出せなかった")


# ---------------------------------------------------------------------------
# コマンドライン（コマンドの出口。構造の直しの設計の 7.2）
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    from wiki_text import localize_argparse  # noqa: PLC0415  （文字だけを扱うモジュール）

    parser = argparse.ArgumentParser(description="wiki を閲覧する（静的の書き出し、サーバー、中核の入出力）。")
    localize_argparse(parser)
    common_args = argparse.ArgumentParser(add_help=False)
    common_args.add_argument("--debug", action="store_true", help="思わない失敗のスタックトレースを標準エラーに出す")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="静的の HTML を書き出す", parents=[common_args])
    b.add_argument("wiki")
    b.add_argument("--out", default=None, help="書き出し先（既定は <data_dir>/browse/<wiki_key>/）")
    b.add_argument("--raw", choices=("text", "link", "basename", "none"), default=None,
                   help="raw: の出し方（既定は、既定の書き出し先で link、--out で basename）")
    _add_scope_arguments(b)
    sub.add_parser("core", help="中核を、標準入力と標準出力の JSON で呼ぶ", parents=[common_args])
    _add_server_commands(sub, common_args)
    args = parser.parse_args(argv)
    try:
        _load()
        return _dispatch(args)
    except Exception as e:  # noqa: BLE001  コマンドの出口の境界。種類だけの短い知らせと、0 でない終了コード
        return _exit_for(e, getattr(args, "debug", False))


def _dispatch(args) -> int:
    # wiki のパスを受け取るサブコマンド（build、serve、stop）は、振り分けの前の 1 か所で判定する（ADR-0019）。
    # 新しいサブコマンドも、wiki を引数に取れば既定でここを通る。core は要求ごとに wiki を受け取り、要求ごとに
    # 判定する（core_loop）。
    root = wiki_root(args.wiki) if hasattr(args, "wiki") else None
    if args.cmd == "build":
        build(root, args.out, args.raw, args.scope)
        return EXIT_OK
    if args.cmd == "core":
        sys.stdin.reconfigure(encoding="utf-8")
        return core_loop(sys.stdin, sys.stdout)
    return _run_server_command(args, root)


def _exit_for(e: Exception, debug: bool) -> int:
    if isinstance(e, Failed) or (fs is not None and isinstance(e, fs.Stop)):
        say(str(e))
    elif fs is not None and isinstance(e, (fs.Unknown, fs.Absent)):
        say(f"確かめられないので、何も変えずに止める: {_unknown_text(e)}")
    elif core is not None and isinstance(e, core.InputsUnknown):
        say(f"Wiki の入力の一覧を作れない（{e}）。アクセス権を確かめる")
    elif core is not None and isinstance(e, core.NameCollision):
        say(str(e))
    else:
        say(f"思わない失敗で止まった（{type(e).__name__}）。--debug を付けると詳しく出す")
    if debug:
        import traceback  # noqa: PLC0415
        try:
            traceback.print_exception(e, file=sys.stderr)
        except (OSError, ValueError):
            pass
    if fs is not None:
        flush_warnings()
    return EXIT_FAILED


def _add_scope_arguments(p) -> None:
    """画面に出す wiki のスコープ。wiki を選んだ側（/wiki:browse）が、その wiki が指示や設定で何として登録されて
    いるかで決めて渡す。このスクリプトは、スコープを決めるために何も読まず、wiki の場所から推し量らない。"""
    p.add_argument("--scope", choices=("user", "project"), default=None,
                   help="wiki のスコープ（user か project）。省くと画面に出さない")


def _add_server_commands(sub, common_args) -> None:
    """serve と stop（設計の 8）。"""
    s = sub.add_parser("serve", help="127.0.0.1 のサーバーを起動して開く（動いていれば使い回す）", parents=[common_args])
    s.add_argument("wiki")
    s.add_argument("--page", default=None, help="開くページ（[[slug]] と同じ規則で探す）")
    s.add_argument("--no-open", action="store_true", help="ブラウザで開かない（URL を出すだけ）")
    s.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE, help="操作がないときに終わるまでの秒数（既定 1800）")
    s.add_argument("--verbose", action="store_true", help="要求の方法、口の種類、状態の番号を標準エラーに出す")
    _add_scope_arguments(s)
    t = sub.add_parser("stop", help="動いているサーバーを止める", parents=[common_args])
    t.add_argument("wiki")


def _run_server_command(args, root: Path) -> int:
    """root は、main() が判定した wiki のルート。"""
    if args.cmd == "serve":
        return serve(root, args.page, not args.no_open, args.idle_timeout, args.verbose, args.scope)
    if args.cmd == "stop":
        return stop(root)
    raise Failed(f"未対応のコマンド: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
