<!-- Adapted from: docs/commands.html (source: README.md "Commands" table, commands/{init,ingest,query,lint,stats,graph,upgrade}.md). -->

# コマンド

Claude Code のプラグインには、10個の `/wiki:*` スラッシュコマンドが付属する。ほかのエージェントでは、同じ依頼を自然言語で伝える。どちらの方法でも、裏で同じスキルが呼び出される。

| コマンド | 内容 |
| --- | --- |
| [`/wiki:init [--user]`](#wikiinit) | ユーザースコープの wiki か現在のプロジェクトの wiki を作る。 |
| [`/wiki:ingest <source>`](#wikiingest-source) | 新しいソースを処理して wiki に取り込む。グラフ層があれば更新する。 |
| [`/wiki:query <question>`](#wikiquery-question) | wiki をもとに、出典つきで質問に答える。 |
| [`/wiki:lint`](#wikilint) | 構造と内容の健全性をチェックする。 |
| [`/wiki:stats`](#wikistats) | 規模、リンク密度と、スケーリングのどの閾値に達しているかを表示する。 |
| [`/wiki:graph <action>`](#wikigraph-action) | 型付きグラフ層をコンパイル、lint、またはクエリする。 |
| [`/wiki:learn <task artifacts>`](#wikilearn-task-artifacts) | 検証済みの経験を記録し、再利用できるパターンにまとめる。 |
| [`/wiki:evolve <goal or action>`](#wikievolve-goal-or-action) | スキルの改善案をステージング、評価、確認、適用、またはロールバックする。 |
| [`/wiki:upgrade`](#wikiupgrade) | 既存の wiki を、現在のプラグインのバージョンにアップグレードする。 |
| [`/wiki:help`](#wikihelp) | コマンドの一覧を、説明と引数つきで表示する。 |

## `/wiki:init`

新しい LLM Wiki を作る。`--user` を付けると `~/info-hive/wiki/` に作り、raw ソースは `~/info-hive/raw/` に置く。付けなければ、現在のプロジェクトの `wiki/` と `raw/` に作る。オプションの `--wiki-dir` と `--raw-dir` で、これらの既定値を変えられる。

```bash
/wiki:init
/wiki:init --user
```

このコマンドは、保存先のスコープとパスを確認して wiki を作り、`uv` を通じてバージョンを固定した FastEmbed、sqlite-vec、PyYAML をインストールして動作を確かめる。続けて、ローカルの埋め込みモデルをキャッシュし、パースキャッシュを作り、現在あるすべてのセクションをベクトル化する。実行環境の JSON が `"status": "ready"` を返してから、`SCHEMA.md` の内容をユーザーと一緒に確認し、構成に合ったユーザースコープまたはプロジェクトのエージェントのメモリとの連携を提案する。ソースを取り込んだり、プロジェクトを自動で巡回したりはしない。

## `/wiki:ingest <source>`

スキルの取り込みワークフローに従い、ソース（論文、記事、文字起こし、PDF、メモ）を wiki に取り込む。

```bash
/wiki:ingest raw/attention-is-all-you-need.pdf
```

エージェントは `SCHEMA.md` を読み、raw ソースを `raw/` に置き、大きい場合は分割して読み、要点を話し合い、関係するページを wiki から洗い出す。続けて、ソースの要約ページを書き、`str_replace` で必要な箇所だけを更新し、新しいエンティティとコンセプトに新しいページを作り（それぞれに被リンクを付ける）、インデックスを更新し、`log.md` に1行追記する。グラフ層があり、ソースが型付きエッジを裏づけている場合は、グラフに lint をかけて抽出し直す。詳しくは[ワークフロー: 取り込み](./workflows.md#ソースを取り込む)を参照。

PDF で `rag@info-hive` の MCP ツールが使える場合は、原本を `raw/` に複製せず、先に `register_pdf` で登録する。`job_status` がジョブの `completed` を返してから、抽出済みの Markdown を読んで要約ページを作る。`queued`、`running`、`paused` は処理中であり、取り込みの完了とは報告しない。

## `/wiki:query <question>`

wiki をもとに、出典つきで質問に答える。

```bash
/wiki:query How does attention scale with sequence length?
```

エージェントはインデックス（または該当する分割インデックス）を読み、必要な `[[wikilinks]]` をたどり、出典つきで回答をまとめる。インデックスの要約だけでは足りない場合は、ローカルのハイブリッド検索を実行する。

```bash
uv run --script skills/info-hive/scripts/wiki_search.py \
  "<query terms>" --top 10 --cache --json
```

FastEmbed と sqlite-vec によるセマンティックな順位を、BM25 の結果と統合する。API キーは不要で、wiki やクエリのテキストを外部に送ることもない。依存パッケージを使わない語彙ベースの検索にするには、`python skills/info-hive/scripts/wiki_search.py "<query>" --no-embed` で PEP 723 の依存解決を経由せずに実行する。ページ単位で順位を付けるには `--granularity page` を使う。[検索と取得](./search.md)を参照。関係を問う質問では `graph.sqlite` も参照でき、内容のある回答は `wiki/synthesis/` に保存できる。オプションの[判定レイヤー](./decisions.md)を使う場合、`--decide` を付けると、クエリへの回答に役立たない行が除外される。

## `/wiki:lint`

構造と内容の健全性をチェックする。`--suggest-pages` を付けると、独立したページにすべきコンセプトを挙げる。

```bash
/wiki:lint
```

構造のチェックでは `wiki_lint.py` を実行し、孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の欠落、更新の止まったページ、重複したスラッグを検出する。内容のチェックでは、最近更新されたページとハブページを読み、矛盾や足りない点を探す。検出結果はすべて編集案として示され、黙って適用されるものはない。詳しくは[ワークフロー: lint](./workflows.md#lint-で健全性を確かめる)を参照。

## `/wiki:stats`

wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。

```bash
/wiki:stats
```

`wiki_stats.py` を実行し、出力を解釈する。インデックスの分割の閾値（150 ページまたはインデックス 300 行）、検索の日常的な利用の閾値（300 ページ）、週1回の lint の閾値（500 ページ）に近づいているか、ソフト上限（空白を除いて 4,000 文字か 400 行）やハード上限（8,000 文字か 800 行）を超えるページがあるか、リンク密度が低くないかを確かめる。

## `/wiki:graph <action>`

wiki の型付きグラフ層をコンパイル、lint、またはクエリする。グラフはオプションで、`wiki/graph/ontology.yaml` が必要である。

```bash
/wiki:graph extract
/wiki:graph neighbors --node product:konvy
/wiki:graph edges --subject person:stephanie-emmanouel
/wiki:graph path --from person:praney-behl --to product:konvy
/wiki:graph facts --about product:konvy
```

`extract` は markdown をコンパイルして `nodes.jsonl`、`edges.jsonl`、`graph.sqlite`、`graph.graphml` を作る。`lint` は型付きエッジを検証する。クエリ系のサブコマンドは、コンパイル済みのグラフをたどる。正本は markdown のままなので、グラフはいつ削除して作り直してもよい。[グラフ層のガイド](./graph.md)を参照。

## `/wiki:upgrade`

既存の wiki をアップグレードする。ファイル操作は何度実行しても同じ結果になり、実行環境のインストールとインデックスの同期は必ず行い、`SCHEMA.md` のマージはユーザーと1つずつ確認しながら進める。

```bash
/wiki:upgrade
```

インストールされている `init_wiki.py` と、設定されたユーザースコープまたはプロジェクトの wiki を、それぞれ別に特定してから、`--upgrade` を付けて実行する。既存のコンテンツには手を付けずに、バージョンを固定したローカルの実行環境一式をインストールして確かめ、パースとベクトルのインデックスを同期し、`SCHEMA.md` に欠けているセクションを1つずつ確認する。[アップグレード](./upgrade.md)を参照。

## `/wiki:learn <task artifacts>`

検証済みの成功や失敗を変更不可の raw の経験として記録し、ソースページとして取り込み、証拠、適用できる範囲、反例を添えた再利用できるパターンにまとめる。スキルは変更せず、費用のかかるモデルの評価も始めない。[学習とスキルの改善](./evolution.md)を参照。

## `/wiki:evolve <goal or action>`

範囲を区切った学習を実行し、観測した証拠をまとめ、1つのスキル全体に対する変更を提案し、検証用のデータで選び、固定した結果を独立にテストする。`history` と `show <id>` で過去の試行を確認できる。`apply <id>` は、承認とレビューを済ませて合格した候補をインストールする。`rollback <id>` は、間にほかの編集がなければ、元の状態に戻す。ランナーのセットアップ、デモ用のスイート、予算の制御、復旧については[学習とスキルの改善](./evolution.md)を参照。

## `/wiki:help`

コマンドの一覧を、説明と引数つきで表示する。

```bash
/wiki:help
```

`wiki_help.py` が `commands/*.md` の frontmatter（`description`、`argument-hint`、`argument-description`）を読み、コマンドごとに 3 行の Markdown の箇条書きにする。1 行目はコマンド名と引数の書式、2 行目は説明、3 行目は引数の説明である。コマンド名は太字のインラインコード、引数の書式はインラインコードにするので、`<質問>` の山かっこや `|` も崩れずに表示される。引数のないコマンドは「引数はない。」と出る。`/wiki:help` は、この出力をコードブロックに入れずに書式付きで示す。一覧は frontmatter から作るので、コマンドを足したり変えたりしたときに別に直す場所はない。

```markdown
- **`/wiki:query`** `<質問>`
  - LLM Wiki に問い合わせ、蓄積した知識から出典つきで質問に答える。
  - wiki に問い合わせる質問。日本語の質問は分かち書きせずにそのまま書く。
- **`/wiki:stats`**
  - wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。
  - 引数はない。
```
