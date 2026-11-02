---
title: はじめに
description: 使っているエージェント向けに LLM Wiki スキルをインストールし、init、最初の取り込み、最初のクエリ、最初の lint を順に試す。
---

<!-- Adapted from: docs/getting-started.html (source: README.md "Installation"/"Quick start", commands/wiki/init.md, commands/wiki/ingest.md). -->

# はじめに {#getting-started}

使っているコーディングエージェント向けにスキルをインストールし、初期化、取り込み、クエリ、lint の4ステップを繰り返す。

## インストール {#install}

インストール方法は2通りある。Claude Code には、プラグイン一式（スキル、`/wiki:*` スラッシュコマンド、マーケットプレイスのマニフェスト）が入る。ほかの対応エージェントには、[`skills` CLI](https://github.com/vercel-labs/skills) を使って、同じ [agentskills.io](https://agentskills.io) 形式のスキルをインストールする。全体像は[エージェント対応表](/agents)を参照。

最初に [`uv`](https://docs.astral.sh/uv/getting-started/installation/) をインストールする。前提条件はこれだけで、同梱のツール用に、バージョンを固定した独立した環境を `uv` が作る。

### Claude Code: プラグイン一式 {#a-claude}

ネイティブの方法で、スキル、スラッシュコマンド、マニフェストがまとめて入る。

```bash
/plugin marketplace add praneybehl/llm-wiki-plugin
/plugin install llm-wiki@llm-wiki
```

インストールすれば、プラグインはどのプロジェクトでも動く。インストールしただけでは、wiki をどこに置くかは決まらない。

以下のエージェントでは、`skills` CLI で同じスキルをインストールする。どれも同じコマンドで、`-a` の値だけが違う。`-g` を付けるとすべてのプロジェクトで使えるグローバルなインストールになり、外すと現在のプロジェクトだけにインストールする。スラッシュコマンドは Claude Code 専用で、ほかのエージェントでは自然言語でスキルを呼び出す。

### Codex (OpenAI) {#a-codex}

```bash
npx skills add praneybehl/llm-wiki-plugin -a codex -g
```

`/skills`、`$llm-wiki`、または自然言語で呼び出す。同梱のスクリプトも実行できる。

### Cursor {#a-cursor}

```bash
npx skills add praneybehl/llm-wiki-plugin -a cursor -g
```

`/llm-wiki` または自然言語で呼び出す。同梱のスクリプトも実行できる。

### Gemini CLI {#a-gemini}

```bash
npx skills add praneybehl/llm-wiki-plugin -a gemini-cli -g
```

`/skills` の管理コマンドまたは自然言語で呼び出す。同梱のスクリプトも実行できる。

### OpenCode {#a-opencode}

```bash
npx skills add praneybehl/llm-wiki-plugin -a opencode -g
```

自然言語で依頼すると、エージェントがネイティブの `skill` ツールを通じてスキルを呼び出す。OpenCode は `.claude/skills/` と `~/.claude/skills/` も読むので、Claude Code 向けにインストールしたものもそのまま使える。同梱のスクリプトも実行できる。

### OpenClaw {#a-openclaw}

```bash
npx skills add praneybehl/llm-wiki-plugin -a openclaw -g
```

スキルは自動的にユーザーコマンドとして公開される。同梱のスクリプトも実行できる。

### Pi Agent {#a-pi}

```bash
npx skills add praneybehl/llm-wiki-plugin -a pi -g
```

`/skill:llm-wiki` または自然言語で呼び出す。同梱のスクリプトも実行できる。

### OMP ("Oh My Pi") {#a-omp}

OMP は、管理対象のスキルとユーザーのスキルを `~/.omp/agent/skills/` から読み込み、`skill://` で自動的に公開する。OMP はまだ `npx skills` のレジストリに載っていないので、リポジトリをクローンし、スキルへのシンボリックリンクを張ってインストールする。

```bash
git clone https://github.com/praneybehl/llm-wiki-plugin.git
mkdir -p ~/.omp/agent/skills
ln -s "$(pwd)/llm-wiki-plugin/skills/llm-wiki" ~/.omp/agent/skills/llm-wiki
```

### その他のランタイム: クローンしてシンボリックリンクを張る {#a-other}

Hermes Agent をはじめ、まだ `npx skills` のレジストリに載っていない agentskills.io 互換のランタイムでは、エージェントのスキル用ディレクトリにシンボリックリンクを張ればスキルを使える。

```bash
git clone https://github.com/praneybehl/llm-wiki-plugin.git
mkdir -p ~/.hermes/skills
ln -s "$(pwd)/llm-wiki-plugin/skills/llm-wiki" ~/.hermes/skills/llm-wiki
```

wiki そのものは、特定のエージェントに依存しない。中身は markdown ファイルを置いたディレクトリにすぎない。あるエージェントで取り込み、別のエージェントでクエリすることもできる。

## wiki の置き場所を決める {#choose-the-wiki-location}

スキルのスコープと wiki のスコープは、互いに独立している。

- **個人用のグローバルな wiki:** `~/wiki/` のような、ユーザーレベルの固定パスに置く1つのナレッジベース。raw ソースは `~/wiki/raw/` に置く。このパスをエージェントのグローバルな指示に追加する。どのプロジェクトからでも、残しておくべき知見やソースをこの wiki に追加できる。
- **プロジェクトの wiki:** `wiki/` と `raw/` を1つのプロジェクトの中に置く。そのプロジェクトのエージェントのメモリファイルに、相対パスで場所を書いておく。知識をプロジェクトごとに分けておけ、リポジトリと一緒にバージョン管理しやすい。

- **ホスト型 wiki:** 自分のアカウントの Cloudflare Worker 上に置く1つの wiki。どのマシンのエージェントからでも MCP で使う。多くのエージェントやマシンで1つの wiki を共有する必要がある場合に選ぶ。[ホスト型 wiki を使う](#use-a-hosted-wiki)を参照。

`-g` でスキルをグローバルにインストールしても、スキルがどこでも使えるようになるだけで、グローバルな wiki はできない。同様に、グローバルな wiki があっても、プロジェクトを自動で巡回して取り込むことはない。エージェントがプロジェクトの資料を取り込むのは、依頼されたとき、またはグローバルな指示で残しておくべき知見の保存が求められているときである。

## 最初の wiki を作る {#your-first-wiki}

以下のコマンドは、Claude Code の `/wiki:*` スラッシュコマンドの書き方を使っている。ほかのエージェントでは、同じ依頼を自然言語で伝える。

1. **構造を初期化する。** プロジェクトをまたいで共有する個人用の wiki 1つにする場合:

   ```bash
   /wiki:init --global
   ```

   現在のプロジェクトだけで使う wiki にする場合は、`--global` を付けずに `/wiki:init` を実行する。

   > **期待される動作**: wiki の構造を作り、FastEmbed 0.8.0、sqlite-vec 0.1.9、PyYAML 6.0.3 をインストールする。ローカルの `BAAI/bge-small-en-v1.5` モデルがなければダウンロードし、パースキャッシュを作り、現在あるすべてのセクションをベクトル化する。セットアップが完了したといえるのは、実行環境の JSON が `"status": "ready"` を返したときだけである。そのあと、エージェントがスキーマの内容を一緒に確認し、エージェントのメモリとの連携を提案する。承認なしにメモリに書き込むことはない。

   > 以前のバージョンの **wiki がすでにある**場合は、代わりに [`/wiki:upgrade`](/upgrade) を実行する。既存のページには手を付けずに、同じ必須の実行環境のセットアップと、コーパス全体の差分同期を行う。

2. **最初のソースを追加する。** テキストを含むファイルなら何でも、設定した raw ルート（グローバル構成なら `~/wiki/raw/`）に置き、次を実行する。

   ```bash
   /wiki:ingest ~/wiki/raw/your-source.pdf
   ```

   > **期待される動作**: エージェントは設定された wiki を特定し、ソースを読み（大きい場合は分割して読む）、要点をユーザーと話し合い、ソースの要約を書く。関係するエンティティとコンセプトのページは必要な箇所だけを更新し、新しいページを作り（それぞれに少なくとも1つの被リンクを付ける）、インデックスを更新し、`log.md` に1行追記する。

3. **質問する。** ソースがいくつか入ったら:

   ```bash
   /wiki:query What does my wiki say about X?
   ```

   > **期待される動作**: エージェントはインデックスを読んで候補のページを見つけ、それらを読み、`[[wikilink]]` の出典つきで回答をまとめる。内容のある回答は、調べた成果が蓄積するように `wiki/synthesis/` に保存することを提案する。

4. 定期的に**健全性をチェックする**。

   ```bash
   /wiki:lint
   ```

   > **期待される動作**: 孤立ページ、リンク切れの wikilink、大きすぎるページ、frontmatter の欠落、古くなった主張を検出する。検出結果は編集案として示され、wiki が黙って書き換えられることはない。

::: info
スラッシュコマンドを使わなくてもよい。自然言語で依頼しても、エージェントは同じスキルを呼び出す。コマンドは、明示的に呼び出したいときのためにある。[コマンドリファレンス](/commands)を参照。
:::

## ホスト型 wiki を使う {#use-a-hosted-wiki}

ホスト型 wiki はオプションである。この場合も、各エージェントには上の手順でスキルをインストールする。そのうえで次の手順を行う。

1. [ホスト型 wiki](/hosted) のページにある **Deploy to Cloudflare** ボタンでサーバーをデプロイする。Cloudflare に求められたら `ADMIN_TOKEN` を入力する。
2. `https://<your-worker>/setup` を開き、`ADMIN_TOKEN` でサインインし、wiki `main` を作り、エージェントごとにトークンを1つずつ作る。
3. 各エージェントで接続する。

   ```bash
   /wiki:init --hosted https://<your-worker>
   ```

   > **期待される動作**: エージェントは、トークンと OAuth のどちらを使うかを聞く。トークンの場合は、URL とトークンを保存し、接続を確認し、クライアント用の MCP の設定を示す。OAuth の場合は、`/mcp` の URL をクライアントに追加し、ユーザーがクライアントからサインインしたあと、エージェントが `wiki_overview` を呼び出す。そのあと、メモリに書く記述を提案する。承認なしに書き込むことはない。

4. 既存のローカルの wiki を移すには、`/wiki:migrate` を実行する。アップロードし、アップロードしたすべてのファイルを検証し、スキップしたファイルを報告する。ローカルのフォルダーを読み取り専用のミラーとして残すこともできる。

以降、`/wiki:ingest`、`/wiki:query`、`/wiki:lint` はこれまでと同じように使える。エージェントはローカルのファイルの代わりに MCP ツールを使う。

## オプションの学習ワークフロー {#optional-learning-workflow}

検証済みのタスクのあとで `/wiki:learn`（または「このタスクから学んで」）を使うと、教訓を残せる。テスト済みのスキルの改善を作るには `/wiki:evolve` を使う。初期化すると、経験とパターンのテンプレートと、アーカイブについての説明がインストールされる。既存の wiki には `/wiki:upgrade` で追加される。モデルの評価が自動で始まることはない。[学習とスキルの改善](/evolution)を参照。
