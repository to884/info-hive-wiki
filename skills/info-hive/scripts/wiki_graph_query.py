#!/usr/bin/env python3
"""
wiki_graph_query.py — コンパイル済みの wiki のグラフ（graph.sqlite）をクエリする。

目的のページを早く見つけるために使う。ノードにつながっているものを探す、主語のまわりの
型付きのエッジを一覧にする、2つのノードの間の経路を探す、ノードについての事実をすべて出力する、
といったことができる。グラフはナビゲーション用のインデックスにすぎない。重要な主張については、
`source` フィールドをたどって wiki のページと raw ファイルを確かめる。

サブコマンド:
    neighbors --node <id>            <id> から1ホップ先のノードを一覧にする
    edges    --subject <id>          <id> から出ているエッジをすべて一覧にする
             [--predicate <p>]        述語で絞り込む
    path     --from <id> --to <id>   有向の最短経路（BFS、最大の深さ 6）
             [--max-depth N]
    facts    --about <id>            <id> から出るエッジと入るエッジ

共通のオプション:
    --db <path>           graph.sqlite のパス（既定: <wiki>/graph/graph.sqlite）
    --json                テキストの代わりに JSON を出力する

--db を省いたときは、<wiki>/graph/ontology.yaml がなければグラフ層を導入する前の wiki として
扱い、終了コード 1 で止まる（extract、lint と同じ）。手順の正本は references/graph-workflow.md にある。

例:
    python wiki_graph_query.py wiki/ neighbors --node product:konvy
    python wiki_graph_query.py wiki/ edges    --subject person:stephanie-emmanouel
    python wiki_graph_query.py wiki/ path     --from person:praney-behl --to product:konvy
    python wiki_graph_query.py wiki/ facts    --about product:konvy
"""

import argparse
import json
import sqlite3
import sys
from collections import deque
from pathlib import Path

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import localize_argparse, nfc  # noqa: E402
import wiki_graph_common as common  # noqa: E402
import wiki_initialized  # noqa: E402


EVIDENCE_SNIPPET_LEN = 140
COMMANDS = ("neighbors", "edges", "path", "facts")


class QueryError(ValueError):
    """問い合わせに答えられない（DB やノードがない）。CLI は終了コード 1 で止まり、MCP はツールのエラーにする。"""


def open_db(path: Path) -> sqlite3.Connection:
    """graph.sqlite を読み取り専用で開く。問い合わせは DB を書き換えない。"""
    path = Path(path)
    if not path.exists():
        raise QueryError(f"graph.sqlite が見つからない: {path}\n先に wiki_graph_extract.py を実行する。")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def require_node(conn: sqlite3.Connection, node_id: str, label: str = "ノード") -> dict:
    node = fetch_node(conn, node_id)
    if not node:
        raise QueryError(f"{label}が見つからない: {node_id}")
    return node


def fetch_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def edges_from(conn: sqlite3.Connection, subject: str, predicate: str | None = None) -> list[dict]:
    q = "SELECT * FROM edges WHERE subject = ?"
    params: list = [subject]
    if predicate:
        q += " AND predicate = ?"
        params.append(predicate)
    q += " ORDER BY predicate, object"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def edges_to(conn: sqlite3.Connection, obj: str, predicate: str | None = None) -> list[dict]:
    q = "SELECT * FROM edges WHERE object = ?"
    params: list = [obj]
    if predicate:
        q += " AND predicate = ?"
        params.append(predicate)
    q += " ORDER BY predicate, subject"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def truncate(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= EVIDENCE_SNIPPET_LEN:
        return text
    return text[: EVIDENCE_SNIPPET_LEN - 1].rstrip() + "…"


def render_edge_row(e: dict) -> str:
    pieces = [
        f"  {e['subject']} --[{e['predicate']}]--> {e['object']}",
    ]
    confidence = e.get("confidence") or "-"
    status = e.get("status") or "-"
    src = e.get("source") or "-"
    pieces.append(f"      ソース: {src}  confidence={confidence}  status={status}")
    if e.get("evidence"):
        pieces.append(f"      証拠: {truncate(e['evidence'])}")
    pieces.append(f"      （ページ: {e['page']}）")
    return "\n".join(pieces)


def cmd_neighbors(conn: sqlite3.Connection, node_id: str) -> dict:
    node = require_node(conn, node_id)
    out_edges = edges_from(conn, node_id)
    in_edges = edges_to(conn, node_id)

    neighbors: dict[str, dict] = {}
    for e in out_edges:
        neighbors.setdefault(e["object"], {"node_id": e["object"], "out": [], "in": []})
        neighbors[e["object"]]["out"].append(e)
    for e in in_edges:
        neighbors.setdefault(e["subject"], {"node_id": e["subject"], "out": [], "in": []})
        neighbors[e["subject"]]["in"].append(e)

    # 可能なら隣接ノードのタイトルを解決する
    for nid, slot in neighbors.items():
        target = fetch_node(conn, nid)
        slot["title"] = target["title"] if target else nid
        slot["path"] = target["path"] if target else None

    return {
        "node": node,
        "neighbors": sorted(neighbors.values(), key=lambda n: n["node_id"]),
    }


def cmd_edges(conn: sqlite3.Connection, subject: str, predicate: str | None = None) -> dict:
    es = edges_from(conn, subject, predicate)
    return {"subject": subject, "predicate": predicate, "edges": es}


def cmd_facts(conn: sqlite3.Connection, about: str) -> dict:
    node = require_node(conn, about)
    return {
        "node": node,
        "outbound": edges_from(conn, about),
        "inbound": edges_to(conn, about),
    }


def cmd_path(conn: sqlite3.Connection, start: str, goal: str, max_depth: int = 6) -> dict:
    require_node(conn, start, "--from のノード")
    require_node(conn, goal, "--to のノード")

    queue = deque([(start, [start], [])])
    visited = {start}
    while queue:
        node, node_path, edge_path = queue.popleft()
        if node == goal:
            return {"from": start, "to": goal, "path_nodes": node_path, "path_edges": edge_path}
        if len(node_path) - 1 >= max_depth:
            continue
        for e in edges_from(conn, node):
            nxt = e["object"]
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append((nxt, node_path + [nxt], edge_path + [e]))
    return {"from": start, "to": goal, "path_nodes": [], "path_edges": []}


def render(result: dict, command: str) -> str:
    out: list[str] = []
    if command == "neighbors":
        n = result["node"]
        out.append(f"ノード: {n['id']}  （{n['title']}）  {n['node_type']}  [{n['path']}]")
        out.append(f"隣接ノード数: {len(result['neighbors'])}")
        for nb in result["neighbors"]:
            out.append("")
            out.append(f"  → {nb['node_id']}  （{nb['title']}）")
            for e in nb.get("out", []):
                out.append(f"      出る  [{e['predicate']}]  confidence={e.get('confidence') or '-'}  ソース: {e.get('source') or '-'}")
            for e in nb.get("in", []):
                out.append(f"      入る  [{e['predicate']}]  {e['subject']} から  ソース: {e.get('source') or '-'}")
    elif command == "edges":
        out.append(f"{result['subject']} から出るエッジ"
                   + (f"（述語: {result['predicate']}）" if result['predicate'] else ""))
        for e in result["edges"]:
            out.append("")
            out.append(render_edge_row(e))
    elif command == "facts":
        n = result["node"]
        out.append(f"{n['id']} についての事実  （{n['title']}）  [{n['path']}]")
        out.append("")
        out.append(f"出るエッジ（{len(result['outbound'])} 本）:")
        for e in result["outbound"]:
            out.append(render_edge_row(e))
        out.append("")
        out.append(f"入るエッジ（{len(result['inbound'])} 本）:")
        for e in result["inbound"]:
            out.append(render_edge_row(e))
    elif command == "path":
        if not result["path_nodes"]:
            out.append(f"深さの上限までに、{result['from']} から {result['to']} への経路は見つからなかった。")
        else:
            out.append(f"{result['from']} から {result['to']} への経路（{len(result['path_edges'])} ホップ）:")
            for e in result["path_edges"]:
                out.append("")
                out.append(render_edge_row(e))
    return "\n".join(out)


def run_query(wiki: Path, command: str, *, node: str | None = None, subject: str | None = None,
              predicate: str | None = None, start: str | None = None, goal: str | None = None,
              max_depth: int = 6, about: str | None = None, db: Path | None = None) -> dict:
    """1 つの問い合わせを実行して結果を返す。CLI と MCP の graph ツールが使う。

    db を省くと、extract、lint と同じくオントロジーの有無でグラフ層が有効かを決め、なければ
    wiki_graph_common.GraphLayerDisabled を送出する。オントロジーを消した wiki に古い graph.sqlite が
    残っていても、それには答えない。db を明示したときは、その DB を使う（extract --out で別の場所に
    作った場合など）。DB やノードがなければ QueryError を送出する。
    """
    wiki = Path(wiki)
    if command not in COMMANDS:
        raise QueryError(f"未知の問い合わせ: {command}。使えるのは {', '.join(COMMANDS)}")
    # グラフのノード ID は NFC で書かれている。macOS のターミナルなどから NFD で渡されても
    # 一致するように、ノード ID を NFC にそろえる。
    node, subject, start, goal, about = (nfc(v) if isinstance(v, str) else v
                                         for v in (node, subject, start, goal, about))
    required = {"neighbors": ("node", node), "edges": ("subject", subject), "facts": ("about", about)}
    if command in required and not required[command][1]:
        raise QueryError(f"{command} には {required[command][0]} が要る")
    if command == "path" and not (start and goal):
        raise QueryError("path には from と to が要る")
    if db is None:
        common.require_ontology(wiki)
    conn = open_db(Path(db) if db else wiki / "graph" / "graph.sqlite")
    try:
        if command == "neighbors":
            return cmd_neighbors(conn, node)
        if command == "edges":
            return cmd_edges(conn, subject, predicate)
        if command == "path":
            return cmd_path(conn, start, goal, max_depth)
        return cmd_facts(conn, about)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wiki", type=Path, help="wiki のディレクトリ")
    parser.add_argument("--db", type=Path, help="graph.sqlite のパス（既定: <wiki>/graph/graph.sqlite）")
    parser.add_argument("--json", action="store_true", help="テキストの代わりに JSON を出力する。キーは英語のまま")

    sub = parser.add_subparsers(dest="command", required=True)

    p_n = sub.add_parser("neighbors", help="ノードから 1 ホップ先のノードを一覧にする")
    p_n.add_argument("--node", required=True, help="起点のノード ID")

    p_e = sub.add_parser("edges", help="主語から出ているエッジをすべて一覧にする")
    p_e.add_argument("--subject", required=True, help="主語のノード ID")
    p_e.add_argument("--predicate", help="述語で絞り込む")

    p_p = sub.add_parser("path", help="2 つのノードの間の有向の最短経路を探す")
    p_p.add_argument("--from", dest="from", required=True, help="起点のノード ID")
    p_p.add_argument("--to", required=True, help="終点のノード ID")
    p_p.add_argument("--max-depth", type=int, default=6, help="たどる深さの上限（既定: 6）")

    p_f = sub.add_parser("facts", help="ノードから出るエッジと入るエッジを出力する")
    p_f.add_argument("--about", required=True, help="調べるノード ID")

    localize_argparse(parser)
    args = parser.parse_args()

    # SCHEMA.md のないディレクトリは、初期化された wiki とみなさない（ADR-0019）。
    wiki_initialized.require_or_exit(args.wiki)
    try:
        result = run_query(args.wiki, args.command, node=getattr(args, "node", None),
                           subject=getattr(args, "subject", None), predicate=getattr(args, "predicate", None),
                           start=getattr(args, "from", None), goal=getattr(args, "to", None),
                           max_depth=getattr(args, "max_depth", 6), about=getattr(args, "about", None),
                           db=args.db)
    except common.GraphLayerDisabled as exc:
        common.report_disabled(exc)
        sys.exit(common.EXIT_GRAPH_DISABLED)
    except QueryError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    else:
        print(render(result, args.command))


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
