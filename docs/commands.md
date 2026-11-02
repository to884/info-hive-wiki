---
title: コマンド
description: 10個の /wiki:* スラッシュコマンド（init、ingest、query、lint、stats、graph、migrate、upgrade、learn、evolve）のリファレンス。
---

<!-- Adapted from: docs/commands.html (source: README.md "Commands" table, commands/wiki/{init,ingest,query,lint,stats,graph,migrate,upgrade}.md). -->

# コマンド {#commands}

Claude Code のプラグインには、10個の `/wiki:*` スラッシュコマンドが付属する。ほかのエージェントでは、同じ依頼を自然言語で伝える。どちらの方法でも、裏で同じスキルが呼び出される。

どのコマンドも、ローカルの wiki と[ホスト型 wiki](/hosted) の両方で使える。ホスト型 wiki では、エージェントはローカルのファイルやスクリプトの代わりに MCP ツール（または `wiki_remote.py`）を使う。

| コマンド | 内容 |
| --- | --- |
| [`/wiki:init [--global]`](#init)、[`/wiki:init --hosted <url>`](#init) | 個人用のグローバルな wiki か現在のプロジェクトの wiki を作る。または、このエージェントをホスト型 wiki に接続する。 |
| [`/wiki:ingest <source>`](#ingest) | 新しいソースを処理して wiki に取り込む。グラフ層があれば更新する。 |
| [`/wiki:query <question>`](#query) | wiki をもとに、出典つきで質問に答える。 |
| [`/wiki:lint`](#lint) | 構造と内容の健全性をチェックする。 |
| [`/wiki:stats`](#stats) | 規模、リンク密度と、スケーリングのどの閾値に達しているかを表示する。 |
| [`/wiki:graph <action>`](#graph) | 型付きグラフ層をコンパイル、lint、またはクエリする。 |
| [`/wiki:learn <task artifacts>`](#learn) | 検証済みの経験を記録し、再利用できるパターンにまとめる。 |
| [`/wiki:evolve <goal or action>`](#evolve) | スキルの改善案をステージング、評価、確認、適用、またはロールバックする。 |
| [`/wiki:migrate [<wiki-dir>]`](#migrate) | ローカルの wiki をホスト型 wiki に移し、すべてのファイルを検証し、読み取り専用のミラーを残す。 |
| [`/wiki:upgrade`](#upgrade) | 既存の wiki を、現在のプラグインのバージョンにアップグレードする。 |

## `/wiki:init` {#init}

新しい LLM Wiki を作る。`--global` を付けると `~/wiki/` に作り、raw ソースは `~/wiki/raw/` に置く。付けなければ、現在のプロジェクトの `wiki/` と `raw/` に作る。オプションの `--wiki-dir` と `--raw-dir` で、これらの既定値を変えられる。

```bash
/wiki:init
/wiki:init --global
```

このコマンドは、保存先のスコープとパスを確認して wiki を作り、`uv` を通じてバージョンを固定した FastEmbed、sqlite-vec、PyYAML をインストールして動作を確かめる。続けて、ローカルの埋め込みモデルをキャッシュし、パースキャッシュを作り、現在あるすべてのセクションをベクトル化する。実行環境の JSON が `"status": "ready"` を返してから、`SCHEMA.md` の内容をユーザーと一緒に確認し、構成に合ったグローバルまたはプロジェクトのエージェントのメモリとの連携を提案する。ソースを取り込んだり、プロジェクトを自動で巡回したりはしない。

```bash
/wiki:init --hosted https://llm-wiki.example.workers.dev
```

`--hosted <url>` を付けると、代わりに、デプロイ済みの[ホスト型 wiki](/hosted) にこのエージェントを接続する。エージェント用のトークンを求め（または OAuth を提案し）、`wiki_remote.py login` で接続情報を保存し、`status` で確認し、クライアント用の MCP の設定を出力し、ホスト型 wiki 用のメモリの記述を提案する。クライアントの設定とメモリの記述は、ユーザーの承認を得てから書き込む。トークンをプロジェクト内のファイルやメモリファイルに保存することはない。

## `/wiki:ingest <source>` {#ingest}

スキルの取り込みワークフローに従い、ソース（論文、記事、文字起こし、PDF、メモ）を wiki に取り込む。

```bash
/wiki:ingest raw/attention-is-all-you-need.pdf
```

エージェントは `SCHEMA.md` を読み、raw ソースを `raw/` に置き、大きい場合は分割して読み、要点を話し合い、関係するページを wiki から洗い出す。続けて、ソースの要約ページを書き、`str_replace` で必要な箇所だけを更新し、新しいエンティティとコンセプトに新しいページを作り（それぞれに被リンクを付ける）、インデックスを更新し、`log.md` に1行追記する。グラフ層があり、ソースが型付きエッジを裏づけている場合は、グラフに lint をかけて抽出し直す。ホスト型 wiki では、エージェントは `wiki_remote.py raw put` で raw ファイルをアップロードし、ページの変更をすべて1つの `wiki_commit`（`create`、`edit`、`append`）で送る。グラフはサーバーが更新する。詳しくは[ワークフロー: 取り込み](/workflows#ingest)を参照。

## `/wiki:query <question>` {#query}

wiki をもとに、出典つきで質問に答える。

```bash
/wiki:query How does attention scale with sequence length?
```

エージェントはインデックス（または該当する分割インデックス）を読み、必要な `[[wikilinks]]` をたどり、出典つきで回答をまとめる。インデックスの要約だけでは足りない場合は、ローカルのハイブリッド検索を実行する。

```bash
uv run --script skills/llm-wiki/scripts/wiki_search.py \
  "<query terms>" --top 10 --cache --json
```

FastEmbed と sqlite-vec によるセマンティックな順位を、BM25 の結果と統合する。API キーは不要で、wiki やクエリのテキストを外部に送ることもない。依存パッケージを使わない語彙ベースの検索にするには、`python skills/llm-wiki/scripts/wiki_search.py "<query>" --no-embed` で PEP 723 の依存解決を経由せずに実行する。ページ単位で順位を付けるには `--granularity page` を使う。[検索と取得](/search)を参照。関係を問う質問では `graph.sqlite` も参照でき、内容のある回答は `wiki/synthesis/` に保存できる。ホスト型 wiki では、エージェントは `wiki_search`、`wiki_read`、`wiki_graph` を呼び出す。オプションの[判定レイヤー](/decisions)を使う場合、`--decide` を付けると、クエリへの回答に役立たない行が除外される。

## `/wiki:lint` {#lint}

構造と内容の健全性をチェックする。`--suggest-pages` を付けると、独立したページにすべきコンセプトを挙げる。

```bash
/wiki:lint
```

構造のチェックでは `wiki_lint.py` を実行し、孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の欠落、更新の止まったページ、重複したスラッグを検出する。内容のチェックでは、最近更新されたページとハブページを読み、矛盾や足りない点を探す。検出結果はすべて編集案として示され、黙って適用されるものはない。ホスト型 wiki では、`wiki_lint` を1回呼び出すと、構造の検出結果、グラフの検出結果、未解決のレビューフラグがまとめて返る。詳しくは[ワークフロー: lint](/workflows#lint)を参照。

## `/wiki:stats` {#stats}

wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。

```bash
/wiki:stats
```

`wiki_stats.py` を実行し、出力を解釈する。インデックスの分割の閾値（150 ページまたはインデックス 300 行）、検索の日常的な利用の閾値（300 ページ）、週1回の lint の閾値（500 ページ）に近づいているか、400 行のソフト上限や 800 行のハード上限を超えるページがあるか、リンク密度が低くないかを確かめる。ホスト型 wiki では、数値は `wiki_remote.py stats` から取得する。

## `/wiki:graph <action>` {#graph}

wiki の型付きグラフ層をコンパイル、lint、またはクエリする。グラフはオプションで、`wiki/graph/ontology.yaml` が必要である。

```bash
/wiki:graph extract
/wiki:graph neighbors --node product:konvy
/wiki:graph edges --subject person:stephanie-emmanouel
/wiki:graph path --from person:praney-behl --to product:konvy
/wiki:graph facts --about product:konvy
```

`extract` は markdown をコンパイルして `nodes.jsonl`、`edges.jsonl`、`graph.sqlite`、`graph.graphml` を作る。`lint` は型付きエッジを検証する。クエリ系のサブコマンドは、コンパイル済みのグラフをたどる。正本は markdown のままなので、グラフはいつ削除して作り直してもよい。[グラフ層のガイド](/graph)を参照。ホスト型 wiki では、サーバーがコミットのたびにグラフを最新に保つので、`extract` は不要である。クエリ系の操作には `wiki_graph` を使う。

## `/wiki:migrate` {#migrate}

ローカルの wiki を[ホスト型 wiki](/hosted) に移す。

```bash
/wiki:migrate ~/wiki --raw ~/wiki/raw
```

このコマンドは、接続を確認し、ローカルで lint を実行し、`wiki_remote.py import --dry-run` を実行してレポートを示す。ユーザーが承認すると、インポートを実行する。インポートは小さなバッチに分けてアップロードし、内容が同じファイルはスキップし、中断しても続きから再開する。そのあと、インポートが選んだすべてのページと raw ファイルについて、SHA-256 をサーバーのマニフェストと照合する。終了コード 0 だけでは、すべてのファイルが選ばれたことの保証にはならないので、`skipped` と `raw.skipped` のリストも読む。想定どおりの除外（生成されるグラフのファイル）と、欠けているソースコンテンツ（大きすぎる、UTF-8 のテキストでない、シンボリックリンク、拒否されたパス）を分け、欠けている各ファイルについて、直すか了承するかをユーザーに求める。[ローカルの wiki を移行する](/hosted#migrate-a-local-wiki)を参照。さらに、lint の件数と何件かの検索結果を比べてから、ローカルのフォルダーを読み取り専用の `pull` ミラーにすることと、メモリの記述を置き換えることを提案する。検証が通り、ユーザーが承認するまで、ローカルの wiki とエージェントの指示は変わらない。

## `/wiki:upgrade` {#upgrade}

既存の wiki をアップグレードする。ファイル操作は何度実行しても同じ結果になり、実行環境のインストールとインデックスの同期は必ず行い、`SCHEMA.md` のマージはユーザーと1つずつ確認しながら進める。

```bash
/wiki:upgrade
```

インストールされている `init_wiki.py` と、設定されたグローバルまたはプロジェクトの wiki を、それぞれ別に特定してから、`--upgrade` を付けて実行する。既存のコンテンツには手を付けずに、バージョンを固定したローカルの実行環境一式をインストールして確かめ、パースとベクトルのインデックスを同期し、`SCHEMA.md` に欠けているセクションを1つずつ確認する。[アップグレード](/upgrade)を参照。

## `/wiki:learn <task artifacts>` {#learn}

検証済みの成功や失敗を変更不可の raw の経験として記録し、ソースページとして取り込み、証拠、適用できる範囲、反例を添えた再利用できるパターンにまとめる。スキルは変更せず、費用のかかるモデルの評価も始めない。[学習とスキルの改善](/evolution)を参照。

## `/wiki:evolve <goal or action>` {#evolve}

範囲を区切った学習を実行し、観測した証拠をまとめ、1つのスキル全体に対する変更を提案し、検証用のデータで選び、固定した結果を独立にテストする。`history` と `show <id>` で過去の試行を確認できる。`apply <id>` は、承認とレビューを済ませて合格した候補をインストールする。`rollback <id>` は、間にほかの編集がなければ、元の状態に戻す。ランナーのセットアップ、デモ用のスイート、予算の制御、復旧については[学習とスキルの改善](/evolution)を参照。
