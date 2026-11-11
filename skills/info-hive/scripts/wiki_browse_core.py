"""
wiki_browse_core.py — 閲覧（browse）の中核。

Wiki を 1 度だけ読んで写し（Snapshot）を作り、そこからページ、リンク、被リンク、関係表、ナビゲーション、
診断を組み立て（Core）、ページの HTML を描く。書き込みはしない。静的の書き出しとサーバーは、どちらも
このモジュールの同じ関数でページを描く（守ること 6）。設計は `docs/plans/wiki-browse-stage1-2026-11-09.md`
の 2〜5 にある。

ページの集合、リンクの解決、`sources:` の解決は、lint と同じ wiki_pages の関数で決める（守ること 2）。
関係表だけが PyYAML（グラフの抽出）を使う。PyYAML がなければ、関係表は「使えない」になる。
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import string
import sys
import threading
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiki_browse_fs  # noqa: E402
import wiki_lint  # noqa: E402
import wiki_pages  # noqa: E402
from wiki_text import canonical_meta, nfc, posix_rel  # noqa: E402
from markdown_it.token import Token  # noqa: E402  （wiki_pages が同梱のものを読み込んだあと）

try:
    import yaml  # noqa: F401
    import wiki_graph_extract  # noqa: E402
except (ImportError, SystemExit):
    wiki_graph_extract = None

PROTOCOL_VERSION = 1
CORE_VERSION = "browse-core-1"
NAME_LIMIT = 200
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'")
RAW_MODES = ("text", "link", "basename", "none")
ASSETS = ("assets/app.js", "assets/style.css")
ASSET_DIR = Path(__file__).resolve().parent / "browse_assets"
DATA_FILES = ("nav.js", "data/manifest.js", "data/diagnostics.js")
EXTERNAL_SCHEMES = ("http", "https", "mailto")
ONTOLOGY_REL = "graph/ontology.yaml"


_DISPLAY = wiki_pages.new_parser()


class NameCollision(Exception):
    """出力の名前が、接尾辞の桁を増やしても重なった。"""


# ---------------------------------------------------------------------------
# 写し（設計の 2）
# ---------------------------------------------------------------------------

@dataclass
class InputFile:
    kind: str            # "page"、"index"、"ontology"
    path: Path           # Wiki の中のパス
    disk_rel: str        # ディスク上の綴りの / 区切りの相対パス
    rel: str             # NFC にそろえた / 区切りの相対パス
    text: "str | None" = None
    error: "str | None" = None   # "os" か "decode"
    sha: "str | None" = None
    real: "Path | None" = None   # シンボリックリンクとジャンクションを解いた実体


def list_inputs(root: Path) -> list:
    """入力の集合を、(種類, パス) の並びで返す。中身は読まない。"""
    items = [("page", p) for p in wiki_pages.iter_page_files(root)]
    items += [("index", p) for p in wiki_lint.index_files(root)]
    onto = root / ONTOLOGY_REL
    if onto.is_file():
        items.append(("ontology", onto))
    return items


def signature(root: Path, items: "list | None" = None) -> str:
    """入力の印。各入力の相対パス、大きさ、更新時刻と、索引とオントロジーの有無から作る。"""
    items = list_inputs(root) if items is None else items
    h = hashlib.sha256()
    for kind, path in items:
        try:
            st = os.stat(path)
            part = f"{st.st_size}\0{st.st_mtime_ns}"
        except OSError:
            part = "missing"
        h.update(f"{kind}\0{path.relative_to(root).as_posix()}\0{part}\n".encode("utf-8"))
    return h.hexdigest()


def external_inputs(root: Path, items: "list | None" = None) -> list:
    """入力のうち、実体が Wiki のルートの外にあるものの実体のパス。中身は読まない（設計の 6.1）。"""
    items = list_inputs(root) if items is None else items
    out = []
    for _kind, path in items:
        real = Path(os.path.realpath(path))
        if not wiki_browse_fs.is_within(real, root):
            out.append(real)
    return out


def read_text_like_lint(data: bytes) -> str:
    """lint の read_text(encoding="utf-8-sig") と同じ文字にする。BOM を除き、改行を LF にそろえる。"""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline=None).read()


@dataclass
class Snapshot:
    root: Path
    signature: str
    inputs: list
    generation: str
    unreadable: bool
    external: list = field(default_factory=list)

    def by_kind(self, kind: str) -> list:
        return [f for f in self.inputs if f.kind == kind]


def take_snapshot(root: Path) -> Snapshot:
    """入力を 1 度だけ読み、写しを作る。印は読む前に取る。"""
    root = Path(root)
    items = list_inputs(root)
    sig = signature(root, items)
    inputs = []
    unreadable = False
    gen = hashlib.sha256(CORE_VERSION.encode("utf-8"))
    for kind, path in items:
        rel_disk = path.relative_to(root).as_posix()
        f = InputFile(kind=kind, path=path, disk_rel=rel_disk, rel=posix_rel(str(path.relative_to(root))))
        try:
            data = path.read_bytes()
        except OSError:
            f.error = "os"
        else:
            f.sha = hashlib.sha256(data).hexdigest()
            try:
                f.text = read_text_like_lint(data)
            except UnicodeDecodeError:
                f.error = "decode"
        f.real = Path(os.path.realpath(path))
        if f.error == "os":
            unreadable = True
        gen.update(f"{kind}\0{rel_disk}\0{f.sha or '!' + str(f.error)}\n".encode("utf-8"))
        inputs.append(f)
    external = [f.real for f in inputs if f.real is not None and not wiki_browse_fs.is_within(f.real, root)]
    return Snapshot(root=root, signature=sig, inputs=inputs, generation=gen.hexdigest()[:20],
                    unreadable=unreadable, external=external)


# ---------------------------------------------------------------------------
# 出力の名前（設計の 3.2）
# ---------------------------------------------------------------------------

WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                              *(f"COM{c}" for c in "0123456789¹²³"),
                              *(f"LPT{c}" for c in "0123456789¹²³")})
WINDOWS_BAD = set('<>:"\\|?*')
SUFFIX_RE = re.compile(r"~[0-9a-f]{8,64}$")
RESERVED_TOP = frozenset({"index.html", "nav.js", "data", "assets"})
CASE_SUFFIX_ROOM = 17   # 大文字と小文字だけが違う名前に足す接尾辞（~ と最大 16 桁）の分


def short_hash(text: str, digits: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:digits]


def is_reserved_windows(name: str) -> bool:
    return name.split(".")[0].rstrip(" .").upper() in WINDOWS_RESERVED


def safe_component(part: str, key: str, limit: int, is_dir: bool, top: bool) -> str:
    """1 つの名前の要素を、どの OS でも作れる名前にする。変えたときだけ ~<key の SHA-256 の先頭 8 桁> を付ける。"""
    changed = False
    out = []
    for ch in part:
        if ch in WINDOWS_BAD or ord(ch) < 0x20:
            out.append("_")
            changed = True
        else:
            out.append(ch)
    name = "".join(out)
    if name.rstrip(" .") != name:
        name = name.rstrip(" .")
        changed = True
    if not name:
        # 空の要素（Linux の `\tmp\a.md` は、/ にそろえると先頭が空になる）、`.`、`..` は名前にしない。
        # そのまま残すと、出力の名前が絶対パスや、書き出し先の外を指すパスになる。
        name = "_"
        changed = True
    if is_reserved_windows(name):
        # 接尾辞は末尾に付くので、最初の . より前が予約名のままになる。先頭に _ を付ける。
        name = "_" + name
        changed = True
    if SUFFIX_RE.search(name):
        changed = True
    if is_dir and name.lower().endswith(".html"):
        changed = True
    if name.startswith(".browse-"):
        changed = True
    if top and (name if is_dir else name + ".html").casefold() in RESERVED_TOP:
        changed = True
    room = 0 if is_dir else CASE_SUFFIX_ROOM
    budget = limit - len(".html") - 9 - room
    if len(name.encode("utf-8")) > budget:
        name = name.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        changed = True
    if changed:
        name = f"{name}~{short_hash(key)}"
    return name


def export_name(page_id: str, limit: int = NAME_LIMIT) -> str:
    """ページの識別子から、出力のパスを決める。返すのは、空の要素と `.`、`..` を持たない相対パスである。"""
    parts = page_id.split("/")
    out = [safe_component(p, "/".join(parts[:i + 1]) + ("/" if i < len(parts) - 1 else ""), limit,
                          is_dir=i < len(parts) - 1, top=i == 0)
           for i, p in enumerate(parts)]
    name = "/".join(out) + ".html"
    if not is_safe_relative(name):
        raise NameCollision(f"出力の名前を相対パスにできない: {page_id!r}")
    return name


def is_safe_relative(name: str) -> bool:
    """/ 区切りの相対パスで、空の要素、`.`、`..`、`\\`、NUL、ドライブ名を持たないなら真。"""
    parts = name.split("/")
    return bool(name) and all(p not in ("", ".", "..") and "\\" not in p and "\0" not in p and ":" not in p
                              for p in parts)


def _fixed_files() -> list:
    return ["index.html", *DATA_FILES, *ASSETS]


def _clashes(names: dict) -> set:
    """大文字と小文字を同一視して重なる出力の、ページの識別子の集合。生成器のファイルとの重なりと、ファイルとディレクトリの重なりも数える。"""
    files = defaultdict(list)
    for fixed in _fixed_files():
        files[fixed.casefold()].append(None)
    dirs = defaultdict(list)
    for fixed in _fixed_files():
        parts = fixed.casefold().split("/")
        for i in range(1, len(parts)):
            dirs["/".join(parts[:i])].append(None)
    for pid, name in names.items():
        files[name.casefold()].append(pid)
        parts = name.casefold().split("/")
        for i in range(1, len(parts)):
            dirs["/".join(parts[:i])].append(pid)
    bad = set()
    for key, owners in files.items():
        if len(owners) > 1:
            bad.update(o for o in owners if o is not None)
        if key in dirs:
            bad.update(o for o in owners if o is not None)
            bad.update(o for o in dirs[key] if o is not None)
    return bad


def assign_names(page_ids, limit: int = NAME_LIMIT) -> dict:
    """全ページの出力の名前を決める。重なった名前には接尾辞を足し、まだ重なれば桁を 4 桁ずつ増やす。"""
    base = {pid: export_name(pid, limit) for pid in page_ids}
    names = dict(base)
    digits = {}
    bad = _clashes(names)
    while bad:
        for pid in bad:
            d = digits.get(pid, 4) + 4
            if d > 64:
                raise NameCollision(f"出力の名前が重なる: {sorted(bad)[:5]}")
            digits[pid] = d
            names[pid] = base[pid][:-len(".html")] + f"~{short_hash(pid, d)}.html"
        bad = _clashes(names)
    return names


def rel_href(from_path: str, to_path: str) -> str:
    """出力のパスどうしの相対の href。名前の要素ごとにパーセントエンコードする。"""
    src = PurePosixPath(from_path).parent.parts
    dst = PurePosixPath(to_path).parts
    common = 0
    while common < min(len(src), len(dst) - 1) and src[common] == dst[common]:
        common += 1
    up = [".."] * (len(src) - common)
    rest = list(dst[common:])
    return "/".join(p if p == ".." else urllib.parse.quote(p, safe="") for p in up + rest) or "."


def root_prefix(path: str) -> str:
    return "../" * (len(PurePosixPath(path).parts) - 1)


# ---------------------------------------------------------------------------
# 見出し（設計の 3.4）
# ---------------------------------------------------------------------------

_ASCII_PUNCT_NOT_HYPHEN = "".join(c for c in string.punctuation if c != "-")
_PUNCT_RE = re.compile("[" + re.escape(_ASCII_PUNCT_NOT_HYPHEN) + "]")


def heading_key(text: str) -> str:
    key = nfc(text).strip().lower()
    key = re.sub(r"\s+", "-", key)
    key = _PUNCT_RE.sub("", key)
    return key or "section"


def display_href(href: str) -> str:
    """リンクにしない Markdown のリンクの行き先を、表示の文字にする（描画と見出しの鍵で同じものを使う）。"""
    return _DISPLAY.normalizeLinkText(href)


def _keeps_link(children, i: int) -> bool:
    """children[i] の link_open が、外部へのリンクとして残るなら真（描画の _transform_inline と同じ判定）。"""
    href = children[i].attrs.get("href", "")
    j = i + 1
    while j < len(children) and children[j].type != "link_close":
        if children[j].type == "text" and wiki_pages.WIKILINK_FULL_RE.search(children[j].content):
            return False
        j += 1
    try:
        scheme = urllib.parse.urlparse(href).scheme.lower()
    except ValueError:
        return False
    return scheme in EXTERNAL_SCHEMES


def inline_text(children) -> str:
    """inline の子を、表示される文字にする（wikilink は表示の文字、画像は代替テキスト、リンクにしない
    Markdown のリンクは行き先を括弧に入れて添える。描画と同じ文字になる）。"""
    out = []
    children = list(children or [])
    pending = []
    for i, c in enumerate(children):
        if c.type == "link_open":
            keep = _keeps_link(children, i) or c.markup == "autolink"
            pending.append(None if keep else display_href(c.attrs.get("href", "")))
            continue
        if c.type == "link_close":
            suffix = pending.pop() if pending else None
            if suffix is not None:
                out.append(f"（{suffix}）")
            continue
        if c.type == "text":
            out.append(wiki_pages.WIKILINK_FULL_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), c.content))
        elif c.type == "code_inline":
            out.append(c.content)
        elif c.type == "image":
            out.append(c.content)
        elif c.type in ("softbreak", "hardbreak"):
            out.append(" ")
    return "".join(out)


def assign_heading_ids(tokens) -> list:
    """見出しに ID を付け、(鍵, ID, 表示の文字) の並びを返す。ID は鍵に、重なれば連番を付けたもの。"""
    used = set()
    found = []
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        text = inline_text(tokens[i + 1].children) if i + 1 < len(tokens) else ""
        key = heading_key(text)
        candidate, n = key, 0
        while candidate in used:
            n += 1
            candidate = f"{key}-{n}"
        used.add(candidate)
        tok.attrSet("id", candidate)
        found.append((key, candidate, text))
    return found


# ---------------------------------------------------------------------------
# raw:（設計の 3.8）
# ---------------------------------------------------------------------------

def classify_raw(value: str) -> dict:
    value = str(value)
    if wiki_lint.DRIVE_PATH_RE.match(value) or value.startswith(("/", "\\", "~")):
        return {"kind": "absolute", "value": value}
    if wiki_lint.URL_SCHEME_RE.match(value):
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError:
            # `http://[` のように解釈できない URL。描くときに例外にせず、URL として文字で出す。
            return {"kind": "url", "value": value}
        if parsed.scheme.lower() == "file":
            path = urllib.parse.unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:[\\/]", path):
                path = path[1:]
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                path = f"//{parsed.netloc}{path}"
            return {"kind": "absolute", "value": path}
        return {"kind": "url", "value": value}
    return {"kind": "relative", "value": value}


def file_url(path: str) -> str:
    p = path.replace("\\", "/")
    if p.startswith("~"):
        p = os.path.expanduser(p).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "file:///" + urllib.parse.quote(p, safe="/:")
    return "file://" + urllib.parse.quote(p, safe="/:")


# ---------------------------------------------------------------------------
# 中核
# ---------------------------------------------------------------------------

def _page_id(rel: str) -> str:
    return rel[:-3] if rel.lower().endswith(".md") else rel


class Recorder:
    """描画が、利用者の内容として出した文字を、出力のファイルごとに覚える（設計の 5）。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.by_file = defaultdict(list)
        self.all = []
        # どのファイルに出てもよい利用者の内容（ページの題名。リンクの文字としてほかのページにも出る）。
        self.common = []

    def add(self, file: str, value) -> None:
        if value is None or not self.enabled:
            return
        text = str(value)
        if text:
            self.by_file[file].append(text)
            self.all.append(text)


class Core:
    """1 つの写しから組み立てた中核。描く関数は静的の書き出しとサーバーで同じものを使う。"""

    def __init__(self, snapshot: Snapshot, name_limit: int = NAME_LIMIT, record: bool = True):
        self.snapshot = snapshot
        self.generation = snapshot.generation
        self.parser = wiki_pages.new_parser()
        self.diagnostics = []
        self._body_cache = {}
        # 絶対パスの検査（静的の書き出し）のための記録。サーバーは記録しない（要求ごとに増えるので）。
        self.recorder = Recorder(record)
        self._render_lock = threading.Lock()
        self._build_pages()
        if record:
            for pid in self.drawn:
                self.recorder.common.append(self.title(pid))
                self.recorder.all.append(self.title(pid))
        self.out_name = assign_names(sorted(self.drawn), name_limit)
        self.name_to_id = {name: pid for pid, name in self.out_name.items()}
        for pid in sorted(self.drawn):
            if self.out_name[pid] != pid + ".html":
                self.diagnostics.append({"kind": "renamed", "page": pid, "output": self.out_name[pid]})
        self._prepass()
        self._backlinks()
        self._relations()
        self._nav()

    # --- ページ ---

    def _build_pages(self) -> None:
        self.pages = {}          # disk_rel -> ページ（lint の形）
        self.page_files = {}     # disk_rel -> InputFile
        by_id = defaultdict(list)
        for f in self.snapshot.by_kind("page"):
            pid = _page_id(f.rel)
            if f.text is None:
                self.diagnostics.append({"kind": "read_error", "page": pid, "reason": f.error})
                continue
            raw_meta, _fm, body, malformed = wiki_lint.parse_frontmatter_raw(f.text)
            meta, _conflicts = canonical_meta(raw_meta)
            p = {"rel_path": f.rel, "disk_rel_path": f.disk_rel, "slug": nfc(f.path.stem), "meta": meta,
                 "body": body, "malformed_fm": malformed, "id": pid, "text": f.text}
            self.pages[f.disk_rel] = p
            self.page_files[f.disk_rel] = f
            by_id[pid].append(f.disk_rel)
        self.drawn = {}          # 識別子 -> disk_rel（描くページ）
        for pid, disks in by_id.items():
            if len(disks) == 1:
                self.drawn[pid] = disks[0]
            else:
                self.diagnostics.append({"kind": "id_collision", "page": pid, "files": sorted(disks)})
        self.pages_by_path, self.pages_by_slug = wiki_pages.build_page_index(
            list(self.pages.values()), lambda p: p["disk_rel_path"])

    def page(self, pid: str) -> dict:
        return self.pages[self.drawn[pid]]

    def title(self, pid: str) -> str:
        p = self.page(pid)
        return str(p["meta"].get("title") or p["slug"])

    def resolve_disks(self, disks: list) -> "str | None":
        """当たるページの並びから、描かれているページ 1 つに決まればその識別子を返す。"""
        if len(disks) != 1:
            return None
        pid = self.pages[disks[0]]["id"]
        return pid if self.drawn.get(pid) == disks[0] else None

    def resolve(self, target: str) -> dict:
        """wikilink の行き先を解決する（設計の 3.3）。"""
        disks = wiki_pages.link_candidates(target, self.pages_by_path, self.pages_by_slug)
        if not disks:
            return {"state": "unresolved", "target": target}
        pid = self.resolve_disks(disks)
        if pid is None:
            return {"state": "ambiguous", "target": target,
                    "candidates": sorted(self.pages[d]["id"] for d in disks)}
        result = {"state": "resolved", "target": target, "id": pid}
        if "#" in target:
            anchor = nfc(target.split("#", 1)[1]).strip()
            key = heading_key(anchor)
            match = next((hid for k, hid, _t in self.headings.get(pid, []) if k == key), None)
            if match is None:
                result["missing_heading"] = anchor
            else:
                result["anchor"] = match
        return result

    # --- 本文の下ごしらえ ---

    def _prepass(self) -> None:
        self.tokens = {}
        self.headings = {}
        for pid, disk in self.drawn.items():
            tokens = self.parser.parse(self.pages[disk]["body"])
            self.tokens[pid] = tokens
            self.headings[pid] = assign_heading_ids(tokens)
        self.links = {}
        # 描かないページ（id_collision）のリンクも、lint と同じく数える。描かないので、診断は全体にだけ載せる。
        self.hidden_links = {}
        for disk, p in sorted(self.pages.items()):
            pid = p["id"]
            drawn = self.drawn.get(pid) == disk
            tokens = self.tokens[pid] if drawn else self.parser.parse(p["body"])
            found = []
            for tok in tokens:
                if tok.type == "inline":
                    for child in tok.children or []:
                        if child.type == "text" and "[[" in child.content:
                            for m in wiki_pages.WIKILINK_FULL_RE.finditer(child.content):
                                target = nfc(m.group(1).strip())
                                r = self.resolve(target)
                                r["label"] = (m.group(2) or m.group(1)).strip()
                                found.append(r)
            if drawn:
                self.links[pid] = found
            else:
                self.hidden_links[disk] = found
            for r in found:
                where = {"page": pid} if drawn else {"page": None, "source_page": pid}
                if r["state"] != "resolved":
                    self.diagnostics.append({"kind": r["state"], **where, "target": r["target"]})
                elif r.get("missing_heading"):
                    self.diagnostics.append({"kind": "missing_heading", **where, "target": r["target"]})

    def link_targets(self, pid: str) -> list:
        """そのページの本文の wikilink の行き先（lint の extract_wikilinks と同じものになる）。"""
        return [r["target"] for r in self.links[pid]]

    def unresolved(self) -> list:
        """未解決のリンクの (ページの相対パス, 行き先)。lint の broken_links と同じ集合になる。"""
        shown = [(self.page(pid)["rel_path"], r) for pid in self.drawn for r in self.links[pid]]
        hidden = [(self.pages[d]["rel_path"], r) for d, rs in self.hidden_links.items() for r in rs]
        return sorted((rel, r["target"]) for rel, r in shown + hidden if r["state"] == "unresolved")

    def _backlinks(self) -> None:
        inbound = defaultdict(set)
        for pid in self.drawn:
            for r in self.links[pid]:
                if r["state"] == "resolved" and r["id"] != pid:
                    inbound[r["id"]].add(pid)
        self.backlinks = {pid: sorted(inbound.get(pid, ())) for pid in self.drawn}

    # --- 関係表（設計の 3.6） ---

    def _relations(self) -> None:
        self.relations = {pid: {"out": [], "in": []} for pid in self.drawn}
        self.relations_state = "none"
        onto = self.snapshot.by_kind("ontology")
        if not onto:
            return
        f = onto[0]
        if f.text is None or wiki_graph_extract is None:
            self.relations_state = "error"
            self.diagnostics.append({"kind": "relations_error",
                                     "reason": "unavailable" if wiki_graph_extract is None else f.error})
            return
        try:
            ontology = wiki_graph_extract.parse_ontology(f.text)
        except Exception:  # noqa: BLE001  OntologyError のほか、思わない例外でも関係表だけを使えないにする
            self.relations_state = "error"
            self.diagnostics.append({"kind": "relations_error", "reason": "invalid"})
            return
        gpages = []
        diag = []
        for disk, p in sorted(self.pages.items()):
            # グラフの抽出と同じ読み方（wiki 0.9.5 の read_frontmatter）。最上位の欄は lint の値、graph だけを
            # PyYAML から取る（設計の 2）。PyYAML が組み立てられない値は、関係表の診断になる。
            try:
                meta, _body, problems = wiki_graph_extract.read_frontmatter(p["text"])
            except Exception:  # noqa: BLE001  そのページの関係を使えないにして、ほかのページは続ける
                meta, problems = dict(p["meta"]), [{"kind": "unreadable_graph", "reason": "unexpected"}]
                meta.pop("graph", None)
            gpages.append({"path": str(self.page_files[disk].path), "rel_path": p["rel_path"],
                           "disk_rel_path": disk, "slug": p["slug"], "meta": meta, "body": p["body"],
                           "links": wiki_pages.extract_wikilinks(p["body"]), "fm_problems": problems})
        try:
            _nodes, page_node_ids, _aliases = wiki_graph_extract.build_nodes(gpages, ontology, diag)
            edges = wiki_graph_extract.build_edges(gpages, page_node_ids, diag)
        except Exception:  # noqa: BLE001  オントロジーの中の形が誤っている（node_types が配列など）
            self.relations_state = "error"
            self.diagnostics.append({"kind": "relations_error", "reason": "invalid"})
            return
        self.relations_state = "ok"
        node_pages = defaultdict(list)
        for disk, node in page_node_ids.items():
            node_pages[node].append(disk)
        rel_to_disk = {p["rel_path"]: d for d, p in self.pages.items()}
        for e in edges:
            if e["predicate"] in ("mentions", "summarizes_raw"):
                continue
            src_pid = self._drawn_by_rel(e["page"], rel_to_disk)
            obj_disks = node_pages.get(e["object"], [])
            obj_pid = self.resolve_disks(obj_disks) if len(obj_disks) == 1 else None
            row = {"predicate": e["predicate"], "status": e.get("status", ""),
                   "valid_from": (e.get("extras") or {}).get("valid_from", ""),
                   "valid_to": (e.get("extras") or {}).get("valid_to", ""),
                   "confidence": e.get("confidence", ""), "source": e.get("source", ""),
                   "evidence": e.get("evidence", "")}
            if src_pid is not None:
                self.relations[src_pid]["out"].append({**row, "other_id": obj_pid, "other_text": e["object"]})
            if obj_pid is not None and len(obj_disks) == 1:
                self.relations[obj_pid]["in"].append({**row, "other_id": src_pid,
                                                      "other_text": _page_id(e["page"])})
        # グラフの抽出の診断を、それを書いたページに載せる（設計の 3.6）。duplicate_node_id は page を持たず、
        # pages（ディスク上の相対パスの並び）を持つので、そのそれぞれのページに載せる。
        for d in diag:
            entry = {k: v for k, v in d.items() if k not in ("page", "pages")}
            if "pages" in d:
                entry["pages"] = sorted(self.pages[x]["id"] for x in d["pages"] if x in self.pages)
            if d.get("kind") == "duplicate_node_id":
                placed = [self.pages[x]["id"] for x in d.get("pages", [])
                          if x in self.pages and self.drawn.get(self.pages[x]["id"]) == x]
                for pid in placed:
                    self.diagnostics.append({**entry, "page": pid})
                if not placed:
                    self.diagnostics.append({**entry, "page": None})
            else:
                self.diagnostics.append({**entry, "page": self._drawn_by_rel(d.get("page", ""), rel_to_disk),
                                         "source_page": _page_id(d.get("page", ""))})

    def _drawn_by_rel(self, rel: str, rel_to_disk: dict) -> "str | None":
        disk = rel_to_disk.get(rel)
        if disk is None:
            return None
        pid = self.pages[disk]["id"]
        return pid if self.drawn.get(pid) == disk else None

    # --- ナビゲーション（設計の 3.7） ---

    def _nav(self) -> None:
        self.nav_groups = []
        listed = set()
        index_files = self.snapshot.by_kind("index")
        group_of_file = {}
        for f in index_files:
            if f.text is None:
                continue   # 読めない索引のグループは作らないので、そこへのリンクも作らない
            gid = "_g" + short_hash(f.disk_rel, 10)   # NFC で同じになる 2 つの名前でも重ならない
            group_of_file.setdefault(_page_id(f.rel), gid)
        counter = [0]

        def new_group(title, gid=None):
            counter[0] += 1
            return {"id": gid or f"_g{counter[0]}", "title": title, "items": [], "children": []}

        for f in index_files:
            is_top = f.rel == "index.md"
            if f.text is None:
                self.diagnostics.append({"kind": "read_error", "page": _page_id(f.rel), "reason": f.error})
                continue
            raw_meta, _fm, body, _malformed = wiki_lint.parse_frontmatter_raw(f.text)
            meta, _c = canonical_meta(raw_meta)
            tokens = self.parser.parse(body)
            first_heading = next((inline_text(tokens[i + 1].children) for i, t in enumerate(tokens)
                                  if t.type == "heading_open" and i + 1 < len(tokens)), None)
            gid = "_g" + short_hash(f.disk_rel, 10)
            if is_top:
                top = new_group("", gid)
            else:
                top = new_group(str(meta.get("title") or first_heading or f.path.stem), gid)
            self.recorder.add("nav.js", top["title"])
            stack = [(0, top)]
            pending = []
            for i, tok in enumerate(tokens):
                if tok.type == "heading_open":
                    level = int(tok.tag[1])
                    text = inline_text(tokens[i + 1].children)
                    self.recorder.add("nav.js", text)
                    while len(stack) > 1 and stack[-1][0] >= level:
                        stack.pop()
                    g = new_group(text)
                    stack[-1][1]["children"].append(g)
                    stack.append((level, g))
                elif tok.type == "list_item_open":
                    pending.append(True)
                elif tok.type == "list_item_close":
                    if pending:
                        pending.pop()
                elif tok.type == "inline" and pending and pending[-1]:
                    pending[-1] = False
                    item = self._nav_item(tok, group_of_file)
                    if item is not None:
                        stack[-1][1]["items"].append(item)
                        if item.get("id"):
                            listed.add(item["id"])
            self.nav_groups.append(top)
        rest = defaultdict(list)
        for pid in sorted(self.drawn):
            if pid not in listed:
                rest[str(self.page(pid)["meta"].get("type") or "")].append(pid)
        if rest:
            g = new_group("索引にないページ")
            for t in sorted(rest):
                sub = new_group(t or "種類なし")
                sub["items"] = [{"state": "resolved", "id": pid, "label": self.title(pid)} for pid in rest[t]]
                g["children"].append(sub)
            self.nav_groups.append(g)

    def _nav_item(self, inline_tok, group_of_file) -> "dict | None":
        for child in inline_tok.children or []:
            if child.type == "text" and "[[" in child.content:
                m = wiki_pages.WIKILINK_FULL_RE.search(child.content)
                if not m:
                    continue
                target = nfc(m.group(1).strip())
                label = (m.group(2) or m.group(1)).strip()
                self.recorder.add("nav.js", target)
                self.recorder.add("nav.js", label)
                clean = wiki_pages.clean_link_target(target)
                if clean in group_of_file and clean.startswith("indexes/"):
                    return {"state": "group", "group": group_of_file[clean], "label": label}
                r = self.resolve(target)
                if r["state"] == "resolved":
                    return {"state": "resolved", "id": r["id"], "label": self.title(r["id"]),
                            "anchor": r.get("anchor")}
                return {"state": r["state"], "label": label}
        return None

    # --- 描画 ---

    def page_names(self) -> list:
        return sorted(self.out_name.values())

    def knows(self, name: str) -> bool:
        return name in self.name_to_id or name in ("index.html", *DATA_FILES)

    def _link_html(self, path: str, r: dict) -> str:
        label = html.escape(r["label"])
        if r["state"] == "resolved":
            href = rel_href(path, self.out_name[r["id"]])
            if r.get("anchor"):
                href += "#" + urllib.parse.quote(r["anchor"], safe="")
            cls = "wl" + (" wl-missing-heading" if r.get("missing_heading") else "")
            return f'<a class="{cls}" href="{html.escape(href)}">{label}</a>'
        if r["state"] == "ambiguous":
            return f'<span class="wl wl-ambiguous" title="行き先を 1 つに決められない">{label}</span>'
        return f'<span class="wl wl-unresolved" title="ページがない">{label}</span>'

    def _transform_inline(self, children, path: str, counter: list, file: str) -> list:
        out = []
        i = 0
        while i < len(children):
            c = children[i]
            if c.type == "link_open":
                j = i + 1
                depth = 1
                while j < len(children):
                    if children[j].type == "link_open":
                        depth += 1
                    elif children[j].type == "link_close":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                inner = children[i + 1:j]
                href = c.attrs.get("href", "")
                self.recorder.add(file, href)
                self.recorder.add(file, display_href(href))
                try:
                    scheme = urllib.parse.urlparse(href).scheme.lower()
                except ValueError:
                    scheme = ""
                has_wl = any(t.type == "text" and wiki_pages.WIKILINK_FULL_RE.search(t.content) for t in inner)
                if scheme in EXTERNAL_SCHEMES and not has_wl:
                    c.attrSet("rel", "noopener noreferrer")
                    c.attrSet("class", "ext")
                    out.append(c)
                    out.extend(self._transform_inline(inner, path, counter, file))
                    if j < len(children):
                        out.append(children[j])
                else:
                    out.extend(self._transform_inline(inner, path, counter, file))
                    if c.markup != "autolink":
                        out.append(Token("text", "", 0, content=f"（{display_href(href)}）"))
                i = j + 1
                continue
            if c.type == "image":
                self.recorder.add(file, c.content)
                self.recorder.add(file, c.attrs.get("src"))
                out.append(Token("text", "", 0, content=c.content))
                i += 1
                continue
            if c.type == "text" and "[[" in c.content and wiki_pages.WIKILINK_FULL_RE.search(c.content):
                self.recorder.add(file, c.content)
                pos = 0
                for m in wiki_pages.WIKILINK_FULL_RE.finditer(c.content):
                    if m.start() > pos:
                        out.append(Token("text", "", 0, content=c.content[pos:m.start()]))
                    r = counter[1][counter[0]]
                    counter[0] += 1
                    out.append(Token("html_inline", "", 0, content=self._link_html(path, r)))
                    pos = m.end()
                if pos < len(c.content):
                    out.append(Token("text", "", 0, content=c.content[pos:]))
                i += 1
                continue
            if c.type in ("text", "code_inline"):
                self.recorder.add(file, c.content)
            out.append(c)
            i += 1
        return out

    def body_html(self, pid: str) -> str:
        if pid in self._body_cache:
            return self._body_cache[pid]
        path = self.out_name[pid]
        tokens = self.tokens.pop(pid)
        counter = [0, self.links[pid]]
        for tok in tokens:
            if tok.type in ("th_open", "td_open") and "style" in (tok.attrs or {}):
                style = tok.attrs.pop("style")
                m = re.search(r"text-align:(left|center|right)", style)
                if m:
                    tok.attrSet("class", f"ta-{m.group(1)}")
            if tok.type in ("fence", "code_block"):
                self.recorder.add(path, tok.content)
            if tok.type == "inline" and tok.children:
                tok.children = self._transform_inline(tok.children, path, counter, path)
        rendered = self.parser.renderer.render(tokens, self.parser.options, {})
        self._body_cache[pid] = rendered
        return rendered

    def _meta_rows(self, pid: str, path: str, raw_mode: str) -> str:
        meta = self.page(pid)["meta"]
        rows = []
        for key, label in (("type", "種類"), ("tags", "タグ"), ("summary", "要約"),
                           ("created", "作成"), ("updated", "更新")):
            value = meta.get(key)
            if value in (None, "", []):
                continue
            text = "、".join(map(str, value)) if isinstance(value, list) else str(value)
            self.recorder.add(path, text)
            rows.append(f"<tr><th>{label}</th><td>{html.escape(text)}</td></tr>")
        sources = wiki_pages.ref_values(meta.get("sources"))
        if sources:
            cells = []
            for ref in sources:
                self.recorder.add(path, ref)
                _status, found, _t = wiki_pages.classify_source_ref(
                    ref, self.pages_by_path, self.pages_by_slug, lambda d: self.pages[d]["meta"].get("type"))
                sp = self.resolve_disks(found)
                if sp is not None:
                    cells.append(f'<a href="{html.escape(rel_href(path, self.out_name[sp]))}">'
                                 f"{html.escape(self.title(sp))}</a>")
                else:
                    cells.append(f'<span class="wl wl-unresolved">{html.escape(ref)}</span>')
            rows.append(f"<tr><th>出典</th><td>{'、'.join(cells)}</td></tr>")
        raws = wiki_pages.ref_values(meta.get("raw"))
        if raws and raw_mode != "none":
            sha = str(meta.get("raw_sha256") or "")
            cells = [self._raw_html(classify_raw(v), sha, raw_mode, path) for v in raws]
            rows.append(f"<tr><th>原本</th><td>{'<br>'.join(cells)}</td></tr>")
        return "".join(rows)

    def _raw_html(self, raw: dict, sha: str, mode: str, path: str) -> str:
        value = raw["value"]
        if raw["kind"] != "absolute" or mode == "text":
            self.recorder.add(path, value)
            return f"<code>{html.escape(value)}</code>"
        if mode == "link":
            self.recorder.add(path, value)
            if value.startswith("~"):
                # file_url が ~ をホームに展開する。展開したパスも、利用者が選んだ raw の値として通す（設計の 5）。
                self.recorder.add(path, os.path.expanduser(value))
            return f'<a class="raw" href="{html.escape(file_url(value))}">{html.escape(value)}</a>'
        base = re.split(r"[\\/]", value)[-1]
        tail = f"（SHA-256 {html.escape(sha[:12])}…）" if sha else ""
        return f"<code>{html.escape(base)}</code>{tail}"

    def _relations_html(self, pid: str, path: str) -> str:
        if self.relations_state == "none":
            return ""
        if self.relations_state == "error":
            return '<section id="_relations"><h2>関係</h2><p class="state-error">オントロジーを読めない</p></section>\n'
        rel = self.relations[pid]
        if not rel["out"] and not rel["in"]:
            return '<section id="_relations"><h2>関係</h2><p class="state-empty">関係はない</p></section>\n'
        rows = []
        for direction, label in (("out", "→"), ("in", "←")):
            for r in rel[direction]:
                if r["other_id"] is not None:
                    other = (f'<a href="{html.escape(rel_href(path, self.out_name[r["other_id"]]))}">'
                             f'{html.escape(self.title(r["other_id"]))}</a>')
                else:
                    other = html.escape(str(r["other_text"]))
                cells = [label, html.escape(str(r["predicate"])), other]
                for key in ("status", "valid_from", "valid_to", "confidence", "source", "evidence"):
                    value = str(r.get(key) or "")
                    self.recorder.add(path, value)
                    cell = html.escape(value)
                    if key == "source" and value:
                        found = self.resolve(nfc(value))
                        if found["state"] == "resolved":
                            cell = (f'<a href="{html.escape(rel_href(path, self.out_name[found["id"]]))}">'
                                    f"{html.escape(self.title(found['id']))}</a>")
                    cells.append(cell)
                self.recorder.add(path, r["other_text"])
                rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        head = "".join(f"<th>{h}</th>" for h in ("向き", "述語", "相手", "状態", "開始", "終了", "確からしさ",
                                                 "出典", "証拠"))
        return (f'<section id="_relations"><h2>関係</h2><table class="relations"><thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(rows)}</tbody></table></section>\n")

    def page_diagnostics(self, pid: str) -> list:
        return [d for d in self.diagnostics if d.get("page") == pid]

    def _diagnostics_html(self, items: list, path: str) -> str:
        if not items:
            return ""
        lis = []
        for d in items:
            parts = [d["kind"]]
            for key in ("target", "reason", "output", "node_id", "predicate", "object", "source", "source_page"):
                if d.get(key):
                    parts.append(f"{key}: {d[key]}")
                    if key != "reason":   # reason は生成器が決める文字なので、利用者の内容に数えない
                        self.recorder.add(path, d[key])
            lis.append(f"<li>{html.escape(' / '.join(map(str, parts)))}</li>")
        return f'<section id="_diagnostics"><h2>診断</h2><ul>{"".join(lis)}</ul></section>\n'

    def _head(self, path: str, title: str, stale: "str | None") -> str:
        prefix = root_prefix(path)
        self.recorder.add(path, title)
        banner = ""
        if stale:
            banner = (f'<p id="_stale" class="stale">Wiki の最新の内容を表示できていない。前の内容を表示している'
                      f"（{html.escape(stale)}）</p>\n")
        return (f'<!doctype html>\n<html lang="ja" data-root="{prefix}" data-generation="{self.generation}">\n'
                f'<head>\n<meta charset="utf-8">\n'
                f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
                f'<meta name="referrer" content="no-referrer">\n'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f"<title>{html.escape(title)}</title>\n"
                f'<link rel="stylesheet" href="{prefix}assets/style.css">\n</head>\n<body>\n'
                f'<nav id="_nav"><noscript><a href="{prefix}index.html">索引へ</a></noscript></nav>\n'
                f'<main id="_main">\n{banner}')

    def _tail(self, path: str, extra_data: tuple = ()) -> str:
        prefix = root_prefix(path)
        scripts = "".join(f'<script src="{prefix}{name}"></script>\n'
                          for name in ("nav.js", "data/manifest.js", *extra_data, "assets/app.js"))
        return f"</main>\n{scripts}</body>\n</html>\n"

    def render_page(self, pid: str, raw_mode: str = "text", stale: "str | None" = None) -> str:
        path = self.out_name[pid]
        title = self.title(pid)
        back = "".join(f'<li><a href="{html.escape(rel_href(path, self.out_name[b]))}">'
                       f"{html.escape(self.title(b))}</a></li>" for b in self.backlinks[pid])
        for b in self.backlinks[pid]:
            self.recorder.add(path, self.title(b))
        diags = list(self.page_diagnostics(pid))
        if stale:
            diags.insert(0, {"kind": "stale", "reason": stale})
        return (self._head(path, title, stale)
                + f'<article id="_article" data-page-id="{html.escape(pid)}">\n'
                + f'<table id="_meta" class="meta">{self._meta_rows(pid, path, raw_mode)}</table>\n'
                + f'<div id="_body">\n{self.body_html(pid)}</div>\n</article>\n'
                + f'<section id="_backlinks"><h2>被リンク</h2><ul>{back}</ul></section>\n'
                + self._relations_html(pid, path)
                + self._diagnostics_html(diags, path)
                + self._tail(path))

    def _nav_html(self, groups, path: str) -> str:
        parts = []
        for g in groups:
            items = []
            for it in g["items"]:
                label = html.escape(it["label"])
                if it["state"] == "resolved":
                    href = rel_href(path, self.out_name[it["id"]])
                    if it.get("anchor"):
                        href += "#" + urllib.parse.quote(it["anchor"], safe="")
                    items.append(f'<li><a href="{html.escape(href)}">{label}</a></li>')
                elif it["state"] == "group":
                    items.append(f'<li><a href="#{html.escape(it["group"])}">{label}</a></li>')
                else:
                    items.append(f'<li><span class="wl wl-{it["state"]}">{label}</span></li>')
            title = f"<h2>{html.escape(g['title'])}</h2>" if g["title"] else ""
            parts.append(f'<section class="nav-group" id="{html.escape(g["id"])}">{title}'
                         f'<ul>{"".join(items)}</ul>{self._nav_html(g["children"], path)}</section>')
        return "".join(parts)

    def render_index(self, stale: "str | None" = None) -> str:
        path = "index.html"
        diags = list(self.diagnostics)
        if stale:
            diags.insert(0, {"kind": "stale", "reason": stale})
        return (self._head(path, "索引", stale)
                + f'<h1>索引</h1>\n<div id="_index">{self._nav_html(self.nav_groups, path)}</div>\n'
                + self._diagnostics_html(diags, path)
                + self._tail(path, ("data/diagnostics.js",)))

    # --- データのファイル ---

    def _nav_json(self) -> dict:
        def conv(groups):
            out = []
            for g in groups:
                items = []
                for it in g["items"]:
                    item = {"state": it["state"], "label": it["label"]}
                    if it["state"] == "resolved":
                        item["path"] = self.out_name[it["id"]]
                        if it.get("anchor"):
                            item["anchor"] = it["anchor"]
                    elif it["state"] == "group":
                        item["group"] = it["group"]
                    items.append(item)
                out.append({"id": g["id"], "title": g["title"], "items": items, "children": conv(g["children"])})
            return out
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation, "groups": conv(self.nav_groups)}

    def manifest(self, generated_at: str = "") -> dict:
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation, "generated_at": generated_at,
                "capabilities": {"search": "none", "relations": self.relations_state, "rag": False,
                                 "multi_wiki": False, "live_reload": False},
                "pages": len(self.drawn)}

    def diagnostics_json(self) -> dict:
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation, "items": self.diagnostics}

    def render_data(self, name: str, generated_at: str = "", stale: "str | None" = None) -> str:
        if name == "nav.js":
            return js_data("WIKI_NAV", self._nav_json())
        if name == "data/manifest.js":
            m = self.manifest(generated_at)
            if stale:
                m["stale"] = stale
            return js_data("WIKI_MANIFEST", m)
        if name == "data/diagnostics.js":
            d = self.diagnostics_json()
            if stale:
                d["items"] = [{"kind": "stale", "reason": stale}] + d["items"]
            return js_data("WIKI_DIAGNOSTICS", d)
        raise KeyError(name)

    def render(self, name: str, raw_mode: str = "text", generated_at: str = "",
               stale: "str | None" = None) -> str:
        """出力の名前 1 つを描く。中核が知らない名前なら KeyError。ほかの要求と同時に呼んでよい。"""
        with self._render_lock:
            return self._render(name, raw_mode, generated_at, stale)

    def _render(self, name: str, raw_mode: str, generated_at: str, stale: "str | None") -> str:
        if name == "index.html":
            return self.render_index(stale)
        if name in DATA_FILES:
            return self.render_data(name, generated_at, stale)
        return self.render_page(self.name_to_id[name], raw_mode, stale)

    def render_all(self, raw_mode: str, generated_at: str = "") -> dict:
        out = {}
        for name in self.page_names():
            out[name] = self.render(name, raw_mode)
        out["index.html"] = self.render_index()
        for name in DATA_FILES:
            out[name] = self.render_data(name, generated_at)
        return out


def js_data(name: str, value) -> str:
    """<script src> で読むデータ。</、U+2028、U+2029 をエスケープする。"""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    text = text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"window.{name}={text};\n"


def asset_bytes(name: str) -> bytes:
    return (ASSET_DIR / PurePosixPath(name).name).read_bytes()


# ---------------------------------------------------------------------------
# 絶対パスの検査（設計の 5）
# ---------------------------------------------------------------------------

def path_forms(path: str) -> list:
    """絶対パスを、出力に現れうる形にする。"""
    posix = str(path).replace("\\", "/").rstrip("/")
    win = posix.replace("/", "\\")
    forms = {posix, win, win.replace("\\", "\\\\"), posix.replace("/", "\\/")}
    forms |= {html.escape(f) for f in list(forms)}
    forms.add(urllib.parse.quote(posix, safe=""))
    forms.add(urllib.parse.quote(posix, safe="/:"))
    return sorted(f for f in forms if len(f) >= 4)


def find_leaks(files: dict, needles: list, recorder: Recorder) -> tuple:
    """出力のファイルから絶対パスを探す。(生成器が入れたもの, 利用者が書いたもの) を返す。

    検索対象の絶対パスごとに判定する。あるファイルで、どれか 1 つでも、そのファイルの利用者の内容の記録に
    ないパスが見つかれば、生成器が入れたものとする。
    """
    fold = os.name == "nt" or sys.platform == "darwin"
    norm = (lambda s: s.lower()) if fold else (lambda s: s)
    leaks, warnings = set(), set()
    for name, content in files.items():
        text = norm(content)
        aggregate = name in ("index.html", *DATA_FILES)
        recorded = (recorder.all if aggregate else recorder.by_file.get(name, [])) + recorder.common
        recorded = [norm(r.replace(BS * 2, BS)) for r in recorded]
        for needle in needles:
            if not any(norm(f) in text for f in path_forms(needle)):
                continue
            plain = [norm(str(needle).replace(BS, "/").rstrip("/")),
                     norm(str(needle).replace("/", BS).rstrip(BS))]
            if any(q in r for r in recorded for q in plain):
                warnings.add(name)
            else:
                leaks.add(name)
    return sorted(leaks), sorted(warnings - leaks)


BS = chr(92)
