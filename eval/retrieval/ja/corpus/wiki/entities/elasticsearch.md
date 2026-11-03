---
type: "entity"
kind: "product"
title: "Elasticsearch"
tags: ["検索", "ソフトウェア"]
sources: ["search-study-notes"]
created: 2026-02-15
updated: 2026-04-25
---

# Elasticsearch

Elasticsearch is a distributed search and analytics engine built on Apache Lucene. 社内の文書検索やログの分析に広く使われている。

## Index and shards

An index is split into primary shards, and each shard can have replica shards on other nodes. shard の数は index を作るときに決め、あとから変えるには reindex が必要になる。

## Japanese analysis

Japanese text needs an analyzer that splits sentences into words. The analysis-kuromoji plugin provides a morphological analyzer, and an n-gram tokenizer is a common fallback. kuromoji は辞書にもとづいて語を分けるので、user dictionary に社内の用語を登録しておくとよい。

## Inverted index

Each field is stored in an inverted index that maps terms to the documents containing them, which is why exact term queries are fast. Relevance is scored with BM25 by default.
