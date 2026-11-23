"""承認した下書き、索引とログの操作をライブ wiki に適用する。"""

from __future__ import annotations

import argparse
import base64
from contextlib import nullcontext
from copy import deepcopy
import difflib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import unicodedata
import uuid
from datetime import date

from wiki_apply import (AREA, PENDING, LIVE_JOURNAL as JOURNAL, LIVE_OWNER as OWNER, LIVE_TRANSACTION as TRANSACTION,
                        PLAN_ID, RETURN_JOURNAL, Conflict, PublicationError, archived_root,
                        checked, digest, encoded, mode_marker, publication_evidence, read_publication_journal, relative, sync_directory,
                        verified_active)
from wiki_fsguard import Refused, WriteGuard, _mount_points, _reserved_windows_name, external_raw_guard
from wiki_initialized import require
from wiki_lint import parse_frontmatter
from wiki_mcp_common import data_dir
from wiki_pages import (absolute_user_path, clean_link_target, is_page_path, link_kind,
                        new_parser, ref_values, walk_pages)
from wiki_raw_roots import read_raw_roots, record_raw_root

WAIT_SECONDS = 180
DELAYS = (0.01, 0.05, 0.1)
LIVE_HEAD = "live-head.json"
OPS = ("ingest", "query", "learn", "prepare")
PLACEHOLDERS = {
    "（ソースを取り込むと追加される）", "（エンティティのページを作ると追加される）",
    "（コンセプトのページを作ると追加される）", "（クエリの回答を保存すると追加される）",
}
COMMANDS = {"begin", "new", "edit", "index-add", "log-entry", "log-sub", "depends-on", "diff", "rebase",
            "unlock", "pending", "retry", "discard-save", "discard-live-pending"}


class Stuck(Conflict):
    """日誌に残ったファイルの状態を人が確かめる必要がある。"""


def write(guard, path, content, *, replace=False, temporary=None, before_publish=None):
    if path.parent != guard.root:
        missing = []
        parent = path.parent
        while parent != guard.root and guard.directory_info(parent) is None:
            missing.append(parent)
            parent = parent.parent
        guard.ensure_directory(path.parent)
        for folder in reversed(missing):
            sync_directory(folder.parent)
    guard.write_bytes(path, content, replace=replace, delays=DELAYS, mode=0o600,
                      temporary=temporary, before_publish=before_publish)
    sync_directory(path.parent)


def remove(guard, path):
    guard.remove_file(path)
    sync_directory(path.parent)


def load(guard, path):
    value = guard.read_bytes(path)
    return json.loads(value) if value is not None else None


def read_journal(guard, area):
    try:
        journal = load(guard, area / JOURNAL)
        if journal is not None and (not isinstance(journal, dict) or
                not {"wiki_identity", "raw_root", "result", "changes"} <= journal.keys()):
            raise ValueError("日誌の項目が足りません")
        return journal
    except (OSError, ValueError) as error:
        raise Stuck(f"中断した日誌を読めません: {error}") from error


def portable_path(name):
    path = relative(name)
    if (unicodedata.normalize("NFC", name) != name or any(
            part.startswith(".") or _reserved_windows_name(part) or re.search(r"~\d", part) or
            len(part.encode("utf-8")) > 200 or len(part.encode("utf-16-le")) > 400 or
            any(ord(ch) < 32 or ch in '<>"|?*' for ch in part) for part in path.parts)):
        raise PublicationError(f"正規の相対パスを指定してください: {name}")
    return path


def page_path(name):
    path = portable_path(name)
    if (not is_page_path(name) or path.name.casefold() == "schema.md" or
            any(p.casefold().endswith(".md") for p in path.parts[:-1]) or
            path.parts[0].casefold() in
            {"indexes", "graph", "assets", "ontology", "index.md", "log.md", "schema.md", "readme.md"}):
        raise PublicationError(f"下書きに指定できないページです: {name}")
    return path


def index_path(name):
    path = portable_path(name)
    if name == "index.md":
        return path
    if (path.parts[0] != "indexes" or len(path.parts) < 2 or path.suffix.casefold() != ".md" or
            path.name.casefold() == "schema.md" or any(p.casefold().endswith(".md") for p in path.parts[:-1])):
        raise PublicationError(f"索引のパスが不正です: {name}")
    return path


def overlapping(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def folded_parts(path):
    return tuple(unicodedata.normalize("NFC", part.casefold()) for part in path.parts)


def overlapping_names(a, b):
    left, right = folded_parts(a), folded_parts(b)
    size = min(len(left), len(right))
    return left[:size] == right[:size]


def work_base():
    return absolute_user_path(data_dir() / "apply-work")


def wiki_guard(wiki):
    guard = WriteGuard(wiki, strict_root=True)
    if archived_root(guard.root):
        raise PublicationError("保存済みの公開版に書き込めません")
    require(guard.root)
    guard.file_info(guard.root / "SCHEMA.md")
    return guard


def directory_tree(root, *, reject_mounts=False):
    """リンクをたどらず、木に含まれる実体を列挙する。読めない枝があれば書く前に止める。"""
    identities = {}
    pending = [root]
    device = root.stat().st_dev
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or link_kind(info) or
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                continue
            identity = (info.st_dev, info.st_ino)
            if reject_mounts and info.st_dev != device:
                raise PublicationError(f"書き込み領域にマウントがあります: {path}")
            if identity not in identities:
                identities[identity] = path
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
        except FileNotFoundError:
            if path == root:
                raise
    return identities


def protect_roots(guard, raw_root, additional=(), *, allow_local=True):
    roots, failures = read_raw_roots(guard.root)
    if failures:
        raise PublicationError("wiki が使った原本の根を確認できません")
    local = guard.root / "raw"
    if os.path.lexists(local):
        if not allow_local:
            raise PublicationError("wiki 内に raw がある配置は公開の書く段に渡せません")
        guard.directory_info(local)
        roots.append(local)
    work = work_base()
    data = absolute_user_path(data_dir())
    if overlapping(work, guard.root) or overlapping(data.resolve(), guard.root):
        raise PublicationError("作業用ディレクトリと wiki が重なっています")
    writer_ids = {}
    for root in (guard.root, work):
        if os.path.lexists(root):
            anchor = WriteGuard(root, strict_root=True)
            if _mount_points(root, required=True):
                raise PublicationError(f"書き込み領域にマウントがあります: {root}")
            found = directory_tree(anchor.root, reject_mounts=True)
        else:
            anchor = next(path for path in root.parents if os.path.lexists(path))
            identity = WriteGuard(anchor, strict_root=True).root_id
            found = {identity: anchor}
        if writer_ids.keys() & found.keys():
            raise PublicationError("作業用ディレクトリと wiki が同じ実体を参照しています")
        writer_ids.update(found)
    explicit = Path(raw_root)
    if allow_local and explicit.resolve().is_relative_to(local):
        explicit = WriteGuard(explicit, strict_root=True).root
    else:
        explicit = external_raw_guard(guard.root, explicit).root
    for root in dict.fromkeys([*roots, *additional, explicit]):
        raw = root.resolve()
        if overlapping(raw, data.resolve()):
            raise PublicationError(f"作業用ディレクトリまたは wiki と raw が重なっています: {root}")
        local_raw = allow_local and raw.is_relative_to(local)
        if overlapping(raw, guard.root) and not local_raw:
            raise PublicationError(f"作業用ディレクトリまたは wiki と raw が重なっています: {root}")
        if raw.is_dir():
            raw_ids = directory_tree(raw)
            for identity in writer_ids.keys() & raw_ids.keys():
                if not local_raw or writer_ids[identity] != raw_ids[identity]:
                    raise PublicationError(f"書き込み領域が raw の実体と重なっています: {writer_ids[identity]} / {raw_ids[identity]}")


def check_raw_refs(guard, contents, *, is_draft=False, targets=(), allow_local=True):
    """版の比較に原本の本文を読み込まず、既存と提案後の原本の行き先を守る。"""
    wiki = guard.root
    external = set()
    targets = (guard.root / AREA.parent, *targets)
    for path, data in contents:
        if data is None:
            continue
        meta, _, _ = parse_frontmatter(data.decode("utf-8-sig"))
        for ref in ref_values(meta.get("raw")):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref) and not re.match(r"^[a-zA-Z]:", ref):
                continue
            if os.name != "nt" and (re.match(r"^[a-zA-Z]:", ref) or ref.startswith("\\")):
                continue
            raw = Path(ref).expanduser()
            bases = (wiki.parent,) if meta.get("session_id") else (wiki.parent, wiki, path.parent)
            for base in bases:
                candidate = Path(os.path.abspath(base / raw))
                resolved = candidate.resolve()
                if overlapping(resolved, absolute_user_path(data_dir()).resolve()):
                    raise PublicationError(f"ページが作業用領域を原本として参照しています: {path}")
                protected_local = allow_local and resolved.is_relative_to(wiki / "raw")
                if resolved.is_relative_to(wiki) and (
                        is_draft and not protected_local and (raw.is_absolute() or os.path.lexists(candidate) or resolved in targets)
                        or any(overlapping_names(resolved, target) for target in targets)):
                    raise PublicationError(f"ページが wiki または作業用領域を原本として参照しています: {path}")
                if not resolved.is_relative_to(wiki):
                    try:
                        info = resolved.stat()
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    # PDF など外部の単独ファイルは、そのファイルだけを守る。置換先の
                    # ハードリンクは WriteGuard が拒む。ディレクトリの原本は木全体を守る。
                    if stat.S_ISDIR(info.st_mode):
                        external.add(resolved)
    return external


def restrict_raw(guard, raw_root, candidate_pages=(), targets=(), *, view=None):
    source = WriteGuard(view.root) if view else guard
    found = walk_pages(source.root)
    if found.unknown or found.unlistable:
        raise PublicationError("既存のページが使う原本を確認できません")
    allow_local = view is None
    roots = check_raw_refs(guard, ((guard.root / p.rel, source.read_bytes(source.root / p.rel)) for p in found.files),
                           targets=targets, allow_local=allow_local)
    roots.update(check_raw_refs(guard, candidate_pages, is_draft=True, targets=targets, allow_local=allow_local))
    protect_roots(guard, raw_root, roots, allow_local=allow_local)


def live_mode(guard):
    from wiki_restore import check as check_restore

    area = guard.root / AREA
    check_restore(guard, area)
    if mode_marker(guard)[0] == "live":
        if guard.file_info(area / RETURN_JOURNAL) is not None:
            raise Conflict("ライブへの切り戻しが中断しています。recover で完了させてください")
        if read_publication_journal(guard, area) is not None:
            raise Conflict("ライブからの再公開が中断しています。recover で完了させてください")
        verified_active(guard, area)
        return
    if publication_evidence(guard, area):
        raise Conflict("公開版を使う wiki は公開用の計画から更新してください")


def work_directory(path):
    path = absolute_user_path(path)
    checked(path.name, PLAN_ID)
    if path.parent != work_base():
        raise PublicationError("begin が返した作業用ディレクトリを指定してください")
    return WriteGuard(path, strict_root=True)


def read_plan(work):
    plan = load(work, work.root / "plan.json")
    if (not isinstance(plan, dict) or plan.get("format") != 1 or plan.get("plan_id") != work.root.name
            or not isinstance(plan.get("wiki"), str) or not isinstance(plan.get("wiki_identity"), list)
            or not isinstance(plan.get("raw_root"), str) or not isinstance(plan.get("pages"), dict)
            or not isinstance(plan.get("operations"), list)):
        raise PublicationError("ライブ書き込み計画がありません")
    return plan


def plan_wiki(plan):
    guard = wiki_guard(plan["wiki"])
    if list(guard.root_id) != plan["wiki_identity"]:
        raise Conflict("試行を始めた wiki の実体が変わりました")
    protect_roots(guard, plan["raw_root"], allow_local="publication" not in plan)
    return guard


def begin(wiki, raw_root, name, *, release_id=None, allow_publication=False):
    guard = wiki_guard(wiki)
    raw_root = absolute_user_path(raw_root)
    release = None
    if allow_publication:
        from wiki_apply import check_publication_writer, resolve_publication
        try:
            release = resolve_publication(guard.root, release_id)
        except PublicationError as error:
            raise PublicationError(f"公開版を確認できません: {error}") from error
        if release is not None:
            check_publication_writer(guard, guard.root / AREA)
        else:
            live_mode(guard)
    else:
        live_mode(guard)
    restrict_raw(guard, raw_root, view=release)
    parent = work_base()
    existing = next(path for path in (parent, *parent.parents) if os.path.lexists(path))
    anchor = WriteGuard(existing, strict_root=True)
    directory = parent / uuid.uuid4().hex
    anchor.ensure_directory(directory)
    plan = {"format": 1, "plan_id": directory.name, "name": one_line(name),
            "wiki": str(guard.root), "wiki_identity": list(guard.root_id), "raw_root": str(raw_root),
            "pages": {}, "operations": []}
    if release:
        plan["publication"] = {"release_id": release.release_id, "definition_id": release.definition_id}
        plan["dependencies"] = []
    work = WriteGuard(directory, strict_root=True)
    write(work, directory / "plan.json", encoded(plan))
    return {"apply_dir": str(directory), "plan_id": directory.name,
            **({"release_id": release.release_id, "read_root": str(release.root)} if release else {})}


def one_line(value):
    if (not isinstance(value, str) or not value.strip() or len(value.splitlines()) != 1 or
            value.splitlines()[0] != value or any(ord(ch) < 32 for ch in value)):
        raise PublicationError("空でない 1 行の文字列を指定してください")
    return value


def canonical_target(guard, relative_path):
    """既存の名前と大小文字や Unicode の表記が異なる場合も競合にする。"""
    current_path = guard.root
    for component in relative_path.parts:
        key = unicodedata.normalize("NFC", component.casefold())
        current_path = current_path / component
        if current_path.parent != guard.root and guard.directory_info(current_path.parent) is None:
            return guard.root / relative_path
        with os.scandir(current_path.parent) as entries:
            for entry in entries:
                existing = unicodedata.normalize("NFC", entry.name.casefold())
                if existing == "schema.md" and current_path.parent != guard.root:
                    raise Conflict(f"別の wiki に属するパスです: {current_path}")
                if entry.name != component and existing == key:
                    raise Conflict(f"表記が重なる名前があります: {entry.path} / {current_path}")
    return current_path


def register(work, plan, guard, command, name):
    path = page_path(name)
    source = guard
    if "publication" in plan:
        from wiki_public_save import base_view
        if command == "rebase":
            raise Conflict("公開版の依存先を読み直して begin から新しい計画を用意してください")
        source = WriteGuard(base_view(guard, plan).root, strict_root=True)
    else:
        live_mode(guard)
    restrict_raw(guard, plan["raw_root"], targets=(guard.root / path,))
    original = source.read_bytes(canonical_target(source, path))
    existing = plan["pages"].get(name)
    draft = work.root / ("new" if command == "new" else "edit") / path
    if command != "rebase" and existing:
        if existing["kind"] == command:
            return {"path": str(draft)}
        raise Conflict(f"別の操作で登録済みです: {name}")
    if command == "new":
        if original is not None:
            raise Conflict(f"新規ページの名前が使用済みです: {name}")
        write(work, draft, b"")
    else:
        if original is None:
            raise Conflict(f"更新するページがありません: {name}")
        base = work.root / "base" / path
        if command == "rebase":
            if not existing or existing["kind"] != "edit":
                raise Conflict("edit で登録したページを指定してください")
            # 前の下書きを保存し、現在の版に変更を手で当て直せるようにする。
            proposal = work.read_bytes(draft)
            if proposal is None:
                raise PublicationError("下書きがありません")
            previous = work.root / "previous" / uuid.uuid4().hex / path
            write(work, previous, proposal)
        write(work, base, original, replace=command == "rebase")
        write(work, draft, original, replace=command == "rebase")
    plan["pages"][name] = {"kind": "new" if command == "new" else "edit",
                           "before": None if original is None else digest(original)}
    result = {"path": str(draft)}
    if command == "rebase":
        result["previous"] = str(previous)
    return result


def snapshot(work, plan):
    drafts = {}
    fingerprints = {}
    paths = {}
    for name, page in plan["pages"].items():
        path = page_path(name)
        key = folded_parts(path)
        if key in paths:
            raise PublicationError(f"同じ行き先が複数あります: {name}, {paths[key]}")
        paths[key] = name
        kind = page["kind"]
        if kind not in {"edit", "new"}:
            raise PublicationError("下書きの操作が不正です")
        after = work.read_bytes(work.root / kind / path)
        before = work.read_bytes(work.root / "base" / path) if kind == "edit" else None
        if (after is None or not after.strip() or
                (digest(before) if before is not None else None) != page["before"] or
                (kind == "edit") != (before is not None)):
            raise PublicationError(f"下書きまたは基準版がありません、あるいは内容が変わりました: {name}")
        after.decode("utf-8-sig")
        drafts[name] = (before, after)
        fingerprints[name] = digest(after)
    return drafts, digest(encoded({"plan": plan, "drafts": fingerprints}))


def document(data):
    text = data.decode("utf-8-sig")
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text)
    offset = 0
    if lines and lines[0].strip() == "---":
        offset = next((i + 1 for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
        if offset == 0:
            raise PublicationError("索引またはログの frontmatter が閉じていません")
    newline = re.search(r"\r\n|\r|\n", text)
    return (lines[:offset], lines[offset:], newline[0] if newline else "\n",
            b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"")


def parse(lines, references=None):
    env = {} if references is None else {"references": deepcopy(references)}
    tokens = new_parser().parse("".join(lines), env)
    return tokens, env.get("references", {})


def token_value(token):
    return (token.type, token.tag, token.nesting, token.level, token.hidden, token.content, token.attrs,
            [token_value(child) for child in token.children or []])


def headings(tokens, lines):
    return [(i, token.map[0], token.tag, lines[token.map[0]].strip())
            for i, token in enumerate(tokens)
            if token.type == "heading_open" and token.level == 0 and token.markup.startswith("#")]


def section(tokens, lines, heading, nth):
    all_headings = headings(tokens, lines)
    matches = [i for i, h in enumerate(all_headings) if h[3] == heading]
    if not matches or (nth is None and len(matches) != 1) or (nth is not None and not 1 <= nth <= len(matches)):
        raise Conflict(f"見出しを 1 つに決められません: {heading}")
    index = matches[(nth or 1) - 1]
    start, _, tag, _ = all_headings[index]
    end, line, _, _ = next((h for h in all_headings[index + 1:] if h[2] <= tag),
                            (len(tokens), len(lines), "", ""))
    return start + 3, end, line


def insert(lines, at, additions, newline, *, blank=False):
    prefix = lines[:at]
    if prefix and not prefix[-1].endswith(("\r", "\n")):
        prefix[-1] += newline
    if blank and prefix and prefix[-1].strip():
        prefix.append(newline)
    suffix = lines[at:]
    return prefix + [line + newline for line in additions] + suffix


def index_add(lines, tokens, op, newline, references):
    line = one_line(op["line"])
    if not re.fullmatch(r"- \[\[[^\[\]]+\]\]: .+", line):
        raise PublicationError("索引の行は - [[ページ]]: 要約 の形で指定してください")
    fragment, _ = parse([line + newline], references)
    if [t.type for t in fragment] != ["bullet_list_open", "list_item_open", "paragraph_open",
                                     "inline", "paragraph_close", "list_item_close", "bullet_list_close"]:
        raise PublicationError("索引の行を 1 件の箇条書きとして読めません")
    start, end, at = section(tokens, lines, op["section"], op["nth"]) if op["section"] else (0, len(tokens), len(lines))
    page = clean_link_target(line.split("]]", 1)[0][4:]).casefold()
    if not page:
        raise PublicationError("索引の項目にページ名を指定してください")
    for i, token in enumerate(tokens):
        match = re.match(r"\[\[([^\[\]]+)\]\]: ", token.content) if (
            token.type == "inline" and i >= 2 and tokens[i - 2].type == "list_item_open") else None
        if match and clean_link_target(match[1]).casefold() == page:
            raise Conflict(f"索引に同じページがすでにあります: {page}")
    if end > start and tokens[end - 1].type == "bullet_list_close":
        opening = next(t for t in reversed(tokens[start:end]) if t.type == "bullet_list_open" and t.level == 0)
        if opening.markup == "-":
            at = opening.map[1]
            while at > 0 and not lines[at - 1].strip():
                at -= 1
            for token in fragment:
                if token.type in {"paragraph_open", "paragraph_close"}:
                    token.hidden = next(t.hidden for t in tokens[start:end] if t.type == token.type and t.level == 2)
            return insert(lines, at, [line], newline), tokens[:end - 1] + fragment[1:-1] + tokens[end - 1:]
    if (end >= start + 3 and tokens[end - 3].type == "paragraph_open" and tokens[end - 3].level == 0
            and tokens[end - 2].type == "inline" and tokens[end - 2].content in PLACEHOLDERS
            and tokens[end - 1].type == "paragraph_close"):
        at = tokens[end - 3].map[0]
        return insert(lines[:at] + lines[at + 1:], at, [line], newline), tokens[:end - 3] + fragment + tokens[end:]
    return insert(lines, at, [line], newline, blank=True), tokens[:end] + fragment + tokens[end:]


def log_change(lines, tokens, op, newline, references):
    heading = one_line(op["heading"])
    if not re.fullmatch(r"## \[\d{4}-\d{2}-\d{2}\] (?:ingest|query|learn|prepare) \| .+", heading):
        raise PublicationError("ログの見出しが不正です")
    sublines = ["   " + one_line(line).strip() for line in op["lines"]]
    fragment, _ = parse([s + newline for s in [heading, *sublines]], references)
    expected_shape = ["heading_open", "inline", "heading_close"]
    if sublines:
        expected_shape.extend(("paragraph_open", "inline", "paragraph_close"))
    if [t.type for t in fragment] != expected_shape:
        raise PublicationError("ログのサブ行は段落として読める形で指定してください")
    if op["kind"] == "log-entry":
        nth = 1 + sum(h[3] == heading for h in headings(tokens, lines))
        return (insert(lines, len(lines), [heading, *sublines], newline, blank=True),
                tokens + fragment, {"heading": heading, "nth": nth})
    start, end, at = section(tokens, lines, heading, op["nth"])
    if end > start and tokens[end - 1].type == "paragraph_close":
        before_inline = tokens[end - 2]
        at = before_inline.map[1]
        joined, _ = parse([before_inline.content + newline, *[s + newline for s in sublines]], references)
        if len(joined) != 3 or joined[1].type != "inline":
            raise PublicationError("ログの段落にサブ行を追記できません")
        expected = tokens[:end - 2] + [joined[1]] + tokens[end - 1:]
        return insert(lines, at, sublines, newline), expected, None
    return insert(lines, at, sublines, newline, blank=True), tokens[:end] + fragment[3:] + tokens[end:], None


def changed_document(data, op):
    prefix, lines, newline, bom = document(data)
    tokens, references = parse(lines)
    receipt = None
    if op["kind"] == "index-add":
        output, expected = index_add(lines, tokens, op, newline, references)
    elif op["kind"] in {"log-entry", "log-sub"}:
        output, expected, receipt = log_change(lines, tokens, op, newline, references)
    else:
        raise PublicationError("索引、ログの操作が不正です")
    actual, new_references = parse(output)
    if ([token_value(t) for t in expected] != [token_value(t) for t in actual] or
            reference_values(new_references) != reference_values(references)):
        raise Conflict("追記によって他の Markdown の解釈が変わるため適用できません")
    return bom + "".join(prefix + output).encode("utf-8"), receipt


def reference_values(references):
    return {name: (ref["href"], ref["title"]) for name, ref in references.items()}


def calculate(guard, plan, drafts, *, apply=False):
    live_mode(guard)
    protect_drafts(guard, plan, drafts)
    return calculate_view(guard, plan, drafts, apply=apply)


def protect_drafts(guard, plan, drafts, *, view=None):
    targets = {guard.root / path for path in drafts}
    targets.update(guard.root / (index_path(op["file"]) if op["kind"] == "index-add" else "log.md")
                   for op in plan["operations"])
    restrict_raw(guard, plan["raw_root"], ((guard.root / path, after) for path, (_, after) in drafts.items()),
                 targets, view=view)


def calculate_view(guard, plan, drafts, *, apply=False):
    from wiki_apply import require_semantic_format1
    require_semantic_format1(guard)
    changes = dict(drafts)
    log_entries = []
    for name, (before, _) in drafts.items():
        current_page = guard.read_bytes(canonical_target(guard, page_path(name)))
        if apply and current_page != before:
            raise Conflict(f"ページの基準版が変わりました。rebase して差分を確認してください: {name}")
    for op in plan["operations"]:
        path = index_path(op["file"]) if op["kind"] == "index-add" else Path("log.md")
        name = path.as_posix()
        if name not in changes:
            content = guard.read_bytes(canonical_target(guard, path))
            if content is None:
                raise Conflict(f"索引またはログがありません: {name}")
            changes[name] = (content, content)
        before, previous = changes[name]
        after, entry = changed_document(previous, op)
        changes[name] = (before, after)
        if entry:
            log_entries.append(entry)
    return {name: pair for name, pair in changes.items() if pair[0] != pair[1]}, log_entries


def diff_text(changes):
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for name, (before, after) in changes.items()
                   for line in difflib.unified_diff((before or b"").decode("utf-8-sig").splitlines(keepends=True),
                                                    after.decode("utf-8-sig").splitlines(keepends=True),
                                                    name if before is not None else "/dev/null", name))


def pack(data):
    return None if data is None else base64.b64encode(data).decode("ascii")


def unpack(data):
    return None if data is None else base64.b64decode(data, validate=True)


def transaction_changes(guard, journal):
    changes = []
    names = set()
    for entry in journal["changes"]:
        name = entry["path"]
        path = Path("log.md") if name == "log.md" else (
            index_path(name) if name == "index.md" or name.startswith("indexes/") else page_path(name))
        before, after = (unpack(entry[key]) for key in ("before", "after"))
        canonical = name.casefold()
        if (canonical in names or before == after or after is None or
                (digest(before) if before is not None else None) != entry["before_sha256"] or
                digest(after) != entry["after_sha256"]):
            raise PublicationError(f"日誌の指紋が一致しません: {name}")
        names.add(canonical)
        changes.append((canonical_target(guard, path), before, after))
    return changes


def verify_applied(guard, checksums):
    for name, checksum in checksums.items():
        data = guard.read_bytes(guard.root / relative(name))
        if (digest(data) if data is not None else None) != (checked(checksum) if checksum is not None else None):
            raise Stuck(f"適用済みの記録と成果物が一致しません: {name}")


def ordered_receipts(guard, area):
    records = []
    for path, _ in guard.tree_entries(area / "live-receipts"):
        record = load(guard, path)
        if not isinstance(record, dict) or record.get("plan_id") != path.stem:
            raise Stuck("ライブの適用済み記録を照合できません")
        if "generation" in record:
            generation = record["generation"]
            files = record.get("after_sha256")
            if (type(generation) is not int or generation < 1 or not isinstance(files, dict) or not files
                    or not isinstance(record.get("files"), list) or set(files) != set(record["files"])):
                raise Stuck("ライブの適用済み記録の形式が不正です")
            records.append(record)
    records.sort(key=lambda record: record["generation"])
    if any(i != record["generation"] for i, record in enumerate(records, 1)):
        raise Stuck("ライブの適用済み記録の順序に欠損があります")
    return records


def live_receipts(guard, area):
    records = ordered_receipts(guard, area)
    if load(guard, area / LIVE_HEAD) != (records[-1] if records else None):
        raise Stuck("ライブの先頭記録と完了記録が一致しません")
    return records


def commit_live_head(guard, area, result):
    if "generation" not in result:
        return  # 成果物を日誌から照合できる旧形式の中断に限る。
    records = ordered_receipts(guard, area)
    previous = records[-2] if len(records) > 1 else None
    current_head = load(guard, area / LIVE_HEAD)
    if not records or records[-1] != result or current_head not in (previous, result):
        raise Stuck("ライブの日誌と先頭の完了記録が一致しません")
    if current_head != result:
        write(guard, area / LIVE_HEAD, encoded(result), replace=True)


def verify_live_retry(guard, area, result):
    if "after_sha256" not in result:
        raise Stuck("旧形式のライブ完了記録には成果物の指紋がありません")
    latest = dict(result["after_sha256"])
    for later in live_receipts(guard, area):
        if later["generation"] > result["generation"]:
            latest.update({name: checksum for name, checksum in later["after_sha256"].items() if name in latest})
    verify_applied(guard, latest)


def completed_live_retry(guard, area, prior, expected):
    from wiki_restore import check as check_restore, require_completion

    check_restore(guard, area)
    if prior["plan_sha256"] != expected:
        raise Conflict("別の承認済み差分がこの計画ですでに適用されています")
    verify_live_retry(guard, area, prior)
    require_completion(guard, area, "live", prior)
    return dict(prior, state="already_applied")


def verify_latest_live(guard, area):
    latest = {}
    for result in live_receipts(guard, area):
        latest.update(result["after_sha256"])
    verify_applied(guard, latest)


def clear_transaction(guard, area, *, committed=True):
    if committed:
        remove(guard, area / JOURNAL)
    if guard.file_info(area / TRANSACTION) is not None:
        remove(guard, area / TRANSACTION)
    if not committed:
        remove(guard, area / JOURNAL)


def finish_without_journal(guard, area):
    from wiki_restore import acknowledge

    marker = load(guard, area / TRANSACTION)
    if marker is None:
        return None
    result = load(guard, area / "live-receipts" / f"{checked(marker['plan_id'], PLAN_ID)}.json")
    if result != marker:
        raise Stuck("ライブ書き込みの記録は残っていますが日誌がありません。日誌を復元してください")
    verify_live_retry(guard, area, result)
    acknowledge(guard, area, ("live", result))
    return result


def rollback(guard, area, journal, changes):
    # 戻す段で止まっても、次の持ち主は戻す段を繰り返す。
    journal["phase"] = "rollback"
    write(guard, area / JOURNAL, encoded(journal), replace=True)
    blocked = []
    for path, before, after, temporary in reversed(changes):
        try:
            value = guard.read_bytes(path)
            if value == before:
                continue
            if value != after:
                blocked.append(str(path))
            elif before is None:
                remove(guard, path)
            else:
                write(guard, path, before, replace=True, temporary=temporary)
        except OSError:
            blocked.append(str(path))
    if blocked:
        raise Stuck("現在の内容を残して日誌を保留しました: " + "、".join(blocked))
    clear_transaction(guard, area, committed=False)
    return dict(journal["result"], state="aborted", reason=journal["reason"])


def finish(guard, area, journal, checkpoint):
    from wiki_restore import acknowledge

    if list(guard.root_id) != journal["wiki_identity"]:
        raise Conflict("日誌を作った wiki の実体が変わりました")
    try:
        changes = transaction_changes(guard, journal)
        plan_id = checked(journal["result"]["plan_id"], PLAN_ID)
        changes = [(path, before, after, path.with_name(f".wiki-live-{plan_id}-{i}.tmp"))
                   for i, (path, before, after) in enumerate(changes)]
        restrict_raw(guard, journal["raw_root"], ((path, data) for path, before, after, _ in changes
                     if is_page_path(path.relative_to(guard.root).as_posix()) for data in (before, after)),
                     (item for path, _, _, temporary in changes for item in (path, temporary)))
        for path, _, _, temporary in changes:
            guard.clear_exclusive_temp(temporary, path)
            if path.parent == guard.root or guard.directory_info(path.parent) is not None:
                sync_directory(path.parent)
    except (KeyError, TypeError, OSError, ValueError) as error:
        raise Stuck(f"日誌の入力または原本の保護を確かめられません: {error}") from error
    receipt = area / "live-receipts" / f"{plan_id}.json"
    prior = load(guard, receipt)
    result = journal["result"]
    marker = load(guard, area / TRANSACTION)
    if marker is not None and marker != result:
        raise Stuck("ライブ書き込みの記録と日誌が一致しません")
    if prior:
        if prior != result:
            raise PublicationError("適用結果と日誌が一致しません")
        verify_applied(guard, {path.relative_to(guard.root).as_posix(): digest(after)
                              for path, _, after, _ in changes})
        commit_live_head(guard, area, prior)
        acknowledge(guard, area, ("live", prior))
        clear_transaction(guard, area)
        return prior
    if marker is None:
        write(guard, area / TRANSACTION, encoded(result), replace=True)
    if journal.get("phase") == "rollback":
        return rollback(guard, area, journal, changes)
    try:
        for path, before, after, _ in changes:
            value = guard.read_bytes(path)
            if value != before and value != after:
                raise Conflict(f"日誌と現在の内容が食い違っています: {path}")
        for i, (path, before, after, temporary) in enumerate(changes):
            value = guard.read_bytes(path)
            if value == after:
                continue
            if value != before:
                raise Conflict(f"復旧中にファイルが変わりました: {path}")
            write(guard, path, after, replace=before is not None, temporary=temporary,
                  before_publish=lambda: checkpoint(f"before_write:{i}"))
            checkpoint(f"after_write:{i}")
    except (OSError, Conflict) as error:
        journal["reason"] = str(error)
        return rollback(guard, area, journal, changes)
    write(guard, receipt, encoded(journal["result"]), replace=True)
    commit_live_head(guard, area, result)
    checkpoint("after_receipt")
    acknowledge(guard, area, ("live", result))
    clear_transaction(guard, area)
    return journal["result"]


def acquire(guard, area, plan):
    live_mode(guard)
    if guard.file_info(area / OWNER) is not None:
        raise Conflict("中断した書き手の記録があります。unlock を実行してください")
    owner = {"lock_id": uuid.uuid4().hex, "plan_id": plan.get("plan_id", plan.get("result", {}).get("plan_id")),
             "pid": os.getpid(), "host": socket.gethostname(), "raw_root": plan["raw_root"],
             "started_at": time.time()}
    write(guard, area / OWNER, encoded(owner), replace=True)


def with_gate(guard, timeout):
    guard.ensure_directory(guard.root / AREA)
    return guard.hold_lock(guard.root / AREA / "apply.lock", timeout=timeout)


def preview(work, plan, guard):
    drafts, checksum = snapshot(work, plan)
    changes, _ = calculate(guard, plan, drafts)
    return {"plan_sha256": checksum, "diff": diff_text(changes)}


def pending_path(guard, plan_id, checksum):
    return guard.root / PENDING / f"{plan_id}-{checksum}.json"


def pending_discarded(guard, path):
    tombstone = load(guard, guard.root / ".wiki-cache/apply-pending-discarded" / path.name)
    if tombstone is None:
        return False
    plan_id, key = path.stem.split("-", 1)
    if tombstone != {"plan_id": plan_id, "plan_sha256": key, "wiki_identity": list(guard.root_id)}:
        raise Stuck("ライブの保留の取り消し記録が一致しません")
    return True


def pending_blockers(guard):
    return [path for path, info in guard.tree_entries(guard.root / PENDING)
            if not (stat.S_ISREG(info.st_mode) and re.fullmatch(r"\.wiki-write-[0-9a-f]{32}\.tmp", path.name))
            and (not re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{64}\.json", path.name)
                 or not pending_discarded(guard, path))]


def discard_live_pending(wiki, plan_id, expected, timeout):
    guard = wiki_guard(wiki)
    area = guard.root / AREA
    checked(plan_id, PLAN_ID)
    checked(expected)
    path = pending_path(guard, plan_id, expected)
    tombstone = guard.root / ".wiki-cache/apply-pending-discarded" / path.name
    with with_gate(guard, timeout), guard.hold_lock(area / "pending.lock", timeout=timeout):
        live_mode(guard)
        from wiki_apply import check_live_writer
        check_live_writer(guard, area)
        prior = load(guard, area / "live-receipts" / f"{plan_id}.json")
        if prior and prior["plan_sha256"] == expected:
            raise Conflict("適用済みのライブの差分は取り消せません")
        if not pending_discarded(guard, path):
            entry = load(guard, path)
            if (not entry or entry.get("plan_sha256") != expected
                    or entry.get("plan", {}).get("plan_id") != plan_id
                    or entry["plan"].get("wiki_identity") != list(guard.root_id)):
                raise Conflict("ライブに該当する承認済みの保留がありません")
            write(guard, tombstone, encoded({"plan_id": plan_id, "plan_sha256": expected,
                                              "wiki_identity": list(guard.root_id)}))
        if guard.file_info(path) is not None:
            remove(guard, path)
    return {"state": "discarded", "plan_id": plan_id, "plan_sha256": expected}


def save_pending(guard, plan, checksum, drafts, preview_diff):
    path = pending_path(guard, plan["plan_id"], checksum)
    # 同じ承認を待つプロセスが複数あっても、記録は 1 件だけ残す。
    restrict_raw(guard, plan["raw_root"], ((guard.root / name, pair[1]) for name, pair in drafts.items()))
    record = {"plan": plan, "plan_sha256": checksum, "diff": preview_diff,
              "apply_dir": str(work_base() / plan["plan_id"]), "saved_at": time.time()}
    with guard.hold_lock(guard.root / AREA / "pending.lock", timeout=WAIT_SECONDS):
        live_mode(guard)
        if pending_discarded(guard, path):
            raise Conflict("この承認済みのライブの保留は取り消されています")
        try:
            write(guard, path, encoded(record))
        except FileExistsError:
            pass
    pending = guard.tree_entries(path.parent)
    if sum(file.suffix == ".json" for file, _ in pending) >= 5:
        print("pending_alert: 保留の差分が 5 件以上あります", file=sys.stderr)


def apply(directory, expected, *, checkpoint=lambda _: None, timeout=WAIT_SECONDS):
    checked(expected)
    work = work_directory(directory)
    plan = read_plan(work)
    guard = plan_wiki(plan)
    area = guard.root / AREA
    receipt = area / "live-receipts" / f"{plan['plan_id']}.json"
    path = pending_path(guard, plan["plan_id"], expected)
    existing_receipt = load(guard, receipt)
    drafts = None
    if existing_receipt is None:
        drafts, checksum = snapshot(work, plan)
        if checksum != expected:
            raise Conflict("承認した計画の指紋が一致しません")
        restrict_raw(guard, plan["raw_root"], ((guard.root / name, pair[1]) for name, pair in drafts.items()))
        # 待ち始めたときの下書きのバイト列を固定する。索引とログの操作はロックの中で最新に当てる。
        preview_diff = diff_text(drafts)
    deadline = time.monotonic() + timeout
    while True:
        try:
            with with_gate(guard, max(0, deadline - time.monotonic())):
                if pending_discarded(guard, path):
                    raise Conflict("この承認済みのライブの保留は取り消されています")
                prior = load(guard, receipt)
                if prior and mode_marker(guard)[0] == "publication":
                    verified_active(guard, area)
                    return completed_live_retry(guard, area, prior, expected)
                live_mode(guard)
                if guard.file_info(area / OWNER) is None:
                    existing = read_journal(guard, area)
                    if existing:
                        acquire(guard, area, existing)
                        try:
                            restored = finish(guard, area, existing, lambda _: None)
                        finally:
                            remove(guard, area / OWNER)
                        if restored["plan_id"] == plan["plan_id"] and restored["state"] == "applied":
                            if restored["plan_sha256"] != expected:
                                raise Conflict("別の承認済み差分がこの計画ですでに適用されています")
                            if guard.file_info(path):
                                remove(guard, path)
                            return dict(restored, state="already_applied")
                    elif finish_without_journal(guard, area):
                        remove(guard, area / TRANSACTION)
                    prior = load(guard, receipt)
                    if prior:
                        result = completed_live_retry(guard, area, prior, expected)
                        if guard.file_info(path):
                            remove(guard, path)
                        return result
                    if drafts is None:
                        raise Stuck("確認したライブの完了記録が失われました")
                    generation = len(live_receipts(guard, area)) + 1
                    changes, entries = calculate(guard, plan, drafts, apply=True)
                    if not changes:
                        raise Conflict("適用する差分がありません")
                    result = {"state": "applied", "plan_id": plan["plan_id"], "plan_sha256": expected,
                              "files": list(changes), "log_entries": entries,
                              "generation": generation,
                              "after_sha256": {name: digest(after) for name, (_, after) in changes.items()}}
                    mode, proof = mode_marker(guard)
                    if mode == "live":
                        result["return_sha256"] = proof
                        result["created_files"] = [name for name, (before, _) in changes.items() if before is None]
                        result["before_sha256"] = {
                            name: digest(before) if before is not None else None for name, (before, _) in changes.items()
                        }
                    journal = {"wiki_identity": plan["wiki_identity"], "raw_root": plan["raw_root"],
                               "result": result, "changes": [
                                   {"path": name, "before": pack(before), "after": pack(after),
                                    "before_sha256": digest(before) if before is not None else None,
                                    "after_sha256": digest(after)} for name, (before, after) in changes.items()]}
                    acquire(guard, area, plan)
                    try:
                        roots, failures = read_raw_roots(guard.root)
                        if failures:
                            raise PublicationError("wiki が使った原本の根を確認できません")
                        if Path(plan["raw_root"]) not in roots:
                            record_raw_root(guard.root, Path(plan["raw_root"]), writer="wiki_apply")
                        write(guard, area / JOURNAL, encoded(journal), replace=True)
                        checkpoint("after_journal")
                        result = finish(guard, area, journal, checkpoint)
                        if result["state"] == "aborted":
                            raise Conflict(f"変更を戻しました: {result['reason']}")
                        if guard.file_info(path):
                            remove(guard, path)
                        return result
                    finally:
                        remove(guard, area / OWNER)
        except Refused as error:
            # 実行中の処理で発生した例外は待ち時間の超過と扱わない。
            if error.kind != "busy":
                raise
        if time.monotonic() >= deadline:
            if drafts is None:
                raise Conflict("ライブの完了をロック内で照合できません")
            save_pending(guard, plan, expected, drafts, preview_diff)
            raise Conflict(f"書き込みを待ちきれず、承認した差分を保留しました: {path}")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def unlock(wiki, lock_id=None):
    guard = wiki_guard(wiki)
    area = guard.root / AREA
    if guard.directory_info(area) is None:
        return {"state": "none"}
    owner = load(guard, area / OWNER)
    if owner is None:
        return {"state": "none"}
    check_recovery_roots(guard, owner["raw_root"])
    try:
        with with_gate(guard, 0):
            live_mode(guard)
            owner = load(guard, area / OWNER)
            if owner is None:
                return {"state": "none"}
            if lock_id is not None and owner["lock_id"] != lock_id:
                raise Conflict("指定した書き手と一致しません")
            protect_roots(guard, owner["raw_root"])
            check_recovery_roots(guard, owner["raw_root"])
            if read_journal(guard, area) is None:
                finish_without_journal(guard, area)
            remove(guard, area / OWNER)
            return {"state": "unlocked", "lock_id": owner["lock_id"]}
    except Refused as error:
        if error.kind != "busy":
            raise
        raise Conflict("書き手が実行中です") from error


def check_recovery_roots(guard, raw_root):
    try:
        restrict_raw(guard, raw_root)
    except FileNotFoundError as error:
        raise Stuck(f"復旧に必要な原本の根を確認できません。元の場所に戻して再実行してください: {raw_root}") from error


def recovery_summary(journal, owner):
    if journal is None:
        return {"state": "locked" if owner else "none", "owner": owner}
    return {"state": journal.get("phase", "prepared"), "plan_id": checked(journal["result"]["plan_id"], PLAN_ID),
            "reason": journal.get("reason"), "files": [entry["path"] for entry in journal["changes"]],
            "raw_root": journal["raw_root"], "owner": owner}


def recover(wiki, *, show=False, discard=None):
    guard = wiki_guard(wiki)
    area = guard.root / AREA
    if guard.directory_info(area) is None:
        return {"state": "none"}
    existing = read_journal(guard, area)
    if show:
        return recovery_summary(existing, load(guard, area / OWNER))
    if existing:
        check_recovery_roots(guard, existing["raw_root"])
    with with_gate(guard, WAIT_SECONDS):
        live_mode(guard)
        if guard.file_info(area / OWNER) is not None:
            raise Conflict("中断した書き手の記録があります。unlock を実行してください")
        journal = read_journal(guard, area)
        if journal is None:
            result = finish_without_journal(guard, area)
            if result is not None:
                remove(guard, area / TRANSACTION)
                return result
            verify_latest_live(guard, area)
            return {"state": "none"}
        plan_id = checked(journal["result"]["plan_id"], PLAN_ID)
        state = journal.get("phase", "prepared")
        check_recovery_roots(guard, journal["raw_root"])
        if discard is not None:
            if checked(discard, PLAN_ID) != plan_id or state != "rollback":
                raise Conflict("保留中の日誌の plan_id を指定してください")
            destination = area / "live-discarded" / f"{plan_id}-{uuid.uuid4().hex}.json"
            write(guard, destination, encoded(journal))
            clear_transaction(guard, area, committed=False)
            return {"state": "discarded", "plan_id": plan_id, "journal": str(destination)}
        acquire(guard, area, journal)
        try:
            return finish(guard, area, journal, lambda _: None)
        finally:
            remove(guard, area / OWNER)


def pending(wiki):
    guard = wiki_guard(wiki)
    directory = guard.root / PENDING
    if guard.directory_info(directory) is None:
        return {"pending": []}
    return {"pending": [load(guard, path) for path in pending_blockers(guard)
                        if re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{64}\.json", path.name)]}


def operate(args):
    if args.cmd == "begin":
        return begin(args.wiki, args.raw_root, args.name, release_id=args.release_id, allow_publication=True)
    if args.cmd == "retry":
        from wiki_public_save import resume
        return resume(args.wiki, args.plan_id, args.expect_sha256, timeout=args.wait)
    if args.cmd == "discard-save":
        from wiki_public_save import discard
        return discard(args.wiki, args.plan_id, args.expect_sha256, timeout=args.wait)
    if args.cmd == "discard-live-pending":
        return discard_live_pending(args.wiki, args.plan_id, args.expect_sha256, args.wait)
    if args.cmd == "unlock":
        return unlock(args.wiki, args.lock_id)
    if args.cmd == "recover":
        return recover(args.wiki, show=args.show, discard=args.discard)
    if args.cmd == "pending":
        from wiki_public_save import pending_public
        result = pending(args.wiki)
        result["pending"].extend(pending_public(args.wiki))
        return result
    if args.cmd == "apply":
        if "publication" in read_plan(work_directory(args.apply_dir)):
            from wiki_public_save import queue_save
            return queue_save(args.apply_dir, args.expect_sha256, timeout=args.wait)
        return apply(args.apply_dir, args.expect_sha256, timeout=args.wait)
    work = work_directory(args.apply_dir)
    # diff は wiki にも計画にも書かない。
    with (nullcontext() if args.cmd == "diff" else work.hold_lock(work.root / "plan.lock", timeout=WAIT_SECONDS)):
        plan = read_plan(work)
        guard = plan_wiki(plan)
        receipt = guard.root / AREA / "live-receipts" / f"{plan['plan_id']}.json"
        if guard.file_info(receipt) is not None:
            raise Conflict("この計画は適用済みです。続けて書くには begin から始めてください")
        if "publication" in plan:
            from wiki_public_save import ensure_draft, public_preview
            ensure_draft(guard, plan)
            if args.cmd == "diff":
                return public_preview(work, plan, guard)
        if args.cmd == "diff":
            return preview(work, plan, guard)
        if args.cmd in {"new", "edit", "rebase"}:
            result = register(work, plan, guard, args.cmd, args.path)
        elif args.cmd == "depends-on":
            from wiki_public_save import register_dependency
            result = register_dependency(guard, plan, args.path)
        else:
            if args.cmd == "index-add":
                index_path(args.file)
                op = {"file": args.file, "section": args.section, "line": one_line(args.line), "nth": args.nth}
            else:
                heading = (f"## [{date.fromisoformat(args.date).isoformat()}] {args.op} | {one_line(args.title)}"
                           if args.cmd == "log-entry" else args.entry)
                op = {"heading": heading, "lines": [one_line(line) for line in args.line],
                      "nth": getattr(args, "nth", None)}
            plan["operations"].append(dict(op, kind=args.cmd))
            result = {"state": "registered", "plan_id": plan["plan_id"]}
        write(work, work.root / "plan.json", encoded(plan), replace=True)
        return result


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    setup = sub.add_parser("begin")
    setup.add_argument("--wiki", required=True, type=Path)
    setup.add_argument("--raw-root", required=True, type=Path)
    setup.add_argument("--name", required=True)
    setup.add_argument("--release-id")
    for command in ("retry", "discard-save", "discard-live-pending"):
        save = sub.add_parser(command)
        save.add_argument("--wiki", required=True, type=Path)
        save.add_argument("--plan-id", required=True)
        save.add_argument("--expect-sha256", required=True)
        save.add_argument("--wait", type=int, default=WAIT_SECONDS, choices=range(WAIT_SECONDS + 1),
                          metavar=f"0..{WAIT_SECONDS}")
    for command in ("recover", "unlock", "pending"):
        admin = sub.add_parser(command)
        admin.add_argument("--wiki", required=True, type=Path)
        if command == "unlock":
            admin.add_argument("--lock-id")
        if command == "recover":
            admin.add_argument("--live", action="store_true")
            choice = admin.add_mutually_exclusive_group()
            choice.add_argument("--show", action="store_true")
            choice.add_argument("--discard")
    for command in ("new", "edit", "rebase", "depends-on", "diff", "index-add", "log-entry", "log-sub", "apply"):
        item = sub.add_parser(command)
        item.add_argument("--apply-dir", required=True, type=Path)
        if command in {"new", "edit", "rebase", "depends-on"}:
            item.add_argument("--path", required=True)
        if command == "index-add":
            item.add_argument("--file", required=True)
            item.add_argument("--section")
            item.add_argument("--nth", type=int)
            item.add_argument("--line", required=True)
        if command == "log-entry":
            item.add_argument("--op", required=True, choices=OPS)
            item.add_argument("--title", required=True)
            item.add_argument("--date", default=date.today().isoformat())
            item.add_argument("--line", action="append", default=[])
        if command == "log-sub":
            item.add_argument("--entry", required=True)
            item.add_argument("--nth", type=int)
            item.add_argument("--line", required=True, action="append")
        if command == "apply":
            item.add_argument("--expect-sha256", required=True)
            item.add_argument("--wait", type=int, default=WAIT_SECONDS, choices=range(WAIT_SECONDS + 1),
                              metavar=f"0..{WAIT_SECONDS}")
    for command in sub.choices.values():
        command.add_argument("--json", action="store_true", help="結果を JSON で表示する")
    args = parser.parse_args(argv)
    try:
        result = operate(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 5 if isinstance(error, Stuck) else 4 if isinstance(error, Conflict) else 1
    if args.cmd == "diff" and not args.json:
        print(result["diff"], end="")
        print(f"plan_sha256: {result['plan_sha256']}")
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 4 if result.get("state") == "pending" else 0
