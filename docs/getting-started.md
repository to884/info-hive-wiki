<!-- Adapted from: docs/getting-started.html (source: README.md "Installation"/"Quick start", commands/init.md, commands/ingest.md). -->

# はじめに

使っているコーディングエージェント向けにスキルをインストールし、初期化、取り込み、クエリ、lint の4ステップを繰り返す。

## インストール

Claude Code と Codex には、スキルと MCP サーバーを含む `wiki@info-hive` プラグインをインストールできる。ほかの対応エージェントでは、[Agent Skills](https://agentskills.io) 形式のスキルを使う。

エージェントが使う環境で Git、uv、Python のコマンドが実行できることを確認する。初期化は `python` を直接呼ぶので、`python --version` で Python 3.12 以上と表示されることを確かめる。[事前準備](./agents.md#事前準備)

Web ページの取得に普段のブラウザーを使う場合は、[Playwright の準備](#url-取り込みで使う-playwright-を準備する)も行う。

### Claude Code のプラグイン

Claude Code のチャットで実行する。スキル、MCP サーバー、`/wiki:*` コマンドが入る。

```text
/plugin marketplace add to884/info-hive-marketplace
/plugin install wiki@info-hive
```

マーケットプレイスとプラグイン本体のリポジトリは公開されている。HTTPS での取得には GitHub の認証情報は不要。Claude Code が SSH での取得に失敗する場合は、[プラグインを HTTPS で取得する手順](./agents.md#プラグインを-https-で取得する)に進む。

インストールすれば、プラグインはどのプロジェクトでも動く。インストールしただけでは、wiki をどこに置くかは決まらない。

### Codex のプラグイン

Codex CLI が使えるターミナルで次を実行する。

```bash
codex plugin marketplace add to884/info-hive-marketplace
codex plugin add wiki@info-hive
```

MCP は自動登録される。導入後は新しい Codex のチャットを開き、`$wiki:info-hive help` で操作の一覧を表示する。

### スキル単体

以下のエージェントでは、Node.js と [`skills` CLI](https://github.com/vercel-labs/skills) を使ってスキルをインストールする。スキル単体で使う場合は同梱の CLI を利用できる。どれも同じコマンドで、`-a` の値だけが違う。`-g` を付けるとすべてのプロジェクトで使えるグローバルなインストールになり、外すと現在のプロジェクトだけにインストールする。スラッシュコマンドは Claude Code 専用で、ほかのエージェントでは自然言語でスキルを呼び出す。

#### Codex

```bash
npx skills add to884/info-hive-wiki -a codex -g
```

`/skills`、`$info-hive`、または自然言語で呼び出す。同梱のスクリプトも実行できる。

#### Cursor

```bash
npx skills add to884/info-hive-wiki -a cursor -g
```

`/info-hive` または自然言語で呼び出す。同梱のスクリプトも実行できる。

#### Gemini CLI

```bash
npx skills add to884/info-hive-wiki -a gemini-cli -g
```

`/skills` の管理コマンドまたは自然言語で呼び出す。同梱のスクリプトも実行できる。

#### OpenCode

```bash
npx skills add to884/info-hive-wiki -a opencode -g
```

自然言語で依頼すると、エージェントがネイティブの `skill` ツールを通じてスキルを呼び出す。OpenCode は `.claude/skills/` と `~/.claude/skills/` も読むので、Claude Code 向けにインストールしたものもそのまま使える。同梱のスクリプトも実行できる。

#### OpenClaw

```bash
npx skills add to884/info-hive-wiki -a openclaw -g
```

スキルは自動的にユーザーコマンドとして公開される。同梱のスクリプトも実行できる。

#### Pi Agent

```bash
npx skills add to884/info-hive-wiki -a pi -g
```

`/skill:info-hive` または自然言語で呼び出す。同梱のスクリプトも実行できる。

### OMP ("Oh My Pi")

OMP は、管理対象のスキルとユーザーのスキルを `~/.omp/agent/skills/` から読み込み、`skill://` で自動的に公開する。OMP はまだ `npx skills` のレジストリに載っていないので、リポジトリをクローンし、スキルへのシンボリックリンクを張ってインストールする。

```bash
git clone https://github.com/to884/info-hive-wiki.git
mkdir -p ~/.omp/agent/skills
ln -s "$(pwd)/info-hive-wiki/skills/info-hive" ~/.omp/agent/skills/info-hive
```

### その他のランタイム: クローンしてシンボリックリンクを張る

Hermes Agent をはじめ、まだ `npx skills` のレジストリに載っていない agentskills.io 互換のランタイムでは、エージェントのスキル用ディレクトリにシンボリックリンクを張ればスキルを使える。

```bash
git clone https://github.com/to884/info-hive-wiki.git
mkdir -p ~/.hermes/skills
ln -s "$(pwd)/info-hive-wiki/skills/info-hive" ~/.hermes/skills/info-hive
```

wiki そのものは、特定のエージェントに依存しない。中身は markdown ファイルを置いたディレクトリにすぎない。あるエージェントで取り込み、別のエージェントでクエリすることもできる。

## URL 取り込みで使う Playwright を準備する

通常は HTTP で本文を取得する。本文が短い場合や HTTP 401/403 が返った場合は Playwright に切り替える。ログインが必要なページでは、取り込みを依頼するときに「ログインが必要なので、普段のブラウザーで取り込んで」と伝える。

普段の Chrome または Edge を使う場合は、次の準備を行う。

1. [Node.js の公式ダウンロードページ](https://nodejs.org/en/download)から、20 以上の LTS 版を導入する。新しいターミナルで `node --version` と `npm --version` を実行し、両方の版が表示されることを確かめる。
2. 取り込みに使う Chrome または Edge のプロファイルで [Playwright Extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm) を開き、拡張機能を追加する。Edge で他のストアからの拡張機能について確認が表示されたら、許可して追加する。
3. Node.js を導入する前からエージェントを開いていた場合は、終了して起動し直す。URL の取り込みで Playwright の接続画面が出たら、タブを選ばずに「Allow」を押す。

Windows では既定の Chrome または Edge を使う。既定がほかのブラウザーの場合や Windows 以外では、取り込みを依頼するときに Chrome または Edge を指定する。

必要な Playwright のパッケージの取得と起動は、wiki の取り込みスクリプトが行う。初回は npm レジストリに接続する。

公開ページを独立したブラウザーで読みたい場合は、「独立した Chromium で取り込んで」と伝える。この方法では必要な Chromium も wiki が取得する。

## wiki の置き場所を決める

スキルのスコープと wiki のスコープは、互いに独立している。

- **ユーザースコープの wiki:** `~/info-hive/wiki/` のような、ユーザーレベルの固定パスに置く1つのナレッジベース。raw ソースは `~/info-hive/raw/` に置く。このパスをエージェントのグローバルな指示に追加する。どのプロジェクトからでも、残しておくべき知見やソースをこの wiki に追加できる。
- **プロジェクトの wiki:** `wiki/` と `raw/` を1つのプロジェクトの中に置く。そのプロジェクトのエージェントのメモリファイルに、相対パスで場所を書いておく。知識をプロジェクトごとに分けておけ、リポジトリと一緒にバージョン管理しやすい。

`-g` でスキルをグローバルにインストールしても、スキルがどこでも使えるようになるだけで、ユーザースコープの wiki はできない。同様に、ユーザースコープの wiki があっても、プロジェクトを自動で巡回して取り込むことはない。エージェントがプロジェクトの資料を取り込むのは、依頼されたとき、またはグローバルな指示で残しておくべき知見の保存が求められているときである。

## 最初の wiki を作る

以下は Claude Code の `/wiki:*` で進める例。Codex プラグインでは `$wiki:info-hive init --user` のように、操作名と引数を `$wiki:info-hive` に続ける。スキル単体では、同じ依頼を自然言語で伝える。

1. **構造を初期化する。** プロジェクトをまたいで共有するユーザースコープの wiki 1つにする場合:

   ```bash
   /wiki:init --user
   ```

   現在のプロジェクトだけで使う wiki にする場合は、`--user` を付けずに `/wiki:init` を実行する。

   > **期待される動作**: wiki の構造を作り、FastEmbed 0.8.0、sqlite-vec 0.1.9、PyYAML 6.0.3 をインストールする。ローカルの `intfloat/multilingual-e5-small` モデル（約 0.25 GB）がなければダウンロードし、パースキャッシュを作り、現在あるすべてのセクションをベクトル化する。セットアップが完了したといえるのは、実行環境の JSON が `"status": "ready"` を返したときだけである。そのあと、エージェントがスキーマの内容を一緒に確認し、エージェントのメモリとの連携を提案する。承認なしにメモリに書き込むことはない。

   > 以前のバージョンの **wiki がすでにある**場合は、代わりに [`/wiki:upgrade`](./upgrade.md) を実行する。既存のページには手を付けずに、同じ必須の実行環境のセットアップと、コーパス全体の差分同期を行う。

2. **最初のソースを追加する。** テキストを含むファイルなら何でも、設定した raw ルート（ユーザースコープの構成なら `~/info-hive/raw/`）に置き、次を実行する。

   ```bash
   /wiki:ingest ~/info-hive/raw/your-source.pdf
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

> スラッシュコマンドを使わなくてもよい。自然言語で依頼しても、エージェントは同じスキルを呼び出す。コマンドは、明示的に呼び出したいときのためにある。[コマンドリファレンス](./commands.md)を参照。

## オプションの学習ワークフロー

検証済みのタスクのあとで `/wiki:learn`（または「このタスクから学んで」）を使うと、教訓を残せる。テスト済みのスキルの改善を作るには `/wiki:evolve` を使う。初期化すると、経験とパターンのテンプレートと、アーカイブについての説明がインストールされる。既存の wiki には `/wiki:upgrade` で追加される。モデルの評価が自動で始まることはない。[学習とスキルの改善](./evolution.md)を参照。
