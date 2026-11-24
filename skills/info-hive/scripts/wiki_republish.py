"""切り戻し後のライブ世代を、前の公開版に続く候補にする。"""

from pathlib import Path
import os
from tempfile import TemporaryDirectory
import uuid

import wiki_apply as publish
import wiki_live_apply as live
from wiki_fsguard import WriteGuard
from wiki_live_return import verify_result
from wiki_pages import absolute_user_path, is_page_path
from wiki_raw_roots import read_raw_roots


GRAPH_OUTPUTS = {"graph/graph.sqlite", "graph/nodes.jsonl", "graph/edges.jsonl", "graph/graph.graphml"}
SEMANTIC_OUTPUTS = {"ontology/ossie/wiki-data.sqlite", "ontology/ossie/records.jsonl", "ontology/ossie/release.json"}
GENERATED = GRAPH_OUTPUTS | SEMANTIC_OUTPUTS
SEMANTIC_REPORT = "graph/semantic-projection.json"
SEMANTIC_CATALOG = "ontology/definition.json"


def generated_files(version):
    return GENERATED | {SEMANTIC_CATALOG, SEMANTIC_REPORT} if version == 2 else GENERATED


def check_format(guard, active, plan_id, capability):
    from wiki_semantic_projection import REPUBLISH, cutback_format

    return cutback_format(guard, active, REPUBLISH, plan_id, capability)


def live_boundary(result, records):
    known = result.get("live_generation")
    if known is None and result["live_receipt"] is not None:
        known = result["live_receipt"]["generation"]
    if known is None and not records:
        known = 0
    if known is not None and (type(known) is not int or not 0 <= known <= len(records)):
        raise publish.Conflict("再公開計画と切り戻し時のライブの世代が一致しません")
    return known


def missing_predecessors(published, era):
    latest = dict(published)
    gaps = set()
    for record in era:
        before = record.get("before_sha256", {})
        for name, checksum in record["after_sha256"].items():
            if name not in before or before[name] != latest.get(name):
                gaps.add(name)
            latest[name] = checksum
    return gaps


def live_inputs(guard, area, active, checksum, include, exclude, *, version=1):
    result = verify_result(guard, area, active[0], checksum)
    records = live.live_receipts(guard, area)
    live.verify_latest_live(guard, area)
    boundary = live_boundary(result, records)
    before = publish.live_files(guard)
    publish.verify_file_names(before)
    history = publish.live_receipt_fingerprints(guard)
    baseline = publish.live_fingerprint(guard, before, history)
    for paths in (include, exclude):
        if not isinstance(paths, list) or any(not isinstance(name, str) for name in paths):
            raise publish.PublicationError("再公開の対象ファイルの形式が不正です")
    if set(include) & set(exclude):
        raise publish.Conflict("再公開の対象に同じパスが追加と除外の両方にあります")
    published = active[1]["files"]
    generated = generated_files(version)
    fixed_definitions = set(publish.DEFINITION_FILES) if version == 2 else set()
    semantic = {name for name in published.keys() | before.keys() if name.startswith("ontology/")}
    if version == 2 and any(published.get(name) != before.get(name) for name in semantic | fixed_definitions):
        raise publish.Conflict("ライブの固定定義または Ossie 成果物が変わりました。切り戻し元の固定公開版の内容に戻してください")
    for name in include:
        publish.relative(name)
        if name not in before or name in generated or name in fixed_definitions or name.startswith("ontology/"):
            raise publish.Conflict(f"再公開の追加ファイルに使えないパスです: {name}")
    era = records[boundary:] if boundary is not None else [item for item in records if item.get("return_sha256") == checksum]
    saved = {name for record in era for name in record["after_sha256"]}
    created = {name for record in era if record.get("return_sha256") == checksum
               for name in record.get("created_files", ())}
    if "private_paths" in result:
        eligible = saved - set(result["private_paths"])
        ambiguous = set()
    else:
        eligible = (saved & published.keys()) | created
        unclassified = {name for record in (era if boundary is not None else records) for name in record["after_sha256"]}
        ambiguous = (unclassified & before.keys()) - published.keys() - created
    unapproved = {name for name, sha in published.items()
                  if name not in generated and before.get(name) != sha and name not in saved}
    unapproved.update(name for name in missing_predecessors(published, era)
                      if name in published.keys() | eligible and before.get(name) != published.get(name))
    for name in exclude:
        publish.relative(name)
        unpublished_change = name in (ambiguous | (unapproved - published.keys()))
        removed = name in published and name not in before and name not in saved and name not in generated
        if not unpublished_change and not removed:
            raise publish.Conflict(f"再公開の対象から明示的に除けないパスです: {name}")
    unresolved = ambiguous - set(include) - set(exclude)
    if unresolved:
        raise publish.Conflict("旧形式で公開範囲を判定できないパスを --include か --exclude で指定してください: "
                               + "、".join(sorted(unresolved)))
    unapproved.difference_update(include)
    unapproved.difference_update(exclude)
    if unapproved:
        raise publish.Conflict("ライブの完了記録にない公開対象の変更を確認してください。公開には --include、"
                               "未公開ファイルを除く場合は --exclude を指定してください: " + "、".join(sorted(unapproved)))
    selected = ((published.keys() | eligible | set(include)) - set(exclude)) & before.keys()
    # 意味成果物と固定定義は切り戻し元の公開版から引き継ぐ。
    if version == 1 and any(published.get(name) != before.get(name) for name in semantic):
        raise publish.Conflict("ライブで Ossie の成果物が変わりました。意味レコードの変更を照合してください")
    expected = {name: before[name] for name in selected if name not in generated}
    publish.verify_file_names(expected)
    return baseline, expected


def guarded_output(guard, raw_root, target, *, authorize_parent=None):
    out = absolute_user_path(target)
    historical, failures = read_raw_roots(guard.root)
    if failures:
        raise publish.PublicationError("wiki が使った原本の根を確認できません")
    roots = [guard.root, raw_root, *historical]
    if any(live.overlapping(out, root) or live.overlapping(out.resolve(), root.resolve()) for root in roots):
        raise publish.Conflict("再公開候補は wiki と原本の根から離れた場所に作ってください")
    if os.path.lexists(out):
        raise publish.Conflict("再公開候補の出力先には未作成のディレクトリを指定してください")
    parent = WriteGuard(next(p for p in out.parents if os.path.lexists(p)), strict_root=True)
    if authorize_parent is not None:
        authorize_parent(parent)
    parent.ensure_directory(out)
    return WriteGuard(out, strict_root=True)


def require_candidate_scope(capability, guard, plan_id):
    from wiki_semantic_projection import REPUBLISH, require_capability

    require_capability(capability, guard, plan_id, operation=REPUBLISH)


def copy_candidate_inputs(source, archive, destination, expected, previous, version):
    artifacts = {name: previous[name] for name in generated_files(version) & previous.keys()}
    changes = {}
    for name, expected_sha in sorted((expected | artifacts).items()):
        origin = archive if name in artifacts else source
        relative = publish.relative(name)
        data = publish.read(origin, origin.root / relative)
        if publish.digest(data) != expected_sha:
            raise publish.Conflict(f"再公開の入力ファイルの指紋が一致しません: {name}")
        live.write(destination, destination.root / relative, data)
        if is_page_path(name) and previous.get(name) != expected_sha:
            changes[name] = (None, data)
    return changes


def build_plan(wiki, raw_root, target, include=(), exclude=(), *, capability=None):
    from wiki_public_projection import update_graph, update_projections

    guard = live.wiki_guard(wiki)
    area = guard.root / publish.AREA
    live.live_mode(guard)
    mode, checksum = publish.mode_marker(guard)
    if mode != "live":
        raise publish.Conflict("切り戻し後のライブ wiki を指定してください")
    active = publish.read_active(guard, area)
    plan_id = uuid.uuid4().hex
    version = check_format(guard, active, plan_id, capability)
    include = sorted(set(include))
    exclude = sorted(set(exclude))
    baseline, expected = live_inputs(guard, area, active, checksum, include, exclude, version=version)
    raw_root = absolute_user_path(raw_root)
    live.protect_roots(guard, raw_root)
    def authorize_parent(parent):
        require_candidate_scope(capability, parent, plan_id)

    destination = guarded_output(guard, raw_root, target, authorize_parent=authorize_parent if version == 2 else None)
    if version == 2:
        require_candidate_scope(capability, destination, plan_id)
    archive = WriteGuard(area / "releases" / active[0] / "wiki", strict_root=True)
    changes = copy_candidate_inputs(guard, archive, destination, expected, active[1]["files"], version)
    check_assets(destination)
    if version == 2:
        from wiki_semantic_projection import write_page_snapshot

        write_page_snapshot(destination, semantic_snapshot(guard, active, destination, expected))
        update_graph(destination)
    else:
        update_projections(destination, changes, force=True)
    if publish.fingerprint_live(guard) != baseline or publish.mode_marker(guard) != (mode, checksum):
        raise publish.Conflict("候補の準備中にライブが変わりました。新しい場所に候補を作り直してください")
    files = publish.source_files(destination.root)
    return {"format": 3, "wiki": str(guard.root), "plan_id": plan_id,
            "base": active[0], "base_definition": active[1]["definition_id"], "definition_id": publish.definition_fingerprint(files),
            "live_base": baseline, "return_sha256": checksum, "include": include, "exclude": exclude, "files": files}


def check_assets(destination):
    from wiki_lint import collect_pages

    # 原本は公開候補にコピーしない。公開する添付の参照だけを確かめる。
    missing = [item for page in collect_pages(destination.root) for item in page["missing_assets"]]
    if missing:
        raise publish.Conflict("公開候補に添付がありません。公開する添付を --include で指定してください")


def semantic_snapshot(guard, active, candidate, files):
    import wiki_semantic_definitions as fixed
    import wiki_semantic_store as store
    from wiki_semantic_projection import BUNDLE, archive_view, page_snapshot

    base = archive_view(guard, active)
    archive = WriteGuard(base.root, strict_root=True)
    previous = store.read_bundle(archive.root / BUNDLE)
    fixed.verify_stored(archive, archive.root / "ontology", previous)
    old = active[1]["files"]
    articles = {name: sha for name, sha in files.items() if is_page_path(name)}
    changes = {}
    for name, checksum in articles.items():
        if checksum != old.get(name):
            data = publish.read(candidate, candidate.root / publish.relative(name))
            if publish.digest(data) != checksum:
                raise publish.Conflict("承認した再公開候補の記事が変わりました")
            changes[name] = (None, data)
    return page_snapshot(previous, base, changes, articles)


def verify_format1_projections(area, active, candidate, expected, files):
    from wiki_public_projection import update_projections

    archive = WriteGuard(area / "releases" / active[0] / "wiki", strict_root=True)
    # 承認候補の派生物は使わず、固定した公開版と承認済みのステージから再生成する。
    with TemporaryDirectory(prefix="info-hive-republish-check-") as directory:
        reference = WriteGuard(directory, strict_root=True)
        changes = copy_candidate_inputs(candidate, archive, reference, expected, active[1]["files"], 1)
        update_projections(reference, changes, force=True)
        for name in sorted(GENERATED):
            data = reference.read_bytes(reference.root / name)
            checksum = publish.digest(data) if data is not None else None
            if checksum != files.get(name):
                raise publish.Conflict(f"再公開の派生物が承認済み入力からの再生成結果と一致しません: {name}。"
                                       "適用と同じ実行環境で候補を作り直し、あらためて承認してください")


def check_candidate(guard, area, active, plan, candidate, *, capability=None):
    version = check_format(guard, active, plan["plan_id"], capability)
    fingerprint, expected = live_inputs(guard, area, active, plan["return_sha256"],
                                        plan["include"], plan["exclude"], version=version)
    actual = {name: value for name, value in plan["files"].items() if name not in generated_files(version)}
    if fingerprint != plan["live_base"] or actual != expected:
        raise publish.Conflict("再公開候補に承認済みのライブの内容が揃っていません")
    if version == 1:
        verify_format1_projections(area, active, candidate, expected, plan["files"])
    if version == 2:
        from wiki_public_projection import verify_graph_projection
        import wiki_semantic_definitions as fixed
        import wiki_semantic_store as store
        from wiki_semantic_projection import BUNDLE

        graph = {"graph/graph.sqlite", SEMANTIC_REPORT} | (active[1]["files"].keys() & GRAPH_OUTPUTS)
        jsonl = {"graph/nodes.jsonl", "graph/edges.jsonl"}
        if graph & jsonl:
            graph |= jsonl
        if plan["files"].keys() & (GRAPH_OUTPUTS | {SEMANTIC_REPORT}) != graph:
            raise publish.Conflict("再公開候補の互換グラフの形式が切り戻し元と一致しません")
        value = store.read_bundle(candidate.root / BUNDLE)
        fixed.verify_stored(candidate, candidate.root / "ontology", value)
        if semantic_snapshot(guard, active, candidate, expected) != value:
            raise publish.Conflict("再公開の意味スナップショットが現在の根拠と一致しません。候補を作り直してください")
        verify_graph_projection(candidate, plan["files"])


def describe_delta(wiki, target, plan):
    from wiki_semantic_format import detect

    guard = WriteGuard(wiki)
    out = WriteGuard(target)
    area = guard.root / publish.AREA
    previous = publish.manifest(guard, area, plan["base"])["files"]
    archive = area / "releases" / plan["base"] / "wiki"
    new = plan["files"]
    added, removed = new.keys() - previous.keys(), previous.keys() - new.keys()
    modified = {name for name in new.keys() & previous.keys() if new[name] != previous[name]}
    markdown = {}
    for name in sorted(added | removed | modified):
        if name.endswith(".md"):
            path = publish.relative(name)
            old = publish.read(guard, archive / path) if name in previous else None
            changed = publish.read(out, out.root / path) if name in new else None
            markdown[name] = (old, changed)
    result = {"added": sorted(added), "modified": sorted(modified), "removed": sorted(removed),
              "markdown_diff": live.diff_text(markdown)}
    if detect(WriteGuard(archive, strict_root=True)) == 2:
        result["assessment_impact"] = describe_assessment_impact(archive, previous, out.root, new)
    return result


def describe_assessment_impact(archive, published, candidate, planned):
    from wiki_semantic_projection import BUNDLE
    from wiki_semantic_store import read_bundle

    before = read_bundle(archive / BUNDLE)
    after = read_bundle(candidate / BUNDLE)
    for value, files in ((before, published), (after, planned)):
        if (files.get(f"{BUNDLE}/records.jsonl") != publish.digest(value.jsonl)
                or files.get(f"{BUNDLE}/release.json") != publish.digest(value.manifest_json)):
            raise publish.Conflict("差分表示の意味スナップショットが公開計画と一致しません")
    records = {row.record_id: row for row in after.records}
    changes = []
    for key in sorted(set(after.manifest["current_assessments"]) - set(before.manifest["current_assessments"])):
        assessment = records[key].payload
        state = assessment["evidence_state"]
        if state in {"affected", "unavailable"}:
            report = records[assessment["reports"][-1]].payload
            changes.append({"claim_id": records[assessment["revision_id"]].payload["claim_id"], "state": state,
                            "reasons": sorted({check["reason"] for check in report["checks"].values()})})
    counts = {state: sum(change["state"] == state for change in changes) for state in ("affected", "unavailable")}
    return {"counts": counts, "changes": changes}


def seal_republication(guard, area, tx, checkpoint):
    proof = tx["return_sha256"]
    old = tx["previous"]
    archived = publish.archived_return(area, proof)
    source = area / publish.RETURN_RECEIPT
    if guard.file_info(archived) is None:
        verify_result(guard, area, old, proof)
        guard.ensure_directory(archived.parent)
        publish.sync_directory(archived.parent.parent)
        publish.write_publication_file(guard, archived, publish.read(guard, source), tx, checkpoint, "return_archive_prepared")
    verify_result(guard, area, old, proof, stored_path=archived)
    if guard.file_info(source) is not None:
        verify_result(guard, area, old, proof)
        live.remove(guard, source)
    checkpoint("return_archived")
    publish.check_initial_live(guard, tx["live_base"], "再公開")
    publish.write_publication_file(guard, guard.root / publish.MARKER, publish.MARKER_BYTES,
                                   tx, checkpoint, "republish_marker_prepared", replace=True)
    checkpoint("republish_switched")
