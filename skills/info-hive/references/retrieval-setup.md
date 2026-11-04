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
- **モデルがキャッシュされている:** ローカルの `intfloat/multilingual-e5-small` モデルがダウンロード済みである。
- **wiki が同期されている:** `setup_wiki.py` が `"status": "ready"` を出力し、そのセクション数がベクトル数と一致している。

データベースのファイルがあるだけでは、コーパスが同期されている証拠にはならない。初期化とアップグレードでは必ずセットアップのスクリプトを実行し、スクリプトは差分の同期を行ってから準備完了を報告する。

## 1回でまとめて聞く

1. **保存先の範囲とパス:** ユーザースコープの wiki（`~/info-hive/wiki/` と `~/info-hive/raw/` を提案する）か、プロジェクトの wiki（既定は `wiki/` と `raw/`）かを選んでもらい、両方のパスを確認する。
2. **モデルのキャッシュ:** `~/.cache/info-hive/fastembed/` を使うか、`FASTEMBED_CACHE_PATH` を別のローカルのディレクトリに設定するか。
3. **グラフ層:** 型付きグラフのメタデータを今設定するか、生成されるグラフは使える状態にしておくだけで使わないか、後回しにするか。
4. **エージェントとの連携:** どのエージェントのメモリファイルから wiki を指すか、または見送るか。

セットアップの前に、次のことを説明する。

- 初期化とアップグレードのたびに、`uv` を通じて、バージョンを固定した FastEmbed、sqlite-vec、PyYAML をインストールする。
- セットアップでは、バージョンを固定したモデルを1回だけダウンロードし、パースキャッシュを作り、現在のすべてのセクションをベクトル化する。2回目以降は、新しいセクションと変更されたセクションだけをベクトル化し、削除されたセクションは取り除く。
- wiki のセクションとクエリはマシンの外に出ない。API キー、リモートのエンドポイント、リクエストごとの料金、プロバイダーのデータ保持の方針は関係しない。
- wiki ごとのベクトルは `<wiki-root>/.wiki-cache/embeddings.sqlite` にある派生データで、削除しても問題ない。
- `python wiki_search.py "<query>" --no-embed` を直接実行すれば、あとで依存パッケージを使わない BM25 の検索ができる。日本語のクエリも、分かち書きせずにそのまま渡せる。ただし、これは初期化やアップグレードでの必須のセットアップの代わりにはならない。

## 秘密情報を含まない状態を記録する

`SCHEMA.md` の `## Retrieval` 節には、セマンティック検索について次の行を書く。行頭の `- Semantic backend: local FastEmbed + sqlite-vec` は、アップグレードのときにスクリプトがこの設定の有無を見分ける目印なので、英語のまま残す。続きは日本語で書く。`INFO_HIVE_EMBED_MODEL` で `intfloat/multilingual-e5-large` に切り替えている場合は、括弧の中を `intfloat/multilingual-e5-large`、1024 次元にする。

```markdown
- Semantic backend: local FastEmbed + sqlite-vec（`intfloat/multilingual-e5-small`、384 次元）。wiki やクエリのテキストがマシンの外に出ることはない。
- セマンティック検索を初めて使うときに、モデルのファイルを `~/.cache/info-hive/fastembed/` にダウンロードする。モデルのキャッシュの場所を変えるには `FASTEMBED_CACHE_PATH` を設定する。
- セマンティック検索のセットアップの確認: YYYY-MM-DD | 未構築
```

ユーザーがマシン固有のスキーマを明示的に望まない限り、絶対パスは記録しない。旧来の `Embedding mode`、プロバイダー、エンドポイント、API の検証のフィールドは追加しない。

## 必須のセットアップを検証する

初期化とアップグレードは、次のコマンドを自動で呼び出す。

```bash
uv run --script skills/info-hive/scripts/setup_wiki.py --wiki wiki --cache
```

準備完了を報告する前に、次のすべてを満たしていることを確かめる。

1. JSON が `"status": "ready"` を報告している。
2. 依存パッケージに、バージョンを固定した FastEmbed、sqlite-vec、PyYAML が並んでいる。
3. モデルが `intfloat/multilingual-e5-small` で、次元数が `384` である。`INFO_HIVE_EMBED_MODEL` でモデルを切り替えている場合は、そのモデルが報告されていればよい。
4. `wiki/.wiki-cache/search-index.json` と `embeddings.sqlite` がある。
5. `sections` が、`semantic_sections` と `semantic_vectors` の行数と一致している。
6. セットアップを2回目に実行しても同じ件数が報告され、ベクトル化のバッチが出力されない。

セットアップが失敗した場合は、失敗したコマンドと、外に出しても安全な例外の内容をそのまま報告する。`uv`、モデルをダウンロードできるか、ローカルのディスクの空き容量、sqlite の拡張を読み込めるかを確かめる。初期化やアップグレードが完了したと説明してはならない。

## 移植元の llm-wiki で作った wiki から移るとき

llm-wiki の埋め込みモデルの既定は、英語専用の `BAAI/bge-small-en-v1.5` である。info-hive は `intfloat/multilingual-e5-small` を使うので、`embeddings.sqlite` に記録されたモデル名が合わない。そのため、次の検索かセットアップで、ベクトルの索引は自動で作り直され、すべてのセクションがローカルでベクトル化し直される。`search-index.json` もスキーマが違うので作り直される。どちらも手で移したり消したりする必要はない。初期化やアップグレードの手順のとおり、`setup_wiki.py` を実行して準備完了を確かめる。

`SCHEMA.md` の `## Retrieval` 節に `BAAI/bge-small-en-v1.5` と書いてあれば、上の書き方に合わせて直すことを提案する。内容を示し、承認を得てから直す。

llm-wiki の古い版が作った `wiki/.wiki-cache/embeddings.jsonl` は使われなくなったファイルで、無視される。以前のプロバイダーのメタデータが含まれていることがある。すべて派生データなので、削除を提案する。既存のファイルなので、削除にはユーザーの承認が必要である。内容の移行や、リモートへの再送信は必要ない。必須のセットアップが、ローカルで `embeddings.sqlite` を作る。
