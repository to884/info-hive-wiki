<!-- Adapted from: docs/commands.html (source: README.md "Commands" table, commands/{init,ingest,query,lint,stats,graph,upgrade}.md). -->

# コマンド

Claude Code のプラグインには、11個の `/wiki:*` スラッシュコマンドが付属する。ほかのエージェントでは、同じ依頼を自然言語で伝える。どちらの方法でも、裏で同じスキルが呼び出される。

`/wiki:init` と `/wiki:help` 以外のコマンドは、初期化された wiki（ルートに `SCHEMA.md` がある）だけを扱う。wiki の場所に `SCHEMA.md` がなければ（まだ初期化していないか、GitHub の wiki の写しのような、ほかの用途の `wiki/` のとき）、何も書かずに止まり、`/wiki:init` を案内する。同梱のスクリプトと MCP のツールも同じ判定で止まる。

| コマンド | 内容 |
| --- | --- |
| [`/wiki:init [--user]`](#wikiinit) | ユーザースコープの wiki か現在のプロジェクトの wiki を作る。 |
| [`/wiki:ingest <source>`](#wikiingest-source) | 新しいソースを処理して wiki に取り込む。グラフ層があれば更新する。 |
| [`/wiki:query <question>`](#wikiquery-question) | wiki をもとに、出典つきで質問に答える。 |
| [`/wiki:lint`](#wikilint) | 構造と内容の健全性をチェックする。 |
| [`/wiki:stats`](#wikistats) | 規模、リンク密度と、スケーリングのどの閾値に達しているかを表示する。 |
| [`/wiki:graph <action>`](#wikigraph-action) | 型付きグラフ層をコンパイル、lint、またはクエリする。 |
| [`/wiki:browse [slug]`](#wikibrowse-slug) | wiki をブラウザで閲覧する。 |
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

エージェントは `SCHEMA.md` を読み、raw ソースを `raw/` に置き、大きい場合は分割して読み、要点を話し合い、関係するページを wiki から洗い出す。続けて、ソースの要約ページを書き、`str_replace` で必要な箇所だけを更新し、新しいエンティティとコンセプトに新しいページを作り（それぞれに被リンクを付ける）、要約ページの「主張と行き先」の表に、主張ごとにどのページへ送ったかを残し、インデックスを更新し、`log.md` に1行追記する。グラフ層があり、ソースが型付きエッジを裏づけている場合は、グラフに lint をかけて抽出し直す。詳しくは[ワークフロー: 取り込み](./workflows.md#ソースを取り込む)を参照。

URL を渡すと、`wiki_fetch.py` がページの本文を Markdown にして画像とともに `raw/` に保存する。サイトに共通の雑音は取得の段階で消す。エージェントは残った雑音を削り（消すことだけをする）、日本語でなければ段落ごとに訳し、画像の直後に `> <表示名> 補足: ` の説明を書いてから、通常の手順で取り込む。書き換えは段ごとに `wiki_rawcheck.py` が取得した直後の写しと照らし、コード、リンク、画像、表の形を変えていれば止める。有料の記事のようにブラウザでしか本文を取れないページは、保存した HTML を `--html-file` で渡す。原文の HTML の SHA-256 を記録するので、同じ内容のページを取り直しても新しいファイルはできない。

```bash
/wiki:ingest https://example.com/posts/kalman-filter
```

PDF で `rag@info-hive` の MCP ツールが使える場合は、原本を `raw/` に複製せず、先に `register_pdf` で登録する。`job_status` がジョブの `completed` を返してから、抽出済みの Markdown を読んで要約ページを作る。`queued`、`running`、`paused` は処理中であり、取り込みの完了とは報告しない。

## `/wiki:query <question>`

wiki をもとに、出典つきで質問に答える。

```bash
/wiki:query How does attention scale with sequence length?
```

エージェントはインデックス（または該当する分割インデックス）を読み、必要な `[[wikilinks]]` をたどり、出典つきで回答をまとめる。インデックスの要約だけでは足りない場合は、ローカルのハイブリッド検索を実行する。検索は、まずプラグインの MCP サーバーのツール `query` で行う（[MCP サーバー](../skills/info-hive/references/mcp-server.md)）。サーバーはモデルと索引を読み込んだまま常駐するので、1 回の検索は数十ミリ秒で返る。ツールが使えないときは、次の CLI に戻る。

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

構造のチェックでは MCP のツール `lint`（使えなければ `wiki_lint.py`）を使い、孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の欠落、更新の止まったページ、重複したスラッグを検出する。内容のチェックでは、最近更新されたページとハブページを読み、矛盾や足りない点を探す。検出結果はすべて編集案として示され、黙って適用されるものはない。詳しくは[ワークフロー: lint](./workflows.md#lint-で健全性を確かめる)を参照。

## `/wiki:stats`

wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。

```bash
/wiki:stats
```

MCP のツール `stats`（使えなければ `wiki_stats.py`）で集計し、結果を解釈する。ツールは検索の索引の状態も返す。インデックスの分割の閾値（150 ページまたはインデックス 300 行）、検索の日常的な利用の閾値（300 ページ）、週1回の lint の閾値（500 ページ）に近づいているか、ソフト上限（空白を除いて 10,000 文字か 100 行）やハード上限（25,000 文字か 200 行）を超えるページがあるか、リンク密度が低くないかを確かめる。

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

## `/wiki:browse [slug]`

wiki を、日本語の CSS と、ナビゲーション、メタ欄、被リンク、ページごとの関係表、未解決リンクの表示つきの HTML で閲覧する。必要なときだけ 127.0.0.1 で待ち受けるサーバーを起動し、操作がない状態が 30 分続くと終わる。閲覧は読むだけで、wiki のファイルを変えない。

```bash
/wiki:browse
/wiki:browse 知識グラフ
/wiki:browse --stop
```

サイドバーの先頭とタブの題名に、wiki のスコープ（ユーザースコープか、プロジェクトか）を出す。スコープは、その wiki が指示や設定で何として登録されているかで、コマンドが決める。グローバルなエージェントの指示がユーザースコープの wiki として指す wiki はユーザースコープ、プロジェクトの指示がそのプロジェクトの wiki として指す wiki と現在のプロジェクトの `wiki/` はプロジェクトとし、作業領域の設定で見つけた wiki は設定の `kind` に従う。手がかりがないか、食い違えば、スコープを出さない。スコープを wiki の場所から推し量ることはしない。

slug を付けると、`[[slug]]` と同じ規則でページを探して開く。リンクの判定は lint と同じで、未解決リンクの一覧は lint の「リンク切れの wikilink」と一致する。ページを開いたときに Markdown を HTML に変換するので、ページを編集して再読み込みすれば、新しい内容が出る。URL には起動ごとのトークンが入り、ほかのページや、同じ PC のほかの利用者からは開けない。

持ち出せる静的の HTML は、`wiki_browse.py build <WIKI_ROOT> --out <書き出し先>` で書き出せる。書き出し先は wiki の外の空のディレクトリにする。

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
