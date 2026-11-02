# 検索のセットアップのための質問

`/wiki:init` と `/wiki:upgrade` の前に、この質問をまとめて行う。どちらのコマンドも、そのあとでローカルの実行環境一式をインストールし、動作を確認する。セットアップを後回しにしたり、一部だけで済ませたりする手順はない。

## 質問する前に調べる

次の点を確かめる。

- `uv` が使えるか（必須の前提条件）。
- `FASTEMBED_CACHE_PATH` が設定されているか（パスを報告するだけで、黙って変更してはならない）。
- `<wiki-root>/.wiki-cache/search-index.json` があるか。
- `<wiki-root>/.wiki-cache/embeddings.sqlite` があり、`semantic_sections` の行が入っているか。

状態は正確に報告する。

- **前提条件が整っている:** `uv` と Python 3.10 以上が使える。
- **セマンティック検索の依存パッケージが整っている:** バージョンを固定した実行環境で、FastEmbed、sqlite-vec、PyYAML を読み込める。
- **モデルがキャッシュされている:** ローカルの `BAAI/bge-small-en-v1.5` モデルがダウンロード済みである。
- **wiki が同期されている:** `setup_wiki.py` が `"status": "ready"` を出力し、そのセクション数がベクトル数と一致している。

データベースのファイルがあるだけでは、コーパスが同期されている証拠にはならない。初期化とアップグレードでは必ずセットアップのスクリプトを実行し、スクリプトは差分の同期を行ってから準備完了を報告する。

## 1回でまとめて聞く

1. **保存先の範囲とパス:** 個人用のグローバルな wiki（`~/wiki/` と `~/wiki/raw/` を提案する）か、プロジェクトの wiki（既定は `wiki/` と `raw/`）かを選んでもらい、両方のパスを確認する。
2. **モデルのキャッシュ:** `~/.cache/llm-wiki/fastembed/` を使うか、`FASTEMBED_CACHE_PATH` を別のローカルのディレクトリに設定するか。
3. **グラフ層:** 型付きグラフのメタデータを今設定するか、生成されるグラフは使える状態にしておくだけで使わないか、後回しにするか。
4. **エージェントとの連携:** どのエージェントのメモリファイルから wiki を指すか、または見送るか。

セットアップの前に、次のことを説明する。

- 初期化とアップグレードのたびに、`uv` を通じて、バージョンを固定した FastEmbed、sqlite-vec、PyYAML をインストールする。
- セットアップでは、バージョンを固定したモデルを1回だけダウンロードし、パースキャッシュを作り、現在のすべてのセクションをベクトル化する。2回目以降は、新しいセクションと変更されたセクションだけをベクトル化し、削除されたセクションは取り除く。
- wiki のセクションとクエリはマシンの外に出ない。API キー、リモートのエンドポイント、リクエストごとの料金、プロバイダーのデータ保持の方針は関係しない。
- wiki ごとのベクトルは `<wiki-root>/.wiki-cache/embeddings.sqlite` にある派生データで、削除しても問題ない。
- `python wiki_search.py "<query>" --no-embed` を直接実行すれば、あとで依存パッケージを使わない BM25 の検索ができる。ただし、これは初期化やアップグレードでの必須のセットアップの代わりにはならない。

## 秘密情報を含まない状態を記録する

現在の `SCHEMA.md` の検索のブロックには、次のように書く。

```markdown
- Semantic backend: local FastEmbed + sqlite-vec (`BAAI/bge-small-en-v1.5`, 384 dimensions). No wiki or query text leaves the machine.
- First semantic use downloads model artifacts to `~/.cache/llm-wiki/fastembed/`; set `FASTEMBED_CACHE_PATH` to override the model cache.
- Semantic setup verified: YYYY-MM-DD | not yet built
```

ユーザーがマシン固有のスキーマを明示的に望まない限り、絶対パスは記録しない。旧来の `Embedding mode`、プロバイダー、エンドポイント、API の検証のフィールドは追加しない。

## 必須のセットアップを検証する

初期化とアップグレードは、次のコマンドを自動で呼び出す。

```bash
uv run --script skills/llm-wiki/scripts/setup_wiki.py --wiki wiki --cache
```

準備完了を報告する前に、次のすべてを満たしていることを確かめる。

1. JSON が `"status": "ready"` を報告している。
2. 依存パッケージに、バージョンを固定した FastEmbed、sqlite-vec、PyYAML が並んでいる。
3. モデルが `BAAI/bge-small-en-v1.5` で、次元数が `384` である。
4. `wiki/.wiki-cache/search-index.json` と `embeddings.sqlite` がある。
5. `sections` が、`semantic_sections` と `semantic_vectors` の行数と一致している。
6. セットアップを2回目に実行しても同じ件数が報告され、ベクトル化のバッチが出力されない。

セットアップが失敗した場合は、失敗したコマンドと、外に出しても安全な例外の内容をそのまま報告する。`uv`、モデルをダウンロードできるか、ローカルのディスクの空き容量、sqlite の拡張を読み込めるかを確かめる。初期化やアップグレードが完了したと説明してはならない。

## ホスト型 wiki

ホスト型 wiki では、ローカルの実行環境のセットアップは不要である。サーバーが自身の検索インデックスを作る。

- キーワード検索は、wiki の Durable Object の中の SQLite FTS5 を使う。コミットした内容は、すぐにキーワードで検索できる。
- セマンティック検索は、Workers AI の `@cf/baai/bge-small-en-v1.5`（CLS プーリング、L2 正規化、384 次元）と Vectorize のインデックスを使う。個人用の wiki なら、Workers AI の無料枠である1日 10,000 ニューロンに収まる。
- 順位の統合には、`wiki_search.py` と同じ定数の RRF を使う。BM25 の定数と埋め込みの実行環境が異なるので、結果はローカルの結果と同等だが、同一ではない。
- サーバーは、コミットのたびに、変更されたセクションをバッチでベクトル化する。`wiki_overview` と検索のレスポンスにある `vector_lag` は、Vectorize がまだ返せないセクションの数である。サーバーはベクトルを1つずつ確認し、Vectorize に保存されていなければ送り直す。
- Vectorize や Workers AI のバインディングがない場合、検索は語彙ベースだけになる。検索のレスポンスにモードが示される。
- wiki のテキストとクエリは、自分の Cloudflare アカウントに送られる。所有者がホスト型の判定バックエンドを有効にしない限り（`decision-models.md`）、ほかのプロバイダーに送られることはない。

`.wiki-cache/` をホスト型 wiki にコピーしてはならない。インポートのコマンドはこれをスキップする。ローカルのベクトルとホスト型のベクトルが混ざることはない。`pull` ミラーでは、オフラインで検索するためにローカルの `setup_wiki.py` を実行できる。

ホスト型 wiki の `SCHEMA.md` の検索のブロックには、次のように記録する。

```markdown
- Semantic backend: hosted (Workers AI `@cf/baai/bge-small-en-v1.5` + Vectorize, 384 dimensions). Wiki text stays in the owner's Cloudflare account.
```

## v2 のプロバイダーのキャッシュからアップグレードする

`wiki/.wiki-cache/embeddings.jsonl` は使われなくなったファイルで、無視される。以前のプロバイダーのメタデータが含まれていることがある。すべて派生データなので、削除を提案する。既存のファイルなので、削除にはユーザーの承認が必要である。内容の移行や、リモートへの再送信は必要ない。v3 の必須のセットアップが、ローカルで `embeddings.sqlite` を作る。
