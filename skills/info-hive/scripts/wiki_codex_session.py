#!/usr/bin/env python3
"""Codex の読み取り API から会話を取り出す。未加工の会話をファイルに保存しない。

呼び出し元は wiki_session.py。モデルのターン、resume、履歴の移行は実行しない。
Codex 自身が legacy 履歴の検索用の投影を更新する場合はある。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_session as ws  # noqa: E402

EXTRACTED_BY = "wiki_codex_session.py 0.1"
SOURCE_KIND = "codex-session"
SOURCE_HASH_KIND = "codex-thread-range-v1"
TIMEOUT = 120.0
PAGE_SIZE = 100
MAX_PAGES = 10000
SOURCES = frozenset({"cli", "vscode", "appServer"})
HISTORY_MODES = frozenset({"legacy", "paginated"})
PHASES = frozenset({None, "commentary", "final_answer"})
TOOL_TYPES = frozenset({"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
                        "webSearch", "imageView", "imageGeneration", "toolSearch"})
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _fail(message: str, code: int = 1):
    raise ws.Fail(message, code)


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not ws.UUID_RE.fullmatch(value):
        _fail("Codex のチャット ID は UUID で指定する")
    return value


def current_session_id() -> str:
    """子エージェントでは THREAD_ID が現在のチャット、SESSION_ID は親のことがある。"""
    value = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not value:
        _fail("実行中の Codex のチャット ID がない。session:<ID> で明示する")
    return _session_id(value)


def _is_current(sid: str) -> bool:
    value = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    return isinstance(value, str) and value == sid


class Rpc:
    """上限つきの JSON-RPC。サーバーのエラー文と標準エラーは外へ出さない。"""

    def __init__(self, max_bytes: int, timeout: float = TIMEOUT):
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            _fail("--max-bytes は正の整数にする")
        executable = shutil.which("codex")
        if executable is None:
            _fail("Codex CLI がない。codex を PATH に置く")
        self.deadline = time.monotonic() + timeout
        self.limit = max_bytes
        self.messages: queue.Queue = queue.Queue()
        self.next_id = 0
        self.closed = False
        try:
            self.process = subprocess.Popen(
                [executable, "app-server", "-c", "features.plugins=false"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except OSError:
            _fail("Codex の読み取り API を起動できない。Codex CLI の導入を確認する")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "info-hive-session", "version": "0.1"},
                                        "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def _read(self):
        used = 0
        try:
            while True:
                # readline の長さにも上限を付け、大きい 1 行で先にメモリを使い切らない。
                line = self.process.stdout.readline(self.limit - used + 1)
                if not line:
                    self.messages.put(("error", "Codex の読み取り API が応答の前に終了した"))
                    return
                used += len(line)
                if used > self.limit:
                    self.messages.put(("error", "Codex の API 出力が --max-bytes の上限を超えた"))
                    return
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    self.messages.put(("error", "Codex の API から UTF-8 の JSON を読めない"))
                    return
                self.messages.put(("message", value))
        except (OSError, ValueError):
            self.messages.put(("error", "Codex の API 出力を読めない"))

    def _send(self, message: dict):
        if time.monotonic() >= self.deadline:
            _fail("Codex の履歴の取得が時間の上限を超えた")
        try:
            data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (OSError, ValueError):
            _fail("Codex の読み取り API に接続できない")

    def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        ident = self.next_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                _fail("Codex の履歴の取得が時間の上限を超えた")
            try:
                kind, value = self.messages.get(timeout=remaining)
            except queue.Empty:
                _fail("Codex の履歴の取得が時間の上限を超えた")
            if kind == "error":
                _fail(value)
            if not isinstance(value, dict):
                _fail("Codex の API 応答の形が違う")
            if value.get("id") != ident:
                # このクライアントは通知を購読せず、サーバーからの処理依頼も受けない。
                if "method" in value and "id" in value:
                    _fail("Codex の読み取り API が未対応の処理を要求した")
                continue
            if "error" in value:
                _fail(f"Codex の API {method} が失敗した。CLI の版とチャット ID を確認する")
            result = value.get("result")
            if not isinstance(result, dict):
                _fail("Codex の API 応答の result がオブジェクトではない")
            return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # npm の codex は Node/cmd の子として実体を起動することがある。
            # 自分が起動したプロセス群だけを終え、実体を残さない。
            if os.name == "nt":
                killer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
                try:
                    subprocess.run([str(killer), "/PID", str(self.process.pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    self.process.kill()
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        try:
            self.process.stdout.close()
        except (OSError, ValueError):
            pass
        self.reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _pages(client: Rpc, method: str, params: dict):
    cursor = None
    seen = set()
    for _ in range(MAX_PAGES):
        request = dict(params)
        if cursor is not None:
            request["cursor"] = cursor
        result = client.request(method, request)
        rows = result.get("data")
        if not isinstance(rows, list):
            _fail("Codex の API のページに data の配列がない")
        yield rows
        cursor = result.get("nextCursor")
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            _fail("Codex の API のページのカーソルが重複しているか読めない")
        seen.add(cursor)
    _fail("Codex の履歴のページ数が上限を超えた")


def _thread(client: Rpc, sid: str) -> dict:
    result = client.request("thread/read", {"threadId": sid, "includeTurns": False})
    metadata = result.get("thread")
    if not isinstance(metadata, dict) or metadata.get("id") != sid:
        _fail("Codex の API が別のチャットのメタデータを返した")
    return metadata


def _metadata(metadata: dict) -> dict:
    if (not isinstance(metadata, dict) or not isinstance(metadata.get("source"), str)
            or metadata.get("source") not in SOURCES):
        _fail("人が使う Codex のチャットの記録ではない（未対応の source）", 2)
    if metadata.get("parentThreadId") is not None:
        _fail("子エージェントの記録は人との会話として取り込まない", 2)
    mode = metadata.get("historyMode", "legacy")
    if not isinstance(mode, str) or mode not in HISTORY_MODES:
        _fail("未対応の Codex の履歴形式である", 2)
    sid = _session_id(metadata.get("id"))
    cwd = metadata.get("cwd")
    version = metadata.get("cliVersion")
    git = metadata.get("gitInfo")
    branch = git.get("branch", "") if isinstance(git, dict) else ""
    if branch is None:
        branch = ""
    if not isinstance(cwd, str) or not isinstance(version, str) or not isinstance(branch, str):
        _fail("Codex のチャットのメタデータの形が違う", 2)
    return {"id": sid, "source": metadata["source"], "historyMode": mode,
            "cwd": cwd, "cliVersion": version, "gitBranch": branch}


def _snapshot(client: Rpc, sid: str) -> dict:
    metadata = _thread(client, sid)
    stable = _metadata(metadata)
    legacy = stable["historyMode"] == "legacy"
    turns = [row for page in _pages(client, "thread/turns/list", {
        "threadId": sid, "limit": PAGE_SIZE, "sortDirection": "asc",
        "itemsView": "full" if legacy else "summary"}) for row in page]
    if legacy:
        # legacy では items/list 自体が未対応の版がある。移行せず、同じ API の full を使う。
        items = []
        for turn in turns:
            if (not isinstance(turn, dict) or turn.get("itemsView") != "full"
                    or not isinstance(turn.get("items"), list)):
                _fail("Codex の legacy 履歴から、完全なターンを取得できない")
            for item in turn["items"]:
                items.append({"turnId": turn.get("id"), "item": item,
                              "startedAtMs": item.get("startedAtMs") if isinstance(item, dict) else None,
                              "completedAtMs": item.get("completedAtMs") if isinstance(item, dict) else None})
    else:
        items = [row for page in _pages(client, "thread/items/list", {
            "threadId": sid, "limit": PAGE_SIZE, "sortDirection": "asc"}) for row in page]
    if _metadata(_thread(client, sid)) != stable:
        _fail("取得中に Codex のチャットのメタデータが変わった。もう一度取得する")
    return {"thread": metadata, "turns": turns, "items": items}


def fetch_snapshot(session_id: str, max_bytes: int = ws.DEFAULT_MAX_BYTES, timeout: float = TIMEOUT) -> dict:
    sid = _session_id(session_id)
    with Rpc(max_bytes, timeout) as client:
        return _snapshot(client, sid)


def _identifier(value) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        _fail("Codex の履歴に、読めない項目かターンの ID がある", 2)
    return value


def _range(snapshot: dict, before: str | None) -> tuple[dict, list, dict]:
    if not isinstance(snapshot, dict):
        _fail("Codex の履歴のスナップショットの形が違う", 2)
    meta = _metadata(snapshot.get("thread"))
    if before is not None:
        _identifier(before)
    turns, entries = snapshot.get("turns"), snapshot.get("items")
    if not isinstance(turns, list) or not isinstance(entries, list):
        _fail("Codex の履歴に turns と items の配列がない", 2)
    turn_map = {}
    for turn in turns:
        if not isinstance(turn, dict):
            _fail("Codex の履歴のターンがオブジェクトではない", 2)
        tid = _identifier(turn.get("id"))
        if tid in turn_map:
            _fail("Codex の履歴のターン ID が重複している", 2)
        turn_map[tid] = (len(turn_map), turn)
    selected = []
    seen = set()
    previous = -1
    cut = before is None
    boundary_turn = turn_map.get(before, (None,))[0] if before is not None else None
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
            _fail("Codex の履歴の項目の形が違う", 2)
        tid = _identifier(entry.get("turnId"))
        item = entry["item"]
        ident = _identifier(item.get("id"))
        if ident in seen:
            _fail("Codex の履歴の項目 ID が重複している", 2)
        seen.add(ident)
        if tid not in turn_map:
            _fail("Codex の履歴の項目が、一覧にないターンを指している", 2)
        index = turn_map[tid][0]
        if index < previous:
            _fail("Codex の履歴のターンの順序が昇順ではない", 2)
        previous = index
        if before is not None and (ident == before or (boundary_turn is not None and index >= boundary_turn)):
            cut = True
            break
        selected.append(entry)
    if boundary_turn is not None:
        cut = True  # 項目のないターンを境目にすることもできる。
    if not cut:
        _fail("--before の項目かターンが Codex の履歴にない")
    return meta, selected, turn_map


def _time(entry: dict, turn: dict, tz) -> datetime | None:
    for key in ("startedAtMs", "completedAtMs"):
        value = entry.get(key)
        if value is None:
            value = entry.get("item", {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(tz)
            except (ValueError, OverflowError, OSError):
                pass
    value = turn.get("startedAt")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).astimezone(tz)
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _user_text(item: dict, excluded: Counter) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        _fail("Codex の userMessage の content が配列ではない", 2)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            excluded["user:block:unknown"] += 1
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            text = ws.normalize_newlines(block["text"])
            # 人の発言である根拠は API の userMessage。引用した指示の見た目では分類しない。
            parts.append(text)
        else:
            kind = block.get("type")
            kind = kind if isinstance(kind, str) and ws.KEY_RE.fullmatch(kind) else "unknown"
            excluded[f"user:block:{kind}"] += 1
    return "\n\n".join(parts).strip("\n")


def extract_snapshot(snapshot: dict, opts: ws.Options, max_bytes: int = ws.DEFAULT_MAX_BYTES) -> ws.Extract:
    """純粋な抜き出し。許可した本文以外は raw に通さず、同じ境目から同じハッシュを作る。"""
    meta, entries, turn_map = _range(snapshot, opts.before)
    if opts.session_id_arg is not None and opts.session_id_arg != meta["id"]:
        _fail("--session-id と Codex のチャット ID が違う")
    excluded, ignored = Counter(), Counter()
    records, visible = [], []
    warnings = [("codex_model_unavailable", None)]
    tz = opts.tz()
    for entry in entries:
        item = entry["item"]
        kind = item.get("type")
        if not isinstance(kind, str) or not ws.KEY_RE.fullmatch(kind):
            kind = "unknown"
        tid = entry["turnId"]
        dt = _time(entry, turn_map[tid][1], tz)
        # 出典のハッシュは表示の時差に依存させない。見出しだけ利用者の時差に直す。
        when = dt.astimezone(timezone.utc).isoformat(timespec="milliseconds") if dt else ""
        row = {"turn_id": tid, "id": item["id"], "kind": kind, "time": when}
        if kind == "userMessage":
            text = _user_text(item, excluded)
            row["text"] = text
            if text.strip():
                visible.append(("user", text, dt, item["id"], tid))
            else:
                excluded["user:empty"] += 1
        elif (kind == "agentMessage" and (item.get("phase") is None or isinstance(item.get("phase"), str))
              and item.get("phase") in PHASES and item.get("delivery") is None):
            text = item.get("text")
            if not isinstance(text, str):
                _fail("Codex の agentMessage の本文が文字列ではない", 2)
            row["text"] = ws.normalize_newlines(text).strip("\n")
            row["phase"] = item.get("phase")
            if text.strip():
                visible.append(("codex", row["text"], dt, item["id"], tid))
        elif kind in TOOL_TYPES:
            # 引数、標準出力、検索結果、差分は読まず、固定した種類名だけを要約する。
            if opts.tools == "summary":
                visible.append(("tool", ws.tool_line(kind, ""), dt, item["id"], tid))
            else:
                excluded[f"assistant:tool:{kind}"] += 1
        else:
            excluded[f"item:{kind}"] += 1
        records.append(row)
    payload = {"format": SOURCE_HASH_KIND, "thread": meta, "items": records}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1 or len(data) > max_bytes:
        _fail("Codex の会話の範囲が --max-bytes の上限を超えた")
    if not any(kind == "user" for kind, *_ in visible):
        _fail("取り込める人の発言が Codex の会話の範囲にない", 2)
    red = ws.Redactions()
    raw_parts = [text for _kind, text, _dt, _ident, _tid in visible] + [meta["cwd"], meta["gitBranch"]]
    redacted = ws.redact_parts(raw_parts, red)
    turns, humans, times = [], [], []
    target_by_turn = {}
    for (kind, _text, dt, ident, tid), text in zip(visible, redacted):
        if dt:
            times.append(dt)
        when = ws._heading_time(dt)
        if kind == "user":
            turn = ws.Turn(n=len(turns) + 1, kind="user", time=when, body=text)
            turns.append(turn)
            target_by_turn[tid] = turn.n
            humans.append((ident, when, text))
        else:
            target = target_by_turn.get(tid)
            if target is None:
                warnings.append(("reply_unresolved", len(turns) + 1))
            turns.append(ws.Turn(n=len(turns) + 1, kind="codex", time=when,
                                 body=text, model="不明", reply_to=target))
    cwd = ws.redact(" ".join(redacted[-2].split("\n")), red)
    branch = ws.redact(" ".join(redacted[-1].split("\n")), red)
    version = meta["cliVersion"] if ws.VERSION_RE.fullmatch(meta["cliVersion"]) else ""
    return ws.Extract(
        session_id=meta["id"], turns=turns, cwd=cwd, git_branch=branch,
        started_at=min(times).isoformat(timespec="seconds") if times else "",
        ended_at=max(times).isoformat(timespec="seconds") if times else "",
        versions=version, source_sha256=hashlib.sha256(data).hexdigest(), source_bytes=len(data),
        cut_before=opts.before or "", options=opts, excluded=excluded, ignored=ignored, redactions=red,
        warnings=warnings, broken_lines=0, humans=humans, source_kind=SOURCE_KIND, source_sha256_of=SOURCE_HASH_KIND,
        extracted_by=EXTRACTED_BY, version_field="codex_versions",
    )


def extract_session(session_id: str, opts: ws.Options, max_bytes: int = ws.DEFAULT_MAX_BYTES) -> ws.Extract:
    sid = _session_id(session_id)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        _fail("--max-bytes は正の整数にする")
    if _is_current(sid) and opts.before is None:
        _fail("実行中の Codex の会話には、locate で決めた --before を明示する")
    # 同じ境目を二度読む。タイトル、状態、取得後の追記はハッシュに含めない。
    # API 全体の出力量も上限に含めるため、二回分と JSON のメタデータの余裕を与える。
    with Rpc(max_bytes * 3 + 65536) as client:
        first = extract_snapshot(_snapshot(client, sid), opts, max_bytes)
        second = extract_snapshot(_snapshot(client, sid), opts, max_bytes)
    if first.source_sha256 != second.source_sha256 or first.base_sha256 != second.base_sha256:
        _fail("取得中に Codex の会話の範囲が変わった。境目を確認してもう一度取得する")
    return second


def locate(session_id: str | None, current: bool = False) -> dict:
    sid = current_session_id() if current or session_id is None else _session_id(session_id)
    if current and session_id is not None and sid != session_id:
        _fail("--current と --session-id が別の Codex のチャットを指している")
    snapshot = fetch_snapshot(sid)
    meta, entries, _turn_map = _range(snapshot, None)
    active = current or _is_current(sid)
    before = None
    if active:
        if not snapshot["turns"]:
            _fail("実行中の Codex のチャットに、境目にできるターンがない", 2)
        # 最新の発言は steering のこともある。取り込みの処理自体が raw に混ざらないよう、
        # 依頼を含むターン全体を除く。明示の --before では項目 ID も使える。
        before = _identifier(snapshot["turns"][-1].get("id"))
    return {"session_id": sid, "transcript": "codex:" + sid, "before": before, "current": active,
            "cutoff_kind": "turn" if active else None,
            "history_mode": meta["historyMode"], "source_kind": SOURCE_KIND,
            "turns": len(snapshot["turns"]), "items": len(entries)}


def list_sessions(limit: int = 20) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        _fail("一覧の件数は 1〜1000 にする")
    rows = []
    with Rpc(ws.DEFAULT_MAX_BYTES) as client:
        for archived in (False, True):
            collected = 0
            for page in _pages(client, "thread/list", {
                "limit": min(limit, PAGE_SIZE), "archived": archived, "sortKey": "updated_at",
                "sortDirection": "desc", "modelProviders": [], "sourceKinds": sorted(SOURCES),
                "useStateDbOnly": True,
            }):
                for metadata in page:
                    meta = _metadata(metadata)
                    updated = metadata.get("updatedAt")
                    rows.append({"session_id": meta["id"], "transcript": "codex:" + meta["id"],
                                 "cwd": ws.redact(meta["cwd"]), "history_mode": meta["historyMode"],
                                 "archived": archived,
                                 "updated_at": updated if isinstance(updated, int) else 0})
                    collected += 1
                if collected >= limit:
                    break
    return sorted(rows, key=lambda row: row["updated_at"], reverse=True)[:limit]
