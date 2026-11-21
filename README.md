# info-hive

AI エージェントが、日本語の PDF、Web 記事、文字起こし、メモを Markdown の wiki に整理します。あとから質問すると、wiki の内容をもとに、出典つきで答えます。得られた知識は、次の作業でも使えるように保存できます。

Claude Code と Codex では `wiki@info-hive` プラグインとして、ほかの対応エージェントでは `info-hive` スキルとして使います。スキルは [Agent Skills](https://agentskills.io) 形式です。

## インストール

最初に [`uv`](https://docs.astral.sh/uv/getting-started/installation/) をインストールする。同梱のスクリプト用に、必要な Python と依存パッケージを `uv` が用意する。初回は検索用モデルも取得するため、数分かかることがある。

### Claude Code

Claude Code のチャットで次を実行する。スキル、MCP サーバー、`/wiki:*` コマンドがまとめて入る。

```text
/plugin marketplace add to884/info-hive-marketplace
/plugin install wiki@info-hive
```

### Codex

ターミナルで次を実行する（Codex CLI 0.166.0 で確認）。スキルと MCP サーバーをまとめて導入できる。

```bash
codex plugin marketplace add to884/info-hive-marketplace
codex plugin add wiki@info-hive
```

導入後は新しいチャットを開き、`$wiki:info-hive <操作> <引数>` の形で呼び出す。MCP は自動登録され、Claude CLI は不要である。操作の一覧は `$wiki:info-hive help`、個別の使い方は `$wiki:info-hive help query` で確認できる。

スキルだけを使う場合は `npx skills add to884/info-hive-wiki -a codex -g` で導入する。この場合の呼び出し名は `$info-hive` で、MCP の自動登録とプラグインの操作手順は含まれない。詳しくは[エージェント別の導入方法](docs/agents.md#インストールのコマンド例)を参照。

### その他のエージェント

Cursor、Gemini CLI、OpenCode、OpenClaw、Pi には、Node.js と [`skills` CLI](https://github.com/vercel-labs/skills) を使ってスキルを導入する。次は Cursor の例である。

```bash
npx skills add to884/info-hive-wiki -a cursor -g
```

`-a` の値は[エージェント対応表](docs/agents.md#対応表)に合わせて変える。`-g` を外すと、現在のプロジェクトだけにインストールする。OMP と Hermes Agent の手動での導入方法も、同じ文書にある。

スキル単体の導入では、MCP は自動登録されない。同梱の CLI を使い、「この資料を wiki に追加して」のように自然言語で依頼する。

## クイックスタート

プロジェクトをまたいで使える wiki を作ります。まず `init` を実行し、保存先を確認して初期化します。最初の資料を `~/info-hive/raw/your-source.pdf` に置いたら、`ingest` で取り込み、`query` で質問します。例のパスと質問は、使いたい資料と質問に置き換えてください。

### Claude Code

```text
/wiki:init --user
/wiki:ingest ~/info-hive/raw/your-source.pdf
/wiki:query この資料の要点は？
```

### Codex

```text
$wiki:info-hive init --user
$wiki:info-hive ingest ~/info-hive/raw/your-source.pdf
$wiki:info-hive query この資料の要点は？
```

プロジェクト専用の wiki を作る場合は、`init` の `--user` を外します。既定では、そのプロジェクトの `wiki/` と `raw/` を使います。プラグインやスキルを入れる範囲と、wiki の保存範囲は別々に選べます。

定期的な点検には、Claude Code では `/wiki:lint`、Codex では `$wiki:info-hive lint` を使います。検出した問題は修正案として示し、黙って wiki を書き換えることはありません。

## できること

- 資料を要約し、関連するページ、リンク、インデックスを更新します。
- 日本語の語彙検索と意味検索を組み合わせ、出典つきで質問に答えます。
- リンク切れ、孤立ページ、frontmatter の欠落、大きすぎるページを点検します。
- ページ間の関係をグラフで調べ、wiki をブラウザーで閲覧します。
- `/wiki:overview <ページ>` で、図を中心にした概要を本文と別に保存し、`/wiki:browse` で表示します。元のページが更新されると、警告を表示して概要を折りたたみます。
- 検証済みのタスクの経験を記録し、スキルの変更を提案・評価します。

wiki の正本は手元の Markdown です。同じ wiki を複数のエージェントで使えます。検索の埋め込みと索引もローカルで処理し、外部のベクトルデータベースや埋め込み API は使いません。

Claude Code では、任意の [rag@info-hive](https://github.com/to884/info-hive-rag) を有効にすると、PDF の抽出・OCR・索引化を永続ジョブとして処理できます。`queued` は、依頼を受け付けた段階です。この時点では、PDF の索引化は完了していません。rag がなくても PDF は取り込めます。rag の Codex 対応は含みません。

Claude Code と Codex では、`ingest session` で実行中の会話を取り込めます。Codex の呼び出し方は `$wiki:info-hive ingest session`、指定した会話を取り込む場合は `$wiki:info-hive ingest session:<ID>` です。保存する範囲と伏せ字を確認してから、会話を wiki に取り込みます。

スキルの改善は任意のワークフローです。移植元の調査では品質の向上は示されていません。[仕組みと検証の限界](docs/evolution.md#研究上の根拠と取り入れ方)で、調査結果の詳細を確認できます。

## ドキュメント

手元の [docs/html/index.html](docs/html/index.html) をブラウザーで開くと、図解と目次から使い方を参照できます。

| 文書 | 内容 |
| --- | --- |
| [エージェント](docs/agents.md) | 対応表、導入方法、エージェントのメモリとの連携 |
| [コマンド](docs/commands.md) | 操作の一覧、引数、使用例 |
| [ワークフロー](docs/workflows.md) | 取り込み、質問、点検の手順 |
| [検索と取得](docs/search.md) | ローカル検索、セクション単位の取得、キャッシュ |
| [グラフ層](docs/graph.md) | 型付きの関係、根拠、問い合わせ |
| [経験の記録とスキルの改善](docs/evolution.md) | learn と evolve、評価結果と限界 |
| [アップグレード](docs/upgrade.md) | 既存の wiki の更新と移行 |
| [MCP サーバー](skills/info-hive/references/mcp-server.md) | 実行環境、モデル、社内ネットワークでの証明書の設定、起動時の障害対処 |

## 移植元とクレジット

Praney Behl の [llm-wiki-plugin](https://github.com/praneybehl/llm-wiki-plugin) v4.0.1（コミット `2d24ccf`、MIT ライセンス）を日本語向けに移植しました。起点と取り込んだ修正は [UPSTREAM.md](UPSTREAM.md) に記録しています。移植元のホスト型 wiki は含めていません。

LLM Wiki のパターンは Andrej Karpathy の [llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) にもとづきます。移植元の検索は Cerebras の [How We Built Our Knowledge Base](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base) を参考にし、ローカル検索への移行時には [Basic Memory](https://github.com/basicmachines-co/basic-memory) と比較しています。スキルの改善のワークフローは [WikiSkill](https://arxiv.org/html/2608.27454v1) を応用しています。

## ライセンス

MIT。[LICENSE](LICENSE) に、移植元の Praney Behl と to884 の著作権表示・許諾文を記載しています。

同梱するライブラリと素材の入手元、バージョン、ライセンスは次の文書で確認できます。

- Markdown 解析器の markdown-it-py と mdurl: [_vendor/README.md](skills/info-hive/scripts/_vendor/README.md)、[ライセンス](skills/info-hive/scripts/_vendor/licenses/)
- 閲覧画面の Fluent 2 の CSS、アイコン、Mermaid とその依存: [browse_assets/README.md](skills/info-hive/scripts/browse_assets/README.md)、[ライセンスと第三者の表示](skills/info-hive/scripts/browse_assets/licenses/)

info-hive は Microsoft の製品ではなく、Microsoft の提携、推薦、承認を受けたものでもありません。Fluent、Microsoft、Segoe は Microsoft Corporation の商標です。書体は同梱しません。

## 開発への参加

不具合や要望は[このリポジトリの Issues](https://github.com/to884/info-hive-wiki/issues) に報告してほしい。PR の出し方は [CONTRIBUTING.md](CONTRIBUTING.md) を参照。

設計書、テスト、開発の運用は、開発用リポジトリ [info-hive-dev](https://github.com/to884/info-hive-dev) で管理する。
