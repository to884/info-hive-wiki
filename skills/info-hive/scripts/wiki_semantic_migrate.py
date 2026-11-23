"""P2 の形式1移行プレビュー。元ファイルを更新せず、旧値を検証済みへ昇格させない。"""

from base64 import b64encode
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from wiki_fsguard import WriteGuard
from wiki_semantic_model import Record, ModelError, canonical, digest_id, fingerprint, load_json, relative_path, text, _unique_object
from wiki_semantic_store import Snapshot, snapshot


KNOWN_TABLES = frozenset({"definitions", "entities", "articles", "claims", "revisions", "heads",
                          "evidence", "supports", "references", "record_references", "holds",
                          "decisions", "legacy_refs"})


def _json(raw):
    def reject(value):
        raise ModelError(f"旧 JSON の非有限数は未対応です: {value}")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_float=Decimal, parse_constant=reject)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ModelError("旧 JSON の形式を読み取れません") from error


def _cell(value):
    if value is None:
        return ["null", None]
    if type(value) is int:
        return ["integer", str(value)]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", b64encode(value).decode("ascii")]
    if type(value) is float:
        return ["real", value.hex()]
    raise ModelError("旧 SQLite の値型を保存できません")


def _comparison(value):
    if isinstance(value, Decimal):
        return ("number", value)
    if type(value) is float:
        return ("number", Decimal(str(value)))
    if type(value) is int:
        return ("integer", value)
    if value is None or isinstance(value, (str, bytes)):
        return (type(value).__name__, value)
    raise ModelError("旧 JSONL と SQLite の間で照合できない値型です")


@dataclass(frozen=True)
class MigrationPreview:
    value: Snapshot
    report_json: bytes

    @property
    def report(self):
        return load_json(self.report_json)


def preview_legacy(directory, *, operation_id, basis, catalog=None):
    try:
        return _preview_legacy(directory, operation_id=operation_id, basis=basis, catalog=catalog)
    except (OSError, RecursionError, TypeError) as error:
        raise ModelError(f"旧保存形式を読み取れません: {error}") from error


def _preview_legacy(directory, *, operation_id, basis, catalog):
    """固定した旧バンドルの全行とメタデータを保持する。適用・公開は提供しない。"""
    if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise ModelError("移行操作 ID は32桁の小文字16進数にしてください")
    if not isinstance(basis, dict) or basis.get("mode") not in {"live", "publication"}:
        raise ModelError("公開版またはライブの固定基準を指定してください")
    key = "release_id" if basis["mode"] == "publication" else "live_base_sha256"
    if basis.keys() != {"mode", key}:
        raise ModelError("基準の形式が不正です")
    digest_id(basis[key])
    guard = WriteGuard(Path(directory).absolute(), strict_root=True)
    inputs = {}
    source_locations = {}
    for name in ("records.jsonl", "release.json", "wiki.ontology.json", "wiki.semantic_model.json", "wiki.mapping.json"):
        data = guard.read_bytes(guard.root / name)
        if data is None:
            if name in {"records.jsonl", "release.json"}:
                raise ModelError(f"移行元に必要なファイルがありません: {name}")
        else:
            inputs[name] = data
            source_locations[name] = (guard, guard.root / name)
    catalog_check = None
    if catalog is None and guard.root.name == "ossie" and guard.root.parent.name == "ontology":
        catalog = guard.root.parent / "definition.json"
        catalog_required = False
    else:
        catalog_required = catalog is not None
    if catalog is not None:
        catalog = Path(catalog).absolute()
        catalog_guard = WriteGuard(catalog.parent, strict_root=True)
        raw_catalog = catalog_guard.read_bytes(catalog)
        if raw_catalog is None and catalog_required:
            raise ModelError("指定した旧 catalog がありません")
        catalog_check = (catalog_guard, catalog, raw_catalog)
        if raw_catalog is not None:
            original_catalog = _json(raw_catalog)
            if (not isinstance(original_catalog, dict) or "semantic_format" in original_catalog
                    and (type(original_catalog["semantic_format"]) is not int or original_catalog["semantic_format"] != 1)):
                raise ModelError("旧 catalog は形式1ではありません")
            inputs["ontology/definition.json"] = raw_catalog
            source_locations["ontology/definition.json"] = (catalog_guard, catalog)
    release = _json(inputs["release.json"])
    if (not isinstance(release, dict) or "semantic_format" in release
            and (type(release["semantic_format"]) is not int or release["semantic_format"] != 1)):
        raise ModelError("旧形式1の意味マニフェストを指定してください")
    wiki_id = text(release.get("wiki_id"), "旧 wiki ID")
    release_id = text(release.get("release_id"), "旧版 ID")
    counts = release.get("rows")
    if not isinstance(counts, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in counts.items()):
        raise ModelError("旧版の全表の行数を指定してください")
    data_rows = {table: [] for table in counts}
    records, diagnostics, by_id = [], [], {}
    if catalog_check is None:
        diagnostics.append({"code": "legacy_catalog_not_provided"})
    def record(kind, payload):
        r = Record.create(wiki_id, kind, payload)
        if r.record_id not in by_id:
            records.append(r)
            by_id[r.record_id] = r
        return by_id[r.record_id]
    def legacy(format_, table, old_id, original):
        return record("legacy_record", {"format": format_, "release_id": release_id,
                                        "table": table, "legacy_id": old_id, "original": original})
    def link(old, new, kind="record"):
        record("migration_link", {"legacy_record_id": old.record_id, "target": {"kind": kind, "id": new},
                                  "operation_id": operation_id, "reason": "固定した旧値への対応。採用状態は継承しない"})
    identifiers = set()
    for i, line in enumerate(inputs["records.jsonl"].splitlines()):
        if not line.strip():
            diagnostics.append({"code": "blank_legacy_jsonl_line", "line": i + 1})
            continue
        row = _json(line)
        if not isinstance(row, dict) or not isinstance(row.get("table"), str) or row["table"] not in data_rows:
            raise ModelError("旧 JSONL に未登録の表または不正な行があります")
        table = row["table"]
        old_id = row.get("id", f"line:{i}")
        text(old_id, "旧行 ID")
        if (table, old_id) in identifiers:
            raise ModelError("旧 JSONL の表内で ID が重複しています")
        identifiers.add((table, old_id))
        old = legacy("ossie-v1-jsonl", table, old_id,
                     {"encoding": "utf8-base64", "content": b64encode(line).decode("ascii"),
                      "source_sha256": sha256(inputs["records.jsonl"]).hexdigest()})
        data_rows[table].append(({k: v for k, v in row.items() if k != "table"}, old))
    if {k: len(v) for k, v in data_rows.items()} != counts:
        raise ModelError("旧 JSONL とマニフェストの全行数が一致しません")
    database = guard.root / "wiki-data.sqlite"
    guard.check_sqlite(database)
    if any(guard.file_info(database.with_name(database.name + suffix)) is not None for suffix in ("-wal", "-shm", "-journal")):
        raise ModelError("旧 SQLite の未確定の sidecar は移行できません")
    db_bytes = guard.read_bytes(database)
    if db_bytes is None:
        raise ModelError("旧 SQLite がありません")
    source_hashes = {name: sha256(raw).hexdigest() for name, raw in inputs.items()}
    source_hashes["wiki-data.sqlite"] = sha256(db_bytes).hexdigest()
    source_locations["wiki-data.sqlite"] = (guard, database)
    db_counts = {}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ModelError("旧 SQLite の整合検査に失敗しました")
            # 旧値の参照不能は保持する対象であり、新形式の参照検査とは分ける。
            try:
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    diagnostics.append({"code": "legacy_reference_unresolved", "violations": [list(v) for v in violations]})
            except sqlite3.Error as error:
                diagnostics.append({"code": "legacy_reference_unchecked", "reason": str(error)})
            objects = db.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
            if any(kind in {"view", "trigger"} or sql and "CREATE VIRTUAL TABLE" in sql.upper()
                   for kind, _, sql in objects):
                raise ModelError("旧 DB のビュー・トリガー・仮想表は未対応です")
            for kind, name, sql in objects:
                if kind == "index":
                    legacy("ossie-v1-schema", name, "index", {"kind": kind, "sql": sql})
            for _, name, sql in sorted(x for x in objects if x[0] == "table"):
                quoted = '"' + name.replace('"', '""') + '"'
                columns = [r[1] for r in db.execute(f"PRAGMA table_info({quoted})")]
                rows = db.execute(f"SELECT * FROM {quoted}").fetchall()
                rows.sort(key=lambda r: canonical([_cell(v) for v in r]))
                db_counts[name] = len(rows)
                legacy("ossie-v1-schema", name, "schema", {"sql": sql, "columns": columns})
                json_table = name[4:] if name.startswith("sem_") else None
                pairs = data_rows.get(json_table)
                matches = {}
                if pairs is not None:
                    if any(set(row) != set(columns) for row, _ in pairs):
                        raise ModelError(f"旧 JSONL と DB の列が一致しません: {name}")
                    expected = Counter(tuple(_comparison(row[c]) for c in columns) for row, _ in pairs)
                    actual = Counter(tuple(_comparison(v) for v in row) for row in rows)
                    if expected != actual:
                        raise ModelError(f"旧 JSONL と DB の全行が一致しません: {name}")
                    for row, old in pairs:
                        matches.setdefault(tuple(_comparison(row[c]) for c in columns), []).append(old)
                else:
                    diagnostics.append({"code": "unmapped_sqlite_table", "table": name, "rows": len(rows)})
                for i, values in enumerate(rows):
                    old = legacy("ossie-v1-sqlite", name, f"row:{i}", {
                        "encoding": "sqlite-row-v1", "columns": columns, "values": [_cell(v) for v in values],
                        "source_sha256": source_hashes["wiki-data.sqlite"]})
                    if pairs is not None:
                        matched = matches[tuple(_comparison(v) for v in values)].pop()
                        link(old, matched.record_id)
    except sqlite3.Error as error:
        raise ModelError(f"旧 SQLite を読み取れません: {error}") from error
    if any("sem_" + table not in db_counts for table in counts):
        raise ModelError("旧 JSONL に対応する DB の表がありません")
    for name, wanted in source_hashes.items():
        source_guard, source_path = source_locations[name]
        current = source_guard.read_bytes(source_path)
        if current is None or sha256(current).hexdigest() != wanted:
            raise ModelError("プレビューの途中で移行元の内容が変わりました")
    if catalog_check is not None:
        catalog_guard, catalog_path, original = catalog_check
        if catalog_guard.read_bytes(catalog_path) != original:
            raise ModelError("プレビューの途中で旧 catalog の有無・内容が変わりました")
    for name, raw in inputs.items():
        legacy("ossie-v1-metadata", name, name, {"encoding": "utf8-base64", "content": b64encode(raw).decode("ascii"),
                                               "source_sha256": source_hashes[name]})
    heads = {"entities": {}, "claims": {}}
    for table, pairs in data_rows.items():
        if table not in KNOWN_TABLES:
            diagnostics.append({"code": "unknown_legacy_table", "table": table, "rows": len(pairs)})
        for row, old in pairs:
            if table in {"entities", "articles"} and row.get("wiki_id") != wiki_id:
                diagnostics.append({"code": "foreign_wiki_row", "table": table, "legacy_id": old.payload["legacy_id"]})
                continue
            if table == "entities":
                if not isinstance(row.get("name"), str) or not row["name"].strip():
                    diagnostics.append({"code": "entity_display_unavailable", "legacy_id": old.payload["legacy_id"]})
                    continue
                entity_id = fingerprint("migration-entity-v2", [wiki_id, operation_id, old.payload["legacy_id"]])[:32]
                record("entity", {"entity_id": entity_id, "operation_id": operation_id})
                revision = record("entity_revision", {"entity_id": entity_id, "previous_id": None,
                                                       "display": {"name": row["name"]}, "type_refs": []})
                heads["entities"][entity_id] = revision.record_id
                link(old, entity_id, "entity")
            elif table == "articles":
                try:
                    relative_path(row.get("page"))
                    digest_id(row.get("page_sha256"))
                    text(row.get("title"))
                except ModelError:
                    diagnostics.append({"code": "article_metadata_unavailable", "legacy_id": old.payload["legacy_id"]})
                    continue
                revision = record("article_revision", {"page": row["page"], "sha256": row["page_sha256"],
                                                        "title": row["title"], "origin": "unknown", "derived_from": []})
                link(old, revision.record_id)
    value = snapshot(records, wiki_id=wiki_id, heads=heads)
    # 外側の公開版の基準は呼出側の主張。P3 の適用で実際の読取集合と再照合する。
    report = {"state": "preview_only", "applicable": False, "operation_id": operation_id, "basis": basis,
              "sources": source_hashes, "jsonl_rows": counts, "sqlite_rows": db_counts,
              "catalog_state": "not_provided" if catalog_check is None else "present" if catalog_check[2] is not None else "absent",
              "output_rows": value.manifest["rows"], "semantic_snapshot_id": value.manifest["semantic_snapshot_id"],
              "diagnostics": diagnostics, "domain_definition": "not_configured"}
    report["preview_sha256"] = fingerprint("migration-preview-v2", report)
    return MigrationPreview(value, canonical(report))


def apply_preview(*args, **kwargs):
    raise ModelError("P2 は移行プレビューだけを提供します。採用・適用は有効化されていません")
