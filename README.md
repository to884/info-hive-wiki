# LLM Wiki: AI エージェントのための第二の脳

PDF、記事、文字起こし、メモを、AI エージェントが検索し、出典を示し、最新の状態に保てる共有の wiki に変える。ソースは一度追加するだけでよい。あとから質問し、役に立った回答は残しておける。

Claude Code、Codex、Cursor、Gemini CLI、OpenCode、OpenClaw、Pi、OMP、Hermes で動く。[ドキュメントを読む](https://praneybehl.github.io/llm-wiki-plugin/)。

## LLM Wiki とは

AI エージェントは目の前のタスクには強いが、新しいセッションは限られたコンテキストから始まる。LLM Wiki はエージェントに共有のメモリを与える。このメモリは、すべてのプロジェクトで使う1つの個人用 wiki に置くことも、特定のプロジェクトの中に置くこともできる。

ソースを追加すると、エージェントはそれをリンクでつながった Markdown ページに変える。あとで適切なセクションを見つけ、出典つきで答えられる。役に立った回答は wiki に保存し直せるので、知識は毎回ゼロから組み立て直されるのではなく、積み重なっていく。

正本はすべて、読める Markdown のまま残る。既定のセマンティック検索はローカルで動き、ホスト型のベクトルデータベースや埋め込みサービスは使わない。自分の Cloudflare アカウントに置く任意の[ホスト型 wiki](#ホスト型-wiki) を使えば、複数のマシンのエージェントで1つの wiki を共有できる。

## v4 の新機能

- **任意で使えるホスト型 wiki。** ワンクリックで、自分のアカウントに Cloudflare Worker を1つデプロイできる。どのマシンのエージェントも、MCP か JSON API で wiki を読み書きできる。[ホスト型 wiki](#ホスト型-wiki) を参照。
- **安全な共有の書き込み。** 変更はすべて、作成者とリビジョンを持つアトミックなコミットになる。必要な箇所だけを変える `edit` と `append` の操作は、めったに競合しない。競合したときは、エージェントにページを読み直して再試行するよう伝える。
- **移行とミラー。** `/wiki:migrate` はローカルの wiki を移行し、アップロードしたすべてのファイルを SHA-256 で検証し、インポートがスキップしたファイルを報告する。`wiki_remote.py pull --git` は、Git の履歴や Obsidian で使える読み取り専用のローカルミラーを保つ。
- **任意で使える判定モデル。** 小さなモデルで、検索結果を関連度でふるい分けたり、グラフの証拠をチェックしたり、信頼できないテキストにフラグを立てたりできる。既定では無効。[任意の判定レイヤー](#任意の判定レイヤー)を参照。
- **ローカルモードは変わらない。** 既存の wiki、スクリプト、コマンドは、これまでどおり動く。

v3.2.0 では、完了した作業から学ぶ機能が加わった。`/wiki:learn` は検証済みの経験を記録し、`/wiki:evolve` はスキルの変更を提案、テスト、適用し、ロールバックする。[学習とスキルの改善](https://praneybehl.github.io/llm-wiki-plugin/evolution.html)を参照。改善のワークフローは、[WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution](https://arxiv.org/html/2608.27454v1)（Google Research と Virginia Tech、2026）を応用したものである。[取り入れた点と変えた点](https://praneybehl.github.io/llm-wiki-plugin/evolution.html#research-basis-and-adaptation)と[測定結果](eval/evolution/workflows/README.md)を参照。私たちの調査では2つの提案がどちらも却下され、品質の向上は示されなかった。

## 使う理由

- **プロジェクトのコンテキストを繰り返し説明しなくて済む。** エージェントは、すでに集めた知識を読める。
- **複数のエージェントで同じ wiki を使える。** ファイルはただの Markdown で、特定のモデルやツールに縛られない。
- **すべての回答の出所をたどれる。** 出典から、wiki のページと元のソースに戻れる。
- **wiki を健全に保てる。** 新しいソースが入るたびに、エージェントがリンク、要約、矛盾を更新する。
- **主導権を手放さない。** ページは読むことも、編集することも、バージョン管理することもできる。

LLM Wiki がもっとも役立つのは、時間とともに増えていく知識である。調査、会議のメモ、顧客との通話、論文、記事、プロジェクトの決定事項がこれに当たる。構造化されたレコードやトランザクションが主な課題なら、ふつうのデータベースを使う。

## インストール
前提として必要なのは `uv` だけである。`uv` は、対応しているすべてのプラットフォームで、バージョンを固定したスクリプトの実行環境を作る。`/wiki:init` や、それに当たる自然言語での依頼を実行する前に、[uv の公式の手順](https://docs.astral.sh/uv/getting-started/installation/)でインストールしておく。

プラグインやスキルをインストールすると、エージェントが使うコマンドと同梱のツールがすべてコピーされる。そのあと wiki の初期化で、必須の実行環境のセットアップを行う。対象は、FastEmbed `0.8.0`、sqlite-vec `0.1.9`、PyYAML `6.0.3`、ローカルの `BAAI/bge-small-en-v1.5` モデル、パースキャッシュ、既存の全セクションのベクトルである。アップグレードでも同じセットアップを再実行し、変更されたセクションと削除されたセクションを差分で同期する。


### Claude Code: プラグイン一式

ネイティブの方法である。スキル、10個の `/wiki:*` スラッシュコマンド、マーケットプレイスのマニフェストが、1回のインストールでそろう。

```
/plugin marketplace add praneybehl/llm-wiki-plugin
/plugin install llm-wiki@llm-wiki
```

インストールすると、プラグインはどのプロジェクトでも使える。インストールしただけでは、wiki をどこに置くかは決まらない。

### ほかのコーディングエージェント: スキルのみ

`llm-wiki` スキルは標準の [agentskills.io](https://agentskills.io) 形式なので、[`skills` CLI](https://github.com/vercel-labs/skills) が対応しているどのエージェントにもそのままインストールできる。環境に合った `--agent` フラグを選ぶ。

```bash
# グローバルにインストールし、すべてのプロジェクトでスキルを使えるようにする
npx skills add praneybehl/llm-wiki-plugin -a <agent> -g

# または、現在のプロジェクトだけにインストールする
npx skills add praneybehl/llm-wiki-plugin -a <agent>
```

| エージェント | `--agent` の値 | 呼び出し方 | スクリプトの実行 |
|-------|-----------------|------------|:-----------:|
| Claude Code | `claude-code` | `/wiki:*` スラッシュコマンド（同梱）または自然言語 | ✅ |
| Codex (OpenAI) | `codex` | `/skills` または `$llm-wiki` / 自然言語 | ✅ |
| Cursor | `cursor` | `/llm-wiki` または自然言語 | ✅ |
| Gemini CLI | `gemini-cli` | `/skills` の管理コマンド / 自然言語 | ✅ |
| OpenCode | `opencode` | 自然言語（エージェントがネイティブの `skill` ツールを呼び出す） | ✅ |
| OpenClaw | `openclaw` | ユーザーコマンドとして自動で公開される | ✅ |
| Pi Agent | `pi` | `/skill:llm-wiki` または自然言語 | ✅ |
| OMP ("Oh My Pi") | 手動（下記を参照） | 自然言語（スキルは `skill://` から自動で見つかる） | ✅ |

OpenCode は `.claude/skills/` と `~/.claude/skills/` も読むので、Claude Code 用にスキルをインストール済みなら、OpenCode で使うために改めてインストールする必要はない。

**Hermes Agent**（Nous Research）、**OMP**（"Oh My Pi"）など、まだ `npx skills` のレジストリにない agentskills.io 互換のランタイムでも、このスキルは使える。リポジトリをクローンし、`skills/llm-wiki/` をエージェントのスキルディレクトリにシンボリックリンクするかコピーする。Hermes は `~/.hermes/skills/` から読む。OMP は、管理されたスキルとユーザーのスキルを `~/.omp/agent/skills/` から読み、`skill://` で公開する。

```bash
git clone https://github.com/praneybehl/llm-wiki-plugin.git
mkdir -p ~/.hermes/skills ~/.omp/agent/skills
ln -s "$(pwd)/llm-wiki-plugin/skills/llm-wiki" ~/.hermes/skills/llm-wiki
ln -s "$(pwd)/llm-wiki-plugin/skills/llm-wiki" ~/.omp/agent/skills/llm-wiki
```

Claude Code 以外でスキルを使うときに知っておくべきことは次のとおり。

- **スラッシュコマンドは Claude Code 専用である。** 10個の `/wiki:*` コマンドは、Claude Code プラグインのマニフェストとして `commands/wiki/` にある。ほかのエージェントでは、自然言語でスキルを呼び出す（「この論文を wiki に追加して」「X について wiki には何が書いてある？」「wiki に lint をかけて」）。あとは SKILL.md が処理する。
- **同梱のツールはすべて、エージェントから使える。** 一覧のどのエージェントも、インストールしたスキルを通じて、`setup_wiki.py`、ハイブリッド検索と語彙ベースの検索、lint、stats、グラフの lint・抽出・クエリ、初期化を実行できる。依存パッケージを持つスクリプトには、バージョンを固定した PEP 723 のメタデータがあり、`uv run --script` で実行する。初期化とアップグレードでは、準備が整ったと報告する前に実行環境一式を検証する。
- **wiki 自体はエージェントに依存しない。** ただの Markdown ファイルのディレクトリである。あるエージェントで取り込み、別のエージェントでクエリしてもよい。`wiki/` の中に、特定のランタイムに縛るものは何もない。

### wiki を置く場所を選ぶ

スキルの適用範囲と wiki の適用範囲は、別々に選ぶ。スキルをグローバルにインストールすると、すべてのプロジェクトでスキルが使えるようになるが、グローバルな wiki が作られたり選ばれたりするわけではない。

- **プロジェクトをまたいで使う個人用の wiki を1つ置く場合:** `~/wiki/` のような、ユーザーレベルの固定のパスに置き、raw のソースは `~/wiki/raw/` の下に置き、エージェントのグローバルな指示でそこを指す。こうすると、どのプロジェクトでの作業も、意図して同じ wiki に取り込める。
- **プロジェクトごとに wiki を1つ置く場合:** `wiki/` と `raw/` をプロジェクトの中に置き、プロジェクトのエージェントのメモリファイルでそれらを指す。知識をそのリポジトリと切り離しておきたい場合や、リポジトリと一緒にバージョン管理したい場合に選ぶ。

グローバルな wiki があっても、すべてのプロジェクトを自動で巡回して取り込むことはない。エージェントがプロジェクトの事実、決定事項、ソースを追加するのは、ユーザーが取り込みを頼んだときか、グローバルな指示で残しておくべき知見を保存するよう明示しているときである。

## クイックスタート

プロジェクトをまたいで共有する個人用の wiki を1つ作る場合:

```
/wiki:init --global
```

これは、raw のソースを `~/wiki/raw/` の下に置く構成で `~/wiki/` を初期化し、エージェントのグローバルな指示に追加する記述を提案する。

現在のプロジェクトだけで使う wiki を作る場合:

```
/wiki:init
```

以前のバージョンのプラグインで作った wiki がすでにある場合は、代わりに `/wiki:upgrade` を実行する。init と upgrade はどちらも、バージョンを固定したローカルの実行環境一式をインストールして検証し、埋め込みモデルをキャッシュし、パースキャッシュを作り、現在のすべてのベクトルを同期する。足りない wiki のファイルは冪等に追加し、既存のページや `SCHEMA.md` を上書きすることはない。

初期化では、選んだ wiki と raw のソースのルートに、`SCHEMA.md`、`index.md`、`log.md`、ページのテンプレートを用意したあと、実行環境の JSON のレポートを出力する。セットアップが完了したのは、レポートが `"status": "ready"` を示したときだけである。

同じ手順の中で、スキルは、エージェントが今後のセッションでも wiki を覚えているように、wiki の場所を示す記述を提案する。個人用のグローバルな wiki ならグローバルな指示に、プロジェクトの wiki ならプロジェクトのエージェントのメモリファイルに書く。スキルは、ユーザーの承認なしにメモリファイルに書き込むことはない。両方の書き方は `skills/llm-wiki/references/agent-memory-integration.md` を参照。

最初のソースを、設定した raw のルート（グローバルな構成なら `~/wiki/raw/` など）に置いてから、次を実行する。

```
/wiki:ingest ~/wiki/raw/your-source.pdf
```

Claude はソースを読み（大きい場合は分割して読む）、要点をユーザーと話し合い、要約ページを書き、関係する既存のエンティティとコンセプトのページを洗い出し、`str_replace` で必要な箇所だけを更新する。新しいエンティティとコンセプトには新しいページを作り（それぞれに少なくとも1つの被リンクを付ける）、インデックスを更新し、ログに追記する。

ソースがいくつか入ったら、次のように質問する。

```
/wiki:query What does my wiki say about X?
```

Claude はインデックスを読んで候補のページを見つけ、それらを読み、`[[wikilink]]` の出典つきで回答をまとめる。中身のある回答は、調べた結果が積み重なるように `wiki/synthesis/` に保存することを提案する。

定期的に、健全性のチェックを実行する。

```
/wiki:lint
```

これは、孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の欠落、古くなった主張を検出する。検出結果は編集案として示され、Claude が黙って wiki を書き換えることはない。

## コマンド

| コマンド | 内容 |
|---------|--------------|
| `/wiki:init [--global]` | 個人用のグローバルな wiki か、現在のプロジェクトの wiki を初期化する（どちらもグラフ層を含む）。 |
| `/wiki:ingest <source>` | 新しいソースを wiki に取り込む。グラフ層があれば更新する。 |
| `/wiki:query <question>` | wiki をもとに出典つきで質問に答える。関係を問う質問では、使える場合は `graph.sqlite` も参照する。 |
| `/wiki:lint` | 構造と内容の健全性をチェックする。`wiki/graph/ontology.yaml` があればグラフの lint も実行する。 |
| `/wiki:stats` | wiki の規模、リンク密度、スケーリングのどの閾値に達しているかを表示する。 |
| `/wiki:graph <action>` | コンパイル済みのグラフ層に対して `extract` / `lint` / `neighbors` / `edges` / `path` / `facts` を実行する。 |
| `/wiki:learn <task artifacts>` | 検証済みの経験を記録し、根拠にリンクした再利用できるパターンにまとめる。 |
| `/wiki:evolve <goal or action>` | スキルの改善案を作成・評価・確認・適用し、ロールバックする。すべての試行の結果を残す。 |
| `/wiki:init --hosted <url>` | このエージェントをホスト型 wiki に接続する。トークンを保存し、MCP の設定を出力し、メモリに書く記述を提案する。 |
| `/wiki:migrate [<wiki-dir>]` | ローカルの wiki をホスト型 wiki に移行し、アップロードしたすべてのファイルを SHA-256 で検証し、スキップされたファイルを確認し、読み取り専用の `pull` ミラーを残す。 |
| `/wiki:upgrade` | 既存の wiki を現在のプラグインのバージョンにアップグレードする（冪等なファイル操作と、手順を追った SCHEMA.md のマージ）。 |

コマンドを使わなくてもよい。Claude は、自然言語での依頼（「この論文を wiki に追加して」「拡散モデルについて wiki には何が書いてある？」「wiki に lint をかけて」）でも、裏にあるスキルを起動する。コマンドは、明示的に呼び出したいときのためにある。

## アーキテクチャ

wiki は3つの層と3つの操作でできている。

**3つの層**は、*raw のソース*（`raw/` に置く、変更しない元の資料）、*wiki*（`wiki/` にある、LLM が管理する Markdown ページ。`sources/`、`entities/`、`concepts/`、`synthesis/` に分けて置く）、*スキーマ*（`wiki/SCHEMA.md`。その wiki 固有の規約を記した設定ファイルで、ユーザーが Claude と一緒に少しずつ改訂していく）である。

**3つの操作**は、*取り込み*（新しいソースを wiki にコンパイルする。1回で10〜15ページに手を入れる）、*クエリ*（wiki をたどって質問に答える。回答を新しいシンセシスのページとして保存することもできる）、*lint*（構造と内容の整合性を定期的にチェックする）である。

**任意の4つ目の層**として、`wiki/graph/` のグラフもある。ページの frontmatter には、型付きの `graph:` メタデータ（`founded`、`proposed`、`depends_on` など）を、明示的なソースページのスラッグと証拠となる引用とともに宣言できる。同梱の抽出スクリプトは、すべてのページを `nodes.jsonl`、`edges.jsonl`、`graph.sqlite`、`graph.graphml` にコンパイルする。**正本は Markdown のまま**で、グラフはいつでも削除して作り直せる。インデックスだけでは安く答えられない関係を問うクエリ（「X とつながっているものは？」「Y を提案したのは誰？」「A → B の最短経路」）には、`/wiki:graph` を使う。

ページの種類、frontmatter の規約、各設計判断の理由、ワークフローの手順を含むアーキテクチャの詳しい説明は、スキルの中のリファレンス `skills/llm-wiki/references/` を参照。グラフ層については `skills/llm-wiki/references/graph-workflow.md` に書いてある。

## 経験から学ぶ

任意で使う改善のワークフローは、変更不可のタスクの証拠を、通常のソースとコンセプトのパターンに結びつけ、さらに測定にもとづくスキルの変更に結びつける。評価の間、候補のスキルは隔離される。消えずに残る `.evolution/` のアーカイブは、通常の wiki の検索からは除外されるが、出典つきの教訓と要約は検索できる。`wiki_evolve.py --help` か[エージェント向けのワークフロー](skills/llm-wiki/references/evolution-workflow.md)を参照。モデルのランナーでは、リモートの推論に費用がかかることがある。記録とハーネスそのものは、標準ライブラリだけで動く。

## スケーリングの仕組み

LLM Wiki パターンで最大の失敗は、wiki が大きくなるにつれて、コンテキストのボトルネックになることである。このプラグインの設計は、主にこの失敗を避けるように組み立てている。

**アトミックなページ。** 1ページで扱うのは1つのコンセプトだけである。ソフト上限は 400 行、ハード上限は 800 行で、lint スクリプトがこれを強制する。1ページを読む量には上限があるので、ページが1000あっても、5ページを読むクエリは十分にコンテキストに収まる。

**分割したインデックス。** `wiki/index.md` が 300 行を超えるか、wiki が約 150 ページを超えると、インデックスは `wiki/indexes/` の下の種類ごとのファイルに分割される。トップレベルのインデックスは、分割したファイルの小さな目次になる。インデックスを読むのは2段階の検索になるが、各段階の量には上限がある。

**すべてのページに YAML の frontmatter。** `wiki_search.py` スクリプトは、ページの本文を読まずに `type`、`tags`、`updated` で絞り込めるので、検索は wiki 全体の大きさとは関係なくスケールする。

**インデックスを先に読むナビゲーション。** クエリでは最初にインデックスを読み、1行の要約から候補のページを見つけ、それだけを読む。インデックスは、このパターン全体をスケールさせるキャッシュである。

**ローカルのハイブリッド検索。** インデックスの要約で足りないときは、`uv run --script skills/llm-wiki/scripts/wiki_search.py "query terms"` が、ローカルの FastEmbed によるセマンティックなベクトルと BM25 の結果を RRF で統合して、Markdown のセクションに順位を付ける。sqlite-vec は、内容のハッシュを付けたベクトルを `wiki/.wiki-cache/embeddings.sqlite` に保存し、変更されたセクションだけをベクトル化し直す。API キーもプロバイダーも不要で、wiki やクエリのテキストを外部に送ることもない。`--json` は証拠の行を出力し、`--cache` はパースしたセクションを保存する。依存パッケージをまったく使わない方法では、`python skills/llm-wiki/scripts/wiki_search.py "query terms" --no-embed` を実行する。`--granularity page` を付けると、ページ単位の順位付けに戻る。

**必要な箇所だけの編集。** 取り込みの間にページを更新するとき、Claude は `str_replace` で該当するセクションだけを変え、ページ全体を書き直さない。速く、トークンの消費が少なく、wiki を git で管理していれば差分も読みやすいまま保てる。

**ソースの分割取り込み。** 大きな raw のソース（長い PDF、何時間もの文字起こし）は、取り込みの間に分割して読み、全体を一度に読み込まない。取り込みのワークフローが、これを自動で行う。

`/wiki:stats` コマンドで、どの閾値に達しているかと、次に行う移行が分かる。閾値と移行の手順をまとめたスケーリングの手引きは `skills/llm-wiki/references/scaling-playbook.md` にある。

## 目指していないもの

これは Markdown にもとづく個人用のナレッジベースであり、データベースではない。扱う領域が本質的にリレーショナルなら（10万件の顧客レコードの管理、複雑な組織構造、厳密な参照整合性が必要な会計の台帳など）、本物のデータベースのほうが向いている。Markdown と grep と frontmatter の組み合わせは、思ったよりもスケールする（Karpathy 自身の wiki は約 100 記事、40 万語で問題なく動いていた）が、どこかで適したツールは変わる。

知っておくべき失敗のパターンは次の3つで、どれも `skills/llm-wiki/SKILL.md` で扱っている。1つ目は、気づかないうちに内容が壊れることである。1つのソースを読み違えると、それが権威のある wiki ページのように見え、その後の取り込みに影響する。これは、必須の `sources:` frontmatter と lint で抑える。2つ目は、wiki が自分の出力を読むことによるずれである。Claude が raw のソースと照らし合わせずに、以前の wiki ページを正しいものとして扱ってしまう。これは、主張を更新するときに raw のソースを読み直すよう求める取り込みのワークフローの指示で抑える。3つ目は、保守の負担が増え続けることである。lint のレポートが、ユーザーがレビューできるより速く増えていく。これはスケーリングのための規律で抑えるが、それでも起きるなら、スキーマを見直す必要がある。

## ホスト型 wiki

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/praneybehl/llm-wiki-plugin/tree/main/server)

ホスト型 wiki は任意で使うものである。wiki を自分のアカウントの Cloudflare Worker に置くので、複数のマシンのエージェント、リモートのエージェント、ファイルにアクセスできないエージェントで1つの wiki を共有できる。

- **ワンクリックでデプロイできる。** ボタンを押すと、Worker、wiki ごとの SQLite Durable Object、KV、R2、Vectorize、Workers AI が作成される。フォームでは、`ADMIN_TOKEN` と Vectorize の設定（`384` 次元、`cosine` 指標）を入力する。デプロイのあとに必要な作業はない。サーバーは Workers の Free プランで動く。
- **どのエージェントからも MCP で使える。** `/mcp` に8つのツールがある: `wiki_overview`、`wiki_read`、`wiki_search`、`wiki_commit`、`wiki_history`、`wiki_graph`、`wiki_lint`、`wiki_raw`。スクリプトや CI からは、`/api/v1` の JSON API を使う。
- **エージェントごとのアクセス。** エージェントごとにスコープ付きのトークンを発行し、そのラベルがコミットの作成者になる。OAuth に対応した MCP クライアントは、トークンなしで接続できる。
- **正本は Markdown のまま。** ハイブリッド検索（FTS5 の BM25 と Workers AI の `bge-small-en-v1.5` のベクトルを RRF で統合）、リンク、グラフは、コミットのたびに更新される。
- **履歴とバックアップ。** すべてのコミットがリビジョンとして残る。revert は新しいコミットを作る。`export` はすべてのページを書き出す。Durable Objects では、30 日分のポイントインタイムリカバリーが使える。
- **ブラウザー用のリーダー**が `/ui/<wiki>/` にあり、フォルダーツリー、検索、被リンク、履歴、差分、レビューフラグを表示できる。`/setup` のページでは、wiki、トークン、クライアントごとの MCP の設定を作れる。

始め方:

1. 上のボタンを押すか、`server/` で `npm run deploy` を実行して CLI からデプロイする（初回のデプロイで使うシークレットについてはガイドを参照）。
2. `https://<your-worker>/setup` を開き、`ADMIN_TOKEN` でサインインし、wiki と、エージェントごとのトークンを作る。
3. 各エージェントで `/wiki:init --hosted https://<your-worker>` を実行する。
4. 既存の wiki を移すには、`/wiki:migrate` を実行する。

エージェントごとの MCP のセットアップ、OAuth、Free プランと Paid プランの制限、費用、セキュリティについては、[ホスト型 wiki のガイド](https://praneybehl.github.io/llm-wiki-plugin/hosted.html)を参照。

## 任意の判定レイヤー

小さな判定モデルは、コードが用意した選択式の問い（たとえば「このセクションはクエリに答える助けになるか？」）に答える。LLM Wiki はこれを使って、検索結果を関連度でふるい分けたり、型付きのグラフのエッジの証拠をチェックしたり、矛盾を洗い出したり、取り込みの間にページの候補を絞り込んだり、AI エージェントに命令するようなソースのテキストにフラグを立てたりできる。

このレイヤーは既定では無効である。判定の結果でページが書き込まれたり、書き込みが止められたりすることはない。読み取りの処理では、判定に失敗しても処理を通す（fail-open）。バックエンドは、`llama-server` で動かすローカルの Liquid `d1-2B` モデル（データはマシンの外に出ない）、TypeSafe Jev、OpenRouter、Liquid API、ホスト型 wiki では Workers AI 上の Jev から選べる。ホスト型のバックエンドには wiki のテキストが送られるので、有効にするかは自分で決める。プライバシー、ライセンスの条件、費用については[判定モデル](https://praneybehl.github.io/llm-wiki-plugin/decisions.html)を参照。

## ツール

wiki をセットアップしたら、どの Markdown ビューアーでもページを読んで編集できる。[Obsidian](https://obsidian.md) は、グラフビュー、`[[wikilinks]]` の構文、Web Clipper 拡張機能があるので特に相性がよいが、必須ではない。wiki はプロジェクトの中にある、ただの Markdown ファイルのディレクトリである。

ツールには Python 3.10 以上と `uv` が必要である。初期化とアップグレードでは `setup_wiki.py` を呼び出す。その PEP 723 の環境では、FastEmbed 0.8.0、sqlite-vec 0.1.9、PyYAML 6.0.3 のバージョンを固定しており、モデルをキャッシュし、wiki のインデックス全体を同期する。検索、グラフの lint、グラフの抽出も、それぞれバージョンを固定したスクリプトのメタデータを持つ。語彙ベースの `wiki_search.py --no-embed`、`wiki_lint.py`、`wiki_stats.py`、グラフのクエリは、標準ライブラリだけの Python で直接実行できる。同梱のスクリプトは、インストール後は `skills/llm-wiki/scripts/` にある。

## クレジット

任意で使うスキルの改善のワークフローは、[WikiSkill の論文](https://arxiv.org/html/2608.27454v1)を応用したものである。[応用の対応表](https://praneybehl.github.io/llm-wiki-plugin/evolution.html#research-basis-and-adaptation)では、研究のアイデア、このプロジェクトの実装上の選択、検証の限界を区別している。

このパターンは、Andrej Karpathy の [llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)（2026 年 4 月）にもとづく。このプラグインは、そのパターンの実装の1つである。ほかに見ておく価値のある実装として、[vanillaflava/llm-wiki-claude-skills](https://github.com/vanillaflava/llm-wiki-claude-skills)、[skyllwt/OmegaWiki](https://github.com/skyllwt/OmegaWiki)、[axoviq-ai/synthadoc](https://github.com/axoviq-ai/synthadoc) がある。

v2 の検索のアーキテクチャは、Cerebras のエンジニアリングの記事 [How We Built Our Knowledge Base](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base) を参考にした。特に、セクション単位の検索、複数の検索器の結果の統合、証拠のまとめ方、評価の方法を参考にしている。

v3 でローカルの検索に切り替えるときは、[Basic Memory](https://github.com/basicmachines-co/basic-memory) と比べて評価した。LLM Wiki は、同じローカルファーストの形（FastEmbed、SQLite のベクトル検索、差分で更新する派生インデックス）を取り入れている。一方で、Basic Memory のアプリケーションと MCP サービスを2つ目の正本のシステムとして加えることはせず、既存の Markdown のスキーマ、型付きグラフ、RRF による順位付け、エージェントに合わせたワークフローを残している。

## ライセンス

MIT。[LICENSE](./LICENSE) を参照。

## コントリビュート

Issue と PR を歓迎する。スキル本体は `skills/llm-wiki/`、スラッシュコマンドは `commands/wiki/`、ホスト型のサーバーは `server/` にある。[CONTRIBUTING.md](./CONTRIBUTING.md) を参照。PR を出す前に、同梱のスクリプトを小さなテスト用の wiki に対して実行し、何も壊れていないことを確かめてほしい。
