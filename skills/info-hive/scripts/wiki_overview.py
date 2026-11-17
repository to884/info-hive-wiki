#!/usr/bin/env python3
"""ページの概要を検査し、SVG を埋め込んだサイドカーに保存する（ADR-0031）。

    python wiki_overview.py begin|prepare|status|remove <wiki>
    python wiki_overview.py check|write <wiki> --work=<16桁>
    python wiki_overview.py remove <wiki> --orphan=<16桁>

ページ名は begin が返す request_path に JSON で書く。利用者の文字列をシェルに渡さない。
状態の判定と一覧は、CLI、lint、将来の browse で共有する。書き込みの同時実行は保証しない。
"""

from __future__ import annotations

import wiki_text  # Python の版を、ほかのモジュールより先に確かめる

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from xml.parsers import expat

import wiki_initialized
import wiki_pages

HEX_ID = re.compile(r"^[0-9a-f]{16}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SIDECAR = re.compile(r"^([0-9a-f]{16})\.overview\.md$")
GENERATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$")
CACHE = (".wiki-cache", "overview")
ASSETS = ("assets", "overview")
MAX_BODY = 64 * 1024
MAX_SVG = 2 * 1024 * 1024
MAX_SIDECAR = 8 * 1024 * 1024
MAX_FIGURES = 12


class OverviewError(Exception):
    """CLI で JSON にする、利用者に知らせる失敗。"""

    def __init__(self, code: str, message: str, *, payload: dict | None = None, exit_code: int = 1):
        super().__init__(message)
        self.payload = payload or {"status": "error", "code": code, "message": message}
        self.exit_code = exit_code


@dataclass(frozen=True)
class Sidecar:
    """サイドカーを読む側が作る、absent / unreadable / ok。"""

    kind: str
    data: bytes = b""
    reason: str | None = None


@dataclass(frozen=True)
class PageState:
    """ページを読む側が作る、absent / unknown / unreadable / ok。"""

    kind: str
    sha256: str | None = None


@dataclass(frozen=True)
class State:
    """設計の優先順で決めた状態と理由。"""

    state: str
    detail: str | None = None


def page_id(rel: str) -> str:
    """ディスク上の相対パスから、末尾の .md を除いた NFC の識別子を作る。"""
    return wiki_text.nfc(rel.replace("\\", "/")[:-3])


def page_key(identifier: str) -> str:
    return hashlib.sha256(wiki_text.nfc(identifier).encode("utf-8")).hexdigest()[:16]


def _link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _spelling(parent: Path, name: str) -> None:
    if name not in ("assets", ".wiki-cache", "overview"):
        return
    with os.scandir(parent) as entries:
        matches = [entry.name for entry in entries if entry.name.casefold() == name.casefold()]
    if any(value != name for value in matches):
        raise OverviewError("spelling", f"ディレクトリの綴りを {name} にそろえてからやり直す。")


def _dirs(root: Path, parts: tuple[str, ...], *, create: bool = False) -> Path | None:
    """ルート以下の各段を lstat で確かめる。resolve でリンクを消してから判定しない。"""
    step = root
    st = _lstat(step)
    if st is None:
        if create:
            raise OverviewError("unsafe_path", "wiki のルートがなくなったので書き込まない。")
        return None
    if _link(st) or not stat.S_ISDIR(st.st_mode):
        raise OverviewError("unsafe_path", "wiki または行き先のディレクトリが通常のディレクトリではない。")
    for part in parts:
        _spelling(step, part)
        step = step / part
        st = _lstat(step)
        if st is None and create:
            try:
                step.mkdir()
            except FileExistsError:
                pass
            st = _lstat(step)
        if st is None:
            return None
        if _link(st) or not stat.S_ISDIR(st.st_mode):
            raise OverviewError("unsafe_path", "行き先の途中にリンクか、通常のディレクトリでない項目がある。")
    return step


def _file(path: Path, *, missing: bool = False):
    st = _lstat(path)
    if st is None:
        if missing:
            return None
        raise FileNotFoundError(path)
    if _link(st) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise OverviewError("unsafe_path", "行き先または作業用のファイルが、リンクでない単独の通常ファイルではない。")
    return st


def _read(path: Path, limit: int) -> bytes:
    _file(path)
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def _text(data: bytes) -> str:
    return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _cache(root: Path, *, create: bool = False) -> Path | None:
    _spelling(root, "assets")
    return _dirs(root, CACHE, create=create)


def _remove_tree(root: Path, work_id: str) -> None:
    """自分の work/ 直下だけを消す。再帰削除の前に、実体の絶対パスと内側のリンクも確かめる。"""
    parent = _dirs(root, (*CACHE, "work"))
    target = _dirs(root, (*CACHE, "work", work_id))
    if parent is None or target is None:
        return
    if target.resolve().parent != parent.resolve() or target.resolve().name != work_id:
        raise OverviewError("unsafe_path", "作業用のフォルダーの置き場の直下ではないので消さない。")
    pending = [target]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                st = os.lstat(entry.path)
                if _link(st):
                    raise OverviewError("unsafe_path", "作業用のフォルダーにリンクがあるので消さない。")
                if stat.S_ISDIR(st.st_mode):
                    pending.append(Path(entry.path))
    shutil.rmtree(target)


def _cleanup(root: Path) -> None:
    now = time.time()
    parent = _dirs(root, (*CACHE, "work"))
    if parent is not None:
        with os.scandir(parent) as entries:
            names = [entry.name for entry in entries if HEX_ID.fullmatch(entry.name)]
        for name in names:
            try:
                target = _dirs(root, (*CACHE, "work", name))
                if target is not None and _file(target / "plan.json", missing=True) is not None:
                    if os.lstat(target).st_mtime < now - 7 * 86400:
                        _remove_tree(root, name)
            except (OSError, OverviewError):
                continue  # 片付けられない古い作業を、新しい作業の成功条件にしない。
    parent = _dirs(root, (*CACHE, "tmp"))
    if parent is not None:
        with os.scandir(parent) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            path = parent / name
            st = _lstat(path)
            if st is not None and not _link(st) and stat.S_ISREG(st.st_mode) and st.st_mtime < now - 86400:
                _file(path)
                path.unlink()


def begin(root: Path) -> dict:
    cache = _cache(root, create=True)
    _cleanup(root)
    request = cache / "request.json"
    if _file(request, missing=True) is not None:
        request.unlink()
    return {"status": "ready", "request_path": str(request.absolute())}


def _request(root: Path, action: str) -> dict:
    cache = _cache(root)
    path = (cache or root.joinpath(*CACHE)) / "request.json"
    if cache is None or _file(path, missing=True) is None:
        raise OverviewError("no_request", "要求のファイルがない。begin からやり直す。")
    try:
        data = path.read_bytes()
    finally:
        _file(path)
        path.unlink()
    try:
        req = json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        raise OverviewError("invalid_request", "要求の JSON を読めない。begin からやり直す。") from None
    if not isinstance(req, dict) or req.get("format") != 2:
        raise OverviewError("invalid_request", "要求は format が 2 の JSON オブジェクトにする。")
    if req.get("action") != action:
        raise OverviewError("wrong_action", f"要求の action は {action} にする。")
    if ("page" in req) == ("id" in req) or not isinstance(req.get("page", req.get("id")), str):
        raise OverviewError("invalid_request", "page と id は、どちらか一方を文字列で指定する。")
    if not req.get("page", req.get("id", "")).strip() or not isinstance(req.get("refresh", False), bool):
        raise OverviewError("invalid_request", "ページの指定と refresh の型を確かめる。")
    return req


@dataclass
class PageIndex:
    records: list
    by_key: dict
    complete: bool


def _index(root: Path) -> PageIndex:
    try:
        walked = wiki_pages.walk_pages(root)
    except OSError:
        return PageIndex([], {}, False)
    records = [{"id": page_id(f.rel), "disk_rel": f.rel, "rel_path": wiki_text.nfc(f.rel),
                "slug": wiki_text.nfc(PurePosixPath(f.rel).stem)} for f in walked.files]
    by_key: dict = {}
    for record in records:
        by_key.setdefault(page_key(record["id"]), []).append(record)
    return PageIndex(records, by_key, not walked.unlistable and not walked.unknown)


def _complete(index: PageIndex) -> None:
    if not index.complete:
        raise OverviewError("walk_incomplete", "ページの走査が不完全なので、書き込みや削除を行わない。")


def _collision(index: PageIndex, key: str) -> None:
    if len({record["id"] for record in index.by_key.get(key, [])}) > 1:
        raise OverviewError("id_collision", "ページの識別子のハッシュが衝突している。")


def _resolve(req: dict, index: PageIndex) -> dict:
    _complete(index)
    if "id" in req:
        found = [p for p in index.records if p["id"] == req["id"]]
    else:
        paths, slugs = wiki_pages.build_page_index(index.records)
        found = wiki_pages.link_candidates(req["page"], paths, slugs)
    if not found:
        raise OverviewError("not_found", "ページが見つからない。", payload={"status": "not_found"}, exit_code=3)
    if len(found) != 1:
        raise OverviewError("ambiguous", "ページを1つに決められない。",
                            payload={"status": "ambiguous", "candidates": [p["id"] for p in found]}, exit_code=3)
    _collision(index, page_key(found[0]["id"]))
    return found[0]


def _sidecar(root: Path, key: str) -> Sidecar:
    try:
        parent = _dirs(root, ASSETS)
        if parent is None:
            return Sidecar("absent")
        path = parent / f"{key}.overview.md"
        if _file(path, missing=True) is None:
            return Sidecar("absent")
        return Sidecar("ok", _read(path, MAX_SIDECAR))
    except (OSError, OverviewError):
        return Sidecar("unreadable", reason="os_error")


def _page_state(root: Path, index: PageIndex, key: str) -> PageState:
    found = index.by_key.get(key, [])
    if not found:
        return PageState("absent" if index.complete else "unknown")
    if len(found) != 1:
        return PageState("unknown")
    try:
        return PageState("ok", wiki_pages.page_text_sha256((root / found[0]["disk_rel"]).read_bytes()))
    except (OSError, UnicodeDecodeError):
        return PageState("unreadable")


def _problem(code: str, file: str, message: str, line: int = 1) -> dict:
    return {"code": code, "file": file, "line": line, "message": message}


def _tokens_and_refs(body: str) -> tuple[list, list[str], list]:
    tokens = wiki_pages.new_parser().parse(body)
    refs = []
    problems = []
    for token in tokens:
        for child in token.children or []:
            if child.type != "image":
                continue
            src = child.attrGet("src") or ""
            name = src[5:] if src.startswith("#fig-") else ""
            if not NAME.fullmatch(name):
                problems.append(_problem("image_ref", "overview.md", "画像は #fig-<名前> で図を参照する。",
                                         (token.map or [0])[0] + 1))
            elif name not in refs:
                refs.append(name)
    return tokens, refs, problems


def _svg_problems(name: str, data: bytes) -> list:
    # browse と同じ入口を使う。core が lint を読むため、lint の呼び出しが終わった時点で読み込む。
    from wiki_browse_core import is_utf8_svg, svg_size
    problems = []
    file = f"{name}.svg"
    if len(data) > MAX_SVG or not is_utf8_svg(data):
        return [_problem("figure_encoding", file, "図は 2 MiB 以下の UTF-8 の SVG にする。")]
    parser = expat.ParserCreate(namespace_separator="}")
    root = []

    def start(tag, _attrs):
        if not root:
            root.append(tag)

    def forbidden(*_args):
        raise ValueError("DTD と実体の宣言は使わない。")

    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    try:
        parser.Parse(data, True)
        if root != ["http://www.w3.org/2000/svg}svg"]:
            raise ValueError("根の要素が SVG ではない。")
    except (expat.ExpatError, ValueError):
        problems.append(_problem("figure_xml", file, "図を SVG の XML として最後まで読めないか、DTD がある。"))
    if svg_size(data) is None:
        problems.append(_problem("figure_no_size", file, "SVG の width と height、または viewBox で大きさを指定する。"))
    return problems


def _figure_problems(body: str, figures: dict[str, bytes]) -> tuple[list, list[str]]:
    _tokens, refs, problems = _tokens_and_refs(body)
    for name in refs:
        if name not in figures:
            problems.append(_problem("figure_missing", f"{name}.svg", "参照する図がない。"))
    for name, data in figures.items():
        if name not in refs:
            problems.append(_problem("figure_unused", f"{name}.svg", "本文が参照していない図がある。"))
        problems.extend(_svg_problems(name, data))
    if len(figures) > MAX_FIGURES:
        problems.append(_problem("too_many_figures", "overview.md", "図は 12 個以下にする。"))
    return problems, refs


def _metadata(data: bytes) -> tuple[dict, str]:
    from wiki_lint import parse_frontmatter_raw
    text = _text(data)
    meta, frontmatter, body, malformed = parse_frontmatter_raw(text)
    if malformed or frontmatter is None:
        raise OverviewError("unreadable", "frontmatter を読めない。")
    return meta, body


def _split_figures(body: str) -> tuple[str, dict[str, bytes]]:
    tokens = wiki_pages.new_parser().parse(body)
    start = None
    figures: dict[str, bytes] = {}
    for token in tokens:
        info = token.info.split() if token.type == "fence" else []
        if info and info[0] == "svg-figure":
            if token.level != 0 or len(info) != 2 or not NAME.fullmatch(info[1]) or info[1] in figures:
                raise OverviewError("figures", "図のフェンスの形か名前が正しくない。")
            if start is None:
                start = token.map[0]
            figures[info[1]] = token.content.encode("utf-8")
        elif start is not None:
            raise OverviewError("figures", "図のフェンスは本文のあとにまとめて置く。")
    prose = body if start is None else "\n".join(body.split("\n")[:start])
    return prose, figures


def _parse_sidecar(data: bytes) -> tuple[dict, str, dict[str, bytes]]:
    meta, body = _metadata(data)
    prose, figures = _split_figures(body)
    return meta, prose, figures


def embedded_figures(data: bytes) -> dict[str, bytes]:
    """サイドカーの埋め込み図を返す。表示側は先に classify で状態を確かめる。"""
    return _parse_sidecar(data)[2]


def classify(name: str, sidecar: Sidecar, page: PageState) -> State:
    """設計の6.5の表を上から評価する。ページの不在より、サイドカーの破損を先に判定する。"""
    if sidecar.kind == "absent":
        return State("none")
    if sidecar.kind != "ok":
        return State("broken", "unreadable")
    try:
        meta, body = _metadata(sidecar.data)
    except UnicodeDecodeError:
        return State("broken", "unreadable")
    except OverviewError as exc:
        return State("broken", exc.payload["code"])
    if meta.get("format") != "2":
        return State("broken", "format")
    key = name.removesuffix(".overview.md")
    identifier = meta.get("page")
    try:
        matches = isinstance(identifier, str) and bool(identifier) and page_key(identifier) == key
    except UnicodeError:
        matches = False
    if not matches:
        return State("broken", "page_field")
    names = meta.get("figures")
    if not isinstance(meta.get("page_sha256"), str) or not HASH.fullmatch(meta["page_sha256"]) or not isinstance(names, list):
        return State("broken", "fields")
    try:
        body, figures = _split_figures(body)
    except OverviewError:
        return State("broken", "figures")
    problems, refs = _figure_problems(body, figures)
    if len(sidecar.data) > MAX_SIDECAR or problems or names != refs or list(figures) != refs:
        return State("broken", "figures")
    if page.kind == "unknown":
        return State("page_unknown")
    if page.kind == "absent":
        return State("orphan")
    if page.kind == "unreadable":
        return State("page_unreadable")
    if meta["page_sha256"] != page.sha256:
        return State("stale")
    return State("fresh")


def _source(root: Path, rel: str) -> bytes:
    try:
        data = (root / rel).read_bytes()
        wiki_pages.page_text_sha256(data)
        return data
    except (FileNotFoundError, NotADirectoryError):
        raise OverviewError("page_gone", "元のページがなくなった。", payload={"status": "page_gone"}, exit_code=3) from None
    except (OSError, UnicodeDecodeError):
        raise OverviewError("page_unreadable", "元のページを UTF-8 として読めない。保存せず、下書きを残す。") from None


def prepare(root: Path) -> dict:
    req = _request(root, "create")
    index = _index(root)
    record = _resolve(req, index)
    data = _source(root, record["disk_rel"])
    digest = wiki_pages.page_text_sha256(data)
    key = page_key(record["id"])
    existing = classify(key, _sidecar(root, key), PageState("ok", digest))
    rel = f"assets/overview/{key}.overview.md"
    if existing.state == "fresh" and not req.get("refresh", False):
        return {"status": "exists", "page": record["id"], "sidecar": rel}
    parent = _dirs(root, (*CACHE, "work"), create=True)
    while True:
        work_id = secrets.token_hex(8)
        work = parent / work_id
        try:
            work.mkdir()
            break
        except FileExistsError:
            continue
    draft = work / "draft"
    draft.mkdir()
    with (work / "page.md").open("xb") as stream:
        stream.write(data)
    generator = req.get("generator")
    if not isinstance(generator, str) or not GENERATOR.fullmatch(generator):
        generator = None
    plan = {"format": 2, "page": record["id"], "page_rel": record["disk_rel"], "page_sha256": digest,
            "generator": generator, "prepared": datetime.now(timezone.utc).isoformat()}
    with (work / "plan.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(plan, ensure_ascii=False) + "\n")
    return {"status": "ready", "work_id": work_id, "page": record["id"], "page_copy": str((work / "page.md").absolute()),
            "draft_dir": str(draft.absolute()), "existing": existing.state}


def _work(root: Path, work_id: str) -> tuple[Path, dict]:
    if not HEX_ID.fullmatch(work_id):
        raise OverviewError("invalid_work", "作業ID は 16 桁の小文字の16進数にする。")
    work = _dirs(root, (*CACHE, "work", work_id))
    if work is None:
        raise OverviewError("invalid_work", "作業用のフォルダーがない。prepare からやり直す。")
    try:
        _file(work / "plan.json")
        plan = json.loads((work / "plan.json").read_bytes().decode("utf-8"))
        rel = plan["page_rel"]
        if (plan["format"] != 2 or not isinstance(rel, str) or "\\" in rel or rel.startswith("/")
                or ":" in rel or any(part in (".", "..") for part in rel.split("/"))
                or not wiki_pages.is_page_path(rel) or page_id(rel) != plan["page"]
                or not isinstance(plan["page_sha256"], str) or not HASH.fullmatch(plan["page_sha256"])
                or _copy_sha256(work / "page.md") != plan["page_sha256"]):
            raise ValueError("作業の記録が写しと合わない。")
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise OverviewError("invalid_work", "作業の記録かページの写しを読めない。prepare からやり直す。") from None
    return work, plan


def _copy_sha256(path: Path) -> str:
    _file(path)
    return wiki_pages.page_text_sha256(path.read_bytes())


def _draft(root: Path, work_id: str) -> tuple[str, dict, list]:
    draft = _dirs(root, (*CACHE, "work", work_id, "draft"))
    problems = []
    figures = {}
    body = ""
    if draft is None:
        return body, figures, [_problem("draft_missing", "overview.md", "下書きがない。")]
    with os.scandir(draft) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        path = draft / name
        st = _lstat(path)
        if st is None:
            continue
        if _link(st) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            problems.append(_problem("extra_file", name, "下書きには通常のファイルだけを置く。"))
            continue
        if name == "overview.md":
            data = _read(path, MAX_BODY)
            try:
                body = _text(data)
            except UnicodeDecodeError:
                problems.append(_problem("not_utf8", name, "本文を UTF-8 で書く。"))
            if len(data) > MAX_BODY:
                problems.append(_problem("too_large", name, "本文は 64 KiB 以下にする。"))
        elif name.endswith(".svg") and NAME.fullmatch(name[:-4]):
            figures[name[:-4]] = _read(path, MAX_SVG)
        else:
            problems.append(_problem("extra_file", name, "本文と、規定の名前の SVG だけを置く。"))
    if "overview.md" not in names:
        problems.append(_problem("draft_missing", "overview.md", "下書きの本文がない。"))
    if not body.strip():
        problems.append(_problem("empty", "overview.md", "本文が空白だけになっている。"))
    if body.split("\n", 1)[0] == "---":
        problems.append(_problem("frontmatter", "overview.md", "下書きには frontmatter を書かない。"))
    tokens, _refs, _problems = _tokens_and_refs(body)
    for token in tokens:
        if token.type == "heading_open" and token.tag in ("h1", "h2"):
            problems.append(_problem("heading_level", "overview.md", "見出しは ### 以下にする。", token.map[0] + 1))
        if token.type == "fence" and token.info.split()[:1] == ["svg-figure"]:
            problems.append(_problem("figure_fence", "overview.md", "図のフェンスはスクリプトが組み立てる。"))
    if wiki_pages.extract_wikilinks(body):
        problems.append(_problem("wikilink", "overview.md", "概要には wikilink を書かない。"))
    figure_problems, _refs = _figure_problems(body, figures)
    problems.extend(figure_problems)
    return body, figures, problems


def _assemble(plan: dict, body: str, figures: dict) -> bytes:
    _tokens, refs, _problems = _tokens_and_refs(body)
    meta = {"format": 2, "page": plan["page"], "page_sha256": plan["page_sha256"],
            "figures": refs, "generated": date.today().isoformat()}
    if plan.get("generator"):
        meta["generator"] = plan["generator"]
    text = wiki_text.format_frontmatter(meta) + "\n" + body + "\n\n"
    for name in refs:
        svg = _text(figures[name])
        longest = max((len(match.group()) for match in re.finditer(r"`+", svg)), default=0)
        fence = "`" * max(3, longest + 1)
        text += f"{fence}svg-figure {name}\n{svg}\n{fence}\n\n"
    return text.encode("utf-8")


def _checked(root: Path, work_id: str) -> tuple[dict, bytes | None, list]:
    _work_path, plan = _work(root, work_id)
    body, figures, problems = _draft(root, work_id)
    if problems:
        return plan, None, problems
    data = _assemble(plan, body, figures)
    state = classify(page_key(plan["page"]), Sidecar("ok", data), PageState("ok", plan["page_sha256"]))
    if state.state != "fresh":
        problems.append(_problem("assembly", "overview.md", "組み立てた概要を読み直せない。フェンスの閉じ方を確かめる。"))
    return plan, data, problems


def check(root: Path, work_id: str) -> dict:
    _plan, _data, problems = _checked(root, work_id)
    return {"status": "rejected", "problems": problems} if problems else {"status": "ok"}


def write(root: Path, work_id: str) -> dict:
    _work_path, initial_plan = _work(root, work_id)
    index = _index(root)
    _complete(index)
    key = page_key(initial_plan["page"])
    _collision(index, key)
    if not any(p["disk_rel"] == initial_plan["page_rel"] and p["id"] == initial_plan["page"] for p in index.records):
        raise OverviewError("page_gone", "元のページがなくなった。", payload={"status": "page_gone"}, exit_code=3)
    plan, data, problems = _checked(root, work_id)
    if problems:
        return {"status": "rejected", "problems": problems}
    figure_names = _parse_sidecar(data)[0]["figures"]
    parent = _dirs(root, ASSETS, create=True)
    destination = parent / f"{key}.overview.md"
    _file(destination, missing=True)
    tmp_parent = _dirs(root, (*CACHE, "tmp"), create=True)
    current = _source(root, plan["page_rel"])
    page_changed = wiki_pages.page_text_sha256(current) != plan["page_sha256"]
    temporary = tmp_parent / f"{secrets.token_hex(16)}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _dirs(root, ASSETS)
        _file(destination, missing=True)
        from wiki_fetch import replace_with_retry
        try:
            replace_with_retry(temporary, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise OverviewError("cross_device", "作業用の場所と保存先のボリュームが違うので置き換えられない。") from None
            raise
    finally:
        try:
            if _file(temporary, missing=True) is not None:
                temporary.unlink()
        except (OSError, OverviewError):
            pass  # 一時ファイルの片付けの失敗で、置き換えの結果を変えない。
    try:
        _remove_tree(root, work_id)
    except (OSError, OverviewError):
        pass  # 保存は完了した。残った作業は begin の片付けに任せ、成功をエラーに変えない。
    return {"status": "written", "page": plan["page"], "sidecar": f"assets/overview/{key}.overview.md",
            "figures": figure_names, "page_changed": page_changed}


def remove(root: Path, orphan: str | None = None) -> dict:
    if orphan is not None:
        if not HEX_ID.fullmatch(orphan):
            raise OverviewError("invalid_orphan", "orphan は 16 桁の小文字の16進数にする。")
        index = _index(root)
        _complete(index)
        if index.by_key.get(orphan):
            raise OverviewError("not_orphan", "元のページがある概要は --orphan で消さない。")
        key = orphan
    else:
        req = _request(root, "remove")
        record = _resolve(req, _index(root))
        key = page_key(record["id"])
    parent = _dirs(root, ASSETS)
    path = root.joinpath(*ASSETS, f"{key}.overview.md")
    rel = path.relative_to(root).as_posix()
    if parent is None or _file(path, missing=True) is None:
        return {"status": "absent", "sidecar": rel}
    path.unlink()
    return {"status": "removed", "sidecar": rel}


def _scan_sidecars(root: Path) -> tuple[list, list | None]:
    """専用の名前空間の直下だけを調べる。通常のページ走査には混ぜない。"""
    try:
        parent = _dirs(root, ASSETS)
    except OverviewError:
        return [], [{"path": "assets/overview", "code": "unknown_entry", "message": "概要の置き場所が安全なディレクトリではない。"}]
    except OSError:
        return [], None
    if parent is None:
        return [], []
    found = []
    problems = []
    state, iterator = wiki_pages.read_os(os.scandir, parent)
    if state != "ok":
        return [], None
    try:
        while True:
            try:
                state, entry = wiki_pages.read_os(next, iterator)
            except StopIteration:
                break
            if state != "ok":
                return [], None
            state, st = wiki_pages.read_os(os.lstat, entry.path)
            rel = f"assets/overview/{entry.name}"
            match = SIDECAR.fullmatch(entry.name)
            if state == "absent":
                continue
            if state != "ok":
                if match:
                    found.append((match.group(1), Sidecar("unreadable", reason="os_error")))
                else:
                    problems.append({"path": rel, "code": "unknown_entry", "message": "項目の種類を確かめられない。"})
            elif match and not _link(st) and stat.S_ISREG(st.st_mode):
                found.append((match.group(1), _sidecar(root, match.group(1))))
            else:
                problems.append({"path": rel, "code": "unknown_entry", "message": "概要の置き場所に、規定外の項目がある。"})
    finally:
        iterator.close()
    return sorted(found, key=lambda item: item[0]), problems


def inspect_wiki(root: Path) -> dict:
    """状態の一覧と lint の2種類の所見を、同じ観測から作る。何も書かない。"""
    index = _index(root)
    try:
        found, problems = _scan_sidecars(root)
    except OSError:
        found, problems = [], None  # read_os が再送出する EIO なども、概要だけを未検査にする。
    overviews = []
    stale = []
    for key, sidecar in found:
        page = _page_state(root, index, key)
        state = classify(key, sidecar, page)
        meta = {}
        if sidecar.kind == "ok":
            try:
                meta = _metadata(sidecar.data)[0]
            except (OverviewError, UnicodeDecodeError):
                pass
        rel = f"assets/overview/{key}.overview.md"
        identifier = meta.get("page") if isinstance(meta.get("page"), str) else None
        orphan_id = key if page.kind == "absent" else None
        overviews.append({"page": identifier, "sidecar": rel, "orphan_id": orphan_id,
                          "state": state.state, "detail": state.detail})
        if state.state == "stale":
            stale.append({"page": identifier, "sidecar": rel})
        elif state.state not in ("fresh", "none") and problems is not None:
            finding = {"path": rel, "code": state.state, "message": {
                "broken": "概要を読めない。元のページがあれば作り直すか削除する。",
                "orphan": "元のページがない。remove --orphan で削除できる。",
                "page_unknown": "走査が不完全なので、元のページの有無を決められない。",
                "page_unreadable": "元のページを読めない。",
            }[state.state]}
            if identifier is not None:
                finding["page"] = identifier
            if state.detail:
                finding["reason"] = state.detail
            if orphan_id is not None:
                finding.update(orphan_id=orphan_id, page_absent=True)
                if state.state == "broken":
                    finding["message"] = "概要は壊れており、元のページもない。remove --orphan で削除できる。"
                if state.state == "orphan":
                    candidates = [p["id"] for p in index.records
                                  if _page_state(root, index, page_key(p["id"])).sha256 == meta.get("page_sha256")]
                    if candidates:
                        finding["candidates"] = candidates
            problems.append(finding)
    return {"overviews": overviews, "overview_stale": stale if problems is not None else None,
            "overview_problems": problems, "complete": index.complete and problems is not None}


def status(root: Path) -> dict:
    result = inspect_wiki(root)
    return {"overviews": result["overviews"], "problems": result["overview_problems"], "complete": result["complete"]}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise OverviewError("invalid_args", f"引数を確かめる: {message}")


def main(argv=None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        parser = Parser(description="ページの概要を作成・検査・保存する。")
        wiki_text.localize_argparse(parser)
        parser.add_argument("operation", choices=("begin", "prepare", "check", "write", "status", "remove"))
        parser.add_argument("wiki")
        parser.add_argument("--work")
        parser.add_argument("--orphan")
        args = parser.parse_args(argv)
        problem = wiki_initialized.problem(args.wiki)
        if problem is not None:
            raise OverviewError("not_initialized", f"{problem}。{wiki_initialized.INIT_HINT}")
        root = Path(os.path.abspath(args.wiki))
        if args.work is not None and args.operation not in ("check", "write"):
            raise OverviewError("invalid_args", "--work は check と write だけで使う。")
        if args.orphan is not None and args.operation != "remove":
            raise OverviewError("invalid_args", "--orphan は remove だけで使う。")
        if args.operation in ("check", "write"):
            if args.work is None:
                raise OverviewError("invalid_args", "--work で作業ID を指定する。")
            result = (check if args.operation == "check" else write)(root, args.work)
        elif args.operation == "remove":
            result = remove(root, args.orphan)
        else:
            result = {"begin": begin, "prepare": prepare, "status": status}[args.operation](root)
        code = 2 if result.get("status") == "rejected" else 0
    except OverviewError as exc:
        result, code = exc.payload, exc.exit_code
    except OSError:
        result, code = {"status": "error", "code": "io_error", "message": "ファイルの入出力に失敗した。保存先と作業の状態を確かめる。"}, 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
