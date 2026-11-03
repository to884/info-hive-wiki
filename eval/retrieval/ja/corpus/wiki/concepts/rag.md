---
type: "concept"
title: "RAG"
tags: ["AI", "検索"]
sources: ["search-study-notes"]
created: 2026-02-10
updated: 2026-04-20
---

# RAG

RAG（Retrieval-Augmented Generation）は、language model が答える前に関連する文書を検索し、その文書を prompt に含めて回答を生成させる方法である。日本語では検索拡張生成とも呼ぶ。モデルが学習していない社内の情報や新しい情報にもとづいて答えられる。

## Chunking

文書は、そのままでは長すぎるので、段落や見出しの単位で chunk に分けてから索引にする。chunk が大きすぎると無関係な文が混ざり、小さすぎると文脈が失われる。

## Retrieval

検索には、語の一致を見る [[bm25]] と、意味の近さを見る [[embedding]] を組み合わせることが多い。二つの結果は reciprocal rank fusion などで一つの順位にまとめる。

## Re-ranking

最初の検索で多めに候補を集め、reranker と呼ばれる別のモデルで質問との関連度を付け直してから上位だけを渡す。精度は上がるが、応答までの時間は延びる。
