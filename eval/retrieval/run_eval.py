#!/usr/bin/env python3
"""llm-wiki のセクション検索の検索品質を評価するハーネス。

固定したフィクスチャのコーパス（``corpus/wiki``）とクエリセット（``queries.json``）に対して
``skills/info-hive/scripts/wiki_search.py`` をサブプロセスで実行し、モードごとの検索品質を報告する。

  * ``page``    -- ページ単位の BM25（``--granularity page --no-embed``）
  * ``section`` -- セクション単位の BM25（``--no-embed``）
  * ``hybrid``  -- BM25 とローカルの FastEmbed/sqlite-vec による検索を RRF で統合したもの。
                   任意のパッケージがインストールされているときに評価する。
                   インストールの有無は、検索を1回実行し、JSON の mode を読んで判定する。

指標は、負例でないクエリについて、ページのスラッグで照合して計算する（セクションが当たれば、そのページが当たったとみなす）。
指標は recall@5、recall@10、MRR で、いずれも生の結果行に対して計算する（上位を切り出す前に、ページ単位の重複は除かない）。
負例のクエリは、偽陽性率として別に報告する。偽陽性率は、上位5件に1件以上の結果を返したクエリの割合である。

このハーネスは、``--cache`` を使っても出力がバイト単位で変わらないことも確かめる。
固定した3つのクエリについて、キャッシュなし、コールドキャッシュ、ウォームキャッシュのどれでも、``--json`` の出力が同じになることを確認する。

語彙ベースのチェックは標準ライブラリだけを使う。ハイブリッド検索を評価する前に、検索スクリプトを
``uv run --script`` で実行するか、バージョンを固定した任意のパッケージをインストールしておく。

  python3 eval/retrieval/run_eval.py          # 語彙ベースのレポート
  uv run --with fastembed==0.8.0 --with sqlite-vec==0.1.9 \
    python eval/retrieval/run_eval.py          # ハイブリッド検索も含める
  python3 eval/retrieval/run_eval.py --gate   # 回帰ゲートも実行する（CI）

日本語の評価セットは ``ja/`` にあり、``--set ja`` で選ぶ。``--save`` を付けると、
指標とクエリごとの結果を JSON で保存する（``baselines/`` の基準値の作成に使う）。

  python3 eval/retrieval/run_eval.py --set ja
  python3 eval/retrieval/run_eval.py --save eval/retrieval/baselines/en.json
"""

import argparse
import json
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPT = REPO / "skills" / "info-hive" / "scripts" / "wiki_search.py"

# 評価セット。コーパスとクエリ集の組。
# nfd_files に挙げたファイルは、評価のときに一時ディレクトリへ複製し、ファイル名を NFD にする。
# NFD の名前のままリポジトリに入れると、macOS の Git（core.precomposeunicode）では
# 削除と未追跡の 2 つに見えてしまうので、リポジトリには NFC の名前で置く。
SETS = {
    "en": {"corpus": HERE / "corpus" / "wiki", "queries": HERE / "queries.json"},
    "ja": {
        "corpus": HERE / "ja" / "corpus" / "wiki",
        "queries": HERE / "ja" / "queries.json",
        "nfd_files": ["sources/熱中症予防ガイド要約.md"],
    },
}
# 評価するコーパス。main() で --set に合わせて差し替える。
CORPUS = SETS["en"]["corpus"]

# 検索モード。wiki_search.py に渡すフラグで表す。
MODES = {
    "page": {"granularity": "page", "no_embed": True},
    "section": {"granularity": "section", "no_embed": True},
    "hybrid": {"granularity": "section", "no_embed": False},
}

METRIC_COLUMNS = ["recall@5", "recall@10", "mrr", "fp_rate"]

# キャッシュを使っても出力がバイト単位で変わらないことを確かめる、固定した3つのクエリ。
CACHE_QUERIES = {
    "en": [
        "attention mechanism",
        "quantization int8",
        "tensor parallelism",
    ],
    "ja": [
        "消費税の軽減税率",
        "RAG 検索拡張生成",
        "東京",
    ],
}


def build_cmd(query, granularity, filters, no_embed, cache=None):
    cmd = [
        sys.executable, str(SCRIPT), query,
        "--wiki", str(CORPUS), "--json", "--top", "10",
    ]
    if granularity == "page":
        cmd += ["--granularity", "page"]
    if no_embed:
        cmd += ["--no-embed"]
    if cache is not None:
        cmd += ["--cache", str(cache)]
    if filters:
        if filters.get("type"):
            cmd += ["--type", filters["type"]]
        tag = filters.get("tag")
        if tag:
            for value in (tag if isinstance(tag, list) else [tag]):
                cmd += ["--tag", value]
    return cmd


def run_search_bytes(query, granularity="section", filters=None, no_embed=True, cache=None):
    """検索を1回実行し、標準出力をバイト列のまま（JSON の行）返す。"""
    cmd = build_cmd(query, granularity, filters, no_embed, cache)
    proc = subprocess.run(cmd, capture_output=True, cwd=str(REPO))  # デコードせずにバイト列のまま
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"search failed for {query!r}: {detail}")
    return proc.stdout


def run_search(query, granularity="section", filters=None, no_embed=True, cache=None):
    """検索を1回実行し、デコードした標準出力を返す（JSON のパース用）。"""
    return run_search_bytes(query, granularity, filters, no_embed, cache).decode("utf-8")


def result_slugs(stdout):
    return [row["slug"] for row in json.loads(stdout)["results"]]


def recall_at(slugs, expected, k):
    """期待するスラッグのうち、上位 k 件の生の結果行に含まれるものの割合。"""
    top = set(slugs[:k])
    return sum(1 for slug in expected if slug in top) / len(expected)


def reciprocal_rank(slugs, expected):
    """スラッグが期待どおりである最初の生の結果行について 1/順位 を返す（なければ 0）。"""
    for rank, slug in enumerate(slugs, 1):
        if slug in expected:
            return 1.0 / rank
    return 0.0


def run_queries(mode_opts, queries):
    """クエリごとに検索し、[{id, kind, slugs}] を返す。"""
    return [
        {
            "id": query["id"],
            "kind": query["kind"],
            "slugs": result_slugs(run_search(
                query["query"], filters=query.get("filters"), **mode_opts,
            )),
        }
        for query in queries
    ]


def aggregate(queries, runs):
    """クエリごとの結果から指標を計算する。"""
    recall5 = recall10 = mrr = 0.0
    positives = 0
    negatives = 0
    false_positives = 0
    for query, run in zip(queries, runs):
        slugs = run["slugs"]
        if query["kind"] == "negative":
            negatives += 1
            if slugs[:5]:
                false_positives += 1
            continue
        expected = query["expected"]
        positives += 1
        recall5 += recall_at(slugs, expected, 5)
        recall10 += recall_at(slugs, expected, 10)
        mrr += reciprocal_rank(slugs, expected)
    return {
        "recall@5": recall5 / positives if positives else 0.0,
        "recall@10": recall10 / positives if positives else 0.0,
        "mrr": mrr / positives if positives else 0.0,
        "fp_rate": false_positives / negatives if negatives else 0.0,
    }


def aggregate_by_kind(queries, runs):
    """kind ごとの指標。負例の kind では fp_rate だけが意味を持つ。"""
    result = {}
    for kind in sorted({query["kind"] for query in queries}):
        pairs = [(q, r) for q, r in zip(queries, runs) if q["kind"] == kind]
        kind_queries = [q for q, _ in pairs]
        kind_runs = [r for _, r in pairs]
        result[kind] = {"count": len(pairs), **aggregate(kind_queries, kind_runs)}
    return result


def evaluate_mode(mode_opts, queries):
    return aggregate(queries, run_queries(mode_opts, queries))


def hybrid_available():
    """ローカルの埋め込みバックエンドでハイブリッド検索が行われるときに True を返す。

    --no-embed を付けずにセクション検索を1回実行し、JSON の ``mode`` を読む。
    ローカルの依存パッケージがない、または初期化に失敗した場合は False を返す
    （wiki_search は語彙ベースのモードに切り替わる）。
    """
    try:
        payload = json.loads(run_search("attention", no_embed=False))
    except SystemExit:
        return False
    return payload.get("mode") == "hybrid"


def cache_invariant(cache_queries):
    """--json の標準出力は、キャッシュなし、コールドキャッシュ、ウォームキャッシュのどれでもバイト単位で同じでなければならない。

    1つの一時的なキャッシュのパスに対して3回実行する。1回目はすべてのクエリをキャッシュなしで、
    2回目はコールドキャッシュで（最初の呼び出しでファイルを作り、以降はそれを再利用する）、
    3回目はウォームキャッシュで実行する。デコードした文字列ではなく標準出力の生のバイト列を比べるので、
    バイト単位で変わらないことを正しく検証できる。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "idx.json"
        uncached = [run_search_bytes(q, no_embed=True, cache=None) for q in cache_queries]
        cold = [run_search_bytes(q, no_embed=True, cache=cache) for q in cache_queries]
        warm = [run_search_bytes(q, no_embed=True, cache=cache) for q in cache_queries]
    return all(u == c == w for u, c, w in zip(uncached, cold, warm))


def print_table(rows):
    header = "| mode    | " + " | ".join(METRIC_COLUMNS) + " |"
    sep = "|" + "|".join(["---------"] + ["----------"] * len(METRIC_COLUMNS)) + "|"
    print(header)
    print(sep)
    for mode, metrics in rows.items():
        cells = " | ".join(f"{metrics[col]:.3f}" for col in METRIC_COLUMNS)
        print(f"| {mode:<7} | {cells} |")


def git_commit():
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, cwd=str(REPO),
        )
    except OSError:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def installed_version(name):
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        return None


def save_report(path, set_name, queries, rows, runs):
    """指標とクエリごとの結果を JSON で保存する。"""
    sys.path.insert(0, str(SCRIPT.parent))
    import wiki_search

    report = {
        "set": set_name,
        "commit": git_commit(),
        # 測った PC には依存させず、CI と同じに固定した依存パッケージとモデルだけを記録する。
        "environment": {
            "fastembed": installed_version("fastembed"),
            "sqlite-vec": installed_version("sqlite-vec"),
            "embed_model": wiki_search.LOCAL_EMBED_MODEL if "hybrid" in rows else None,
        },
        "queries": len(queries),
        "metrics": rows,
        "by_kind": {mode: aggregate_by_kind(queries, runs[mode]) for mode in rows},
        "per_query": {
            mode: [{"id": r["id"], "kind": r["kind"], "top10": r["slugs"][:10]} for r in runs[mode]]
            for mode in rows
        },
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def prepare_corpus(spec, tmp):
    """評価に使うコーパスのパスを返す。nfd_files があれば、tmp に複製して名前を NFD にする。"""
    corpus = spec["corpus"]
    nfd_files = set(spec.get("nfd_files", []))
    if not nfd_files:
        return corpus
    dest_root = Path(tmp) / "wiki"
    for src in sorted(corpus.rglob("*")):
        rel = src.relative_to(corpus)
        if not src.is_file() or ".wiki-cache" in rel.parts:
            continue
        name = rel.as_posix()
        if name in nfd_files:
            name = unicodedata.normalize("NFD", name)
            nfd_files.discard(rel.as_posix())
        dest = dest_root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    if nfd_files:
        raise SystemExit(f"nfd_files not found in {corpus}: {sorted(nfd_files)}")
    return dest_root


def main():
    global CORPUS
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gate", action="store_true",
        help=(
            "Exit 1 if section recall regresses from page mode, or available "
            "hybrid retrieval regresses section recall/false-positive rate."
        ),
    )
    parser.add_argument(
        "--set", choices=sorted(SETS), default="en",
        help="Evaluation set: en (default) or ja.",
    )
    parser.add_argument(
        "--save", metavar="PATH",
        help="Write metrics and per-query results as JSON to PATH.",
    )
    args = parser.parse_args()

    if not SCRIPT.exists():
        raise SystemExit(f"wiki_search.py not found at {SCRIPT}")
    queries = json.loads(SETS[args.set]["queries"].read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        CORPUS = prepare_corpus(SETS[args.set], tmp)
        exit_code = evaluate(args, queries)
    sys.exit(exit_code)


def evaluate(args, queries):
    """CORPUS を評価して表を出し、終了コードを返す。"""
    modes = ["page", "section"]
    if hybrid_available():
        modes.append("hybrid")
    else:
        print("hybrid: skipped (local semantic dependencies unavailable)", file=sys.stderr)

    runs = {mode: run_queries(MODES[mode], queries) for mode in modes}
    rows = {mode: aggregate(queries, runs[mode]) for mode in modes}
    print_table(rows)
    if args.save:
        save_report(args.save, args.set, queries, rows, runs)
        print(f"\nsaved: {Path(args.save).as_posix()}")

    exit_code = 0

    cache_ok = cache_invariant(CACHE_QUERIES[args.set])
    print(f"\ncache: {'OK' if cache_ok else 'MISMATCH'}")
    if not cache_ok:
        exit_code = 1  # 不一致なら --gate の有無にかかわらず失敗にする

    if args.gate:
        failures = []
        if rows["section"]["recall@10"] < rows["page"]["recall@10"]:
            failures.append("section recall@10 < page recall@10")
        if "hybrid" in rows:
            if rows["hybrid"]["recall@10"] < rows["section"]["recall@10"]:
                failures.append("hybrid recall@10 < section recall@10")
            if rows["hybrid"]["fp_rate"] > rows["section"]["fp_rate"]:
                failures.append("hybrid fp_rate > section fp_rate")
        if failures:
            print(f"gate: FAIL ({'; '.join(failures)})", file=sys.stderr)
            exit_code = 1
        else:
            compared = "section/page + hybrid" if "hybrid" in rows else "section/page"
            print(f"gate: PASS ({compared} retrieval quality)")

    return exit_code


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
