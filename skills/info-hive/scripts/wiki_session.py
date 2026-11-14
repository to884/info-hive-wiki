#!/usr/bin/env python3
"""
wiki_session.py — Claude Code のセッションの記録（JSONL）から会話だけを抜き出し、raw を作る。

/wiki:ingest に session、session:<ID>、session:<パス>、<パス>.jsonl を渡したときに使う（info-hive-dev の
ADR-0020）。記録から会話だけを許可リストで抜き出し、自動の伏せ字をかけ（これを「土台」と呼ぶ）、人の決定
（伏せる位置と消すターンの番号）を当てて、raw を 1 回で作る。編集される写しは作らない。確認の前の会話の文は
ディスクに書かず、標準出力にだけ出す。

サブコマンド:
    locate  --session-id <ID>                     セッションの ID から記録のパスを探す
    list    [--limit 20]                           記録の一覧（新しい順）。会話の文は出さない
    inspect --transcript <P> [共通] [--wiki W --raw-root R --slug S]
                                                   セッションの記録と認めるかと、件数、警告、raw の有無
    show    --transcript <P> [共通] --title <T> [--decisions <F>] [--turns T1-T20] [--from T3:0]
            [--max-chars 20000] [--meta] [--slug S]
                                                   raw と同じバイト列を標準出力に出す（--meta は件数とハッシュ）
    decide  init --transcript <P> [共通] --wiki <W> --slug <S>
            mark --transcript <P> [共通] --decisions <F> --target <T5|cwd|git_branch> --text <文字列>
                 (--nth <k> | --all) [--all-turns]
            drop --transcript <P> [共通] --decisions <F> --turn <T9>
            list --decisions <F>
    write   --transcript <P> [共通] --title <T> --decisions <F> --wiki <W> --raw-root <R> --slug <S>
            --expect-sha256 <render_sha256>       raw を重ねて書かない形で置き、読み取り専用にする
    verify  --raw-root <R> --slug <S> --expect-sha256 <render_sha256>
                                                   raw のバイト列が承認したものか、読み取り専用かを照らす
    scan    --transcript <P> [共通] --decisions <F> [--allow <文字列> ...]  < 書く予定の文
                                                   伏せた文字列と、自動の伏せ字の規則に当たる文字列を探す

共通の引数: [--session-id <ID>] [--before <uuid>] [--tools summary|none] [--keep-reminders]
            [--utc-offset +09:00] [--max-bytes 268435456]
決定のファイルを渡すコマンドでは、省いた共通の引数を決定のファイルから取る。違う値を渡せば拒む。

終了コード: 0 = 成功（write の identical を含む）、1 = 引数かファイルの誤り（ハッシュの不一致、曖昧な mark、
行き先の検査、排他作成ができないファイルシステム、scan で見つかったことを含む）、2 = セッションとして認めない、
3 = conflict か slug_taken。

標準ライブラリと、同梱の markdown-it-py（自己検査だけ）を使う。Python 3.12 以上。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import format_fm_value, localize_argparse, parse_frontmatter_text  # noqa: E402
import wiki_initialized  # noqa: E402

EXTRACTED_BY = "wiki_session.py 0.1"
DECISIONS_FORMAT = 1
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_CHARS = 20000
TOOL_VALUE_MAX = 120
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,64}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,64}$")
OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
TURN_RE = re.compile(r"^T([1-9][0-9]*)$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)  # wiki_lint.py と同じ分け方

# uuid を持たない記録の、観測した種類（2026年11月14日、検証環境の全プロジェクト）。どれも読み飛ばす。
# ここにない種類が uuid なしで現れたら、読み飛ばしたうえで unknown_uuidless_type を出す。
KNOWN_UUIDLESS_TYPES = frozenset({
    "agent-name", "ai-title", "api-request", "api-request-blob", "api-request-shape", "artifact-autoreact-ledger",
    "artifact-comment-monitor", "atis-latch", "bridge-session", "cost-state", "custom-title", "file-history-delta",
    "file-history-snapshot", "frame-link", "last-prompt", "mode", "permission-mode", "pr-link", "queue-operation",
    "relocated", "session-owner", "tools-used", "worktree-state",
})
# 文脈の差し込みの attachment の、観測した種類（queued_command を除く）。origin がなければ透過 T にする。
CONTEXT_ATTACHMENT_TYPES = frozenset({
    "advisor_tool", "agent_listing_delta", "auto_mode", "batching_reminder_sent", "command_permissions",
    "compact_file_reference", "credential_org", "date", "date_change", "deferred_tools_delta", "deferred_tools_record",
    "directory", "edited_text_file", "environment", "file", "hook_additional_context", "hook_cancelled",
    "hook_success", "instructions", "invoked_skills", "max_turns_reached", "mcp_instructions_delta", "model",
    "nested_memory", "plan_file_reference", "plan_mode", "plan_mode_exit", "prompt_snapshot",
    "read_truncation_notice", "remote_session_change", "session_context", "silent_turn_reminder", "skill_listing",
    "skill_mention", "task_reminder", "task_status", "thinking_drop", "thinking_stripped", "total_tokens_reminder",
    "unknown_command_fallback",
})
TRANSPARENT_SYSTEM_SUBTYPES = frozenset({"stop_hook_summary", "informational", "api_error"})
INTERRUPTS = frozenset({"[Request interrupted by user]", "[Request interrupted by user for tool use]"})
APP_ELEMENTS = ("create-pr-command",)
TOOL_FIELDS = {
    "Read": "file_path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path",
    "Grep": "pattern", "Glob": "pattern", "Bash": "description", "PowerShell": "description",
    "WebFetch": "url", "WebSearch": "query", "Agent": "description", "Skill": "skill",
}

# 区分
H, I, A, T, X = "H", "I", "A", "T", "X"

# 印の文。スクリプトが決めた文だけを使う。
MARK_TASK = "（バックグラウンドの作業の通知。内容は除いた）"
MARK_PEER = "（ほかのセッションからの伝言。内容は除いた）"
MARK_COMPACT = "（会話の圧縮の要約。内容は除いた）"
MARK_META = "（スキルの本文などの差し込み。内容は除いた）"
MARK_QUEUED = "（キューに入った入力。人の発言と確かめられないので、内容は除いた）"
MARK_COMPACT_BOUNDARY = "（会話の圧縮の境目）"
MARK_NOT_HUMAN = "（人の発言でない記録。内容は除いた）"
MARK_OTHER = "（会話でない記録。内容は除いた）"
MARK_UNKNOWN = "（返答の対象の記録が範囲にないか、読めない）"
MARK_INTERRUPT = "（ユーザーが中断した）"
MARK_EMPTY_HUMAN = "（人の発言。内容は除いた）"
MARK_DROPPED = "（確認で消した）"
MARK_TEXTS = frozenset({MARK_TASK, MARK_PEER, MARK_COMPACT, MARK_META, MARK_QUEUED, MARK_COMPACT_BOUNDARY,
                        MARK_NOT_HUMAN, MARK_OTHER, MARK_UNKNOWN, MARK_INTERRUPT, MARK_EMPTY_HUMAN, MARK_DROPPED})

PLACE_EMAIL = "［メール］"
PLACE_SECRET = "［伏せ字］"
PLACE_HOME = "~"
PLACE_MANUAL = "［伏せ字］"

# 自動の伏せ字の規則。記録と引数だけで決まり、実行する PC の値は使わない。当てる順にならべる。
RE_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL)
RE_PREFIX = re.compile(r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                       r"|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})(?![A-Za-z0-9])")
RE_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
RE_NAMED = re.compile(r"(?i)(?<![A-Za-z0-9])(token|secret|password|passwd|api[_-]?key|access[_-]?key|client[_-]?secret)"
                      r"([\"']?\s*[:=]\s*[\"']?)([^\s\"'`,;<>［]{6,})")
RE_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
                      r"(?![A-Za-z0-9-])")
RE_HOME_WIN = re.compile(r"(?i)(?<![A-Za-z])[A-Z]:[\\/]+Users[\\/]+([^\\/\s\"'`<>|:*?]+)")
RE_HOME_POSIX = re.compile(r"(?<![A-Za-z0-9_.~-])/(?:Users|home)/([^/\s\"'`<>|:*?]+)")

NUL = chr(0)
REPLACEMENT = chr(0xFFFD)


class Fail(Exception):
    """引数かファイルの誤り。code は終了コード。"""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 記録の範囲


class Broken:
    """UTF-8 か JSON として読めない行。"""


BROKEN = Broken()


@dataclass
class Range:
    path: Path
    records: list            # 行ごとの JSON の値（読めなければ BROKEN）
    data: bytes              # 範囲のバイト列
    before: str | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _parse_line(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return BROKEN


def read_range(path: Path, before: str | None, max_bytes: int) -> Range:
    """ファイルの先頭から、--before の記録の行の先頭まで、または最後の改行までを読む。"""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise Fail(f"記録を読めない: {path}（{exc.strerror or exc}）")
    if not stat.S_ISREG(st.st_mode):
        raise Fail(f"記録がふつうのファイルではない: {path}")
    if st.st_size > max_bytes:
        raise Fail(f"記録が大きすぎる（{st.st_size} バイト。上限は --max-bytes {max_bytes}）: {path}")
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    end = data.rfind(b"\n") + 1
    records = []
    pos = 0
    cut = None
    while pos < end:
        nl = data.index(b"\n", pos)
        value = _parse_line(data[pos:nl])
        if before is not None and isinstance(value, dict) and value.get("uuid") == before:
            cut = pos
            break
        records.append(value)
        pos = nl + 1
    if before is not None and cut is None:
        raise Fail(f"--before の uuid の記録が範囲にない: {before}")
    return Range(path=path, records=records, data=data[:cut if cut is not None else end], before=before)


# ---------------------------------------------------------------- 分類


def _origin_kind(value) -> str | None:
    return value.get("kind") if isinstance(value, dict) and isinstance(value.get("kind"), str) else None


def _content(rec: dict):
    message = rec.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _plain_text(content) -> str | None:
    """本文が文字列か、text のブロック 1 つだけなら、その文字列。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) \
            and content[0].get("type") == "text" and isinstance(content[0].get("text"), str):
        return content[0]["text"]
    return None


def _tool_result_only(content) -> bool:
    return isinstance(content, list) and bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def classify_record(rec: dict) -> str:
    """uuid のある記録を H、I、A、T、X に分ける（設計の 4 節の表）。表にないものはすべて境界 X。"""
    kind = rec.get("type")
    if kind == "user":
        flags = any(rec.get(k) for k in ("isMeta", "isCompactSummary", "isSidechain"))
        if _origin_kind(rec.get("origin")) == "human" and not flags:
            return H
        content = _content(rec)
        if rec.get("origin") is None and _plain_text(content) in INTERRUPTS:
            return I
        if _tool_result_only(content):
            return T
        return X
    if kind == "assistant":
        return X if (rec.get("isSidechain") or rec.get("isApiErrorMessage")) else A
    if kind == "attachment":
        att = rec.get("attachment")
        if not isinstance(att, dict):
            return X
        atype = att.get("type")
        if atype == "queued_command":
            if _origin_kind(att.get("origin")) == "human" and isinstance(att.get("prompt"), str):
                return H
            return X
        if atype in CONTEXT_ATTACHMENT_TYPES and att.get("origin") is None and rec.get("origin") is None:
            return T
        return X
    if kind == "system":
        return T if rec.get("subtype") in TRANSPARENT_SYSTEM_SUBTYPES else X
    return X


def boundary_mark(rec: dict) -> str:
    """境界 X の記録の印の文。"""
    kind = rec.get("type")
    att = rec.get("attachment") if kind == "attachment" and isinstance(rec.get("attachment"), dict) else None
    okind = _origin_kind(att.get("origin")) if att is not None else _origin_kind(rec.get("origin"))
    if okind == "task-notification":
        return MARK_TASK
    if okind == "peer":
        return MARK_PEER
    if rec.get("isCompactSummary"):
        return MARK_COMPACT
    if rec.get("isMeta"):
        return MARK_META
    if att is not None and att.get("type") == "queued_command":
        return MARK_QUEUED
    if kind == "system" and rec.get("subtype") == "compact_boundary":
        return MARK_COMPACT_BOUNDARY
    if kind == "user":
        return MARK_NOT_HUMAN
    return MARK_OTHER


def keep_assistant_block(block) -> str | None:
    """アシスタントのブロックの扱い。"text" は残す、"tool" は 1 行の要約にする、None は除く（許可リスト）。"""
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype == "text" and isinstance(block.get("text"), str):
        return "text"
    if btype in ("tool_use", "server_tool_use"):
        return "tool"
    return None


def _safe_key(value) -> str:
    return value if isinstance(value, str) and KEY_RE.match(value) else "other"


def excluded_key(rec: dict) -> str:
    kind = _safe_key(rec.get("type"))
    if kind == "attachment" and isinstance(rec.get("attachment"), dict):
        return f"attachment:{_safe_key(rec['attachment'].get('type'))}"
    if kind == "system":
        return f"system:{_safe_key(rec.get('subtype'))}"
    return kind


# ---------------------------------------------------------------- 伏せ字


@dataclass
class Redactions:
    count: int = 0
    needles: list = field(default_factory=list)   # (規則の名前, 元の文字列)

    def add(self, rule: str, *values: str) -> None:
        self.count += 1
        for v in values:
            if v:
                self.needles.append((rule, v))


def redact(text: str, red: Redactions | None = None) -> str:
    """自動の伏せ字をかける。red があれば、置き換えた数と元の文字列を足す。"""
    red = red if red is not None else Redactions()

    def secret(m):
        red.add("auto_secret", m.group(0))
        return PLACE_SECRET

    def named(m):
        red.add("auto_secret", m.group(3))
        return m.group(1) + m.group(2) + PLACE_SECRET

    def email(m):
        red.add("auto_email", m.group(0), m.group(1))
        return PLACE_EMAIL

    def home(m):
        red.add("auto_home", m.group(1))
        return PLACE_HOME

    text = RE_PEM.sub(secret, text)
    text = RE_PREFIX.sub(secret, text)
    text = RE_JWT.sub(secret, text)
    text = RE_NAMED.sub(named, text)
    text = RE_EMAIL.sub(email, text)
    text = RE_HOME_WIN.sub(home, text)
    text = RE_HOME_POSIX.sub(home, text)
    return text


PATTERN_RULES = (("pattern_secret", RE_PEM), ("pattern_secret", RE_PREFIX), ("pattern_secret", RE_JWT),
                 ("pattern_secret", RE_NAMED), ("pattern_email", RE_EMAIL), ("pattern_home", RE_HOME_WIN),
                 ("pattern_home", RE_HOME_POSIX))


def pattern_hits(text: str) -> list:
    """自動の伏せ字の規則に当たる箇所（規則の名前, 開始, 終わり）。"""
    hits = []
    for rule, rx in PATTERN_RULES:
        for m in rx.finditer(text):
            hits.append((rule, m.start(), m.end()))
    return hits


# ---------------------------------------------------------------- 本文の加工


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace(NUL, REPLACEMENT)


RE_REMINDERS = re.compile(r"\A(?:\s*<system-reminder>.*?</system-reminder>)+\s*", re.DOTALL)
RE_COMMAND_ONLY = re.compile(r"\A\s*(?:<(command-name|command-message|command-args)>(.*?)</\1>\s*)+\Z", re.DOTALL)
RE_COMMAND_PART = re.compile(r"<(command-name|command-message|command-args)>(.*?)</\1>", re.DOTALL)
RE_APP_ELEMENT = re.compile(r"\A\s*<(" + "|".join(APP_ELEMENTS) + r")>.*?</\1>\s*\Z", re.DOTALL)


def human_text(rec: dict, keep_reminders: bool, excluded: Counter, warn) -> str:
    """人の発言の本文を、設計の 5 節の順に加工する。伏せ字はまだかけない。"""
    if rec.get("type") == "attachment":
        text = rec["attachment"]["prompt"]
    else:
        content = _content(rec)
        parts = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif btype == "image":
                    parts.append("［画像］")
                else:
                    excluded[f"user:block:{_safe_key(btype)}"] += 1
        text = "\n\n".join(parts)
    text = normalize_newlines(text)
    if not keep_reminders:
        m = RE_REMINDERS.match(text)
        if m:
            text = text[m.end():]
            warn("stripped_reminder")
    if RE_COMMAND_ONLY.match(text):
        found = {name: value.strip() for name, value in RE_COMMAND_PART.findall(text)}
        name = found.get("command-name") or found.get("command-message") or ""
        name = " ".join(name.split())
        if name and not name.startswith("/"):
            name = "/" + name
        args = " ".join(found.get("command-args", "").split())
        text = f"コマンド: `{name}{' ' + args if args else ''}`" if name else ""
    elif RE_APP_ELEMENT.match(text):
        text = f"（アプリの要素 {RE_APP_ELEMENT.match(text).group(1)}。中身は除いた）"
    elif text.lstrip().startswith("<"):
        warn("leading_tag")
    text = text.strip("\n")
    return text if text.strip() else ""


def tool_summary(block: dict, red: Redactions) -> str:
    name = block.get("name") if isinstance(block.get("name"), str) else "不明"
    name = " ".join(normalize_newlines(name).split()) or "不明"
    key = TOOL_FIELDS.get(name)
    inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
    value = inputs.get(key) if key else None
    line = f"[ツール] {redact(name, red)}"
    if isinstance(value, str) and value.strip():
        flat = " ".join(normalize_newlines(value).split("\n")).strip()
        flat = redact(flat, red)
        if len(flat) > TOOL_VALUE_MAX:
            flat = flat[:TOOL_VALUE_MAX] + "…"
        line += " " + flat
    return line


# ---------------------------------------------------------------- 抜き出し


@dataclass
class Turn:
    n: int
    kind: str            # "user"、"claude"、"marker"
    time: str
    body: str
    model: str = ""
    reply_to: int | None = None   # claude のときの宛先のターンの番号。None は不明

    @property
    def fenced(self) -> bool:
        return self.kind in ("user", "claude")

    def heading(self) -> str:
        if self.kind == "user":
            return f"T{self.n} ユーザー（{self.time}）"
        if self.kind == "claude":
            target = f"T{self.reply_to} への返答" if self.reply_to is not None else "不明への返答"
            return f"T{self.n} Claude（{self.model}、{self.time}、{target}）"
        return f"T{self.n} 印（{self.time}）"


@dataclass
class Options:
    tools: str = "summary"
    keep_reminders: bool = False
    utc_offset: str = "+00:00"
    session_id_arg: str | None = None
    before: str | None = None

    def tz(self) -> timezone:
        sign, hh, mm = OFFSET_RE.match(self.utc_offset).groups()
        delta = timedelta(hours=int(hh), minutes=int(mm))
        return timezone(-delta if sign == "-" else delta)

    def as_dict(self) -> dict:
        return {"tools": self.tools, "keep_reminders": self.keep_reminders, "utc_offset": self.utc_offset,
                "session_id_arg": self.session_id_arg}


@dataclass
class Extract:
    """記録の範囲から抜き出した土台。render() の入力になる。"""
    session_id: str
    turns: list
    cwd: str
    git_branch: str
    started_at: str
    ended_at: str
    versions: str
    source_sha256: str
    source_bytes: int
    cut_before: str
    options: Options
    excluded: Counter
    ignored: Counter
    redactions: Redactions
    warnings: list
    broken_lines: int
    humans: list                 # (uuid, 時刻, 伏せ字をかけた本文) の並び
    not_objects: int = 0
    untyped: int = 0

    def base(self) -> dict:
        return {
            "extracted_by": EXTRACTED_BY, "session_id": self.session_id, "cwd": self.cwd,
            "git_branch": self.git_branch, "started_at": self.started_at, "ended_at": self.ended_at,
            "claude_code_versions": self.versions, "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes, "cut_before": self.cut_before, "options": self.options.as_dict(),
            "excluded_records": format_counts(self.excluded), "auto_redactions": self.redactions.count,
            "turns": [{"n": t.n, "kind": t.kind, "time": t.time, "model": t.model, "reply_to": t.reply_to,
                       "body": t.body} for t in self.turns],
        }

    @property
    def base_sha256(self) -> str:
        data = json.dumps(self.base(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def target_text(self, target: str) -> str | None:
        if target == "cwd":
            return self.cwd
        if target == "git_branch":
            return self.git_branch
        m = TURN_RE.match(target)
        if m and 1 <= int(m.group(1)) <= len(self.turns):
            turn = self.turns[int(m.group(1)) - 1]
            return turn.body if turn.fenced else None
        return None

    def targets(self) -> list:
        return [f"T{t.n}" for t in self.turns if t.fenced] + ["cwd", "git_branch"]


def format_counts(counter: Counter) -> str:
    return ", ".join(f"{k}={counter[k]}" for k in sorted(counter))


def _time(value, tz: timezone) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _heading_time(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "時刻不明"


def _session_id(path: Path, records: list, forced: str | None) -> str:
    ids = {r.get("sessionId") for r in records if isinstance(r, dict) and isinstance(r.get("sessionId"), str)}
    if forced is not None:
        if forced not in ids:
            raise Fail(f"--session-id の ID が記録のどこにもない: {forced}")
        chosen = forced
    elif UUID_RE.match(path.stem):
        chosen = path.stem
    elif len(ids) == 1:
        chosen = next(iter(ids))
    else:
        raise Fail("セッションの ID を決められない（ファイル名が UUID でなく、範囲の sessionId が 1 種類でない）。"
                   "--session-id を渡す")
    if not SESSION_ID_RE.match(chosen):
        raise Fail("セッションの ID に使えない文字がある。--session-id を渡す")
    return chosen


def extract(rng: Range, opts: Options) -> Extract:
    """記録の範囲から、会話だけを抜き出して土台を作る。"""
    tz = opts.tz()
    excluded, ignored = Counter(), Counter()
    red = Redactions()
    warnings: list = []
    broken = not_objects = untyped = 0
    by_uuid: dict = {}
    order: list = []
    unknown_types: set = set()
    human_parents: Counter = Counter()

    def warn(code, turn=None):
        warnings.append((code, turn))

    for rec in rng.records:
        if rec is BROKEN:
            broken += 1
            continue
        if not isinstance(rec, dict):
            not_objects += 1
            continue
        if "uuid" not in rec:
            rtype = rec.get("type")
            ignored[_safe_key(rtype)] += 1
            if rtype not in KNOWN_UUIDLESS_TYPES:
                unknown_types.add(_safe_key(rtype))
            continue
        uid = rec.get("uuid")
        if not isinstance(uid, str) or not isinstance(rec.get("type"), str):
            untyped += 1
            continue
        if uid in by_uuid:
            warn("possible_rewind")
            continue
        kind = classify_record(rec)
        by_uuid[uid] = (rec, kind)
        order.append(uid)
        if kind == H and isinstance(rec.get("parentUuid"), str):
            human_parents[rec["parentUuid"]] += 1
    if broken:
        warn("broken_lines")
    for t in sorted(unknown_types):
        warn("unknown_uuidless_type")
    if any(v > 1 for v in human_parents.values()):
        warn("possible_rewind")

    session_id = _session_id(rng.path, [r for r in rng.records if isinstance(r, dict)], opts.session_id_arg)
    for uid in order:
        rec, _kind = by_uuid[uid]
        sid = rec.get("sessionId")
        if isinstance(sid, str) and sid != session_id:
            warn("mixed_session_ids")
            break

    # アシスタントの本文
    bodies: dict = {}
    for uid in order:
        rec, kind = by_uuid[uid]
        if kind != A:
            if kind in (T, X):
                excluded[excluded_key(rec)] += 1
            continue
        content = _content(rec)
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (
            content if isinstance(content, list) else [])
        pieces = []
        for block in blocks:
            how = keep_assistant_block(block)
            if how == "text":
                text = normalize_newlines(block["text"]).strip("\n")
                if text.strip():
                    pieces.append(("text", redact(text, red)))
            elif how == "tool":
                if opts.tools == "summary":
                    pieces.append(("tool", tool_summary(block, red)))
                else:
                    excluded["assistant:block:tool_use"] += 1
            else:
                btype = block.get("type") if isinstance(block, dict) else None
                excluded[f"assistant:block:{_safe_key(btype)}"] += 1
        if pieces:
            bodies[uid] = pieces

    # 返答の宛先
    limit = len(by_uuid)
    targets: dict = {}
    unknown_key: dict = {}
    for uid in bodies:
        parent = by_uuid[uid][0].get("parentUuid")
        last = uid
        steps = 0
        target = None
        while True:
            if not isinstance(parent, str) or parent not in by_uuid:
                break
            prec, pkind = by_uuid[parent]
            if pkind in (A, T):
                steps += 1
                if steps > limit:
                    warn("reply_unresolved")
                    break
                last = parent
                parent = prec.get("parentUuid")
                continue
            target = parent
            break
        targets[uid] = target
        if target is None:
            unknown_key[uid] = ("?", parent if isinstance(parent, str) else last)
    referenced = {t for t in targets.values() if t is not None and by_uuid[t][1] == X}

    # ターン
    turns: list = []
    turn_of: dict = {}
    humans: list = []

    def new_turn(kind, dt, body, **kw):
        turn = Turn(n=len(turns) + 1, kind=kind, time=_heading_time(dt), body=body, **kw)
        turns.append(turn)
        return turn

    pending_warn: list = []
    for uid in order:
        rec, kind = by_uuid[uid]
        dt = _time(rec.get("timestamp"), tz)
        if kind == H:
            pending_warn.clear()
            text = human_text(rec, opts.keep_reminders, excluded, lambda code: pending_warn.append(code))
            text = redact(text, red) if text else ""
            turn = new_turn("user", dt, text) if text else new_turn("marker", dt, MARK_EMPTY_HUMAN)
            for code in pending_warn:
                warn(code, turn.n)
            turn_of[uid] = turn.n
            humans.append((uid, turn.time, text))
        elif kind == I:
            turn_of[uid] = new_turn("marker", dt, MARK_INTERRUPT).n
        elif kind == X and uid in referenced:
            turn_of[uid] = new_turn("marker", dt, boundary_mark(rec)).n
        elif kind == A and uid in bodies:
            target = targets[uid]
            if target is not None and target not in turn_of:
                warn("reply_unresolved")
                unknown_key[uid] = ("?", target)
                target = None
            if target is None:
                key = unknown_key[uid]
                if key not in turn_of:
                    turn_of[key] = new_turn("marker", dt, MARK_UNKNOWN).n
                reply_to, group = turn_of[key], key
            else:
                reply_to, group = turn_of[target], target
            model = rec.get("message", {}).get("model") if isinstance(rec.get("message"), dict) else None
            model = model if isinstance(model, str) and MODEL_RE.match(model) else "不明"
            last = turns[-1] if turns else None
            if last is not None and last.kind == "claude" and getattr(last, "_group", None) == group:
                last._pieces.extend(bodies[uid])
            else:
                turn = new_turn("claude", dt, "", model=model, reply_to=reply_to)
                turn._group = group
                turn._pieces = list(bodies[uid])
    for turn in turns:
        if turn.kind == "claude":
            turn.body = join_pieces(turn._pieces)
            del turn._pieces, turn._group

    # frontmatter の値
    cwd = git_branch = ""
    times = []
    versions = set()
    for uid in order:
        rec, _kind = by_uuid[uid]
        if not cwd and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if not git_branch and isinstance(rec.get("gitBranch"), str):
            git_branch = rec["gitBranch"]
        dt = _time(rec.get("timestamp"), tz)
        if dt:
            times.append(dt)
        if isinstance(rec.get("version"), str) and VERSION_RE.match(rec["version"]):
            versions.add(rec["version"])
    cwd = redact(" ".join(normalize_newlines(cwd).split("\n")), red)
    git_branch = redact(" ".join(normalize_newlines(git_branch).split("\n")), red)
    return Extract(
        session_id=session_id, turns=turns, cwd=cwd, git_branch=git_branch,
        started_at=min(times).isoformat(timespec="seconds") if times else "",
        ended_at=max(times).isoformat(timespec="seconds") if times else "",
        versions=", ".join(sorted(versions)), source_sha256=rng.sha256, source_bytes=len(rng.data),
        cut_before=rng.before or "", options=opts, excluded=excluded, ignored=ignored, redactions=red,
        warnings=warnings, broken_lines=broken, humans=humans, not_objects=not_objects, untyped=untyped)


def join_pieces(pieces: list) -> str:
    out = []
    prev = None
    for kind, text in pieces:
        if out:
            out.append("\n" if kind == "tool" and prev == "tool" else "\n\n")
        out.append(text)
        prev = kind
    return "".join(out)


def summarize_warnings(warnings: list) -> list:
    counts: dict = {}
    for code, turn in warnings:
        entry = counts.setdefault(code, {"code": code, "count": 0, "turns": []})
        entry["count"] += 1
        if turn is not None and f"T{turn}" not in entry["turns"]:
            entry["turns"].append(f"T{turn}")
    return [counts[k] for k in sorted(counts)]


# ---------------------------------------------------------------- 決定


@dataclass
class Decisions:
    path: Path | None
    data: dict

    @property
    def spans(self) -> list:
        return self.data["spans"]

    @property
    def drop(self) -> list:
        return self.data["drop"]


def new_decisions(ex: Extract) -> dict:
    return {"format": DECISIONS_FORMAT, "extracted_by": EXTRACTED_BY, "session_id": ex.session_id,
            "source_sha256": ex.source_sha256, "source_bytes": ex.source_bytes, "cut_before": ex.cut_before,
            "options": ex.options.as_dict(), "base_sha256": ex.base_sha256, "spans": [], "drop": []}


def load_decisions(path: Path) -> Decisions:
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Fail(f"決定のファイルを読めない: {path}（{exc}）")
    keys = {"format", "extracted_by", "session_id", "source_sha256", "source_bytes", "cut_before", "options",
            "base_sha256", "spans", "drop"}
    if not isinstance(data, dict) or set(data) != keys or data["format"] != DECISIONS_FORMAT:
        raise Fail(f"決定のファイルの形が違う: {path}")
    opts = data["options"]
    if not (isinstance(opts, dict) and set(opts) == {"tools", "keep_reminders", "utc_offset", "session_id_arg"}
            and opts["tools"] in ("summary", "none") and isinstance(opts["keep_reminders"], bool)
            and isinstance(opts["utc_offset"], str) and OFFSET_RE.match(opts["utc_offset"])
            and (opts["session_id_arg"] is None or isinstance(opts["session_id_arg"], str))):
        raise Fail(f"決定のファイルの options の形が違う: {path}")
    spans = data["spans"]
    if not isinstance(spans, list) or not all(
            isinstance(s, dict) and set(s) == {"target", "start", "end"} and isinstance(s["target"], str)
            and type(s["start"]) is int and type(s["end"]) is int and 0 <= s["start"] < s["end"] for s in spans):
        raise Fail(f"決定のファイルの spans の形が違う: {path}")
    if not isinstance(data["drop"], list) or not all(type(n) is int and n >= 1 for n in data["drop"]):
        raise Fail(f"決定のファイルの drop の形が違う: {path}")
    if not isinstance(data["cut_before"], str) or not isinstance(data["session_id"], str):
        raise Fail(f"決定のファイルの形が違う: {path}")
    return Decisions(path=path, data=data)


def check_decisions(dec: Decisions, ex: Extract) -> None:
    """決定のファイルが、今の記録の範囲と土台に当たることを確かめる。"""
    d = dec.data
    if d["extracted_by"] != EXTRACTED_BY:
        raise Fail(f"決定のファイルを作った抽出器の版が違う: {d['extracted_by']}（今は {EXTRACTED_BY}）")
    if d["source_sha256"] != ex.source_sha256 or d["source_bytes"] != ex.source_bytes:
        raise Fail("記録の範囲が、決定のファイルを作ったときと違う（source_sha256 が一致しない）")
    if d["session_id"] != ex.session_id or d["cut_before"] != ex.cut_before:
        raise Fail("セッションの ID か --before が、決定のファイルと違う")
    if d["base_sha256"] != ex.base_sha256:
        raise Fail("土台が、決定のファイルを作ったときと違う（base_sha256 が一致しない）")
    for s in d["spans"]:
        text = ex.target_text(s["target"])
        if text is None or s["end"] > len(text):
            raise Fail(f"決定のファイルの伏せる位置が土台に当たらない: {s['target']}")
    for n in d["drop"]:
        if n > len(ex.turns):
            raise Fail(f"決定のファイルの消すターンがない: T{n}")


def merged_spans(spans: list) -> dict:
    """対象ごとに、重なりと隣り合いをまとめた区間。"""
    by_target: dict = {}
    for s in spans:
        by_target.setdefault(s["target"], []).append((s["start"], s["end"]))
    out = {}
    for target, items in by_target.items():
        items.sort()
        merged = [list(items[0])]
        for start, end in items[1:]:
            if start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        out[target] = [tuple(m) for m in merged]
    return out


def apply_spans(text: str, intervals: list) -> str:
    out, pos = [], 0
    for start, end in intervals:
        out.append(text[pos:start])
        out.append(PLACE_MANUAL)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def manual_strings(ex: Extract, spans: list) -> list:
    """人が伏せた区間の元の文字列（メモリの中だけで使う）。"""
    out = []
    for target, intervals in merged_spans(spans).items():
        text = ex.target_text(target) or ""
        out.extend(text[s:e] for s, e in intervals)
    return out


def write_decisions(dec: Decisions) -> None:
    data = (json.dumps(dec.data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    tmp = dec.path.with_name(dec.path.name + f".{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dec.path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- raw の生成


class RenderError(Exception):
    """自己検査を通らなかった。raw は書かない。"""


def fence_for(body: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def frontmatter_fields(ex: Extract, title: str, spans: list, drop: list) -> dict:
    merged = merged_spans(spans)
    cwd = apply_spans(ex.cwd, merged.get("cwd", []))
    branch = apply_spans(ex.git_branch, merged.get("git_branch", []))
    return {
        "title": title, "source_kind": "claude-code-session", "session_id": ex.session_id, "cwd": cwd,
        "git_branch": branch, "started_at": ex.started_at, "ended_at": ex.ended_at,
        "claude_code_versions": ex.versions, "source_sha256": ex.source_sha256, "source_sha256_of": "jsonl-range",
        "source_bytes": ex.source_bytes, "cut_before": ex.cut_before, "extracted_by": EXTRACTED_BY,
        "tools": ex.options.tools, "keep_reminders": "true" if ex.options.keep_reminders else "false",
        "utc_offset": ex.options.utc_offset, "turns": len(ex.turns),
        "excluded_records": format_counts(ex.excluded), "auto_redactions": ex.redactions.count,
        "manual_redactions": sum(len(v) for v in merged.values()), "dropped_turns": len(set(drop)),
    }


def rendered_turns(ex: Extract, spans: list, drop: list) -> list:
    """決定を当てたターン（kind、見出し、本文）。"""
    merged = merged_spans(spans)
    dropped = set(drop)
    out = []
    for turn in ex.turns:
        if turn.n in dropped:
            out.append(("marker", turn.heading(), MARK_DROPPED))
        elif turn.fenced:
            out.append(("fenced", turn.heading(), apply_spans(turn.body, merged.get(f"T{turn.n}", []))))
        else:
            out.append(("marker", turn.heading(), turn.body))
    return out


def render_parts(ex: Extract, title: str, spans: list, drop: list) -> list:
    """raw を、frontmatter（T0）とターン（T1…）の部分の並びで作る。つなぐと raw になる。純粋な関数。"""
    fields = frontmatter_fields(ex, title, spans, drop)
    lines = ["---"] + [f"{k}: {format_fm_value(v)}" for k, v in fields.items()] + ["---"]
    parts = [("T0", "\n".join(lines) + "\n\n")]
    turns = rendered_turns(ex, spans, drop)
    for i, (kind, heading, body) in enumerate(turns):
        if kind == "fenced":
            fence = fence_for(body)
            block = f"{fence}text\n{body}\n{fence}"
        else:
            block = body
        text = f"## {heading}\n\n{block}\n"
        if i + 1 < len(turns):
            text += "\n"
        parts.append((f"T{i + 1}", text))
    self_check("".join(p for _n, p in parts), fields, turns)
    return parts


def render(ex: Extract, title: str, spans: list, drop: list) -> bytes:
    return "".join(p for _n, p in render_parts(ex, title, spans, drop)).encode("utf-8")


def self_check(text: str, fields: dict, turns: list) -> None:
    """frontmatter を分け、本文を同梱のパーサーで読み、見出しとターン、フェンスと本文のあるターン、印のターンと
    段落が 1 対 1 に対応することを確かめる。通らなければ RenderError。"""
    import wiki_pages
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise RenderError("frontmatter を分けられない")
    meta = parse_frontmatter_text(m.group(1))
    expected = {k: (str(v) if isinstance(v, str) else v) for k, v in fields.items()}
    got = {k: meta.get(k) for k in expected}
    for k, v in expected.items():
        if isinstance(v, int):
            if str(got[k]) != str(v):
                raise RenderError(f"frontmatter の {k} を読み戻せない")
        elif got[k] != v:
            raise RenderError(f"frontmatter の {k} を読み戻せない")
    if set(meta) != set(expected):
        raise RenderError("frontmatter のキーが違う")
    tokens = wiki_pages.new_parser().parse(text[m.end():])
    want = []
    for kind, heading, body in turns:
        want += [("heading_open", "h2", None), ("inline", "", heading), ("heading_close", "h2", None)]
        if kind == "fenced":
            want.append(("fence", "code", body + "\n"))
        else:
            want += [("paragraph_open", "p", None), ("inline", "", body), ("paragraph_close", "p", None)]
    got_tokens = []
    for t in tokens:
        if t.type == "fence":
            if t.info != "text":
                raise RenderError("フェンスの情報の文字列が text でない")
            got_tokens.append(("fence", "code", t.content))
        elif t.type == "inline":
            got_tokens.append(("inline", "", t.content))
        else:
            got_tokens.append((t.type, t.tag, None))
    if got_tokens != want:
        raise RenderError("本文の構造が、ターンの並びと一致しない")


# ---------------------------------------------------------------- 題名とスラッグ


def nfkc_map(text: str) -> tuple[str, list]:
    """1 文字ずつ NFKC と大文字・小文字の統一をかけた文字列と、各文字の元の位置。"""
    out, index = [], []
    for i, ch in enumerate(text):
        norm = unicodedata.normalize("NFKC", ch).casefold()
        out.append(norm)
        index.extend([i] * len(norm))
    return "".join(out), index


def norm(text: str) -> str:
    return nfkc_map(text)[0]


def check_title(title: str, ex: Extract, spans: list) -> None:
    if not title or title != title.strip() or len(title) > 120:
        raise Fail("題名は、前後に空白のない 120 文字以内の 1 行にする")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in title):
        raise Fail("題名に制御文字か書式の文字がある")
    check_no_secret("題名", title, ex, spans)


def check_no_secret(label: str, value: str, ex: Extract, spans: list) -> None:
    if pattern_hits(value):
        raise Fail(f"{label}に、自動の伏せ字の規則に当たる文字列がある")
    nv = norm(value)
    for s in manual_strings(ex, spans):
        if norm(s) and norm(s) in nv:
            raise Fail(f"{label}に、伏せた区間の文字列がある")


def check_slug_value(slug: str, ex: Extract, spans: list) -> None:
    import wiki_fetch
    try:
        wiki_fetch.check_slug(slug)
    except ValueError as exc:
        raise Fail(str(exc))
    check_no_secret("スラッグ", slug, ex, spans)


# ---------------------------------------------------------------- raw の書き込み


def _is_link(path: Path) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def check_destination(wiki: Path, raw_root: Path, target: Path) -> None:
    """raw の行き先を確かめる（設計の 9 節の手順 4）。"""
    raw_abs = Path(os.path.abspath(raw_root))
    stop = Path(os.path.abspath(wiki)).parent
    chain = [raw_abs]
    if raw_abs.is_relative_to(stop):
        p = raw_abs.parent
        while p != stop and p.is_relative_to(stop):
            chain.append(p)
            p = p.parent
    for p in chain:
        if _is_link(p):
            raise Fail(f"raw のルートか、その途中の階層がリンクかジャンクションなので書かない: {p}")
    if os.path.lexists(target) and (_is_link(target) or not stat.S_ISREG(os.lstat(target).st_mode)):
        raise Fail(f"raw の行き先がリンクか、ふつうのファイルではないので書かない: {target}")
    real_raw = os.path.normcase(os.path.realpath(raw_abs))
    real_wiki = os.path.normcase(os.path.realpath(wiki))
    if os.path.commonpath([real_raw, real_wiki]) in (real_raw, real_wiki):
        raise Fail(f"raw のルートと wiki のルートの一方が、他方の中にある（raw のルート {real_raw}、wiki {real_wiki}）。"
                   "raw のルートには /wiki:init が作る raw/ などを使う")


class ExclusiveUnsupported(Exception):
    """重ねて書かない形で置けないファイルシステム。"""


def publish_exclusive(tmp: Path, target: Path) -> None:
    """一時ファイルを、行き先に重ねて書かない形で置く。行き先があれば FileExistsError。"""
    if os.name == "nt":
        os.rename(tmp, target)
        return
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ExclusiveUnsupported(str(exc))
    os.unlink(tmp)


def before_publish() -> None:
    """公開の直前（試験が例外を差し込む口）。"""


def after_publish() -> None:
    """公開の直後（試験が例外を差し込む口）。"""


def make_read_only(path: Path) -> None:
    mode = os.stat(path).st_mode
    if mode & 0o222:
        os.chmod(path, stat.S_IMODE(mode) & ~0o222)


def is_read_only(path: Path) -> bool:
    return not (os.stat(path).st_mode & 0o222)


def raw_ref_of(wiki: Path, target: Path) -> str:
    base = Path(os.path.abspath(wiki)).parent
    path = Path(os.path.abspath(target))
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def write_raw(wiki: Path, raw_root: Path, slug: str, data: bytes) -> tuple[str, Path]:
    """raw を重ねて書かない形で置く。返り値は（状態, パス）。"""
    target = raw_root / f"{slug}.md"
    while True:
        check_destination(wiki, raw_root, target)
        if os.path.lexists(target):
            return ("identical" if target.read_bytes() == data else "conflict"), target
        if os.path.lexists(wiki / "sources" / f"{slug}.md"):
            return "slug_taken", target
        tmp = raw_root / f".wiki-session-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            before_publish()
            try:
                publish_exclusive(tmp, target)
            except FileExistsError:
                continue
            except ExclusiveUnsupported as exc:
                raise Fail(f"このファイルシステムでは、raw を重ねて書かない形で置けないので書かない（{exc}）")
            after_publish()
            return "created", target
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)


# ---------------------------------------------------------------- 入口の共通


def out_json(obj) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def out_lines(obj: dict) -> None:
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        print(f"{key}: {value}")


def resolve_options(args, dec: Decisions | None) -> Options:
    given = {"tools": args.tools, "keep_reminders": args.keep_reminders, "utc_offset": args.utc_offset,
             "session_id_arg": args.session_id}
    if given["utc_offset"] is not None:
        m = OFFSET_RE.match(given["utc_offset"])
        if not m or int(m.group(2)) > 14 or int(m.group(3)) > 59:
            raise Fail(f"--utc-offset は +09:00 の形で書く: {given['utc_offset']}")
    before = args.before
    if dec is not None:
        stored = dec.data["options"]
        for key, value in given.items():
            if value is None:
                given[key] = stored[key]
            elif value != stored[key]:
                raise Fail(f"--{key.replace('_arg', '').replace('_', '-')} が決定のファイルの値と違う")
        stored_before = dec.data["cut_before"] or None
        if before is None:
            before = stored_before
        elif before != stored_before:
            raise Fail("--before が決定のファイルの値と違う")
    return Options(tools=given["tools"] or "summary", keep_reminders=bool(given["keep_reminders"]),
                   utc_offset=given["utc_offset"] or "+00:00", session_id_arg=given["session_id_arg"],
                   before=before)


def load_extract(args, dec: Decisions | None = None) -> Extract:
    opts = resolve_options(args, dec)
    rng = read_range(Path(args.transcript), opts.before, args.max_bytes)
    ex = extract(rng, opts)
    if dec is not None:
        check_decisions(dec, ex)
    return ex


def verdict(ex: Extract) -> tuple[str, str]:
    if ex.not_objects:
        return "not_session", "JSON のオブジェクトでない行がある"
    if ex.untyped:
        return "not_session", "uuid のある記録に type がない"
    if not ex.humans:
        return "not_session", "人の発言が 1 件もない（claude -p のセッションか、Claude Code の記録でない）"
    return "session", ""


def require_session(ex: Extract) -> None:
    v, reason = verdict(ex)
    if v != "session":
        raise Fail(f"セッションの記録として認めない: {reason}", code=2)


# ---------------------------------------------------------------- サブコマンド


def config_dir() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else Path.home() / ".claude"


def _project_dirs() -> list:
    projects = config_dir() / "projects"
    if not os.path.isdir(projects) or _is_link(projects):
        raise Fail(f"記録の置き場所が見つからないか、リンクなのでたどらない: {projects}")
    out = []
    with os.scandir(projects) as it:
        for entry in it:
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not os.path.isjunction(entry.path):
                out.append(Path(entry.path))
    return sorted(out)


def cmd_locate(args) -> int:
    sid = args.session_id
    if not UUID_RE.match(sid or ""):
        raise Fail(f"セッションの ID は小文字の UUID で渡す: {sid!r}")
    found = []
    for d in _project_dirs():
        cand = d / f"{sid}.jsonl"
        if os.path.lexists(cand) and not _is_link(cand) and stat.S_ISREG(os.lstat(cand).st_mode):
            found.append(cand)
    if len(found) != 1:
        raise Fail(f"記録が {len(found)} 件見つかった（1 件でなければならない）: "
                   + ", ".join(str(p) for p in found) if found else f"記録が見つからない: {sid}")
    result = {"session_id": sid, "transcript": str(found[0])}
    out_json(result) if args.json else print(found[0])
    return 0


def cmd_list(args) -> int:
    items = []
    for d in _project_dirs():
        with os.scandir(d) as it:
            for entry in it:
                if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    items.append((st.st_mtime, entry.name[:-6], d.name, entry.path, st.st_size))
    items.sort(reverse=True)
    rows = [{"session_id": sid, "project": proj, "transcript": path, "bytes": size,
             "modified": datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")}
            for mtime, sid, proj, path, size in items[:args.limit]]
    if args.json:
        out_json(rows)
    else:
        for r in rows:
            print(f"{r['modified']}  {r['session_id']}  {r['bytes']}  {r['transcript']}")
    return 0


def _raw_frontmatter(path: Path) -> dict:
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    m = FRONTMATTER_RE.match(text)
    return parse_frontmatter_text(m.group(1)) if m else {}


def cmd_inspect(args) -> int:
    trio = (args.wiki, args.raw_root, args.slug)
    if any(v is not None for v in trio) and not all(v is not None for v in trio):
        raise Fail("--wiki、--raw-root、--slug は 3 つそろえて渡す")
    ex = load_extract(args)
    v, reason = verdict(ex)
    tail = [{"uuid": uid, "time": t, "head": " ".join(text.split())[:40]} for uid, t, text in ex.humans[-5:]]
    title = ""
    for _uid, _t, text in ex.humans:
        first = text.strip().split("\n", 1)[0].strip() if text else ""
        if first:
            title = first[:60]
            break
    transcript = Path(args.transcript)
    sub = transcript.parent / transcript.stem / "subagents"
    subagent_files = 0
    if os.path.isdir(sub) and not _is_link(sub):
        with os.scandir(sub) as it:
            subagent_files = sum(1 for e in it if e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False))
    result = {
        "verdict": v, "reason": reason, "session_id": ex.session_id,
        "range": {"before": ex.cut_before or None, "source_sha256": ex.source_sha256, "source_bytes": ex.source_bytes},
        "human_tail": tail, "title_candidate": title, "turns": len(ex.turns), "excluded": dict(sorted(ex.excluded.items())),
        "ignored": dict(sorted(ex.ignored.items())), "redactions": ex.redactions.count,
        "warnings": summarize_warnings(ex.warnings), "broken_lines": ex.broken_lines,
        "subagent_files": subagent_files,
    }
    if args.wiki is not None:
        raw_root = Path(args.raw_root)
        target = raw_root / f"{args.slug}.md"
        exists = os.path.lexists(target)
        fm = _raw_frontmatter(target) if exists else {}
        result["raw"] = {"exists": exists, "session_id": fm.get("session_id"),
                         "source_sha256": fm.get("source_sha256"), "cut_before": fm.get("cut_before")}
        result["source_page_exists"] = os.path.lexists(Path(args.wiki) / "sources" / f"{args.slug}.md")
        temps = []
        if os.path.isdir(raw_root):
            with os.scandir(raw_root) as it:
                temps = sorted(e.name for e in it if e.name.startswith(".wiki-session-") and e.name.endswith(".tmp"))
        result["temp_files"] = temps
    out_json(result) if args.json else out_lines(result)
    return 0 if v == "session" else 2


def parse_turn_range(value: str | None, count: int) -> tuple[int, int]:
    if value is None:
        return 0, count
    m = re.match(r"^T([1-9][0-9]*)(?:-T([1-9][0-9]*))?$", value)
    if not m:
        raise Fail(f"--turns は T1-T20 か T5 の形で書く: {value}")
    a = int(m.group(1))
    b = int(m.group(2) or a)
    if a > b or b > count:
        raise Fail(f"--turns の範囲がターンの数（{count}）に合わない: {value}")
    return a, b


def cmd_show(args) -> int:
    dec = load_decisions(Path(args.decisions)) if args.decisions else None
    ex = load_extract(args, dec)
    require_session(ex)
    spans = dec.spans if dec else []
    drop = dec.drop if dec else []
    check_title(args.title, ex, spans)
    if args.slug is not None:
        check_slug_value(args.slug, ex, spans)
    try:
        parts = render_parts(ex, args.title, spans, drop)
    except RenderError as exc:
        raise Fail(f"raw の自己検査を通らない: {exc}")
    if args.meta:
        text = "".join(p for _n, p in parts)
        result = {"base_sha256": ex.base_sha256,
                  "render_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "chars": len(text),
                  "turns": len(ex.turns), "manual_redactions": sum(len(v) for v in merged_spans(spans).values()),
                  "dropped_turns": len(set(drop)), "warnings": summarize_warnings(ex.warnings)}
        out_json(result) if args.json else out_lines(result)
        return 0
    first, last = parse_turn_range(args.turns, len(ex.turns))
    seg, pos = first, 0
    if args.from_ is not None:
        m = re.match(r"^T([0-9]+):([0-9]+)$", args.from_)
        if not m:
            raise Fail(f"--from は T3:120 の形で書く: {args.from_}")
        seg, pos = int(m.group(1)), int(m.group(2))
        if not (first <= seg <= last) or pos > len(parts[seg][1]):
            raise Fail(f"--from の位置が範囲の外にある: {args.from_}")
    if args.max_chars < 1:
        raise Fail("--max-chars は 1 以上にする")
    budget = args.max_chars
    chunks = []
    next_from = None
    while seg <= last:
        text = parts[seg][1]
        take = text[pos:pos + budget]
        chunks.append(take)
        budget -= len(take)
        pos += len(take)
        if pos >= len(text):
            seg, pos = seg + 1, 0
        if budget == 0:
            if seg <= last:
                next_from = f"T{seg}:{pos}"
            break
    out = "".join(chunks)
    if args.json:
        out_json({"text": out, "next": next_from})
    else:
        sys.stdout.flush()
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
        if next_from:
            print(f"続きがある。次は --from {next_from} を付けて呼ぶ。", file=sys.stderr)
    return 0


def cmd_decide(args) -> int:
    if args.action == "list":
        dec = load_decisions(Path(args.decisions))
        result = {"spans": dec.spans, "drop": [f"T{n}" for n in sorted(set(dec.drop))]}
        out_json(result) if args.json else out_lines(result)
        return 0
    if args.action == "init":
        ex = load_extract(args)
        require_session(ex)
        import wiki_fetch
        try:
            wiki_fetch.check_slug(args.slug)
        except ValueError as exc:
            raise Fail(str(exc))
        folder = Path(args.wiki) / ".wiki-cache" / "session"
        step = Path(args.wiki)
        for part in (".wiki-cache", "session"):
            step = step / part
            if os.path.lexists(step) and (_is_link(step) or not os.path.isdir(step)):
                raise Fail(f"決定のファイルの置き場所がリンクか、ディレクトリではない: {step}")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{args.slug}-{secrets.token_hex(4)}.json"
        data = (json.dumps(new_decisions(ex), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with open(path, "xb") as fh:
            fh.write(data)
        result = {"decisions": str(path), "base_sha256": ex.base_sha256, "turns": len(ex.turns)}
        out_json(result) if args.json else out_lines(result)
        return 0
    dec = load_decisions(Path(args.decisions))
    ex = load_extract(args, dec)
    if args.action == "drop":
        for value in args.turn:
            m = TURN_RE.match(value)
            if not m or int(m.group(1)) > len(ex.turns):
                raise Fail(f"消すターンがない: {value}")
            n = int(m.group(1))
            if n not in dec.drop:
                dec.drop.append(n)
        dec.drop.sort()
        write_decisions(dec)
        result = {"drop": [f"T{n}" for n in dec.drop]}
        out_json(result) if args.json else out_lines(result)
        return 0
    # mark
    if not args.text:
        raise Fail("--text に伏せる文字列を渡す")
    if args.all_turns:
        targets = ex.targets()
        every = True
    else:
        if args.target is None:
            raise Fail("--target か --all-turns を渡す")
        if ex.target_text(args.target) is None:
            raise Fail(f"伏せる対象がない（本文のあるターン、cwd、git_branch のどれか）: {args.target}")
        targets = [args.target]
        every = args.all
    found = []
    for target in targets:
        text = ex.target_text(target) or ""
        start = text.find(args.text)
        while start != -1:
            found.append((target, start, start + len(args.text)))
            start = text.find(args.text, start + len(args.text))
    if not found:
        raise Fail("一致する文字列が土台にない（自動の伏せ字をかけたあとの文で探す）")
    if args.nth is not None:
        if every:
            raise Fail("--nth と --all は一緒に使えない")
        if not 1 <= args.nth <= len(found):
            raise Fail(f"--nth の番号が一致の数（{len(found)}）に合わない")
        chosen = [found[args.nth - 1]]
    elif every:
        chosen = found
    elif len(found) > 1:
        raise Fail(f"{len(found)} か所に一致する。--nth <k> か --all で選ぶ")
    else:
        chosen = found
    added = 0
    for target, start, end in chosen:
        span = {"target": target, "start": start, "end": end}
        if span not in dec.spans:
            dec.spans.append(span)
            added += 1
    write_decisions(dec)
    result = {"added": added, "targets": sorted({t for t, _s, _e in chosen}, key=lambda t: (len(t), t)),
              "manual_redactions": sum(len(v) for v in merged_spans(dec.spans).values())}
    out_json(result) if args.json else out_lines(result)
    return 0


def cmd_write(args) -> int:
    wiki = Path(args.wiki)
    raw_root = Path(args.raw_root)
    if not os.path.isdir(raw_root):
        raise Fail(f"raw のルートがない（作らない）: {raw_root}")
    import wiki_fetch
    try:
        wiki_fetch.check_slug(args.slug)
    except ValueError as exc:
        raise Fail(str(exc))
    dec = load_decisions(Path(args.decisions))
    ex = load_extract(args, dec)
    require_session(ex)
    try:
        data = render(ex, args.title, dec.spans, dec.drop)
    except RenderError as exc:
        raise Fail(f"raw の自己検査を通らない: {exc}")
    sha = hashlib.sha256(data).hexdigest()
    if sha != args.expect_sha256:
        raise Fail(f"作った raw のハッシュが、承認したものと一致しない（{sha}）。show --meta からやり直す")
    check_title(args.title, ex, dec.spans)
    check_slug_value(args.slug, ex, dec.spans)
    state, target = write_raw(wiki, raw_root, args.slug, data)
    result = {"raw_state": state, "raw_path": str(target), "raw_ref": raw_ref_of(wiki, target), "render_sha256": sha}
    if state in ("created", "identical"):
        if target.read_bytes() != data:
            raise Fail(f"書いた raw を読み直すと、バイト列が違う: {target}")
        import wiki_lint
        if wiki_lint.missing_raw({"type": "source", "raw": result["raw_ref"]},
                                 wiki / "sources" / f"{args.slug}.md", wiki):
            raise Fail(f"要約ページの raw に書く値で、raw が見つからない: {result['raw_ref']}")
        make_read_only(target)
    out_json(result) if args.json else out_lines(result)
    return 0 if state in ("created", "identical") else 3


def cmd_verify(args) -> int:
    target = Path(args.raw_root) / f"{args.slug}.md"
    if not os.path.lexists(target) or _is_link(target) or not stat.S_ISREG(os.lstat(target).st_mode):
        raise Fail(f"raw がないか、ふつうのファイルではない: {target}")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    result = {"raw_path": str(target), "sha256_matches": sha == args.expect_sha256,
              "read_only": is_read_only(target)}
    out_json(result) if args.json else out_lines(result)
    return 0 if result["sha256_matches"] and result["read_only"] else 1


def scan_text(text: str, ex: Extract, spans: list, allow: list) -> tuple[list, int]:
    """書く予定の文から、伏せた文字列と規則に当たる文字列を探す。返り値は（見つけた場所, 短くて探さない数）。"""
    needles: dict = {}
    for s in manual_strings(ex, spans):
        n = norm(s)
        if n:
            needles.setdefault(n, "manual")
    short = set()
    for rule, value in ex.redactions.needles:
        n = norm(value)
        if len(n) < 4:
            short.add(n)
            continue
        needles.setdefault(n, rule)
    allow_norm = [norm(a) for a in allow if a]
    findings = set()
    for line_no, line in enumerate(text.split("\n"), 1):
        line = line[:-1] if line.endswith("\r") else line
        nline, index = nfkc_map(line)
        masked = set()
        for a in allow_norm:
            start = nline.find(a)
            while start != -1:
                masked.update(range(start, start + len(a)))
                start = nline.find(a, start + 1)
        raw_masked = set()
        for a in allow:
            if not a:
                continue
            start = line.find(a)
            while start != -1:
                raw_masked.update(range(start, start + len(a)))
                start = line.find(a, start + 1)
        for n, rule in needles.items():
            start = nline.find(n)
            while start != -1:
                if not set(range(start, start + len(n))) <= masked:
                    findings.add((line_no, index[start] + 1, rule))
                start = nline.find(n, start + 1)
        for rule, start, end in pattern_hits(line):
            if not set(range(start, end)) <= raw_masked:
                findings.add((line_no, start + 1, rule))
    rows = [{"line": ln, "column": col, "rule": rule} for ln, col, rule in sorted(findings)]
    return rows, len(short)


def cmd_scan(args) -> int:
    dec = load_decisions(Path(args.decisions))
    ex = load_extract(args, dec)
    data = sys.stdin.buffer.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Fail("標準入力を UTF-8 として読めない")
    findings, short = scan_text(text, ex, dec.spans, args.allow or [])
    result = {"findings": findings, "skipped_short": short}
    out_json(result) if args.json else out_lines(result)
    return 1 if findings else 0


# ---------------------------------------------------------------- 引数


def build_parser() -> argparse.ArgumentParser:
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__,
                                                       formatter_class=argparse.RawDescriptionHelpFormatter))
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, transcript=True):
        if transcript:
            p.add_argument("--transcript", required=True, help="セッションの記録（JSONL）のパス")
        p.add_argument("--session-id", default=None, help="セッションの ID（記録のどこかの sessionId にあること）")
        p.add_argument("--before", default=None, help="この uuid の記録の行の先頭までを範囲にする")
        p.add_argument("--tools", choices=("summary", "none"), default=None, help="ツールの呼び出しの 1 行の要約を残すか")
        p.add_argument("--keep-reminders", action="store_const", const=True, default=None,
                       help="人の発言の先頭の <system-reminder> を除かない")
        p.add_argument("--utc-offset", default=None, help="時刻を直す時差（+09:00 の形。既定は +00:00）")
        p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="記録のファイルの大きさの上限")
        p.add_argument("--json", action="store_true", help="結果を JSON で出力する。キーは英語のまま")

    p = localize_argparse(sub.add_parser("locate", help="セッションの ID から記録のパスを探す"))
    p.add_argument("--session-id", required=True)
    p.add_argument("--json", action="store_true")
    p = localize_argparse(sub.add_parser("list", help="記録の一覧（新しい順）"))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = localize_argparse(sub.add_parser("inspect", help="セッションの記録と認めるかと、件数、警告"))
    common(p)
    p.add_argument("--wiki", default=None)
    p.add_argument("--raw-root", default=None)
    p.add_argument("--slug", default=None)
    p = localize_argparse(sub.add_parser("show", help="raw と同じバイト列を標準出力に出す"))
    common(p)
    p.add_argument("--title", required=True)
    p.add_argument("--decisions", default=None)
    p.add_argument("--slug", default=None)
    p.add_argument("--turns", default=None)
    p.add_argument("--from", dest="from_", default=None)
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    p.add_argument("--meta", action="store_true")
    p = localize_argparse(sub.add_parser("decide", help="伏せる位置と消すターンを、決定のファイルに書く"))
    dsub = p.add_subparsers(dest="action", required=True)
    q = localize_argparse(dsub.add_parser("init"))
    common(q)
    q.add_argument("--wiki", required=True)
    q.add_argument("--slug", required=True)
    q = localize_argparse(dsub.add_parser("mark"))
    common(q)
    q.add_argument("--decisions", required=True)
    q.add_argument("--target", default=None)
    q.add_argument("--text", required=True)
    q.add_argument("--nth", type=int, default=None)
    q.add_argument("--all", action="store_true")
    q.add_argument("--all-turns", action="store_true")
    q = localize_argparse(dsub.add_parser("drop"))
    common(q)
    q.add_argument("--decisions", required=True)
    q.add_argument("--turn", action="append", required=True)
    q = localize_argparse(dsub.add_parser("list"))
    q.add_argument("--decisions", required=True)
    q.add_argument("--json", action="store_true")
    p = localize_argparse(sub.add_parser("write", help="raw を重ねて書かない形で置く"))
    common(p)
    p.add_argument("--title", required=True)
    p.add_argument("--decisions", required=True)
    p.add_argument("--wiki", required=True)
    p.add_argument("--raw-root", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--expect-sha256", required=True)
    p = localize_argparse(sub.add_parser("verify", help="raw が承認したバイト列で、読み取り専用かを照らす"))
    p.add_argument("--raw-root", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--expect-sha256", required=True)
    p.add_argument("--json", action="store_true")
    p = localize_argparse(sub.add_parser("scan", help="書く予定の文に、伏せた文字列がないかを探す"))
    common(p)
    p.add_argument("--decisions", required=True)
    p.add_argument("--allow", action="append", default=None)
    return parser


COMMANDS = {"locate": cmd_locate, "list": cmd_list, "inspect": cmd_inspect, "show": cmd_show,
            "decide": cmd_decide, "write": cmd_write, "verify": cmd_verify, "scan": cmd_scan}


def main(argv: list[str] | None = None) -> int:
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse は引数の誤りを終了コード 2 にするが、2 は「セッションとして認めない」なので 1 に変える。
        return 1 if exc.code == 2 else (exc.code or 0)
    # wiki のパスを取るサブコマンドは、振り分けより前のここで判定する（記録を読む前、何かを書く前）。
    if getattr(args, "wiki", None) is not None:
        wiki_initialized.require_or_exit(args.wiki)
    try:
        return COMMANDS[args.command](args)
    except Fail as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
