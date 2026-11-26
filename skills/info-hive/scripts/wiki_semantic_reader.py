"""公開版を固定して記事・主張・根拠を読む。"""

from pathlib import Path

import wiki_apply as publish
import wiki_semantic_store as store
from wiki_fsguard import WriteGuard, external_raw_guard
from wiki_pages import article_path, is_article_path
from wiki_raw_roots import read_raw_roots
from wiki_semantic_adopt import EvidenceInputs
from wiki_semantic_format import detect_bytes
from wiki_semantic_model import ModelError, digest_id, fingerprint, relative_path
from wiki_semantic_projection import adoption_request


_CONTEXT_KEYS = ("semantic_snapshot_id", "definition_snapshot_id", "activation_id", "processing_context_id")


class _UnreadableSource(Exception):
    """原本の内容または所在を確認できなかった。"""


class _PublishedEvidence(EvidenceInputs):
    def __init__(self, records, view, raw, confirmations, guard, files):
        super().__init__(records, view, raw, confirmations)
        self.guard, self.files = guard, files

    def source(self, source):
        payload = source.payload
        scheme, _, path = payload["reference"]["uri"].partition(":")
        relative_path(path)
        checksum = payload["sha256"]
        if (payload["reference"]["version"] != "sha256:" + checksum
                or payload["media_type"] not in {"text/plain; charset=utf-8", "text/markdown; charset=utf-8"}):
            raise ModelError("根拠の原本の版または媒体が不正です")
        if scheme == "raw" and "article_revision_id" not in payload:
            if self.raw is None:
                raise _UnreadableSource("raw_root_unavailable")
            try:
                data = self.raw.read_bytes(self.raw.root / path)
            except OSError as error:
                raise _UnreadableSource("raw_unreadable") from error
            if data is None:
                raise _UnreadableSource("raw_missing")
            if publish.digest(data) != checksum:
                raise _UnreadableSource("raw_changed")
        elif scheme == "article" and payload.get("article_revision_id") in self.records:
            article = self.records[payload["article_revision_id"]]
            if (article.kind != "article_revision" or not is_article_path(path)
                    or (article.payload["page"], article.payload["sha256"]) != (path, checksum)):
                raise ModelError("記事版と根拠の原本参照が一致しません")
            if path not in self.files:
                raise _UnreadableSource("article_missing")
            if self.files[path] != checksum:
                raise _UnreadableSource("article_changed")
            data = _read_article(self.guard, self.files, path)
        else:
            raise ModelError("根拠の原本参照の形式が不正です")
        try:
            data.decode("utf-8")
        except UnicodeError as error:
            raise ModelError("根拠の原本が UTF-8 ではありません") from error
        return data


def _read_fixed(guard, files, name):
    data = publish.read(guard, guard.root / publish.relative(name))
    if files.get(name) != publish.digest(data):
        raise publish.PublicationError(f"公開ファイルの指紋が一致しません: {name}")
    return data


def _read_article(guard, files, path):
    if article_path(guard.root, path).state != "ok":
        raise publish.PublicationError("公開版の記事を読み取れません")
    return _read_fixed(guard, files, path)


def _registered_roots(wiki, wiki_id):
    paths, _ = read_raw_roots(wiki)
    roots = {}
    for path in paths:
        try:
            guard = external_raw_guard(wiki, path)
        except (OSError, ValueError):
            continue
        roots[fingerprint("registered-raw-root-v1", [wiki_id, list(guard.root_id)])] = guard
    return roots


def _adoption_assessments(records):
    # P4 の失効は報告を追記する。採用時の評価は採否と同じ報告一覧を持つ。
    adoptions = {}
    for row in records.values():
        if row.kind != "current_assessment":
            continue
        assessment = row.payload
        decision = records[assessment["decision_id"]].payload
        if decision["disposition"] != "accept" or assessment["reports"] != decision["reports"]:
            continue
        revision_id = assessment["revision_id"]
        if revision_id in adoptions:
            raise ModelError("同じ改訂に複数の採用時評価があります")
        adoptions[revision_id] = row
    return adoptions


def _assessment(row, definition_id):
    if row is None:
        return None
    value = row.payload
    return {"record_id": row.record_id, "definition_snapshot_id": definition_id,
            **{key: value[key] for key in ("activation_id", "processing_context_id", "decision_id",
                                           "compatibility", "evidence_state")}}


def _read_evidence(inputs, revision):
    result = []
    for link in revision.payload["evidence_links"]:
        evidence_id = link["evidence_id"]
        item = {**link, "readability": "unavailable"}
        if inputs is None:
            item["reason"] = "no_adoption"
        else:
            row = inputs.records[evidence_id]
            try:
                inputs.evidence(row, revision.payload["claim_id"])
            except _UnreadableSource as error:
                item["reason"] = error.args[0]
            else:
                item.update(readability="available", evidence=row.to_dict(),
                            source=inputs.records[row.payload["source_id"]].to_dict())
        result.append(item)
    return result


def _claims(value, view, guard, files, claim_id):
    manifest = value.manifest
    heads = manifest["heads"]["claims"]
    wanted = heads.keys() if claim_id is None else heads.keys() & {claim_id}
    if not wanted:
        return []
    records = {row.record_id: row for row in value.records}
    claims = {row.payload["claim_id"]: row for row in value.records if row.kind == "claim"}
    current = {records[key].payload["revision_id"]: records[key] for key in manifest["current_assessments"]}
    adoptions = _adoption_assessments(records)
    roots = _registered_roots(view.wiki, manifest["wiki_id"])
    result = []
    for key in sorted(wanted):
        revision = records[heads[key]]
        adopted = adoptions.get(revision.record_id)
        selected = current.get(revision.record_id)
        inputs = None
        if adopted and revision.payload["evidence_links"]:
            request = adoption_request(records, adopted.payload)
            inputs = _PublishedEvidence(records, view, roots.get(request["raw_root_id"]),
                                        request["original_confirmations"], guard, files)
        claim = claims[key]
        definition_id = claim.payload["relationship"]["definition_snapshot_id"]
        result.append({"claim": claim.to_dict(), "revision": revision.to_dict(),
                       "adoption_assessment": _assessment(adopted, definition_id),
                       "selected_assessment": _assessment(selected, definition_id),
                       "evidence": _read_evidence(inputs, revision)})
    return result


def _empty_fields(summary):
    return {"claim_count": None} if summary else {"article": None, "claims": []}


def _read_view(view, article, claim_id, summary):
    owner = WriteGuard(view.wiki, strict_root=True)
    files = publish.manifest(owner, owner.root / publish.AREA, view.release_id)["files"]
    guard = WriteGuard(view.root, strict_root=True)
    result = {"state": "unconfigured", **_empty_fields(summary)}
    if article is not None:
        result["article"] = {"path": article, "state": "unavailable"}
        if article in files:
            data = _read_article(guard, files, article)
            result["article"].update(state="available", sha256=files[article], text=data.decode("utf-8-sig"))

    catalog = _read_fixed(guard, files, "ontology/definition.json")
    manifest_bytes = _read_fixed(guard, files, "ontology/ossie/release.json")
    records = _read_fixed(guard, files, "ontology/ossie/records.jsonl")
    if detect_bytes(catalog, manifest_bytes, records) == 1:
        return result
    value = store.decode(records, manifest_bytes)
    if catalog != value.catalog_json:
        raise ModelError("catalog と意味マニフェストの定義・版・固定参照が一致しません")
    manifest = value.manifest
    result.update({key: manifest[key] for key in _CONTEXT_KEYS})
    if manifest["activation_id"] is not None:
        result["state"] = "ready"
        if summary:
            result["claim_count"] = len(manifest["heads"]["claims"])
        else:
            result["claims"] = _claims(value, view, guard, files, claim_id)
    return result


def read_semantics(wiki, *, release_id=None, claim_id=None, article=None):
    """指定した公開版、または照会開始時の先頭の版について評価を返す。"""
    return _read_semantics(wiki, release_id=release_id, claim_id=claim_id, article=article)


def read_semantic_status(wiki, *, release_id=None):
    """根拠を開かず、公開版の選択と主張 head の件数を返す。"""
    return _read_semantics(wiki, release_id=release_id, summary=True)


def _read_semantics(wiki, *, release_id, claim_id=None, article=None, summary=False):
    if claim_id is not None:
        digest_id(claim_id)
    if article is not None and not is_article_path(publish.relative(article)):
        raise publish.PublicationError("記事の相対パスを指定してください")
    wiki = Path(wiki).resolve()
    archive = publish.archived_root(wiki)
    requested = release_id if release_id is not None else archive[1] if archive else None
    result = {"state": "not_published", "scope": "tip" if requested is None else "fixed",
              "release_id": requested, **_empty_fields(summary), **dict.fromkeys(_CONTEXT_KEYS)}
    try:
        view = publish.resolve_publication(wiki, release_id)
        if view is None:
            return result
        result["release_id"] = view.release_id
        reply = _read_view(view, article, claim_id, summary)
        # 先頭が進んでも初めに選んだ版を再照合し、読取中の退役も反映する。
        publish.resolve_publication(view.wiki, view.release_id)
    except publish.RetiredPublication:
        return {**result, "state": "retired"}
    return {**result, **reply}
