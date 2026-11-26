#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = []
# ///
"""取り込み済みの会話を Claude Code のワークフローに渡し、差分を提案する。

    python -X utf8 -B wiki_dream.py --scheduled --wiki /path/wiki --raw-root /path/raw --out /path/dream
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from time import monotonic
import uuid


SCRIPTS = Path(__file__).resolve().parent
WORKFLOW = SCRIPTS / "wiki-dream.js"
PAGE_LIMIT = 12
CHUNK_BYTES = 24_000
CHUNK_LINES = 600
RUN_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError)
sys.path.insert(0, str(SCRIPTS))

from wiki_lint import parse_frontmatter
from wiki_fsguard import Refused, WriteGuard
from wiki_initialized import require
from wiki_pages import (
    absolute_user_path, build_page_index, code_line_mask, extract_wikilinks, iter_page_files, link_candidates,
    read_user_bytes, ref_values, resolve_raw,
)
from wiki_text import localize_argparse


@dataclass(frozen=True)
class Snapshot:
    path: Path
    root: Path
    data: bytes

    @property
    def relative(self):
        return self.path.relative_to(self.root).as_posix()

    @property
    def text(self):
        return self.data.decode("utf-8")

    @property
    def lines(self):
        return markdown_lines(self.text)

    @property
    def digest(self):
        return hashlib.sha256(self.data).hexdigest()

    def verify(self):
        if read_user_bytes(self.path, self.root) != self.data:
            raise ValueError(f"作業中に版が変わりました: {self.relative}")


def read_snapshot(path, root):
    return Snapshot(path, root, read_user_bytes(path, root))


def markdown_lines(text):
    # unified diff と同じく LF で数える。CRLF の CR は各行の末尾に残す。
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)


def source_key(entry):
    _, metadata, _ = entry
    return ref_values(metadata["session_id"])[0], ref_values(metadata["raw_sha256"])[0]


class Ledger:
    def __init__(self, db, wiki, raw_root):
        self.db = db
        # 分割した原本の読み込みを確認できた版だけを除外する。
        self.scope = json.dumps([2, *(os.path.normcase(str(root.resolve())) for root in (wiki, raw_root))])
        db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                scope TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('done', 'retry')),
                attempt INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                message TEXT NOT NULL,
                PRIMARY KEY (scope, session_id, sha256)
            )
        """)
        rows = db.execute(
            "SELECT session_id, sha256, state, attempt FROM sessions WHERE scope = ?", (self.scope,),
        )
        self.status = {(session, sha): (state, attempt) for session, sha, state, attempt in rows}
        self.attempt = max((attempt for _, attempt in self.status.values()), default=0) + 1

    def select(self, entries, limit):
        fresh, retry = [], []
        for entry in entries:
            state, attempt = self.status.get(source_key(entry), ("", 0))
            if state == "retry":
                retry.append((attempt, entry))
            elif not state:
                fresh.append(entry)
        retry.sort(key=lambda pair: pair[0])
        if not retry:
            return fresh[:limit]
        # 初回の会話が多い回でも、再試行の枠を残す。
        retry_entries = [entry for _, entry in retry]
        if not fresh:
            return retry_entries[:limit]
        if limit == 1:
            return fresh[:1] if self.attempt % 2 else retry_entries[:1]
        return [fresh[0], *retry_entries, *fresh[1:]][:limit]

    def groups(self, entries):
        fresh, retry = [], []
        for entry in entries:
            if source_key(entry) in self.status:
                retry.append([entry])
            else:
                fresh.append(entry)
        return ([fresh] if fresh else []) + retry

    def save(self, selected, failures, run_id):
        with self.db:
            self.db.executemany(
                """INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (scope, session_id, sha256) DO UPDATE SET
                       state = excluded.state, attempt = excluded.attempt,
                       run_id = excluded.run_id, message = excluded.message""",
                [
                    (self.scope, *source_key(entry), "retry" if name in failures else "done",
                     self.attempt, run_id, failures.get(name, ""))
                    for name, entry in selected.items()
                ],
            )


def select_sources(files, wiki, requested, limit, ledger=None):
    candidates = []
    for path in files:
        if not path.is_relative_to(wiki / "sources") or requested and path.stem not in requested:
            continue
        snapshot = read_snapshot(path, wiki)
        metadata, body, malformed = parse_frontmatter(snapshot.text.lstrip("\ufeff"))
        if not malformed and metadata.get("type") == "source" and len(ref_values(metadata.get("session_id"))) == 1:
            candidates.append((snapshot, metadata, body))
    missing = set(requested) - {entry[0].path.stem for entry in candidates}
    if missing:
        raise ValueError(f"取り込み済みの会話が見つかりません: {', '.join(sorted(missing))}")
    hashed = [entry for entry in candidates if len(ref_values(entry[1].get("raw_sha256"))) == 1]
    unverified = set(requested) - {entry[0].path.stem for entry in hashed}
    if unverified:
        raise ValueError(f"原本の SHA-256 が記録されていません: {', '.join(sorted(unverified))}")

    hashed.sort(key=lambda entry: entry[0].relative)
    hashed.sort(key=lambda entry: str(entry[1].get("ingested", "")), reverse=True)
    hashed.sort(key=lambda entry: entry[0].path.stem.endswith("-会話版"))
    selected = {}
    for entry in hashed:
        # 同じ会話に原本と会話版がある場合は、原本を優先する。
        selected.setdefault(ref_values(entry[1]["session_id"])[0], entry)
    entries = list(selected.values())
    return ledger.select(entries, limit) if ledger is not None else entries[:limit]


def session_input(source, metadata, wiki, raw_root):
    results = resolve_raw(metadata, source.path, wiki, allowed_roots=(raw_root,), raw_roots=(raw_root,))
    if len(results) != 1 or results[0].state != "resolved" or results[0].path is None:
        raise ValueError(f"原本を一意に特定できません: {source.relative}")
    path = results[0].path
    if path.suffix.lower() != ".md" or not path.is_file() or path.stat().st_size > 250_000:
        raise ValueError(f"試行で扱う原本は 250 KB 以下の Markdown です: {source.relative}")
    raw = read_snapshot(path, raw_root)
    if len(raw.data) > 250_000:
        raise ValueError(f"試行の原本サイズの上限を超えました: {source.relative}")
    expected = ref_values(metadata.get("raw_sha256"))
    if expected != [raw.digest]:
        raise ValueError(f"原本の SHA-256 が記録と一致しません: {source.relative}")
    raw_metadata, _, bad = parse_frontmatter(raw.text.lstrip("\ufeff"))
    if bad or ref_values(raw_metadata.get("session_id")) != ref_values(metadata.get("session_id")):
        raise ValueError(f"原本のセッション ID が一致しません: {source.relative}")
    return raw


def unique_link(ref, index):
    matches = link_candidates(ref, *index)
    return matches[0] if len(matches) == 1 else None


def candidates_for(selected, index):
    targets = set()
    for _, metadata, source_body in selected:
        refs = ref_values(metadata.get("concepts")) + extract_wikilinks(source_body)
        for ref in refs:
            relative = unique_link(ref, index)
            if relative and relative.startswith(("concepts/", "entities/", "synthesis/")):
                targets.add(relative)
    return sorted(targets)[:PAGE_LIMIT]


def fit_sources(entries, index):
    batch, targets = [], set()
    for entry in entries:
        proposed = targets.union(candidates_for([entry], index))
        if len(proposed) <= PAGE_LIMIT:
            batch.append(entry)
            targets = proposed
    return batch


def editable_lines(snapshot):
    text = snapshot.text.lstrip("\ufeff")
    _, body, malformed = parse_frontmatter(text)
    prefix = text[:len(text) - len(body)]
    if malformed or not prefix.endswith("\n") or not text.endswith(body):
        raise ValueError(f"記事の frontmatter を確認できません: {snapshot.relative}")
    lines = [line.rstrip("\r\n") for line in markdown_lines(body)]
    return {
        number: line
        for number, (line, code) in enumerate(zip(lines, code_line_mask(lines)), prefix.count("\n") + 1)
        if not code and re.match(r"(?:- )?[^\s#>|<`~\[+*\-]", line)
    }


def write_numbered(path, snapshot, editable=()):
    lines = [line.rstrip("\r\n") for line in snapshot.lines]
    path.write_text(
        "".join(f"{number:04d}{'*' if number in editable else ' '} {line}\n"
                for number, line in enumerate(lines, 1)),
        encoding="utf-8",
    )


def write_chunks(staging, name, raw):
    files, checks, block = [], [], []
    size = 0

    def flush():
        file = f"{name}-part{len(files) + 1}.txt"
        check = uuid.uuid4().hex
        (staging / file).write_text("".join(block) + f"確認コード {check}\n", encoding="utf-8")
        files.append(file)
        checks.append(check)
        block.clear()

    for number, line in enumerate(raw.lines, 1):
        content = line.rstrip("\r\n")
        for start in range(0, max(1, len(content)), 1000):
            piece = f"{number:04d}  {content[start:start + 1000]}\n"
            length = len(piece.encode("utf-8"))
            if size + length > CHUNK_BYTES or len(block) >= CHUNK_LINES:
                flush()
                size = 0
            block.append(piece)
            size += length
    if block:
        flush()
    return files, checks


def claude_workflow(staging, manifest, timeout):
    claude = shutil.which("claude")
    if not claude:
        # デスクトップの定期タスクでは、端末で設定した PATH がなくても既定の導入先を探す。
        name = "claude.exe" if os.name == "nt" else "claude"
        claude = shutil.which(str(Path.home() / ".local" / "bin" / name))
    if not claude:
        raise ValueError("Claude Code の実行ファイルが見つかりません。")
    folder = staging / ".claude" / "workflows"
    folder.mkdir(parents=True)
    shutil.copyfile(WORKFLOW, folder / WORKFLOW.name)
    env = os.environ.copy()
    env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    command = [
        claude, "-p", "--model", "claude-haiku-5-5", "--effort", "low",
        "--setting-sources", "project", "--no-session-persistence", "--strict-mcp-config",
        "--settings", '{"disableAllHooks":true,"autoMemoryEnabled":false}',
        "--tools", "Workflow,Read", "--permission-mode", "dontAsk",
        "--allowedTools", "Workflow(wiki-dream)",
        *(f"Read(./{item['id']}.txt)" for item in (*manifest["sources"], *manifest["pages"])),
        *(f"Read(./{file})" for source in manifest["sources"] for file in source["chunks"]),
        "--output-format", "stream-json", "--verbose", "--",
        "/wiki-dream " + json.dumps(manifest, ensure_ascii=True, separators=(",", ":")),
    ]
    run = subprocess.run(command, cwd=staging, env=env, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    trace = workflow_result(events, manifest, run.returncode)
    return trace


def workflow_result(events, manifest, returncode):
    starts = [
        event for event in events
        if event.get("subtype") == "task_started" and event.get("workflow_name") == "wiki-dream"
    ]
    calls = [
        part for event in events if event.get("type") == "assistant"
        for part in event.get("message", {}).get("content", [])
        if part.get("type") == "tool_use" and part.get("name") == "Workflow"
    ]
    if returncode or len(starts) != 1 or len(calls) != 1:
        raise ValueError(
            f"Claude Code がワークフローを一度だけ起動できませんでした"
            f"（終了コード {returncode}、呼び出し {len(calls)} 件、起動 {len(starts)} 件）。"
        )
    call, start = calls[0], starts[0]
    if call.get("input") != {"name": "wiki-dream", "args": manifest} or start.get("tool_use_id") != call["id"]:
        raise ValueError("ワークフローが受け取った引数を確認できません。")
    if start.get("prompt", "").rstrip() != WORKFLOW.read_text(encoding="utf-8").rstrip():
        raise ValueError("実行されたワークフローが保存済みスクリプトと一致しません。")
    completed = [
        event for event in events if event.get("subtype") == "task_notification"
        and event.get("task_id") == start.get("task_id") and event.get("run_id") == start.get("run_id")
    ]
    if len(completed) != 1 or completed[0].get("status") != "completed":
        raise ValueError("ワークフローが完了しませんでした。")
    notification = completed[0]
    path = Path(notification["output_file"]).resolve(strict=True)
    temporary = Path(tempfile.gettempdir()).resolve() / "claude"
    if not path.is_relative_to(temporary) or path.parent.name != "tasks" or path.stem != start["task_id"]:
        raise ValueError("Claude Code の結果ファイルを確認できません。")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise ValueError("Claude Code の結果が所定の形式ではありません。")
    result = payload["result"]
    if result.get("run") != manifest["run"]:
        raise ValueError("今回のワークフローの結果を確認できません。")
    source_ids = {source["id"] for source in manifest["sources"]}
    incomplete = result["incomplete"]
    if not isinstance(incomplete, list) or len(incomplete) != len(set(incomplete)) or set(incomplete) - source_ids:
        raise ValueError("抽出に失敗した会話の識別子を確認できません。")
    succeeded = source_ids - set(incomplete)
    expected = dict.fromkeys(succeeded, "claude-haiku-5-5")
    if succeeded:
        expected["integration"] = "claude-sonnet-5-5"
    agents = [item for item in payload.get("workflowProgress", []) if item.get("type") == "workflow_agent"]
    models = {agent["label"]: agent["model"] for agent in agents if agent.get("state") == "done"}
    if models != expected or Counter(agent["label"] for agent in agents) != Counter(source_ids | expected.keys()):
        raise ValueError("指定したモデルでの処理結果を確認できません。")
    if Counter(reading["source"] for reading in result["findings"]) != Counter(succeeded):
        raise ValueError("会話ごとの抽出結果に不足か重複があります。")
    if succeeded and not isinstance(result["conclusion"], dict):
        raise ValueError("Sonnet の照合と統合が完了しませんでした。")
    return result, models


def grounded(finding, lines):
    number, quote = finding["line"], finding["quote"]
    return type(number) is int and 1 <= number <= len(lines) and len(quote) >= 8 and quote in lines[number - 1]


def verified_findings(result, raw_lines, excluded):
    readings = [reading for reading in result["findings"] if reading["source"] not in excluded]
    if Counter(reading["source"] for reading in readings) != Counter(raw_lines.keys()):
        raise ValueError("会話ごとの抽出結果に不足か重複があります。")
    evidence = set()
    for reading in readings:
        source = reading["source"]
        lines = raw_lines[source]
        for finding in reading["findings"]:
            if grounded(finding, lines):
                evidence.add((source, finding["line"]))
    return evidence


def cross_checked(ref, evidence, raw_lines, claims):
    source, line = ref["source"], ref["line"]
    checked_line, quote = ref["checked_line"], ref["checked_quote"]
    if type(line) is not int or type(checked_line) is not int or (source, line) not in evidence:
        return False
    return (
        1 <= checked_line <= len(raw_lines[source])
        and abs(line - checked_line) <= 2
        and len(quote) >= 8
        and quote in raw_lines[source][checked_line - 1]
        and all(quote not in claim for claim in claims)
    )


def diff_path(path):
    # Git の差分ヘッダーでは、引用符や制御文字を含むパスを C 形式で引用する。
    escaped = re.sub(r'["\\\x00-\x20\x7f]', lambda match: f"\\{ord(match[0]):03o}", path)
    return f'"{escaped}"' if escaped != path else path


def compose(result, sessions, pages, index, excluded=(), *, base=None):
    raw_lines = {name: raw.lines for name, (_, raw) in sessions.items()}
    evidence = verified_findings(result, raw_lines, excluded)
    claims = [finding["claim"] for reading in result["findings"] for finding in reading["findings"]]
    edits = result["conclusion"]["edits"]
    edit_counts = Counter((edit["page"], edit["line"]) for edit in edits)
    changes, report, skipped = {}, [], []
    for edit in edits:
        page_id, number = edit["page"], edit["line"]
        if page_id not in pages or type(number) is not int:
            raise ValueError("対象にない記事または行が提案されました。")
        page = pages[page_id]
        if edit_counts[(page_id, number)] > 1:
            skipped.append(f"{page.relative}:{number} への提案が重複しています。")
            continue
        old = editable_lines(page).get(number)
        text = edit["text"]
        refs = [(ref["source"], ref["line"]) for ref in edit["evidence"]]
        if not refs or not all(cross_checked(ref, evidence, raw_lines, claims) for ref in edit["evidence"]):
            skipped.append(f"{page.relative}:{number} の根拠を原本で照合できませんでした。")
            continue
        if old is None or "\r" in text or "\n" in text or not text:
            skipped.append(f"{page.relative}:{number} は一行の本文編集として扱えません。")
            continue
        if page.lines[number - 1].rstrip("\r\n") != old:
            raise ValueError(f"検査した行が差分の対象行と一致しません: {page.relative}:{number}")
        before_links = Counter(extract_wikilinks(old))
        after_links = Counter(extract_wikilinks(text))
        if text != old and (
            not re.match(r"(?:- )?[^\s#>|<`~\[+*\-]", text)
            or old.startswith("- ") != text.startswith("- ")
            or "<" in text or re.findall(r"\]\([^)]*\)", old) != re.findall(r"\]\([^)]*\)", text)
            or before_links - after_links
            or any(unique_link(link, index) is None for link in after_links - before_links)
        ):
            skipped.append(f"{page.relative}:{number} の提案が既存の段落またはリンクの形を変えます。")
            continue
        targets = {unique_link(link, index) for link in after_links}
        slugs = sorted({sessions[ref[0]][0].relative.removesuffix(".md") for ref in refs})
        if any(unique_link(slug, index) != slug + ".md" for slug in slugs):
            skipped.append(f"{page.relative}:{number} の出典リンクを一意に解決できません。")
            continue
        for slug in slugs:
            if slug + ".md" not in targets:
                text += f" [[{slug}]]"
        if text == old:
            continue
        changed = changes.setdefault(page_id, {})
        changed[number] = text
        locations = ", ".join(f"{sessions[source][1].path}:{line}" for source, line in dict.fromkeys(refs))
        report.append(f"- {page.relative}:{number} — {edit['reason']}（{locations}）")

    diff = []
    for page_id, replacements in changes.items():
        page = pages[page_id]
        lines = page.lines
        final = list(lines)
        for number, text in replacements.items():
            old = lines[number - 1]
            final[number - 1] = text + old[len(old.rstrip("\r\n")):]
        filename = page.path.relative_to(base if base is not None else page.root).as_posix()
        for line in unified_diff(lines, final, fromfile=diff_path("a/" + filename), tofile=diff_path("b/" + filename)):
            diff.append(line)
            if not line.endswith("\n"):
                diff.append("\n\\ No newline at end of file\n")
    return "".join(diff), report, skipped


def run_batch(wiki, raw_root, args, destination, ledger, base):
    files = iter_page_files(wiki)
    pages_by_rel = {path.relative_to(wiki).as_posix(): path for path in files}
    index = build_page_index(
        [{"slug": path.stem, "rel_path": relative} for relative, path in pages_by_rel.items()],
        value=lambda page: page["rel_path"],
    )
    entries = select_sources(files, wiki, args.source, args.limit, ledger)
    if not entries:
        return []
    groups = [entries] if ledger is None else [fit_sources(group, index) for group in ledger.groups(entries)]
    deadline = monotonic() + args.timeout
    results = []
    for group in groups:
        if results and monotonic() >= deadline:
            print("実行時間の上限に達しました。残りの会話は次回に回します。")
            break
        results.append(run_group(wiki, raw_root, args, destination, ledger, group, pages_by_rel, index, base, deadline))
    return results


def run_group(wiki, raw_root, args, destination, ledger, entries, pages_by_rel, index, base, deadline):
    selected = {f"s{i}": entry for i, entry in enumerate(entries, 1)}
    failures = {}
    run_id = uuid.uuid4().hex
    output = None
    try:
        output = produce_proposal(wiki, raw_root, args, destination, selected, pages_by_rel, index, failures, run_id, base, deadline)
    except RUN_ERRORS as exc:
        if ledger is None:
            raise
        message = "Claude Code の待ち時間の上限を超えました。" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        for name in selected:
            failures.setdefault(name, message)
    if ledger is not None:
        ledger.save(selected, failures, run_id)
    return output, {selected[name][0].relative: message for name, message in failures.items()}


def produce_proposal(wiki, raw_root, args, destination, selected, pages_by_rel, index, failures, run_id, base, deadline):
    sessions, candidates = {}, {}
    for name, entry in selected.items():
        source, meta, _ = entry
        targets = candidates_for([entry], index)
        if not targets:
            if args.scheduled:
                failures[name] = "根拠と照合する関連記事がありません。"
            continue
        try:
            related = {}
            for relative in targets:
                page = candidates.get(relative) or read_snapshot(pages_by_rel[relative], wiki)
                editable_lines(page)
                related[relative] = page
            raw = session_input(source, meta, wiki, raw_root)
        except (OSError, ValueError) as exc:
            if not args.scheduled:
                raise
            failures[name] = str(exc)
            continue
        sessions[name] = (source, raw)
        candidates.update(related)
    if not sessions:
        if not args.scheduled:
            print("会話に関連付けられた既存記事がありません。")
            return None
        return save_proposal(destination, run_id, wiki, selected, {}, {}, failures, "", [], [], "", sessions, base)
    targets = sorted(candidates)[:PAGE_LIMIT]
    pages = {f"p{i}": candidates[relative] for i, relative in enumerate(targets, 1)}
    manifest = {
        "run": run_id,
        "sources": [],
        "pages": [{"id": name, "path": page.relative} for name, page in pages.items()],
    }
    checks = {}
    with tempfile.TemporaryDirectory(prefix="wiki-dream-") as directory:
        staging = Path(directory)
        for name, (_, raw) in sessions.items():
            write_numbered(staging / f"{name}.txt", raw)
            chunks, checks[name] = write_chunks(staging, name, raw)
            manifest["sources"].append({"id": name, "chunks": chunks})
        for name, page in pages.items():
            write_numbered(staging / f"{name}.txt", page, editable_lines(page))
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("claude", args.timeout)
        result, models = claude_workflow(staging, manifest, remaining)

    for name in result["incomplete"]:
        failures[name] = "Haiku の抽出が完了しませんでした。"
    for reading in result["findings"]:
        name = reading["source"]
        if reading.get("checks") != checks[name]:
            failures[name] = "Haiku が全分割ファイルを読んだことを確認できませんでした。"
        elif args.scheduled and not all(grounded(finding, sessions[name][1].lines) for finding in reading["findings"]):
            failures[name] = "Haiku の引用を原本と照合できませんでした。"
    if failures and not args.scheduled:
        raise ValueError("Haiku による全件の抽出が完了しませんでした。")
    completed = {name: pair for name, pair in sessions.items() if name not in failures}
    for page in (*pages.values(), *(snapshot for pair in completed.values() for snapshot in pair)):
        page.verify()
    diff, report, skipped = compose(result, completed, pages, index, failures, base=base) if completed else ("", [], [])
    notes = result["conclusion"]["notes"].strip() if completed else ""
    return save_proposal(destination, run_id, wiki, selected, pages, models, failures, diff, report, skipped, notes, sessions, base)


def save_proposal(destination, run_id, wiki, selected, pages, models, failures, diff, report, skipped, notes, sessions, base):
    lines = ["# Wiki Dreaming の提案", "", f"対象の wiki `{wiki}`"]
    if diff:
        lines.extend(["", f"差分のパスの基準ディレクトリ `{base}`"])
    lines.extend(["", f"対象の会話 {len(selected)} 件、既存記事 {len(pages)} 件。",
                  f"差分 {len(report)} 件、保留 {len(skipped)} 件。", "", *(report or ["変更候補なし。"])])
    lines.extend(f"- {note}" for note in skipped)
    if failures:
        lines.extend(["", "## 次回に回す会話", ""])
        lines.extend(f"- {selected[name][0].relative} — {message}" for name, message in failures.items())
    if notes:
        lines.extend(["", "## 照合で残った点", "", *(f"> {line}" for line in notes.splitlines())])
    receipt = {
        "models": models,
        "inputs": [
            {"source": entry[0].relative, "sha256": source_key(entry)[1],
             "state": "retry" if name in failures else "done",
             **({"raw": sessions[name][1].relative} if name in sessions else {})}
            for name, entry in selected.items()
        ],
        "pages": {page.relative: page.digest for page in pages.values()},
        "edits": len(report),
        "skipped": len(skipped),
    }
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / run_id
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=destination) as pending:
        draft = Path(pending) / run_id
        draft.mkdir()
        (draft / "proposal.diff").write_bytes(diff.encode("utf-8"))
        (draft / "proposal.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (draft / "run.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        draft.rename(output)
    return output


def print_results(results):
    if not results:
        print("今回処理する会話はありません。")
    failed = False
    for output, failures in results:
        if output:
            print(output)
        for source, message in failures.items():
            print(f"{source} は次回に回します。{message}", file=sys.stderr)
            failed = True
    return int(failed)


def propose(wiki, raw_root, args, *, workspace=None):
    wiki = absolute_user_path(require(wiki)).resolve()
    base = workspace if workspace is not None and wiki.is_relative_to(workspace) else wiki
    destination = args.out.resolve()
    protected_roots = (wiki, raw_root) if workspace is None else (wiki, raw_root, workspace / "wiki", workspace / "raw")
    for protected in protected_roots:
        if destination.is_relative_to(protected.resolve()):
            raise ValueError("出力先は wiki と raw の外に指定してください。")
    # 提案はライブ wiki の写しから作る。
    view = subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "wiki_apply.py"), "view", "--wiki", str(wiki)],
                          check=True, capture_output=True, encoding="utf-8")
    if json.loads(view.stdout)["release_id"] is not None:
        raise ValueError("Dreaming はライブ wiki を対象にします。")
    if not args.scheduled:
        return print_results(run_batch(wiki, raw_root, args, destination, None, base))

    destination.mkdir(parents=True, exist_ok=True)
    guard = WriteGuard(destination)
    ledger_path = destination / "ledger.sqlite"
    try:
        # 実行全体を排他し、台帳は提案を保存したグループごとに確定する。
        with guard.hold_lock(destination / "dream.lock"):
            guard.file_info(ledger_path)
            with closing(sqlite3.connect(ledger_path, timeout=0)) as db:
                # 更新前から動いている試作ジョブは SQLite の書き込みロックを持つ。
                try:
                    db.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                        raise
                    print("同じ台帳を使う回が実行中です。")
                    return 0
                with db:
                    ledger = Ledger(db, wiki, raw_root)
                db.execute("PRAGMA busy_timeout=5000")
                results = run_batch(wiki, raw_root, args, destination, ledger, base)
    except Refused as exc:
        if exc.kind != "busy":
            raise
        print("同じ台帳を使う回が実行中です。")
        return 0
    return print_results(results)


def main(argv=None, *, workspace=None):
    parser = argparse.ArgumentParser(description=__doc__)
    defaults = {} if workspace is None else {
        "wiki": workspace / "wiki",
        "raw_root": workspace / "raw",
        "out": workspace / "work" / "wiki-dream",
    }
    parser.add_argument("--wiki", type=Path, required=workspace is None, default=defaults.get("wiki"),
                        help="対象 wiki のルート")
    parser.add_argument("--raw-root", type=Path, required=workspace is None, default=defaults.get("raw_root"),
                        help="取り込み済みの原本のルート")
    parser.add_argument("--source", action="append", default=[], metavar="SLUG", help="会話のソースをスラッグで選ぶ")
    parser.add_argument("--limit", type=int, choices=range(1, 6), default=2, help="一回に読む会話の上限")
    parser.add_argument("--timeout", type=int, default=600, help="今回のワークフロー全体の待ち時間の上限（秒）")
    parser.add_argument("--out", type=Path, required=workspace is None, default=defaults.get("out"),
                        help="提案の出力先")
    parser.add_argument("--scheduled", action="store_true", help="出力先の台帳に記録し、処理済みの会話を除く")
    localize_argparse(parser)
    args = parser.parse_args(argv)
    if len(set(args.source)) > args.limit or args.timeout < 1:
        parser.error("対象件数と待ち時間を確認してください。")
    try:
        return propose(args.wiki.absolute(), args.raw_root.absolute(), args, workspace=workspace)
    except (*RUN_ERRORS, sqlite3.Error) as exc:
        print(f"提案を保存できませんでした。{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
