"""raw の参照を、リンクを読まずに解く（info-hive-dev の ADR-0029、段階2）。

根は呼ぶ側が渡す。候補の所属を決める前に realpath を呼ばない。リンクの検査を全候補で終えてから、
必要なら版と身元を照らす。書き込みと raw の根の記録の作成は、この部品の役割ではない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from wiki_links import link_kind

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass
class RawResolution:
    """値ごとの結果。拒否と検査不能を、missing に落とさない。"""
    raw: str
    state: str
    path: Path | None = None
    candidates: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    unchecked: list[dict] = field(default_factory=list)
    parents_unchecked: list[str] = field(default_factory=list)
    version_unverified: bool = False
    reason: str = ""
    external: bool = False
    scope_unverified: bool = False
    mismatches: list[dict] = field(default_factory=list)


def within(path, root) -> bool:
    """綴りのうえで、根そのものかその下かを比べる。"""
    try:
        p, r = (os.path.normcase(os.path.abspath(os.fspath(v))) for v in (path, root))
        return os.path.commonpath((p, r)) == r
    except ValueError:
        return False


def raw_path_is_safe(ref: str, *, absolute: bool) -> bool:
    """先頭の .. だけを基準から畳む。途中の .. は、前のリンクを OS にたどらせる。"""
    if "\x00" in ref:
        return False
    meaningful = False
    for part in Path(ref).parts:
        if part == ".." and (absolute or meaningful):
            return False
        if part not in ("..", "."):
            meaningful = True
    return True


def _roots(values) -> list[Path]:
    from wiki_pages import absolute_user_path
    result = []
    for value in values:
        given = absolute_user_path(Path(value).expanduser())
        try:
            if not stat.S_ISDIR(given.stat().st_mode):
                raise ValueError(f"根がディレクトリでない: {given}")
        except FileNotFoundError:
            pass  # まだない根も、場所の制限として使える
        for root in (given, given.resolve()):  # 利用者が決めた根だけは解決してよい
            if root not in result:
                result.append(root)
    return result


def file_identity(path: Path, st):
    from wiki_pages import win_file_id
    if os.name == "nt":
        return win_file_id(path)
    if not st.st_ino:
        raise OSError(f"ファイルの識別子を取れない: {path}")
    return st.st_dev, st.st_ino


def _inspect(path: Path, origin: Path | None) -> dict:
    """道とディレクトリの全記事を調べる。ここでは本文を開かない。"""
    from wiki_pages import _list_dir, _stat_entry, _sort_key
    result = {"links": [], "unchecked": [], "files": [], "stat": None}

    def inspect_one(p, *, listed=False):
        try:
            state, st = _stat_entry(str(p))
        except (OSError, ValueError) as exc:
            result["unchecked"].append({"path": str(p), "reason": str(exc)})
            return None
        if state == "denied":
            result["unchecked"].append({"path": str(p), "reason": "属性を取れない"})
            return None
        if state == "absent":
            if listed:
                result["unchecked"].append({"path": str(p), "reason": "一覧にあった項目の属性を取れない"})
            return None
        kind = link_kind(st)
        if kind:
            result["links"].append({"path": str(p), "kind": kind,
                                    "root": str(origin) if origin else None,
                                    "relative_path": p.relative_to(origin).as_posix() if origin else None})
            return None
        return st

    if origin is not None and path == origin:
        try:
            st = path.stat()  # 根そのものと祖先のリンクを許す
        except FileNotFoundError:
            return result
        except OSError as exc:
            result["unchecked"].append({"path": str(path), "reason": str(exc)})
            return result
    else:
        parts = path.relative_to(origin).parts if origin else (path.name,)
        current = origin if origin else path.parent
        for i, part in enumerate(parts):
            current = current / part
            st = inspect_one(current)
            if st is None:
                return result
            if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                return result
    result["stat"] = st
    if stat.S_ISREG(st.st_mode):
        result["files"] = [("", path)]
        return result
    if not stat.S_ISDIR(st.st_mode):
        result["unchecked"].append({"path": str(path), "reason": "通常のファイルかディレクトリでない"})
        return result
    stack, seen = [(path, st)], set()
    while stack:
        directory, dst = stack.pop()
        # 待っている間に置き換わった道を、一覧を取る直前にもう一度調べる。根のリンクの例外は保つ。
        if directory != origin:
            dst = inspect_one(directory, listed=True)
            if dst is None:
                continue
            if not stat.S_ISDIR(dst.st_mode):
                result["unchecked"].append({"path": str(directory), "reason": "走査待ちの道がディレクトリでなくなった"})
                continue
        try:
            ident = file_identity(directory, dst)
            if ident in seen:
                raise OSError("ディレクトリの実体が重複している")
            seen.add(ident)
            entries = _list_dir(str(directory))
        except (OSError, ValueError) as exc:
            result["unchecked"].append({"path": str(directory), "reason": str(exc)})
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            p = Path(entry.path)
            est = inspect_one(p, listed=True)
            if est is None:
                continue
            if stat.S_ISDIR(est.st_mode):
                stack.append((p, est))
            elif stat.S_ISREG(est.st_mode):
                result["files"].append((p.relative_to(path).as_posix(), p))
            else:
                result["unchecked"].append({"path": str(p), "reason": "通常のファイルでない"})
    result["files"].sort(key=lambda item: _sort_key(item[0]))
    return result


def _file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _digest(inspection) -> str:
    st = inspection["stat"]
    if stat.S_ISREG(st.st_mode):
        return _file_digest(inspection["files"][0][1])
    rows = [json.dumps([rel, _file_digest(p)], ensure_ascii=False)
            for rel, p in inspection["files"]]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def raw_digest(path, origin=None) -> str:
    """ファイルのバイト列、または隠し項目を除くディレクトリの一覧の SHA-256。拒否・検査不能なら失敗する。"""
    from wiki_pages import absolute_user_path
    path = absolute_user_path(path)
    origin = absolute_user_path(origin) if origin is not None else None
    if origin is not None and not within(path, origin):
        raise ValueError("raw が根の下にない")
    inspected = _inspect(path, origin)
    if inspected["links"] or inspected["unchecked"]:
        raise ValueError(f"raw を読まない: {inspected['links'] or inspected['unchecked']}")
    if inspected["stat"] is None:
        raise FileNotFoundError(path)
    return _digest(inspected)


def _raw_meta(path):
    from wiki_text import canonical_meta, parse_frontmatter_text
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return {}  # バイナリの原本には、照合できる frontmatter がない
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return canonical_meta(parse_frontmatter_text("\n".join(lines[1:i])))[0]
    return {}  # 本文が水平線で始まる原本もある。身元を証明できないだけで、欠落ではない


def resolve_raw(meta, page_path, wiki_root, wiki_root_given=None, allowed_roots=None, *,
                raw_roots=(), recorded_raw_roots=()):
    """source の raw: を値ごとに解く。raw_roots は呼ぶ側が知っている根だけを渡す。推し量らない。

    allowed_roots は内容を読んでよい場所の制限で、リンク検査の起点ではない。外の原本にも当てる。
    結果は resolved / missing / ambiguous / unknown / link / outside。URL は outside とする。
    """
    from wiki_pages import ref_values, type_value, absolute_user_path
    if type_value(meta.get("type")) != "source":
        return []
    refs = ref_values(meta.get("raw"))
    sessions, urls = ref_values(meta.get("session_id")), ref_values(meta.get("url"))
    hashes = ref_values(meta.get("raw_sha256"))
    aligned = len(refs) == len(hashes) and all(_SHA256.fullmatch(h) for h in hashes)
    if not aligned and (len(sessions) > 1 or len(urls) > 1):
        return [RawResolution(ref, "unknown", reason="版を照合できないとき、session_id と url は値を1つだけ指定する") for ref in refs]
    session = sessions[0] if len(sessions) == 1 else None
    url = urls[0] if len(urls) == 1 else None
    try:
        given = absolute_user_path(Path(wiki_root_given if wiki_root_given is not None else wiki_root).expanduser())
        root = absolute_user_path(Path(wiki_root).expanduser())
        wiki_roots = _roots((given, root))
        known_raw = _roots(raw_roots)
        # 記録は保存時点の綴りと実体を持つ。現在の実体へ解決し直さない。
        historical = tuple(recorded_raw_roots)
        allowed = _roots(allowed_roots) if allowed_roots is not None else None
        page = absolute_user_path(page_path)
        rel_page = next((page.relative_to(w) for w in wiki_roots if within(page, w)), None)
    except (OSError, ValueError, RuntimeError) as exc:
        return [RawResolution(ref, "unknown", reason=str(exc)) for ref in refs]
    results = []
    for index, ref in enumerate(refs):
        result = RawResolution(ref, "missing")
        results.append(result)
        absolute = bool(_DRIVE.match(ref) or ref.startswith(("/", "\\", "~")))
        if not absolute and _SCHEME.match(ref):
            result.state, result.reason = "outside", "URL は raw のファイルとして読まない"
            continue
        if not raw_path_is_safe(ref, absolute=absolute):
            result.state, result.reason = "unknown", "途中の .. か NUL を含む raw: は解けない"
            continue
        if os.name != "nt" and (_DRIVE.match(ref) or ref.startswith("\\")):
            result.reason = "この OS では参照できない絶対パス"
            continue
        try:
            if absolute:
                candidates = [Path(os.path.abspath(Path(ref).expanduser()))]
            else:
                bases = []
                for w in wiki_roots:
                    bases.append(w.parent)
                    if not sessions:
                        bases.extend((w, w / rel_page.parent if rel_page is not None else page.parent))
                candidates = list(dict.fromkeys(Path(os.path.abspath(b / ref)) for b in bases))
        except (OSError, ValueError, RuntimeError) as exc:
            result.state, result.reason = "unknown", str(exc)
            continue
        result.candidates = [str(p) for p in candidates]
        inspected, excluded = [], []
        for p in candidates:
            if allowed is not None and not any(within(p, a) for a in allowed):
                excluded.append(p)
                continue
            raw_matches = [r for r in known_raw if within(p, r)]
            wiki_matches = [w for w in wiki_roots if within(p, w)]
            origin = max(raw_matches or wiki_matches, key=lambda r: len(r.parts), default=None)
            # 過去の根は検査範囲を広げる。既知の根の途中のリンクを検査から外さない。
            if not raw_matches:
                historical_matches = [r for r in historical if within(p, r)]
                if origin is not None:
                    historical_matches.append(origin)
                origin = min(historical_matches, key=lambda r: len(r.parts), default=None)
            external = bool(meta.get("rag_job_id") and absolute and origin is None)
            relative = p.relative_to(origin).parts if origin else (p.name,)
            if not external and any(part.startswith(".") for part in relative):
                excluded.append(p)
                continue
            if external:
                # 一致しない過去の記録だけでは、現在の raw の範囲は分からない。
                result.scope_unverified = not known_raw
                # 外の原本は記事ではない。リンクを含め、raw の版もここでは読まない。
                try:
                    st = p.stat()
                    if stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode):
                        inspected.append((p, {"stat": st}, True))
                except (FileNotFoundError, NotADirectoryError):
                    pass
                except OSError as exc:
                    result.unchecked.append({"path": str(p), "reason": str(exc)})
                continue
            scan = _inspect(p, origin)
            if origin is None and (scan["stat"] is not None or scan["links"] or scan["unchecked"]):
                result.parents_unchecked.append(str(p))
            result.links.extend(scan["links"])
            result.unchecked.extend(scan["unchecked"])
            if scan["stat"] is not None:
                inspected.append((p, scan, False))
        # どの候補でも、拒否を版違いや「ない」に落とさない。本文の読み取りはこのあとだけである。
        if result.links:
            result.state, result.reason = "link", "raw か途中の道がリンクなので読まない"
            continue
        if result.unchecked:
            result.state, result.reason = "unknown", "raw を調べられない"
            continue
        matched = []
        for p, scan, external in inspected:
            try:
                if not external:
                    if aligned:
                        if _digest(scan).lower() != hashes[index].lower():
                            result.mismatches.append({"path": str(p), "kind": "version", "reason": "raw_sha256 と一致しない"})
                            continue
                    else:
                        result.version_unverified = True
                        if stat.S_ISREG(scan["stat"].st_mode) and (session or url):
                            rm = _raw_meta(p)
                            actual_session = ref_values(rm.get("session_id"))
                            actual_urls = ref_values(rm.get("url")) + ref_values(rm.get("final_url"))
                            if session:
                                if actual_session and session not in actual_session:
                                    result.mismatches.append({"path": str(p), "kind": "identity", "reason": "session_id と一致しない"})
                                    continue
                            elif actual_urls and url not in actual_urls:
                                result.mismatches.append({"path": str(p), "kind": "identity", "reason": "url / final_url と一致しない"})
                                continue
                matched.append((p, external, scan["stat"]))
            except (OSError, UnicodeError, ValueError) as exc:
                result.unchecked.append({"path": str(p), "reason": str(exc)})
        # 候補が1つだけなら実体の比較は不要。複数の候補を同じ実体にまとめるときだけ識別子を求める。
        if len(matched) > 1:
            identities = {}
            for p, external, st in matched:
                try:
                    identities.setdefault(file_identity(p, st), (p, external, st))
                except OSError as exc:
                    result.unchecked.append({"path": str(p), "reason": str(exc)})
            matched = list(identities.values())
        if result.unchecked:
            result.state, result.reason = "unknown", "raw の版か身元を確かめられない"
        elif len(matched) == 1:
            result.path, result.external, _st = matched[0]
            result.state = "resolved"
        elif len(matched) > 1:
            result.state, result.reason = "ambiguous", "別の実体の raw が複数ある"
            result.candidates = [str(p) for p, _, _ in matched]
        elif excluded:
            result.state, result.reason = "outside", "許した根の外か、隠し項目の候補がある"
        else:
            result.reason = "版と身元を照らして一致する raw がない"
    return results
