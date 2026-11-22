"""承認したライブ wiki の変更と公開版を日誌で適用する共通の書く段。"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import unicodedata
import uuid
from types import SimpleNamespace

from wiki_fsguard import Refused, WriteGuard, external_raw_guard
from wiki_initialized import require
from wiki_raw_roots import read_raw_roots


AREA = Path(".wiki-cache/apply")
MARKER = ".wiki-publication"
HEAD = "publication-head.json"
JOURNAL = "publication.journal.json"
MARKER_BYTES = b"wiki-publication/1\n"
RETURN_JOURNAL = "return-live.journal.json"
RETURN_RECEIPT = "return-live.json"
LIVE_OWNER = "live-owner.json"
LIVE_JOURNAL = "live-journal.json"
LIVE_TRANSACTION = "live-transaction.json"
PENDING = Path(".wiki-cache/apply-pending")
ID = re.compile(r"[0-9a-f]{64}")
PLAN_ID = re.compile(r"[0-9a-f]{32}")
DEFINITION_FILES = ("graph/ontology.yaml", "ontology/definition.json", "ontology/ossie/wiki.ontology.json")
REQUIRED = {
    "SCHEMA.md", "index.md", "log.md", "graph/graph.sqlite", *DEFINITION_FILES,
    "ontology/ossie/wiki.semantic_model.json", "ontology/ossie/wiki.mapping.json",
    "ontology/ossie/wiki-data.sqlite", "ontology/ossie/records.jsonl", "ontology/ossie/release.json",
}
REPLACE_DELAYS = (0.01, 0.05, 0.1)


class PublicationError(ValueError):
    pass


class Conflict(PublicationError):
    pass


class RetiredPublication(PublicationError):
    """有効な公開履歴にあるが、配信から取り下げた版を指定した。"""


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked(value, pattern=ID):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PublicationError("公開版・計画の識別子が不正です")
    return value


def relative(value):
    if not isinstance(value, str) or any(ch in value for ch in "\\:\0"):
        raise PublicationError("成果物の相対パスが不正です")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or path.as_posix() != value or ".." in path.parts
            or path.parts[0].casefold() in {".wiki-cache", ".git", "raw", MARKER}):
        raise PublicationError(f"成果物に使えないパスです: {value}")
    return Path(*path.parts)


def verify_files(files):
    if not isinstance(files, dict) or not REQUIRED <= files.keys():
        raise PublicationError("公開版に必要な wiki のファイルが揃っていません")
    verify_file_names(files)


def verify_file_names(files):
    names = {}
    leaf_keys = set()
    for name, checksum in files.items():
        path = relative(name)
        checked(checksum)
        for part in (*path.parents, path):
            key = unicodedata.normalize("NFC", part.as_posix().casefold())
            if names.setdefault(key, part.as_posix()) != part.as_posix():
                raise PublicationError("公開ファイル名に正規化後の衝突があります")
        leaf = unicodedata.normalize("NFC", path.as_posix().casefold())
        if leaf in leaf_keys:
            raise PublicationError("公開ファイルに同じ位置の名前があります")
        leaf_keys.add(leaf)
    if any(unicodedata.normalize("NFC", p.as_posix().casefold()) in leaf_keys
           for name in files for p in relative(name).parents if p != Path(".")):
        raise PublicationError("公開ファイルとディレクトリが衝突しています")


def definition_fingerprint(files):
    return digest(encoded({name: files[name] for name in DEFINITION_FILES}))


def sync_directory(directory):
    # POSIX ではファイルの内容に加え、親ディレクトリに公開した名前も同期する。
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read(guard, file):
    data = guard.read_bytes(file)
    if data is None:
        raise PublicationError(f"公開に必要なファイルがありません: {file.name}")
    return data


def current(guard, area):
    # Windows で読取りと置換が競合したときは、名前の検査からやり直す。
    for attempt in range(3):
        try:
            data = guard.read_bytes(area / "current")
            return checked(data.decode("ascii").strip()) if data is not None else None
        except (FileNotFoundError, Refused) as error:
            if attempt == 2 or isinstance(error, Refused) and error.kind != "changed":
                raise
    raise PublicationError("公開版を読み取れません")


def manifest(guard, area, release_id):
    release = area / "releases" / checked(release_id)
    data = read(guard, release / "manifest.json")
    if digest(data) != release_id:
        raise PublicationError("公開マニフェストの指紋が一致しません")
    value = json.loads(data)
    fields = {"parent", "definition_id", "plan_id", "files"}
    with_live = fields | {"live_base", "plan_sha256"}
    if not isinstance(value, dict) or value.keys() not in (fields, with_live, with_live | {"return_sha256"}):
        raise PublicationError("公開マニフェストの形式が不正です")
    if value["parent"] is not None:
        checked(value["parent"])
    checked(value["definition_id"])
    checked(value["plan_id"], PLAN_ID)
    if "plan_sha256" in value:
        checked(value["plan_sha256"])
        if "return_sha256" in value:
            checked(value["return_sha256"])
            checked(value["parent"])
            checked(value["live_base"])
        elif value["parent"] is None:
            checked(value["live_base"])
        elif value["live_base"] is not None:
            raise PublicationError("更新のマニフェストにライブ基準が含まれています")
    verify_files(value["files"])
    if value["definition_id"] != definition_fingerprint(value["files"]):
        raise PublicationError("公開マニフェストと定義の指紋が一致しません")
    return value


def verify_payload(guard, root, files):
    actual = {path.relative_to(root).as_posix() for path, info in guard.tree_entries(root)
              if not stat.S_ISDIR(info.st_mode)}
    if actual != files.keys():
        raise PublicationError("公開ファイルの一覧がマニフェストと一致しません")
    for name, checksum in files.items():
        if digest(read(guard, root / relative(name))) != checksum:
            raise PublicationError(f"公開ファイルの指紋が一致しません: {name}")


@dataclass(frozen=True)
class Publication:
    wiki: Path
    root: Path
    release_id: str
    definition_id: str


def archived_root(wiki):
    """版の保存先を wiki として直接渡した場合も、公開範囲を検査する。"""
    parts = wiki.parts
    for index in range(1, len(parts) - 2):
        if tuple(p.casefold() for p in parts[index:index+2]) != (".wiki-cache", "apply"):
            continue
        if (len(parts) != index + 5 or parts[index+2].casefold() != "releases"
                or parts[index+4].casefold() != "wiki"):
            raise PublicationError("公開用の管理領域を wiki として読めません")
        return Path(*parts[:index]), checked(parts[index+3])
    return None


def resolve_publication(wiki: Path, release_id: str | None = None) -> Publication | None:
    from wiki_restore import LEDGER_ENV

    wiki = Path(wiki).resolve()
    archived = archived_root(wiki)
    if archived:
        wiki, stored = archived
        if release_id is not None and release_id != stored:
            raise PublicationError("保存先と指定した公開版が一致しません")
        release_id = stored
    if (release_id is None and not os.environ.get(LEDGER_ENV)
            and not any(os.path.lexists(wiki / path) for path in (Path(MARKER), AREA))):
        return None
    guard = WriteGuard(wiki)
    area = guard.root / AREA
    return retry_publication_read(lambda: resolve_readable_tip(guard, area, release_id))


def resolve_readable_tip(guard, area, release_id):
    from wiki_restore import check as check_restore, completion, completion_key, require_completion, verify_live

    restore_state = check_restore(guard, area)
    active = None
    if restore_state is not None:
        tx = read_publication_journal(guard, area)
        if tx and tx["previous"] and f"publication:{tx['plan_id']}" not in restore_state["completions"]:
            active = pending_publication_base(guard, area, tx)
    if active is None:
        active = verified_active(guard, area)
        mode, fingerprint = mode_marker(guard) if active and release_id is None else (None, None)
    else:
        mode, fingerprint = "publication", None
    wanted = checked(release_id) if release_id is not None else active[0] if active else None
    if restore_state is not None and wanted in restore_state["retired"]:
        raise RetiredPublication("この公開版は取り下げられています")
    if active is None:
        if release_id is not None:
            raise PublicationError("公開版がありません")
        return None
    if restore_state is not None:
        require_completion(guard, area, "publication", expected_receipt(guard, area, *active), state=restore_state)
    if mode == "live":
        from wiki_live_return import verify_result
        result = verify_result(guard, area, active[0], fingerprint,
                               stored_path=live_return_record_path(guard, area, fingerprint))
        if restore_state is None:
            return None
        returned = completion(guard, area, "return_live", result)
        if (completion_key(returned) in restore_state["completions"]
                or guard.file_info(area / RETURN_JOURNAL) is None):
            require_completion(guard, area, "return_live", result, state=restore_state)
            verify_live(guard, area, restore_state)
            return None
        # ライブへの切り戻しを確定するまでは、直前の公開版を読み続ける。
    for version, entry in history(guard, area, active, stop=wanted):
        if version == wanted:
            if guard.file_info(area / "retired" / f"{version}.json") is not None:
                raise RetiredPublication("この公開版は取り下げられています")
            root = area / "releases" / version / "wiki"
            if version != active[0]:
                verify_release(guard, area, version, entry)
            return Publication(guard.root, root, version, entry["definition_id"])
    raise PublicationError("公開済みの版ではありません")


def file_checksums(guard, *, exclude=()):
    results = {}
    entries = []
    for child in sorted(guard.root.iterdir()):
        if child.name.casefold() in exclude:
            continue
        info = guard.entry_info(child)
        if info is not None:
            entries.extend(guard.tree_entries(child) if stat.S_ISDIR(info.st_mode) else [(child, info)])
    for path, info in entries:
        name = path.relative_to(guard.root).as_posix()
        if stat.S_ISDIR(info.st_mode) or name == "graph/.extract.lock":
            continue
        relative(name)
        results[name] = digest(read(guard, path))
    return results


def source_files(directory):
    files = file_checksums(WriteGuard(directory, strict_root=True))
    verify_files(files)
    return files


def capture_live_base(wiki):
    """初回の候補を用意する前に、ライブの入力と確定済み計画を記録する。"""
    guard = WriteGuard(wiki, strict_root=True)
    require(guard.root)
    return fingerprint_live(guard)


def fingerprint_live(guard):
    return live_fingerprint(guard, live_files(guard), live_receipt_fingerprints(guard))


def live_files(guard):
    return file_checksums(guard, exclude={".git", ".wiki-cache", MARKER})


def live_receipt_fingerprints(guard, *, exclude=None):
    receipts = guard.root / AREA / "live-receipts"
    return {p.name: digest(read(guard, p)) for p, _ in guard.tree_entries(receipts) if p.name != exclude}


def live_fingerprint(guard, files, applied):
    return digest(encoded({"files": files, "receipts": applied, "wiki_identity": guard.root_id}))


def check_initial_live(guard, expected, operation="初回公開"):
    from wiki_live_apply import pending_blockers, verify_latest_live

    if pending_blockers(guard):
        raise Conflict(f"{operation}の前に保留されたライブの承認済み差分を処理してください")
    if fingerprint_live(guard) != expected:
        raise Conflict(f"{operation}の基準からライブ wiki が変わりました。候補を作り直してください")
    verify_latest_live(guard, guard.root / AREA)


def search_publication(publication, query, top, type_, tags, since, per_page):
    import wiki_search as search

    pages = [page for page in search.collect_pages(publication.root)
             if search.passes_filters(page, SimpleNamespace(type=type_, tag=tags or [], since=since))]
    sections = search.collect_sections(pages)
    terms = search.tokenize(query)
    covered = search.covered_pages(terms, pages)
    ranking = search.lexical_ranking(terms, search.build_bm25(sections),
                                     None if covered is None else lambda i: sections[i]["page"]["rel_path"] in covered)
    page_counts = Counter()
    results = []
    for score, item in ranking:
        section = sections[item]
        page = section["page"]
        if page_counts[page["rel_path"]] >= per_page:
            continue
        page_counts[page["rel_path"]] += 1
        results.append(search.json_result(score, page, section, page["sections"]))
        if len(results) == top:
            break
    resolve_publication(publication.wiki, publication.release_id)
    return {"query": query, "wiki": publication.wiki.as_posix(), "release_id": publication.release_id,
            "granularity": "section", "mode": "lexical", "results": results, "warning": None}


def publication_page(publication, page):
    import wiki_search as search

    wanted = search.clean_link_target(page.strip().removeprefix("[[").removesuffix("]]"))
    pages = search.collect_pages(publication.root)
    matches = [p for p in pages if p["rel_path"][:-3] == wanted or p["rel_path"] == page]
    if not matches:
        matches = [p for p in pages if p["slug"] == wanted]
    if len(matches) > 1:
        raise PublicationError("同じスラッグのページが複数あります。相対パスを指定してください")
    if not matches:
        raise FileNotFoundError(f"公開版にページが見つからない: {page}")
    found = matches[0]
    resolve_publication(publication.wiki, publication.release_id)
    return {**{key: found[key] for key in ("slug", "rel_path", "meta", "body", "links")},
            "release_id": publication.release_id, "sections": [
                {key: section[key] for key in ("section_index", "heading_path", "level")}
                for section in found["sections"]
            ]}


def build_plan(wiki, source, *, plan_id=None, base: Publication | None = None, live_base=None):
    wiki = Path(wiki).absolute()
    if base is not None and base.wiki != wiki:
        raise PublicationError("別の wiki の版を基準にできません")
    if base is None:
        checked(live_base)
    elif live_base is not None:
        raise PublicationError("公開済みの wiki には公開版を基準に指定してください")
    files = source_files(source)
    return {"format": 2, "plan_id": checked(plan_id or uuid.uuid4().hex, PLAN_ID), "live_base": live_base,
            "wiki": str(wiki), "base": base.release_id if base else None,
            "base_definition": base.definition_id if base else None,
            "definition_id": definition_fingerprint(files), "files": files}


def writing_area(wiki):
    require(Path(wiki))
    guard = WriteGuard(wiki, strict_root=True)
    if archived_root(guard.root):
        raise PublicationError("保存済みスナップショットに書き込めません")
    area = guard.root / AREA
    if marker_present(guard) and guard.directory_info(area) is None:
        raise PublicationError("公開状態の印がありますが、公開の管理領域がありません")
    if os.path.lexists(guard.root / "raw"):
        raise PublicationError("wiki 内に raw がある配置は公開の書く段に渡せません")
    roots, failures = read_raw_roots(guard.root)
    if failures:
        raise PublicationError("wiki が使った原本の根を確認できません")
    for root in roots:
        external_raw_guard(guard.root, root)
    for path in (area / "releases", area / "staging", area / "receipts", area / "retired", area / "rejected",
                 area / "quarantine"):
        guard.ensure_directory(path)
    return guard, area


def receipt(guard, area, plan_id):
    return read_completion(guard, area / "receipts" / f"{checked(plan_id, PLAN_ID)}.json")


def read_completion(guard, path):
    data = guard.read_bytes(path)
    if data is None:
        return None
    value = json.loads(data)
    if not isinstance(value, dict) or value.keys() != {"plan_id", "plan_sha256", "release_id"}:
        raise PublicationError(f"完了記録の形式が不正です: {path.name}")
    checked(value["plan_id"], PLAN_ID)
    checked(value["plan_sha256"])
    checked(value["release_id"])
    return value


def mode_marker(guard):
    data = guard.read_bytes(guard.root / MARKER)
    if data is None:
        return None, None
    if data == MARKER_BYTES:
        return "publication", None
    match = re.fullmatch(rb"wiki-(returning|live)/1 ([0-9a-f]{64})\n", data)
    if match is None:
        raise PublicationError("wiki の公開状態の印が壊れています")
    return match[1].decode("ascii"), match[2].decode("ascii")


def marker_present(guard):
    return mode_marker(guard)[0] is not None


def check_publication_writer(guard, area):
    from wiki_restore import check as check_restore

    check_restore(guard, area)
    mode, _ = mode_marker(guard)
    if mode in {"returning", "live"} or any(guard.file_info(area / name) is not None
                                           for name in (RETURN_JOURNAL, RETURN_RECEIPT)):
        raise Conflict("ライブへの切り戻し中か切り戻し済みです。公開版への保存を開始できません")


def publication_evidence(guard, area):
    return (marker_present(guard)
            or any(guard.file_info(area / name) is not None for name in ("current", HEAD, JOURNAL, RETURN_JOURNAL, RETURN_RECEIPT))
            or any(guard.tree_entries(area / name) for name in ("receipts", "releases")))


def expected_receipt(guard, area, version, entry):
    approval = entry.get("plan_sha256")
    if approval is None:
        # 旧形式はマニフェストの項目から承認当時の計画を組み立てて照合する。
        parent = entry["parent"]
        plan = {"format": 1, "plan_id": entry["plan_id"], "wiki": str(guard.root),
                "base": parent, "base_definition": manifest(guard, area, parent)["definition_id"] if parent else None,
                "definition_id": entry["definition_id"], "files": entry["files"]}
        approval = digest(encoded(plan))
    return {"plan_id": entry["plan_id"], "plan_sha256": approval, "release_id": version}


def verify_release(guard, area, version, entry, *, payload=True):
    expected = expected_receipt(guard, area, version, entry)
    if receipt(guard, area, expected["plan_id"]) != expected:
        raise PublicationError(f"公開版の完了記録がないか、マニフェストと一致しません: {version}")
    if payload:
        verify_payload(guard, area / "releases" / version / "wiki", entry["files"])
    return expected


def history(guard, area, active, *, stop=None):
    version, entry = active
    while version:
        yield version, entry
        if version == stop:
            return
        version = entry["parent"]
        if version:
            entry = manifest(guard, area, version)


def verify_legacy_tip(guard, area, active):
    # 旧形式に独立した先頭の記録はない。全公開版を現在の履歴に結べる場合だけ採用する。
    versions = set()
    receipts = set()
    for version, entry in history(guard, area, active):
        expected = verify_release(guard, area, version, entry, payload=False)
        versions.add(version)
        receipts.add(f"{expected['plan_id']}.json")
    for folder, expected in (("releases", versions), ("receipts", receipts)):
        directory = area / folder
        guard.directory_info(directory)
        if {p.name for p in directory.iterdir()} != expected:
            raise PublicationError("旧形式の公開履歴から現在の版を一意に照合できません")


def read_publication_journal(guard, area):
    content = guard.read_bytes(area / JOURNAL)
    if content is None:
        return None
    tx = json.loads(content)
    fields = {"previous", "release_id", "plan_id", "plan_sha256", "stage", "live_base"}
    if not isinstance(tx, dict) or tx.keys() not in (fields, fields | {"return_sha256"}):
        raise PublicationError("公開日誌の形式が不正です")
    for name, pattern in (("release_id", ID), ("plan_id", PLAN_ID), ("plan_sha256", ID), ("stage", PLAN_ID)):
        checked(tx[name], pattern)
    if "return_sha256" in tx:
        checked(tx["return_sha256"])
        checked(tx["live_base"])
        if checked(tx["previous"]) == tx["release_id"]:
            raise PublicationError("再公開日誌の基準版が不正です")
    elif tx["previous"] is None:
        checked(tx["live_base"])
    elif checked(tx["previous"]) == tx["release_id"] or tx["live_base"] is not None:
        raise PublicationError("公開日誌の基準版が不正です")
    return tx


def verify_completions(guard, area, active):
    version, entry = active
    completed = list(history(guard, area, active))
    expected = {f"{verify_release(guard, area, release, item, payload=False)['plan_id']}.json"
                for release, item in completed}
    committed_releases = {release for release, _ in completed}
    possible_releases = set(committed_releases)
    directory = area / "receipts"
    guard.directory_info(directory)
    present = {p.name for p in directory.iterdir()}
    tx = read_publication_journal(guard, area)
    if tx and "return_sha256" in tx:
        present.discard(republish_temp(directory / f"{tx['plan_id']}.json", tx).name)
    for release, item in completed:
        if "return_sha256" in item:
            from wiki_live_return import verify_result
            proof = item["return_sha256"]
            verify_result(guard, area, item["parent"], proof, stored_path=archived_return(area, proof))
    if tx:
        if tx["release_id"] == version:
            if tx["previous"] != entry["parent"] or any(tx.get(k) != entry.get(k)
                    for k in ("plan_id", "plan_sha256", "live_base", "return_sha256")):
                raise PublicationError("公開日誌と現在の版が一致しません")
        elif tx["previous"] != version:
            raise PublicationError("公開日誌が現在の版に続いていません")
        else:
            possible_releases.add(tx["release_id"])
            name = f"{tx['plan_id']}.json"
            if name in present and name not in expected:
                candidate = manifest(guard, area, tx["release_id"])
                recorded = verify_release(guard, area, tx["release_id"], candidate, payload=False)
                if (candidate["parent"] != version or any(recorded[k] != tx[k] for k in recorded)
                        or candidate.get("return_sha256") != tx.get("return_sha256")
                        or candidate.get("live_base") != tx.get("live_base")):
                    raise PublicationError("公開日誌と次の版の完了記録が一致しません")
                expected.add(name)
    if expected != present:
        raise PublicationError("公開履歴と完了記録の一覧が一致しません")
    for rejected, _ in guard.tree_entries(area / "rejected"):
        item = json.loads(read(guard, rejected))
        if not isinstance(item, dict):
            raise PublicationError("公開の打ち切り記録が不正です")
        possible_releases.add(checked(item.get("release_id")))
    releases = area / "releases"
    guard.directory_info(releases)
    stored = {p.name for p in releases.iterdir()}
    if not committed_releases <= stored <= possible_releases:
        raise PublicationError("公開履歴と公開先ディレクトリの一覧が一致しません")


def pending_publication_base(guard, area, tx):
    """独立記録に未確定の候補は、日誌で検証した基準版だけを閲覧に使う。"""
    old, candidate = tx["previous"], tx["release_id"]
    mode, _ = mode_marker(guard)
    if mode == "live" and "return_sha256" in tx:
        # 再公開の候補を準備している間は、検証済みのライブを引き続き読む。
        return None
    entry = manifest(guard, area, old)
    prior = verify_release(guard, area, old, entry)
    result = {key: tx[key] for key in ("plan_id", "plan_sha256", "release_id")}
    version = current(guard, area)
    head = read_completion(guard, area / HEAD)
    if (mode != "publication" or version not in (old, candidate)
            or head not in (prior, result) or (head == result and version != candidate)):
        raise PublicationError("公開日誌と公開ポインターまたは先頭記録が一致しません")
    verify_completions(guard, area, (old, entry))
    return old, entry


def verified_active(guard, area):
    mode, fingerprint = mode_marker(guard)
    tx = read_publication_journal(guard, area)
    if mode == "live" and tx and "return_sha256" in tx:
        return republish_visible_tip(guard, area, tx, fingerprint)
    version = current(guard, area)
    if mode == "publication" and tx and "return_sha256" in tx and version != tx["release_id"]:
        raise PublicationError("再公開の印と公開ポインターが一致しません")
    if version is None:
        if publication_evidence(guard, area):
            raise PublicationError("公開の記録がありますが、公開ポインターがありません")
        return None
    entry = manifest(guard, area, version)
    expected = verify_release(guard, area, version, entry)
    head = read_completion(guard, area / HEAD)
    if head in (None, expected) and "plan_sha256" not in entry and mode is None:
        verify_legacy_tip(guard, area, (version, entry))
    elif head != expected or not mode:
        raise PublicationError("公開ポインターと先頭の完了記録、または公開状態の印が一致しません")
    verify_completions(guard, area, (version, entry))
    if mode == "live":
        from wiki_live_return import verify_result
        verify_result(guard, area, version, fingerprint)
    elif mode != "returning" and guard.file_info(area / RETURN_RECEIPT) is not None:
        raise PublicationError("切り戻しの完了記録と wiki の公開状態の印が一致しません")
    return version, entry


def archived_return(area, checksum):
    return area / "returns" / f"{checked(checksum)}.json"


def live_return_record_path(guard, area, checksum):
    path = area / RETURN_RECEIPT
    if guard.file_info(path) is None:
        tx = read_publication_journal(guard, area)
        if tx and tx.get("return_sha256") == checksum:
            return archived_return(area, checksum)
    return path


def republish_temp(path, tx):
    return path.with_name(f".wiki-republish-{tx['plan_id']}-{path.name}.tmp")


def republish_visible_tip(guard, area, tx, checksum):
    from wiki_live_return import verify_result

    if checksum != tx["return_sha256"] or guard.file_info(area / RETURN_JOURNAL) is not None:
        raise PublicationError("切り戻しの印と再公開の日誌が一致しません")
    old, candidate = tx["previous"], tx["release_id"]
    entry = manifest(guard, area, old)
    prior = verify_release(guard, area, old, entry)
    version = current(guard, area)
    head = read_completion(guard, area / HEAD)
    result = {key: tx[key] for key in ("plan_id", "plan_sha256", "release_id")}
    if version not in (old, candidate) or head not in (prior, result) or (head == result and version != candidate):
        raise PublicationError("再公開の日誌と公開ポインターまたは先頭記録が一致しません")
    if version == candidate:
        verify_release(guard, area, candidate, manifest(guard, area, candidate))
    verify_completions(guard, area, (old, entry))
    verify_result(guard, area, old, checksum, stored_path=live_return_record_path(guard, area, checksum))
    return old, entry


def read_active(guard, area):
    return retry_publication_read(lambda: verified_active(guard, area))


def retry_publication_read(read_state):
    # 読み取り中に書き手がポインターや管理記録を進めた場合、落ち着いた状態を読み直す。
    for delay in (0.01, 0.03, 0.08, None):
        try:
            return read_state()
        except RetiredPublication:
            raise
        except (PublicationError, FileNotFoundError, Refused) as error:
            if delay is None or (isinstance(error, Refused) and error.kind != "changed"):
                raise
            time.sleep(delay)


def verified_receipt(guard, area, active, plan_id):
    prior = receipt(guard, area, plan_id)
    if active:
        for version, entry in history(guard, area, active):
            if entry["plan_id"] == plan_id:
                if prior is None:
                    raise PublicationError("公開済みの計画に対応する完了記録がありません")
                return verify_release(guard, area, version, entry, payload=version != active[0])
    if prior is not None:
        raise PublicationError("完了記録に対応する公開版が履歴にありません")
    return None


def read_receipt(wiki, plan_id):
    from wiki_restore import check as check_restore, require_completion

    guard = WriteGuard(wiki)
    area = guard.root / AREA
    state = check_restore(guard, area)
    result = verified_receipt(guard, area, read_active(guard, area), plan_id)
    if result and state is not None:
        require_completion(guard, area, "publication", result, state=state)
    return result


def check_retry(guard, area, active, plan_id, expected):
    from wiki_restore import require_completion

    if guard.file_info(area / "rejected" / f"{plan_id}.json") is not None:
        raise Conflict("打ち切った計画です。再検査して別の計画を作ってください")
    prior = verified_receipt(guard, area, active, plan_id)
    if prior and prior["plan_sha256"] != expected:
        raise Conflict("完了した計画 ID は再利用できません")
    if prior:
        require_completion(guard, area, "publication", prior)
    return prior


def move_publication_directory(guard, source, target):
    for delay in (*REPLACE_DELAYS, None):
        try:
            guard.move_directory(source, target)
            return
        except PermissionError as error:
            if delay is None or getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            time.sleep(delay)


def abort_publication(guard, area, tx, reason):
    target = area / "releases" / tx["release_id"]
    stage = area / "staging" / tx["stage"]
    if guard.directory_info(target) is not None:
        move_publication_directory(guard, target, area / "quarantine" / tx["release_id"])
        sync_directory(area / "quarantine")
        sync_directory(area / "releases")
    if guard.directory_info(stage) is not None:
        move_publication_directory(guard, stage, area / "quarantine" / f"{tx['release_id']}-staging")
        sync_directory(area / "quarantine")
        sync_directory(area / "staging")
    rejected = area / "rejected" / f"{tx['plan_id']}.json"
    if guard.file_info(rejected) is None:
        guard.write_bytes(rejected, encoded({**tx, "reason": str(reason)}))
        sync_directory(area / "rejected")
    guard.remove_file(area / JOURNAL)
    sync_directory(area)
    return {"state": "aborted", "plan_id": tx["plan_id"], "release_id": tx["previous"]}


def finish_journal(guard, area, checkpoint=lambda _: None):
    from wiki_restore import acknowledge, check as check_restore

    check_restore(guard, area)
    journal = area / JOURNAL
    tx = read_publication_journal(guard, area)
    if tx is None:
        return None
    mode_name, fingerprint = mode_marker(guard)
    is_republish = "return_sha256" in tx
    if (guard.file_info(area / RETURN_JOURNAL) is not None or mode_name == "returning"
            or (mode_name == "live" and not is_republish)):
        raise PublicationError("公開の日誌と切り戻しの記録が同時に存在します")
    plan_id, approved_sha, old, release_id = (tx[key] for key in ("plan_id", "plan_sha256", "previous", "release_id"))
    receipt_path = area / "receipts" / f"{plan_id}.json"
    if is_republish:
        for path in (receipt_path, area / "current", area / HEAD, guard.root / MARKER,
                     archived_return(area, tx["return_sha256"])):
            guard.clear_exclusive_temp(republish_temp(path, tx), path)
        if mode_name == "live":
            republish_visible_tip(guard, area, tx, fingerprint)
            try:
                check_initial_live(guard, tx["live_base"], "再公開")
            except Conflict as error:
                if current(guard, area) != old or receipt(guard, area, plan_id) is not None:
                    raise
                return abort_publication(guard, area, tx, error)
        elif mode_name != "publication" or current(guard, area) != release_id:
            raise PublicationError("再公開の日誌と wiki の公開状態の印が一致しません")
    target = area / "releases" / release_id
    begin = current(guard, area)
    if begin not in (old, release_id):
        raise PublicationError("日誌の前後どちらとも公開版が一致しません")
    result = {"plan_id": plan_id, "plan_sha256": approved_sha, "release_id": release_id}
    head = read_completion(guard, area / HEAD)
    mode = marker_present(guard)
    existing = receipt(guard, area, plan_id)
    if existing is not None and existing != result:
        raise PublicationError("日誌と完了記録が一致しません")
    if head == result and begin != release_id:
        raise PublicationError("完了した公開版を指すポインターがありません")
    if old is None:
        if head not in (None, result):
            raise PublicationError("初回公開と先頭の完了記録が一致しません")
        if begin is None:
            names = {p.name for p in (area / "receipts").iterdir()}
            if names != ({f"{plan_id}.json"} if existing is not None else set()):
                raise PublicationError("初回公開の完了記録が日誌と一致しません")
            try:
                check_initial_live(guard, tx["live_base"])
            except Conflict as error:
                if mode or existing is not None:
                    raise
                return abort_publication(guard, area, tx, error)
    else:
        previous = manifest(guard, area, old)
        previous_receipt = verify_release(guard, area, old, previous)
        if head not in (previous_receipt, result) or not mode:
            raise PublicationError("公開日誌と直前の公開記録が一致しません")
        if begin == old:
            verify_completions(guard, area, (old, previous))
    if begin == release_id and (existing is None or not mode):
        raise PublicationError("切り替え済みの公開版に完了記録または公開状態の印がありません")
    if existing is not None and guard.directory_info(target) is None:
        raise PublicationError("完了記録に対応する公開先ディレクトリがありません")
    rejected = area / "rejected" / f"{plan_id}.json"
    try:
        if guard.file_info(rejected) is not None:
            raise PublicationError("日誌の候補はすでに打ち切られています")
        if guard.directory_info(target) is None:
            stage = area / "staging" / checked(tx["stage"], PLAN_ID)
            if begin == release_id or digest(read(guard, stage / "manifest.json")) != release_id:
                raise PublicationError("日誌に対応する成果物の指紋が一致しません")
            move_publication_directory(guard, stage, target)
            sync_directory(area / "releases")
            sync_directory(area / "staging")
        entry = manifest(guard, area, release_id)
        if (entry["parent"] != old or entry["plan_id"] != plan_id or
                entry.get("live_base") != tx["live_base"] or entry.get("plan_sha256") != approved_sha
                or entry.get("return_sha256") != tx.get("return_sha256")):
            raise PublicationError("日誌と公開マニフェストの基準版が一致しません")
        verify_payload(guard, target / "wiki", entry["files"])
    except (PublicationError, Refused, FileNotFoundError, json.JSONDecodeError) as exc:
        if begin != old or existing is not None or (old is None and mode):
            raise
        return abort_publication(guard, area, tx, exc)
    if begin != release_id:
        checkpoint("before_pointer")
        if not mode:
            guard.write_bytes(guard.root / MARKER, MARKER_BYTES)
            sync_directory(guard.root)
        if existing is None:
            write_publication_file(guard, receipt_path, encoded(result), tx, checkpoint, "receipt_prepared")
            sync_directory(area / "receipts")
        write_publication_file(guard, area / "current", (release_id + "\n").encode("ascii"),
                               tx, checkpoint, "pointer_prepared", replace=True)
        checkpoint("pointer_written")
    elif not (is_republish and mode_name == "live"):
        verify_completions(guard, area, (release_id, entry))
    if head != result:
        write_publication_file(guard, area / HEAD, encoded(result), tx, checkpoint, "head_prepared", replace=True)
    if begin != release_id:
        checkpoint("after_pointer")
    if is_republish and mode_name == "live":
        from wiki_republish import seal_republication
        seal_republication(guard, area, tx, checkpoint)
    verified_active(guard, area)
    acknowledge(guard, area, ("publication", result))
    guard.remove_file(journal)
    sync_directory(area)
    return result


def write_publication_file(guard, path, data, tx, checkpoint, point, *, replace=False):
    temporary = republish_temp(path, tx) if "return_sha256" in tx else None
    guard.write_bytes(path, data, replace=replace, delays=REPLACE_DELAYS, temporary=temporary,
                      before_publish=lambda: checkpoint(point))
    sync_directory(path.parent)


def check_live_writer(guard, area):
    if any(guard.file_info(area / name) is not None for name in (LIVE_OWNER, LIVE_JOURNAL, LIVE_TRANSACTION)):
        raise Conflict("ライブ書き込みの中断を unlock と recover --live で処理してください")


def apply_plan(wiki, source, plan, expect_sha256, *, checkpoint=lambda _: None, timeout=180):
    """承認したファイルだけを写す。基準が変わった場合はロックの外で作り直せる。"""
    standard = {"format", "plan_id", "wiki", "base", "base_definition", "definition_id", "files", "live_base"}
    if not isinstance(plan, dict) or plan.keys() not in (standard, standard | {"return_sha256", "include", "exclude"}):
        raise PublicationError("公開計画の形式が不正です")
    is_republish = "return_sha256" in plan
    if plan["format"] != (3 if is_republish else 2) or digest(encoded(plan)) != checked(expect_sha256):
        raise PublicationError("承認した計画の指紋が一致しません")
    wiki = Path(wiki).absolute()
    if str(wiki) != plan["wiki"]:
        raise PublicationError("承認した計画の wiki と一致しません")
    if (plan["base"] is None) != (plan["base_definition"] is None):
        raise PublicationError("基準の公開版または定義版がありません")
    if plan["base"] is not None:
        checked(plan["base"])
        checked(plan["base_definition"])
        if is_republish:
            checked(plan["return_sha256"])
            checked(plan["live_base"])
        elif plan["live_base"] is not None:
            raise PublicationError("更新の公開計画にライブ基準が含まれています")
    else:
        if is_republish:
            raise PublicationError("再公開には切り戻した版を指定してください")
        checked(plan["live_base"])
    plan_id = checked(plan["plan_id"], PLAN_ID)
    files = plan["files"]
    verify_files(files)
    if plan["definition_id"] != definition_fingerprint(files):
        raise PublicationError("計画と定義の指紋が一致しません")
    entry = {"parent": plan["base"], "definition_id": checked(plan["definition_id"]),
             "plan_id": plan_id, "files": files, "live_base": plan["live_base"],
             "plan_sha256": expect_sha256}
    if is_republish:
        entry["return_sha256"] = plan["return_sha256"]
    data = encoded(entry)
    release_id = digest(data)
    guard, area = writing_area(wiki)
    lock = area / "apply.lock"
    with guard.hold_lock(lock, timeout=timeout):
        check_live_writer(guard, area)
        finish_journal(guard, area)
        active = verified_active(guard, area)
        prior = check_retry(guard, area, active, plan_id, expect_sha256)
        if prior:
            return prior
        check_writer_mode(guard, area, plan, active)
    checkpoint("after_initial_check")

    source_guard = WriteGuard(source, strict_root=True)
    mark = uuid.uuid4().hex
    stage = area / "staging" / mark
    guard.ensure_directory(stage / "wiki")
    # 日誌が作られるまではステージを公開済みの版として解決できない。
    try:
        for name, checksum in files.items():
            path = relative(name)
            content = read(source_guard, source_guard.root / path)
            if digest(content) != checksum:
                raise PublicationError(f"承認した成果物が変更されました: {name}")
            destination = stage / "wiki" / path
            guard.ensure_directory(destination.parent)
            guard.write_bytes(destination, content)
        guard.write_bytes(stage / "manifest.json", data)
        for path, info in reversed(guard.tree_entries(stage)):
            if stat.S_ISDIR(info.st_mode):
                sync_directory(path)
        sync_directory(stage)
        sync_directory(area / "staging")

        with guard.hold_lock(lock, timeout=timeout):
            check_live_writer(guard, area)
            finish_journal(guard, area)
            active = verified_active(guard, area)
            prior = check_retry(guard, area, active, plan_id, expect_sha256)
            if prior:
                return prior
            check_writer_mode(guard, area, plan, active)
            active_id = active[0] if active else None
            if active_id != plan["base"] or (active and active[1]["definition_id"] != plan["base_definition"]):
                raise Conflict("公開版または定義が変わりました。ロックの外で試行をやり直してください")
            journal = {"previous": active_id, "release_id": release_id, "plan_id": plan_id,
                       "plan_sha256": expect_sha256, "stage": mark, "live_base": plan["live_base"]}
            if is_republish:
                journal["return_sha256"] = plan["return_sha256"]
            checkpoint("before_journal")
            if digest(read(guard, stage / "manifest.json")) != release_id:
                raise PublicationError("試行のマニフェストの指紋が一致しません")
            verify_payload(guard, stage / "wiki", files)
            if active and "plan_sha256" not in active[1] and not marker_present(guard):
                old_receipt = expected_receipt(guard, area, *active)
                if read_completion(guard, area / HEAD) is None:
                    guard.write_bytes(area / HEAD, encoded(old_receipt))
                    sync_directory(area)
                guard.write_bytes(guard.root / MARKER, MARKER_BYTES)
                sync_directory(guard.root)
            if active is None or is_republish:
                # 待ち切れなかったライブ計画は共通ロックの外で保留を記録する。
                with guard.hold_lock(area / "pending.lock", timeout=180):
                    check_initial_live(guard, plan["live_base"], "再公開" if is_republish else "初回公開")
                    if is_republish:
                        from wiki_republish import check_candidate
                        check_candidate(guard, area, active, plan)
                    guard.write_bytes(area / JOURNAL, encoded(journal))
                    sync_directory(area)
            else:
                guard.write_bytes(area / JOURNAL, encoded(journal))
                sync_directory(area)
            checkpoint("after_journal")
            result = finish_journal(guard, area, checkpoint)
            if result.get("state") == "aborted":
                raise PublicationError("公開候補が壊れていたため、旧版を保ったまま打ち切りました")
            return result
    finally:
        # 日誌がこの候補を指す場合は、次の書き手が復旧する。
        with suppress(OSError, ValueError):
            if guard.directory_info(stage) is not None:
                with guard.hold_lock(lock, timeout=timeout):
                    tx = guard.read_bytes(area / JOURNAL)
                    if not tx or json.loads(tx)["stage"] != mark:
                        entries = guard.tree_entries(stage)
                        for path, info in reversed(entries):
                            if stat.S_ISDIR(info.st_mode):
                                if guard.directory_info(path) is not None:
                                    path.rmdir()
                            else:
                                guard.remove_file(path)
                        stage.rmdir()


def check_writer_mode(guard, area, plan, active):
    if "return_sha256" not in plan:
        check_publication_writer(guard, area)
        return
    if (mode_marker(guard) != ("live", plan["return_sha256"]) or active is None or active[0] != plan["base"]
            or guard.file_info(area / RETURN_JOURNAL) is not None):
        raise Conflict("再公開の対象となるライブ世代が変わりました")


def recover(wiki, *, timeout=180):
    guard, area = writing_area(wiki)
    with guard.hold_lock(area / "apply.lock", timeout=timeout):
        check_live_writer(guard, area)
        if guard.file_info(area / RETURN_JOURNAL) is not None or mode_marker(guard)[0] == "returning":
            from wiki_live_return import resume
            return resume(guard, area)
        result = finish_journal(guard, area)
        if result is None:
            verified_active(guard, area)
        return result


def retire(wiki, release_id):
    from wiki_restore import retire as retire_independently

    checked(release_id)
    guard, area = writing_area(wiki)
    with guard.hold_lock(area / "apply.lock", timeout=180):
        check_live_writer(guard, area)
        if read_publication_journal(guard, area) is not None:
            finish_journal(guard, area)
        if current(guard, area) == release_id:
            raise Conflict("公開中の版は、別の版を公開してから取り下げてください")
        if retire_independently(guard, area, release_id):
            return
        release = resolve_publication(wiki, release_id)
        guard.write_bytes(area / "retired" / f"{release.release_id}.json", encoded({"release_id": release.release_id}))
        sync_directory(area / "retired")


def main(argv=None):
    import wiki_live_apply

    argv = sys.argv[1:] if argv is None else argv
    if argv and (argv[0] in wiki_live_apply.COMMANDS
                 or argv[0] == "apply" and any(arg.split("=")[0] == "--apply-dir" for arg in argv[1:])
                 or argv[0] == "recover" and "--live" in argv[1:]):
        return wiki_live_apply.main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.epilog = ("共通の保存: begin, new, edit, index-add, log-entry, log-sub, depends-on, "
                     "diff, rebase, apply --apply-dir, pending, retry, discard-save")
    sub = parser.add_subparsers(dest="cmd", required=True)
    view = sub.add_parser("view", help="読み取る wiki と公開版を特定する")
    view.add_argument("--wiki", required=True, type=Path)
    view.add_argument("--release-id")
    apply = sub.add_parser("apply", help="承認済みの計画を公開する")
    apply.add_argument("--wiki", type=Path, required=True)
    apply.add_argument("--source", type=Path, required=True)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expect-sha256", required=True)
    restore = sub.add_parser("recover", help="残った公開日誌を完了させる")
    restore.add_argument("--wiki", type=Path, required=True)
    recovery_setup = sub.add_parser("init-restore-ledger", help="別ボリュームの復元記録を確定済みの公開 wiki に設定する")
    recovery_setup.add_argument("--wiki", type=Path, required=True)
    recovery_setup.add_argument("--ledger", type=Path, required=True)
    recovery_setup.add_argument("--raw-root", type=Path, help="独立記録で保護する raw の根")
    reconcile = sub.add_parser("reconcile-restore", help="独立した完了記録を照合し、取り下げを復元する")
    reconcile.add_argument("--wiki", type=Path, required=True)
    reconcile.add_argument("--restore-content", action="store_true", help="別ディスクから本文・成果物・保全原本を復元する")
    withdrawal = sub.add_parser("retire", help="旧公開版を取り下げる")
    withdrawal.add_argument("--wiki", type=Path, required=True)
    withdrawal.add_argument("--release-id", required=True)
    preview = sub.add_parser("plan-return-live", help="公開の変更をライブに書き戻す計画を表示する")
    preview.add_argument("--wiki", type=Path, required=True)
    preview.add_argument("--raw-root", type=Path, required=True)
    cutback = sub.add_parser("return-live", help="承認した計画でライブに切り戻す")
    cutback.add_argument("--wiki", type=Path, required=True)
    cutback.add_argument("--plan", type=Path, required=True)
    cutback.add_argument("--expect-sha256", required=True)
    next_plan = sub.add_parser("plan-republish-live", help="切り戻し後のライブから再公開候補を用意する")
    next_plan.add_argument("--wiki", type=Path, required=True)
    next_plan.add_argument("--raw-root", type=Path, required=True)
    next_plan.add_argument("--out", type=Path, required=True)
    next_plan.add_argument("--include", action="append", default=[], metavar="PATH")
    next_plan.add_argument("--exclude", action="append", default=[], metavar="PATH")
    republish = sub.add_parser("republish-live", help="承認した計画でライブの変更を再公開する")
    republish.add_argument("--wiki", type=Path, required=True)
    republish.add_argument("--source", type=Path, required=True)
    republish.add_argument("--plan", type=Path, required=True)
    republish.add_argument("--expect-sha256", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "recover":
        area = args.wiki / AREA
        if any(os.path.lexists(area / name) for name in (LIVE_OWNER, LIVE_JOURNAL)):
            return wiki_live_apply.main(argv)
    try:
        if args.cmd == "view":
            root = require(args.wiki).resolve()
            release = resolve_publication(root, args.release_id)
            print(json.dumps({"read_root": str(release.root if release else root),
                              "release_id": release.release_id if release else None}, ensure_ascii=False))
            return 0
        if args.cmd == "init-restore-ledger":
            from wiki_restore import enroll
            result = enroll(args.wiki, args.ledger, raw_root=args.raw_root)
        elif args.cmd == "reconcile-restore":
            from wiki_restore import reconcile
            result = reconcile(args.wiki, restore_content=args.restore_content)
        elif args.cmd == "retire":
            retire(args.wiki, args.release_id)
            result = {"state": "retired", "release_id": args.release_id}
        elif args.cmd == "plan-return-live":
            from wiki_live_return import build_plan as plan_return
            plan = plan_return(args.wiki, args.raw_root)
            result = {"plan": plan, "plan_sha256": digest(encoded(plan))}
        elif args.cmd == "plan-republish-live":
            from wiki_republish import build_plan as plan_republish, describe_delta
            plan = plan_republish(args.wiki, args.raw_root, args.out, args.include, args.exclude)
            result = {"plan": plan, "plan_sha256": digest(encoded(plan)), **describe_delta(args.wiki, args.out, plan)}
        elif args.cmd == "return-live":
            from wiki_live_return import apply as return_live
            document = json.loads(args.plan.read_bytes())
            plan = document["plan"] if isinstance(document, dict) and "plan" in document else document
            result = return_live(args.wiki, plan, args.expect_sha256)
        elif args.cmd in {"apply", "republish-live"}:
            plan = json.loads(args.plan.read_bytes())
            if args.cmd == "republish-live":
                if isinstance(plan, dict) and "plan" in plan:
                    plan = plan["plan"]
                if not isinstance(plan, dict) or plan.get("format") != 3:
                    raise PublicationError("ライブからの再公開計画を指定してください")
            result = apply_plan(args.wiki, args.source, plan, args.expect_sha256)
        else:
            result = recover(args.wiki)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 4 if isinstance(error, Conflict) else 1
    print(json.dumps(result, ensure_ascii=False))
    return 4 if args.cmd == "reconcile-restore" and result["state"] == "blocked" else 0


if __name__ == "__main__":
    # 共通の書く段を呼び合うモジュールと、CLI の例外型を共用する。
    sys.modules["wiki_apply"] = sys.modules[__name__]
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
