"""P2 の固定保存形式。正本は JSONL とマニフェスト、SQLite は再生成物。"""

from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from wiki_fsguard import WriteGuard
from wiki_semantic_model import Record, ModelError, canonical, current_eligible, digest_id, fingerprint, load_json, relative_path, text


MANIFEST_KEYS = frozenset({"semantic_format", "wiki_id", "parent", "record_ids", "heads",
                           "definition_snapshot_id", "activation_id", "processing_context_id",
                           "current_assessments", "rows", "semantic_snapshot_id"})
DDL = {
    "records": "CREATE TABLE records (record_id TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL, wiki_id TEXT NOT NULL, payload_json TEXT NOT NULL)",
    "record_refs": "CREATE TABLE record_refs (source_id TEXT NOT NULL REFERENCES records(record_id), field TEXT NOT NULL, target_id TEXT NOT NULL REFERENCES records(record_id), PRIMARY KEY (source_id, field))",
    "heads": "CREATE TABLE heads (space TEXT NOT NULL, id TEXT NOT NULL, revision_id TEXT NOT NULL REFERENCES records(record_id), PRIMARY KEY (space, id))",
    "roles": "CREATE TABLE roles (claim_id TEXT NOT NULL REFERENCES records(record_id), ordinal INTEGER NOT NULL, entity_id TEXT NOT NULL REFERENCES records(record_id), PRIMARY KEY (claim_id, ordinal))",
    "assessments": "CREATE TABLE assessments (record_id TEXT PRIMARY KEY NOT NULL REFERENCES records(record_id))",
    "metadata": "CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value_json TEXT NOT NULL)",
}
INDEXES = {
    "records_kind": "CREATE INDEX records_kind ON records(kind)",
    "refs_target": "CREATE INDEX refs_target ON record_refs(target_id)",
}


def _named_ids(records):
    spaces = {"record": {r.record_id: r for r in records}}
    for namespace, kind, field in (("entity", "entity", "entity_id"), ("claim", "claim", "claim_id"),
                                    ("activation", "definition_activation", "activation_id"),
                                    ("definition", "definition_snapshot", None),
                                    ("context", "processing_context", None)):
        mapping = {}
        for r in records:
            if r.kind != kind:
                continue
            p = r.payload
            if namespace == "definition":
                manifest = p["manifest"]
                if manifest.keys() != {"documents"} or not isinstance(manifest["documents"], list) or not manifest["documents"]:
                    raise ModelError("定義内容には固定文書の一覧を指定してください")
                paths = set()
                for doc in manifest["documents"]:
                    if not isinstance(doc, dict) or doc.keys() != {"path", "name", "sha256"}:
                        raise ModelError("定義文書の参照が不正です")
                    relative_path(doc["path"]); text(doc["name"]); digest_id(doc["sha256"])
                    if doc["path"] in paths:
                        raise ModelError("定義文書のパスが重複しています")
                    paths.add(doc["path"])
                key = fingerprint("definition-v1", manifest)
            elif namespace == "context":
                key = fingerprint("processing-v1", p["lock"])
            else:
                key = p[field]
            if key in mapping:
                raise ModelError(f"{namespace} の ID が重複しています")
            mapping[key] = r
        spaces[namespace] = mapping
    return spaces


def _project(records, manifest):
    """参照先の種類・所有者まで照合する。原文の支持や採用を判断しない。"""
    spaces = _named_ids(records)
    refs, roles = [], []
    def target(namespace, key, kinds=None):
        try:
            r = spaces[namespace][key]
        except (KeyError, TypeError) as error:
            raise ModelError(f"{namespace} の参照が解決しません") from error
        if kinds and r.kind not in kinds:
            raise ModelError(f"参照先のレコード種別が不正です: {r.kind}")
        return r
    for r in records:
        p = r.payload
        def add(field, key, namespace="record", kinds=None):
            t = target(namespace, key, kinds)
            refs.append((r.record_id, field, t.record_id))
            return t
        def array(field, values, kinds=None):
            for key in values:
                digest_id(key)
            if len(values) != len(set(values)):
                raise ModelError(f"{field} の参照が重複しています")
            for i, key in enumerate(values):
                add(f"{field}/{i}", key, kinds=kinds)
        def definition(field, ref):
            d = add(field, ref["definition_snapshot_id"], "definition")
            if ref["document"] not in {doc["path"] for doc in d.payload["manifest"]["documents"]}:
                raise ModelError("参照した文書が定義スナップショットにありません")
        if r.kind == "definition_activation":
            from wiki_semantic_model import activation_id
            if p["activation_id"] != activation_id(r.wiki_id, p["operation_id"], p["previous_id"], p["definition_snapshot_id"]):
                raise ModelError("有効化の ID が内容と一致しません")
            add("definition_snapshot_id", p["definition_snapshot_id"], "definition")
            if p["previous_id"] is not None:
                add("previous_id", p["previous_id"], "activation")
        elif r.kind == "entity_revision":
            add("entity_id", p["entity_id"], "entity")
            if "graph_article_revision_id" in p["display"]:
                add("display/graph_article_revision_id", p["display"]["graph_article_revision_id"],
                    kinds={"article_revision"})
            if p["previous_id"] is not None:
                prev = add("previous_id", p["previous_id"], kinds={"entity_revision"})
                if prev.payload["entity_id"] != p["entity_id"]:
                    raise ModelError("別の実体の改訂を前改訂にできません")
            for i, ref in enumerate(p["type_refs"]):
                definition(f"type_refs/{i}", ref)
        elif r.kind == "claim":
            definition("relationship", p["relationship"])
            for b in p["role_bindings"]:
                entity = add(f"role_bindings/{b['ordinal']}", b["entity_id"], "entity")
                roles.append((r.record_id, b["ordinal"], entity.record_id))
        elif r.kind == "claim_revision":
            add("claim_id", p["claim_id"], "claim")
            if p["previous_id"] is not None:
                prev = add("previous_id", p["previous_id"], kinds={"claim_revision"})
                if prev.payload["claim_id"] != p["claim_id"]:
                    raise ModelError("別の主張の改訂を前改訂にできません")
            for i, link in enumerate(p["evidence_links"]):
                add(f"evidence_links/{i}", link["evidence_id"], kinds={"evidence"})
        elif r.kind == "source_snapshot" and p.get("article_revision_id") is not None:
            add("article_revision_id", p["article_revision_id"], kinds={"article_revision"})
        elif r.kind == "evidence":
            add("source_id", p["source_id"], kinds={"source_snapshot"})
        elif r.kind == "evidence_assessment":
            claim = add("claim_id", p["claim_id"], "claim")
            add("evidence_id", p["evidence_id"], kinds={"evidence"})
            add("definition_snapshot_id", p["definition_snapshot_id"], "definition")
            add("processing_context_id", p["processing_context_id"], "context")
            if claim.payload["relationship"]["definition_snapshot_id"] != p["definition_snapshot_id"]:
                raise ModelError("根拠評価と主張の定義内容が一致しません")
        elif r.kind in {"decision", "current_assessment", "validation_report"}:
            add("processing_context_id", p["processing_context_id"], "context")
            if r.kind != "validation_report":
                add("activation_id", p["activation_id"], "activation")
                array("reports", p["reports"], {"validation_report"})
                if any(target("record", key).payload["processing_context_id"] != p["processing_context_id"] for key in p["reports"]):
                    raise ModelError("検査報告の処理条件が一致しません")
            if r.kind == "decision":
                array("targets", p["targets"])
            elif r.kind == "current_assessment":
                add("revision_id", p["revision_id"], kinds={"entity_revision", "claim_revision"})
                decision = add("decision_id", p["decision_id"], kinds={"decision"})
                if any(decision.payload[k] != p[k] for k in ("activation_id", "processing_context_id")):
                    raise ModelError("現行評価と採否の条件が一致しません")
                if p["revision_id"] not in decision.payload["targets"]:
                    raise ModelError("現行評価の対象改訂に対応する採否がありません")
                deps = p["dependencies"]
                if deps.keys() != {"entities", "evidence"} or any(not isinstance(v, list) for v in deps.values()):
                    raise ModelError("依存には実体改訂と根拠の配列を指定してください")
                array("dependencies/entities", deps["entities"], {"entity_revision"})
                array("dependencies/evidence", deps["evidence"], {"evidence"})
        elif r.kind == "record_link":
            for key in ("from_id", "to_id"):
                add(key, p[key], kinds={"entity_revision", "claim_revision"})
            for key, kind in (("evidence_id", "evidence"), ("decision_id", "decision")):
                if p.get(key) is not None:
                    add(key, p[key], kinds={kind})
        elif r.kind == "article_revision":
            relative_path(p["page"])
            array("derived_from", p["derived_from"], {"article_revision", "source_snapshot", "evidence", "claim_revision"})
        elif r.kind == "migration_link":
            add("legacy_record_id", p["legacy_record_id"], kinds={"legacy_record"})
            add("target", p["target"]["id"], p["target"]["kind"])

    return {"record_refs": sorted(refs), "roles": sorted(roles), **_selected_state(records, manifest, target)}


def _selected_state(records, manifest, target):
    """選択状態の不変条件をまとめて照合する。履歴は残し、不整合な選択だけを拒否する。"""
    heads = []
    predecessors = {r.payload["previous_id"] for r in records
                    if r.kind in {"entity_revision", "claim_revision"} and r.payload["previous_id"] is not None}
    if not isinstance(manifest["heads"], dict) or manifest["heads"].keys() != {"entities", "claims"}:
        raise ModelError("head には実体と主張の辞書を指定してください")
    for space, namespace, kind, owner in (("entities", "entity", "entity_revision", "entity_id"),
                                         ("claims", "claim", "claim_revision", "claim_id")):
        if not isinstance(manifest["heads"][space], dict):
            raise ModelError("head は辞書にしてください")
        for key, revision in manifest["heads"][space].items():
            target(namespace, key)
            row = target("record", revision, {kind})
            if row.payload[owner] != key:
                raise ModelError("head が別の実体・主張の改訂を指しています")
            if row.record_id in predecessors:
                raise ModelError("後継改訂を持つ古い改訂を現行 head にできません")
            heads.append((space, key, revision))
    selectors = [manifest[k] for k in ("definition_snapshot_id", "activation_id", "processing_context_id")]
    if not (all(x is None for x in selectors) or all(isinstance(x, str) for x in selectors)):
        raise ModelError("定義・有効化・処理条件の選択は同時に指定してください")
    if selectors[0] is not None:
        target("definition", selectors[0])
        activation = target("activation", selectors[1])
        target("context", selectors[2])
        if activation.payload["definition_snapshot_id"] != selectors[0]:
            raise ModelError("選択した定義内容と有効化が一致しません")
    assessments = manifest["current_assessments"]
    if not isinstance(assessments, list) or any(not isinstance(key, str) for key in assessments) or assessments != sorted(set(assessments)):
        raise ModelError("現行評価は重複しない ID の昇順配列にしてください")
    selected_revisions = set()
    for key in assessments:
        p = target("record", key, {"current_assessment"}).payload
        if p["revision_id"] in selected_revisions:
            raise ModelError("同じ改訂の現行評価を複数選択できません")
        selected_revisions.add(p["revision_id"])
        if p["activation_id"] != selectors[1] or p["processing_context_id"] != selectors[2]:
            raise ModelError("別の有効化・処理条件の現行評価を選択できません")
        revision = target("record", p["revision_id"])
        space, owner = ("entities", "entity_id") if revision.kind == "entity_revision" else ("claims", "claim_id")
        if manifest["heads"][space].get(revision.payload[owner]) != revision.record_id:
            raise ModelError("現行 head 以外の改訂の評価を選択できません")
        if revision.kind == "claim_revision":
            claim = target("claim", revision.payload["claim_id"])
            if claim.payload["relationship"]["definition_snapshot_id"] != selectors[0]:
                raise ModelError("現行評価の主張が別の定義内容を参照しています")
            expected_entities = {b["entity_id"] for b in claim.payload["role_bindings"]}
            expected_evidence = {link["evidence_id"] for link in revision.payload["evidence_links"]}
        else:
            expected_entities = {revision.payload["entity_id"]}
            expected_evidence = set(p["dependencies"]["evidence"])
        dependencies_verified = False
        if p["evidence_state"] == "verified":
            dependencies = p["dependencies"]
            entity_revisions = [target("record", r, {"entity_revision"}) for r in dependencies["entities"]]
            actual_entities = {r.payload["entity_id"]: r.record_id for r in entity_revisions}
            if (len(actual_entities) != len(entity_revisions) or set(actual_entities) != expected_entities
                    or any(manifest["heads"]["entities"].get(entity) != rev for entity, rev in actual_entities.items())
                    or not expected_evidence or set(dependencies["evidence"]) != expected_evidence):
                raise ModelError("検証済み評価の依存が役割・根拠リンク・現行の実体 head と一致しません")
            dependencies_verified = True
        if p["compatibility"] == "compatible" and p["evidence_state"] == "verified":
            decision = target("record", p["decision_id"])
            reports = [target("record", key).payload["checks"] for key in p["reports"]]
            if (decision.payload["disposition"] != "accept" or not reports
                    or any(not checks or any(c["state"] != "pass" for c in checks.values()) for checks in reports)):
                raise ModelError("検証済みの現行評価には採用と合格した検査報告が必要です")
            if not current_eligible(lifecycle=revision.payload.get("lifecycle", "accepted"),
                    decision_activation=decision.payload["activation_id"], assessment_activation=p["activation_id"],
                    current_activation=selectors[1], assessment_context=p["processing_context_id"],
                    current_context=selectors[2], compatibility=p["compatibility"], evidence_state=p["evidence_state"],
                    dependencies_verified=dependencies_verified):
                raise ModelError("P0 の現行状態の必要条件を満たしていません")
    return {"heads": sorted(heads),
            "assessments": [(key,) for key in assessments]}


@dataclass(frozen=True)
class Snapshot:
    records: tuple[Record, ...]
    manifest_json: bytes

    def __post_init__(self):
        if type(self.records) is not tuple or any(not isinstance(r, Record) for r in self.records):
            raise ModelError("固定レコードの不変配列を指定してください")
        if not isinstance(self.manifest_json, bytes):
            raise ModelError("マニフェストは固定した bytes にしてください")
        m = load_json(self.manifest_json)
        if not isinstance(m, dict) or m.keys() != MANIFEST_KEYS or type(m["semantic_format"]) is not int or m["semantic_format"] != 2:
            raise ModelError("意味マニフェストの形式が不正です")
        text(m["wiki_id"])
        if m["parent"] is not None:
            digest_id(m["parent"])
        ids = [r.record_id for r in self.records]
        if ids != sorted(set(ids)) or m["record_ids"] != ids or any(r.wiki_id != m["wiki_id"] for r in self.records):
            raise ModelError("レコードの ID・順序・wiki の範囲が一致しません")
        counts = {kind: sum(r.kind == kind for r in self.records) for kind in {r.kind for r in self.records}}
        if m["rows"] != counts or any(type(x) is not int for x in m["rows"].values()):
            raise ModelError("種別ごとの全行数が一致しません")
        body = {k: v for k, v in m.items() if k != "semantic_snapshot_id"}
        if m["semantic_snapshot_id"] != fingerprint("semantic-snapshot-v2", body):
            raise ModelError("意味スナップショットの指紋が一致しません")
        if canonical(m) + b"\n" != self.manifest_json:
            raise ModelError("意味マニフェストのバイト表現が正規形ではありません")
        _project(self.records, m)

    @property
    def manifest(self):
        return load_json(self.manifest_json)

    @property
    def jsonl(self):
        return b"".join(canonical(r.to_dict()) + b"\n" for r in self.records)

    @property
    def catalog_json(self):
        m = self.manifest
        catalog = {key: m[key] for key in ("semantic_format", "wiki_id", "semantic_snapshot_id",
                                          "definition_snapshot_id", "activation_id", "processing_context_id")}
        catalog["definitions"] = {key: {"root": f"definitions/{key}", "manifest": row.payload["manifest"]}
                                  for key, row in _named_ids(self.records)["definition"].items()}
        return canonical(catalog) + b"\n"


def snapshot(records, *, wiki_id, parent=None, heads=None, definition_snapshot_id=None,
             activation_id=None, processing_context_id=None, current_assessments=()):
    records = tuple(records)
    if any(not isinstance(r, Record) for r in records):
        raise ModelError("固定レコードを指定してください")
    records = tuple(sorted(records, key=lambda r: r.record_id))
    m = {"semantic_format": 2, "wiki_id": wiki_id, "parent": parent,
         "record_ids": [r.record_id for r in records], "heads": heads if heads is not None else {"entities": {}, "claims": {}},
         "definition_snapshot_id": definition_snapshot_id, "activation_id": activation_id,
         "processing_context_id": processing_context_id, "current_assessments": sorted(current_assessments),
         "rows": {kind: sum(r.kind == kind for r in records) for kind in {r.kind for r in records}}}
    m["semantic_snapshot_id"] = fingerprint("semantic-snapshot-v2", m)
    return Snapshot(records, canonical(m) + b"\n")


def decode(jsonl, manifest):
    if not isinstance(jsonl, bytes) or jsonl and not jsonl.endswith(b"\n"):
        raise ModelError("JSONL は末尾改行を含む bytes にしてください")
    records = tuple(Record.from_dict(load_json(line)) for line in jsonl.splitlines())
    value = Snapshot(records, manifest)
    if value.jsonl != jsonl:
        raise ModelError("JSONL のバイト表現が正規形ではありません")
    return value


def _tables(value):
    return {"records": [(r.record_id, r.kind, r.wiki_id, canonical(r.payload).decode("utf-8")) for r in value.records],
            **_project(value.records, value.manifest),
            "metadata": [("manifest", canonical(value.manifest).decode("utf-8"))]}


def _write_sqlite(value, path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        for ddl in (*DDL.values(), *INDEXES.values()):
            db.execute(ddl)
        rows = _tables(value)
        for table in DDL:
            entries = rows[table]
            if entries:
                db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in entries[0])})", entries)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ModelError("派生 DB の参照が一致しません")


def verify_sqlite(value, path):
    try:
        return _verify_sqlite(value, path)
    except (OSError, RecursionError, TypeError) as error:
        raise ModelError(f"派生 DB の拒否条件を確認してください: {error}") from error


def _verify_sqlite(value, path):
    path = Path(path).absolute()
    guard = WriteGuard(path.parent, strict_root=True)
    guard.check_sqlite(path)
    if any(guard.file_info(path.with_name(path.name + suffix)) is not None for suffix in ("-wal", "-shm", "-journal")):
        raise ModelError("未確定の SQLite sidecar がある版は読めません")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ModelError("派生 DB の整合検査に失敗しました")
            schema = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"))
            if schema != {**DDL, **INDEXES}:
                raise ModelError("派生 DB の表・索引・ビューが固定形式と一致しません")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise ModelError("派生 DB の参照が一致しません")
            for table, expected in _tables(value).items():
                actual = db.execute(f"SELECT * FROM {table}").fetchall()
                typed = lambda rows: Counter(tuple((type(v).__name__, v) for v in row) for row in rows)
                if typed(actual) != typed(expected):
                    raise ModelError(f"JSONL・意味マニフェストと DB の全行が一致しません: {table}")
    except sqlite3.Error as error:
        raise ModelError(f"派生 DB を読み取れません: {error}") from error


def exchange_documents():
    """汎用交換文書の正規バイト。生成と公開直前の照合で共有する。"""
    # 汎用レコードの交換定義。旧 writer の WikiArticle / sem_articles とは共有しない。
    fields = ["record_id", "kind", "wiki_id", "payload_json"]
    identity = {"referent_mappings": [{"relationship": "record_id", "expression": "records.record_id"}]}
    ont = {"version": "0.3.0.dev0", "name": "Semantic records v2", "ontology": [{
        "concept": "SemanticRecord", "type": "EntityType", "identify_by": ["record_id"],
        "relationships": [{"name": f, "roles": [{"concept": "String"}], "verbalizes": [],
                           "multiplicity": "ManyToOne"} for f in fields]}]}
    core = {"version": "0.3.0.dev0", "name": "Semantic records v2 data", "relationships": [],
            "datasets": [{"name": "records", "source": "records", "primary_key": ["record_id"],
                          "fields": [{"name": f, "datatype": "String", "expression": {"sql": f'source."{f}"'}}
                                     for f in fields]}]}
    mapping = {"version": "0.3.0.dev0", "name": "Semantic records v2 mapping",
               "ontology_ref": {"name": ont["name"], "iri": "./wiki.ontology.json"},
               "semantic_model_ref": {"name": core["name"], "iri": "./wiki.semantic_model.json"},
               "concept_mappings": [{"concept": "SemanticRecord", "object_mappings": [identity],
                 "link_mappings": [{"object_mapping": identity, "children": [
                    {"relationship": f, "object_mapping": {"concept": "String", "expression": f"records.{f}"}}
                    for f in fields if f != "record_id"]}]}]}
    documents = {"wiki.ontology.json": ont, "wiki.semantic_model.json": core, "wiki.mapping.json": mapping}
    return {name: canonical(doc) + b"\n" for name, doc in documents.items()}


@contextmanager
def artifacts(value):
    """自分で作った一時ディレクトリにだけ生成する。wiki への適用入口は持たない。"""
    if not isinstance(value, Snapshot):
        raise ModelError("検査した意味スナップショットを指定してください")
    with TemporaryDirectory(prefix="info-hive-semantic-preview-") as name:
        ontology = Path(name) / "ontology"
        folder = ontology / "ossie"
        folder.mkdir(parents=True)
        (ontology / "definition.json").write_bytes(value.catalog_json)
        (folder / "records.jsonl").write_bytes(value.jsonl)
        (folder / "release.json").write_bytes(value.manifest_json)
        for path, data in exchange_documents().items():
            (folder / path).write_bytes(data)
        _write_sqlite(value, folder / "wiki-data.sqlite")
        verify_sqlite(value, folder / "wiki-data.sqlite")
        yield folder


def read_bundle(directory):
    try:
        return _read_bundle(directory)
    except (OSError, RecursionError) as error:
        raise ModelError(f"意味保存版を読み取れません: {error}") from error


def _read_bundle(directory):
    guard = WriteGuard(Path(directory).absolute(), strict_root=True)
    jsonl = guard.read_bytes(guard.root / "records.jsonl")
    manifest = guard.read_bytes(guard.root / "release.json")
    if jsonl is None or manifest is None:
        raise ModelError("JSONL または意味マニフェストがありません")
    value = decode(jsonl, manifest)
    catalog_guard = WriteGuard(guard.root.parent, strict_root=True)
    if catalog_guard.read_bytes(catalog_guard.root / "definition.json") != value.catalog_json:
        raise ModelError("catalog と意味マニフェストの定義・版・固定参照が一致しません")
    verify_sqlite(value, guard.root / "wiki-data.sqlite")
    return value


def load_contract(value, definition_id, directory, *, specification):
    """保存された ready を信頼せず、元文書を P1 の公式検査から再読込する。"""
    from wiki_ossie_contract import compile_contract, inspect_bundle
    selected = _named_ids(value.records)["definition"].get(definition_id)
    if selected is None:
        raise ModelError("保存版に定義内容がありません")
    manifest = selected.payload["manifest"]
    if len(manifest["documents"]) != 1:
        raise ModelError("P1 の単独 Ontology プロファイル以外は未対応です")
    inspected = inspect_bundle(directory, ontology=manifest["documents"][0]["path"], specification=specification)
    contract = compile_contract(inspected)
    if contract.definition_snapshot_id != definition_id:
        raise ModelError("元文書と保存した定義内容の指紋が一致しません")
    expected_context = value.manifest["processing_context_id"]
    if expected_context is not None and contract.processing_context_id != expected_context:
        raise ModelError("処理条件が変わっています。既存の評価を流用できません")
    return contract
