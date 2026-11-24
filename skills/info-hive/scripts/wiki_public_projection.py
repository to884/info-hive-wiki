"""記事の保存に伴い、公開候補のグラフと Ossie の記事履歴を更新する。"""

from contextlib import closing
import json
import sqlite3

import wiki_apply as publish
from wiki_live_apply import write
from wiki_lint import parse_frontmatter
from wiki_pages import is_page_path


ARTICLE_FIELDS = ("id", "wiki_id", "title", "page", "page_sha256")
ARTICLE_SQL = "SELECT id, wiki_id, title, page, page_sha256 FROM sem_articles"


def json_file(guard, path):
    return json.loads(publish.read(guard, path))


def semantic_key(wiki_id, table, key):
    return publish.digest(publish.encoded([wiki_id, table, key]).rstrip(b"\n"))


def article_documents(guard, directory):
    ontology = json_file(guard, directory / "wiki.ontology.json")
    concepts = {entry["concept"] for entry in ontology["ontology"]}
    model = json_file(guard, directory / "wiki.semantic_model.json")
    datasets = [entry for entry in model["datasets"] if entry["name"] == "sem_articles"]
    mapping = json_file(guard, directory / "wiki.mapping.json")
    article_mappings = [entry for entry in mapping["concept_mappings"] if entry["concept"] == "WikiArticle"]
    identity = {"referent_mappings": [{"relationship": "id", "expression": "sem_articles.id"}]}
    attributes = [{"relationship": field, "object_mapping": {"concept": "String", "expression": f"sem_articles.{field}"}}
                  for field in ARTICLE_FIELDS if field != "id"]
    if ("WikiArticle" not in concepts or len(datasets) != 1 or len(article_mappings) != 1
            or datasets[0]["source"] != "sem_articles" or datasets[0]["primary_key"] != ["id"]
            or sorted((field["name"], field["datatype"], field["expression"].get("sql"))
                      for field in datasets[0]["fields"])
            != sorted((field, "String", f'source."{field}"') for field in ARTICLE_FIELDS)
            or article_mappings[0]["object_mappings"] != [identity]
            or article_mappings[0]["link_mappings"] != [{"object_mapping": identity, "children": attributes}]):
        raise publish.Conflict("公開版に記事履歴を保存できる Ossie の定義がありません")


def article_rows(guard, directory, changes):
    article_documents(guard, directory)
    release = json_file(guard, directory / "release.json")
    wiki_id = release["wiki_id"]
    previous_id = publish.checked(release["release_id"])
    rows = {name: [] for name in release["rows"]}
    original = {}
    for line in publish.read(guard, directory / "records.jsonl").splitlines():
        record = json.loads(line)
        table = record.pop("table")
        if table not in rows:
            raise publish.PublicationError("Ossie の版に記録されていない表があります")
        rows[table].append(record)
        if table == "articles":
            if record.keys() != set(ARTICLE_FIELDS) or record["id"] in original:
                raise publish.PublicationError("Ossie の記事履歴が重複しているか、列が一致しません")
            original[record["id"]] = tuple(record[k] for k in ARTICLE_FIELDS)
    if {table: len(entries) for table, entries in rows.items()} != release["rows"]:
        raise publish.PublicationError("Ossie の版と意味レコードの行数が一致しません")
    known = {(row[1], row[3], row[4]) for row in original.values()}
    additions = []
    for name, (_, after) in changes.items():
        if after is None:
            continue
        checksum = publish.digest(after)
        if (wiki_id, name, checksum) in known:
            continue
        meta, _, _ = parse_frontmatter(after.decode("utf-8-sig"))
        title = meta.get("title")
        if not isinstance(title, str) or not title.strip():
            title = name.rsplit("/", 1)[-1][:-3]
        new = dict(zip(ARTICLE_FIELDS, (semantic_key(wiki_id, "articles", [name, checksum]),
                                        wiki_id, title, name, checksum)))
        if new["id"] in original:
            raise publish.PublicationError("Ossie の既存の記事 ID と新しい記事が衝突しました")
        additions.append(tuple(new[k] for k in ARTICLE_FIELDS))
        rows["articles"].append(new)
    update_article_db(guard, directory / "wiki-data.sqlite", original, additions)
    if additions:
        for entries in rows.values():
            entries.sort(key=lambda row: row["id"])
        output = b"".join(publish.encoded({"table": table, **row}) for table, entries in rows.items() for row in entries)
        write(guard, directory / "records.jsonl", output, replace=True)
        release["parent_release_id"] = previous_id
        release["release_id"] = semantic_key(wiki_id, "release", [release.get("definition_id"), previous_id, rows])
        release["rows"]["articles"] = len(rows["articles"])
        if isinstance(release.get("local_mapped_facts"), int):
            release["local_mapped_facts"] += len(ARTICLE_FIELDS) * len(additions)
        release["official_validation"] = {"state": "not_run"}
        write(guard, directory / "release.json", publish.encoded(release), replace=True)


def update_article_db(guard, path, original, additions):
    guard.check_sqlite(path)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            article_table = db.execute("PRAGMA table_info(sem_articles)").fetchall()
            if {row[1] for row in article_table} != set(ARTICLE_FIELDS):
                raise publish.Conflict("公開版の SQLite に対応する記事履歴の表がありません")
            previous = db.execute(ARTICLE_SQL).fetchall()
            if len(previous) != len(original) or {row[0]: row for row in previous} != original:
                raise publish.PublicationError("Ossie の SQLite と記事履歴が一致しません")
            if additions:
                db.executemany("INSERT INTO sem_articles (id, wiki_id, title, page, page_sha256) VALUES (?, ?, ?, ?, ?)",
                               additions)
                if set(db.execute(ARTICLE_SQL).fetchall()) != set(original.values()) | set(additions):
                    raise publish.PublicationError("Ossie の記事履歴を照合できません")
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise publish.PublicationError("Ossie の意味レコードの参照が一致しません")
    except sqlite3.Error as error:
        raise publish.PublicationError(f"Ossie の SQLite を更新できません: {error}") from error


def update_projections(guard, changes, *, force=False):
    publish.require_semantic_format1(guard)
    articles = {path: pair for path, pair in changes.items() if is_page_path(path)}
    if articles or force:
        update_graph(guard)
        article_rows(guard, guard.root / "ontology/ossie", articles)


def update_graph(guard, *, output=None):
    """公開候補の記事と意味版から、その候補のグラフを更新する。"""
    from wiki_graph_extract import (FORMAT_OUTPUTS, OUTPUT_NAMES, SEMANTIC_REPORT, build_graph, collect_pages,
                                    load_ontology, write_outputs)
    from wiki_graph_lint import lint

    graph = guard.root / "graph"
    ontology = load_ontology(graph / "ontology.yaml")
    pages = collect_pages(guard.root)
    findings = lint(pages, ontology)
    for key in ("unreadable_graph", "duplicate_node_ids", "unknown_predicates", "broken_object_refs",
                "missing_source_field", "missing_evidence", "broken_source_refs", "subject_type_mismatch",
                "object_type_mismatch"):
        if findings[key]:
            raise publish.Conflict(f"公開候補のグラフを検査してください: {key}")
    nodes, aliases, edges, report = build_graph(guard, pages, ontology)
    formats = ["sqlite"]
    if any(guard.file_info(graph / name) is not None for name in ("nodes.jsonl", "edges.jsonl")):
        formats.append("jsonl")
    if guard.file_info(graph / "graph.graphml") is not None:
        formats.append("graphml")
    for name in OUTPUT_NAMES:
        guard.file_info(graph / name)
    destination = graph if output is None else output.root
    write_outputs(destination, formats, nodes, aliases, edges, guard=output or guard, report=report)
    return tuple(name for fmt in formats for name in FORMAT_OUTPUTS[fmt]) + ((SEMANTIC_REPORT,) if report else ())


def verify_graph_projection(guard, files):
    """承認済みファイルだけを置いたステージからグラフを生成し、指紋を照合する。"""
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from wiki_fsguard import WriteGuard
    from wiki_graph_extract import OUTPUT_NAMES

    with TemporaryDirectory(prefix="info-hive-graph-check-") as directory:
        output = WriteGuard(Path(directory), strict_root=True)
        names = update_graph(guard, output=output)
        if set(names) != {name for name in OUTPUT_NAMES if "graph/" + name in files}:
            raise publish.Conflict("形式2の互換グラフの成果物が公開計画と一致しません")
        for name in names:
            actual = guard.read_bytes(guard.root / "graph" / name)
            expected = output.read_bytes(output.root / name)
            if actual != expected or publish.digest(expected) != files["graph/" + name]:
                raise publish.Conflict(f"形式2の互換グラフが候補の記事・意味版と一致しません: {name}")
