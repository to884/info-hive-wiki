<!-- Adapted from: docs/agents.html (source: README.md agent table, skills/info-hive/references/agent-memory-integration.md). -->

# エージェント

`info-hive` スキルは標準の [agentskills.io](https://agentskills.io) 形式なので、以下のどのエージェントでも動く。wiki の操作は、インストールしたスキルを通じて使える。対象は、init と実行環境のセットアップ、ハイブリッド検索または語彙ベースの検索、構造の lint、統計、グラフの lint、グラフの抽出、グラフのクエリである。依存パッケージを必要とするスクリプトは、`uv run --script` を通じて、バージョンを固定した PEP 723 の環境で動く。

## 対応表

| エージェント | `--agent` の値 | 呼び出し方 | スクリプトの実行 |
| --- | --- | --- | :---: |
| Claude Code | `claude-code` | `/wiki:*` スラッシュコマンド（同梱）または自然言語 | ✅ |
| Codex (OpenAI) | `codex` | `/skills` または `$info-hive` / 自然言語 | ✅ |
| Cursor | `cursor` | `/info-hive` または自然言語 | ✅ |
| Gemini CLI | `gemini-cli` | `/skills` の管理コマンド / 自然言語 | ✅ |
| OpenCode | `opencode` | 自然言語（エージェントがネイティブの `skill` ツールを呼び出す） | ✅ |
| OpenClaw | `openclaw` | ユーザーコマンドとして自動で公開される | ✅ |
| Pi Agent | `pi` | `/skill:info-hive` または自然言語 | ✅ |
| OMP ("Oh My Pi") | 手動（下記参照） | 自然言語（スキルは `skill://` で自動的に公開される） | ✅ |
| Hermes Agent | 手動（下記参照） | 自然言語 | ✅ |

OpenCode は `.claude/skills/` と `~/.claude/skills/` も読むので、Claude Code 向けにインストールしてあれば、改めてインストールしなくても使える。

## インストールのコマンド例
最初に [`uv`](https://docs.astral.sh/uv/getting-started/installation/) をインストールする。どのエージェントも、バージョンを固定した同じローカルの実行環境を呼び出すので、エージェントごとの Python 環境は要らない。


**Claude Code** には、プラグイン一式（スキルとスラッシュコマンド）が入る。マーケットプレイスは [to884/info-hive-marketplace](https://github.com/to884/info-hive-marketplace) にある。

```bash
/plugin marketplace add to884/info-hive-marketplace
/plugin install wiki@info-hive
```

**Codex、Cursor、Gemini CLI、OpenCode、OpenClaw、Pi** では、`skills` CLI でスキルをインストールする。対応表の `--agent` の値に置き換えて使う。

```bash
npx skills add to884/info-hive-wiki -a codex -g
npx skills add to884/info-hive-wiki -a cursor -g
npx skills add to884/info-hive-wiki -a gemini-cli -g
npx skills add to884/info-hive-wiki -a opencode -g
npx skills add to884/info-hive-wiki -a openclaw -g
npx skills add to884/info-hive-wiki -a pi -g
```

現在のプロジェクトだけにインストールするには、`-g` を外す。

**OMP ("Oh My Pi")** と **Hermes Agent** は、まだ `npx skills` のレジストリに載っていない。リポジトリをクローンし、エージェントのスキル用ディレクトリにスキルへのシンボリックリンクを張る。OMP は、管理対象のスキルとユーザーのスキルを `~/.omp/agent/skills/` から読み込み、`skill://` で公開する。

```bash
git clone https://github.com/to884/info-hive-wiki.git
mkdir -p ~/.omp/agent/skills ~/.hermes/skills
ln -s "$(pwd)/info-hive-wiki/skills/info-hive" ~/.omp/agent/skills/info-hive
ln -s "$(pwd)/info-hive-wiki/skills/info-hive" ~/.hermes/skills/info-hive
```

## エージェントのメモリファイル

wiki を作ったら、エージェントがその場所を覚えておけるように、短い記述を追加する。プロジェクトをまたいで使う個人用の wiki なら、`~/info-hive/wiki/` のようなユーザーレベルの固定パスをエージェントのグローバルな指示に書く。プロジェクト専用の wiki なら、相対パスをプロジェクトのメモリファイルに書く。ファイル名はエージェントによって違う。

| エージェント | メモリファイル |
| --- | --- |
| Claude Code | `CLAUDE.md` |
| Codex (OpenAI) | `AGENTS.md` |
| Cursor | `AGENTS.md`（または `.cursor/rules/*.mdc`） |
| OpenCode | `AGENTS.md`（`CLAUDE.md` も読む） |
| Gemini CLI | `GEMINI.md` |
| Pi Agent | `AGENTS.md` |
| OpenClaw | `AGENTS.md` |
| OMP (Oh My Pi) | `AGENTS.md` |

複数のエージェントを使う場合は、対応しているエージェントでは `AGENTS.md` を正本にし、`CLAUDE.md` をそこへのシンボリックリンクにするのがよい。スキルが、ユーザーの承認なしにメモリファイルに書き込むことはない。グローバル用とプロジェクト用の記述の例は、`skills/info-hive/references/agent-memory-integration.md` にある。

> スラッシュコマンドは Claude Code 専用である。ほかのエージェントでは、「この論文を wiki に追加して」「X について wiki には何と書いてある？」「wiki に lint をかけて」のように、自然言語でスキルを呼び出す。wiki そのものは特定のエージェントに依存しないので、あるエージェントで取り込み、別のエージェントでクエリすることもできる。

## 経験の記録と手順の改善

一覧にあるスキル対応のエージェントはどれも、インストールされた `references/evolution-workflow.md` と `scripts/wiki_evolve.py` を使い、自然言語で `/wiki:learn` と `/wiki:evolve` のワークフローを実行できる。スラッシュコマンドそのものがあるのは Claude Code だけである。記録とライフサイクル管理のツールは、Python の標準ライブラリだけを使う。共通の評価用アダプターは、9つのホストすべてに対応している。ただし、ホストごとに、インストールされているバージョン、認証、使えるツール、報告される使用量の集計が異なる。改善ワークフローの実行要件と、実地検証のレポートを参照。教訓を記録するために、新しくエージェントとの連携を作る必要はない。[学習とスキルの改善](./evolution.md)を参照。
