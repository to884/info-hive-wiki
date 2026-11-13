#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "fastembed==0.8.0",
#   "pyyaml==6.0.3",
#   "sqlite-vec==0.1.9",
# ]
# ///
"""LLM Wiki の実行に必要な依存パッケージ、モデル、ローカルのインデックスをインストールし、検証する。"""

import sys
import argparse
import json
from importlib.metadata import version
from pathlib import Path

# グラフ用スクリプトの固定した依存パッケージを import できるかを確かめる。
import yaml

import wiki_search
from wiki_text import localize_argparse  # wiki_search が同じディレクトリを sys.path に加える
import wiki_initialized
import wiki_workspace


def prepare(wiki_root: Path, cache_path: Path | None = None) -> dict:
    """バージョンを固定したモデルをダウンロードし、現在の wiki の全セクションを同期する。"""
    pages = wiki_search.collect_pages(wiki_root, cache_path)
    sections = wiki_search.collect_sections(pages)
    model, sqlite_vec, dimension = wiki_search.load_local_embedding_backend()
    connection = wiki_search.open_vector_index(wiki_root, sqlite_vec, dimension)
    try:
        locator_ids = wiki_search.sync_vector_index(connection, sqlite_vec, model, sections)
        vector_count = connection.execute("SELECT count(*) FROM semantic_vectors").fetchone()[0]
    finally:
        connection.close()

    if vector_count != len(locator_ids):
        raise RuntimeError(
            f"ベクトルの索引が食い違っている: セクションは {len(locator_ids)} 件、ベクトルは {vector_count} 件"
        )

    return {
        "status": "ready",
        "wiki": str(wiki_root),
        "dependencies": {
            "fastembed": version("fastembed"),
            "pyyaml": version("pyyaml"),
            "sqlite-vec": version("sqlite-vec"),
        },
        "model": wiki_search.embed_model_name(),
        "dimension": dimension,
        "pages": len(pages),
        "sections": len(locator_ids),
        "vectors": vector_count,
        "vector_index": str(wiki_root / ".wiki-cache" / wiki_search.VECTOR_INDEX_NAME),
    }


def main() -> None:
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("--wiki", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    parser.add_argument(
        "--cache",
        nargs="?",
        const="AUTO",
        default=None,
        help="差分で更新するパースキャッシュも作る（値を省くと wiki/.wiki-cache/search-index.json）",
    )
    args = parser.parse_args()
    if args.wiki is None:
        args.wiki = wiki_workspace.default_wiki_or_exit()

    # SCHEMA.md のないディレクトリに、キャッシュと埋め込みを書かない（ADR-0019）。init と upgrade は、
    # SCHEMA.md を置いてからこのスクリプトを呼ぶ。
    wiki_initialized.require_or_exit(args.wiki)
    cache_path = None
    if args.cache:
        cache_path = args.wiki / ".wiki-cache" / "search-index.json" if args.cache == "AUTO" else Path(args.cache)
    print(json.dumps(prepare(args.wiki, cache_path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
