<!-- Adapted from: docs/agents.html (source: README.md agent table, skills/info-hive/references/agent-memory-integration.md). -->

# エージェント

`info-hive` は [agentskills.io](https://agentskills.io) 形式のスキルです。下の表にあるエージェントで使えます。スキルで使える操作は、wiki と実行環境の初期化、ハイブリッド検索または語彙検索、構造の点検（lint）、統計、グラフの点検・抽出・問い合わせです。依存パッケージが必要なスクリプトは、`uv run --script` で実行します。パッケージのバージョンは PEP 723 の形式で固定しています。

## 対応表

| エージェント | `--agent` の値 | 呼び出し方 | スクリプトの実行 |
| --- | --- | --- | :---: |
| Claude Code | `claude-code` | `/wiki:*` スラッシュコマンド（同梱）または自然言語 | ✅ |
| Codex (OpenAI) | `codex`（スキルだけの場合） | プラグインは `$wiki:info-hive`、スキルだけなら `$info-hive` / 自然言語 | ✅ |
| Cursor | `cursor` | `/info-hive` または自然言語 | ✅ |
| Gemini CLI | `gemini-cli` | `/skills` の管理コマンド / 自然言語 | ✅ |
| OpenCode | `opencode` | 自然言語（エージェントがネイティブの `skill` ツールを呼び出す） | ✅ |
| OpenClaw | `openclaw` | ユーザーコマンドとして自動で公開される | ✅ |
| Pi Agent | `pi` | `/skill:info-hive` または自然言語 | ✅ |
| OMP ("Oh My Pi") | 手動（下記参照） | 自然言語（スキルは `skill://` で自動的に公開される） | ✅ |
| Hermes Agent | 手動（下記参照） | 自然言語 | ✅ |

OpenCode は `.claude/skills/` と `~/.claude/skills/` も読むので、Claude Code 向けにインストールしてあれば、改めてインストールしなくても使えます。

## LLM モデルと用途

資料の要約、関連する wiki ページの更新、質問への回答は、Claude Code や Codex などで現在選んでいる会話モデルが担当します。
操作によっては、一部の作業を別のモデルに任せたり、用途に合う設定を利用者に案内したりします。

| 操作 | モデルと設定 | モデルが担当する作業 |
| --- | --- | --- |
| Web ページの取り込み | Claude Code では `wiki:web-raw` の既定値 `sonnet`（Claude Sonnet）。Codex や、このエージェントを使えない環境では現在の会話モデル | 取得した本文を必要に応じて日本語に訳し、画像の説明を加えて原資料（raw）を作ります。検査後の要約と wiki の更新は、取り込みを始めた会話モデルが担当します。 |
| Claude Code のフックが見つけた候補ページの読み取り | R0 は現在の会話モデル。R1・R2 は `wiki:wiki-reader` が `sonnet` と推論エフォート `low` を使用。R3 は同じモデルで `medium` | 候補が 1 件のときや委任が無効なときは会話モデルが読みます。複数の候補から根拠を拾うときは読み取り専用のエージェントに任せます。ページ間の比較、矛盾の確認、設計判断の根拠が必要なときは、会話モデルが理由を示して R3 を選べます。 |
| YouTube 字幕の取り込み | Claude Code では Claude Opus 5.5（`medium`）、Codex では GPT-6.2 Sol（`high`）を推奨 | スクリプトが保存した字幕を読み、要約を書きます。自動字幕に聞き違いがあれば、訂正案や不確かな点を要約に記します。推奨設定への切り替えは利用者がホスト側で行います。 |
| `/wiki:evolve` によるスキルの評価と改善 | 利用者が評価設定で役割ごとにモデル ID を指定 | タスクの実行、経験の整理、変更案の作成、回答の採点を分担します。[評価の設定と実行手順](evolution.md) |

表の `sonnet` は Claude Code のモデル指定名です。候補ページの読み取りでエフォートを選ぶ際に評価したモデルは Claude Sonnet 5.5（`claude-sonnet-5-5`）です。

意味検索には、質問と wiki の本文をベクトルに変換する埋め込みモデルを使います。
MCP サーバーは `cl-nagoya/ruri-v3-130m`、CLI のハイブリッド検索は既定で `intfloat/multilingual-e5-small` を使います。[検索と取得](search.md)

任意の判定レイヤーを有効にすると、専用モデルが検索結果の関連度やグラフの関係を裏づける証拠の有無などを判定します。
既定では無効です。ローカルで使う場合のモデルは Liquid `d1-2B` です。[判定モデルとバックエンドの一覧](decisions.md)

## 事前準備

エージェントがコマンドを実行する環境に、Git と [uv](https://docs.astral.sh/uv/getting-started/installation/)、Python 3.12 以上を用意します。初期化は `python` を直接呼び出すので、次のコマンドで確認します。

```text
git --version
uv --version
python --version
```

uv 経由の同梱スクリプトには Python 3.12〜3.14 を使います。この範囲の Python が見つからなければ、uv が取得します。依存パッケージも uv が用意します。Windows で Claude デスクトップアプリの Code タブを使う場合は、[HTML マニュアルの目次](html/index.html)から「Code タブに wiki を入れる」を開いてください。

## インストールのコマンド例

### Claude Code CLI

Claude Code には、スキル、MCP サーバー、スラッシュコマンドが入ります。マーケットプレイスは [to884/info-hive-marketplace](https://github.com/to884/info-hive-marketplace) にあります。

```bash
/plugin marketplace add to884/info-hive-marketplace
/plugin install wiki@info-hive
```

#### プラグインを HTTPS で取得する

Claude Code が SSH で GitHub に接続できない場合は、環境変数 `CLAUDE_CODE_PLUGIN_PREFER_HTTPS` を `1` に設定して起動します。公開リポジトリを HTTPS で取得する場合、GitHub の認証情報は不要です。

PowerShell では次を実行します。

```powershell
$env:CLAUDE_CODE_PLUGIN_PREFER_HTTPS = '1'
claude
```

macOS・Linux のターミナルでは次を実行します。

```bash
CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1 claude
```

起動した Claude Code のチャットで、マーケットプレイスの追加とプラグインのインストールを実行します。マーケットプレイスがすでに追加されていれば、プラグインのインストールから再試行できます。

### Codex CLI

事前準備を済ませ、Codex CLI が使えるターミナルで次を実行します。スキルと MCP サーバーをまとめて導入できます（Codex CLI 0.166.0 で確認）。

```bash
codex plugin marketplace add to884/info-hive-marketplace
codex plugin add wiki@info-hive
```

導入後は新しい Codex のチャットで `$wiki:info-hive init`、`$wiki:info-hive ingest <ソース>`、`$wiki:info-hive query <質問>` の形で使います。`$wiki:info-hive help` で操作の一覧を表示し、`$wiki:info-hive help query` で個別のヘルプを表示します。MCP は自動登録されます。初回の依存とモデルの取得には数分かかることがあります。社内ネットワークの証明書の設定は [MCP サーバー](../skills/info-hive/references/mcp-server.md)を参照してください。

Claude Code の `/wiki:*` は Codex のスラッシュコマンドにはなりません。Web の raw は Codex 自身が作り、Claude CLI は不要です。`$wiki:info-hive ingest session` で実行中の Codex の会話を、`$wiki:info-hive ingest session:<ID>` で指定した会話を取り込めます。従来の JSONL とデータベースの履歴の両方を、Codex の読み取り API で取得します。取り込みを依頼したターンを除外し、伏せ字と保存前の確認を行います。rag の Codex 対応は含みません。

開発用の `info-hive-dev` のチェックアウトを試すときは、親リポジトリのルートで次を実行します。ローカルのカタログは、チェックアウトした `info-hive-wiki` を指します。

```bash
codex plugin marketplace add .
codex plugin add wiki@info-hive-dev
```

### Codex のスキル単体とその他のエージェント

**Codex のスキルだけを使う場合、および Cursor、Gemini CLI、OpenCode、OpenClaw、Pi** では、Node.js と `skills` CLI を使ってスキルをインストールします。対応表の `--agent` の値に置き換えて使います。スキルだけの場合は、MCP の自動登録とプラグインの `commands/` は含まれません。同梱の CLI とリファレンスを使い、Codex の呼び出し名は `$info-hive` になります。

```bash
npx skills add to884/info-hive-wiki -a codex -g
npx skills add to884/info-hive-wiki -a cursor -g
npx skills add to884/info-hive-wiki -a gemini-cli -g
npx skills add to884/info-hive-wiki -a opencode -g
npx skills add to884/info-hive-wiki -a openclaw -g
npx skills add to884/info-hive-wiki -a pi -g
```

現在のプロジェクトだけにインストールするには、`-g` を外します。

**OMP ("Oh My Pi")** と **Hermes Agent** は、まだ `npx skills` のレジストリに載っていません。リポジトリをクローンし、エージェントのスキル用ディレクトリにスキルへのシンボリックリンクを張ります。OMP は、管理対象のスキルとユーザーのスキルを `~/.omp/agent/skills/` から読み込み、`skill://` で公開します。

```bash
git clone https://github.com/to884/info-hive-wiki.git
mkdir -p ~/.omp/agent/skills ~/.hermes/skills
ln -s "$(pwd)/info-hive-wiki/skills/info-hive" ~/.omp/agent/skills/info-hive
ln -s "$(pwd)/info-hive-wiki/skills/info-hive" ~/.hermes/skills/info-hive
```

## エージェントのメモリファイル

wiki を作ったら、エージェントがその場所を覚えておけるように、短い記述を追加します。プロジェクトをまたいで使うユーザースコープの wiki なら、`~/info-hive/wiki/` のようなユーザーレベルの固定パスをエージェントのグローバルな指示に書きます。プロジェクト専用の wiki なら、相対パスをプロジェクトのメモリファイルに書きます。ファイル名はエージェントによって違います。

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

複数のエージェントを使う場合は、対応しているエージェントでは `AGENTS.md` を正本にし、`CLAUDE.md` はそのシンボリックリンクにできます。スキルが、ユーザーの承認なしにメモリファイルに書き込むことはありません。ユーザースコープ用とプロジェクト用の記述の例は、`skills/info-hive/references/agent-memory-integration.md` にあります。

> スラッシュコマンドは Claude Code 専用です。ほかのエージェントでは、「この論文を wiki に追加して」「X について wiki には何と書いてある？」「wiki に lint をかけて」のように、自然言語でスキルを呼び出します。wiki そのものは特定のエージェントに依存しないので、あるエージェントで取り込み、別のエージェントでクエリすることもできます。

## 経験の記録と手順の改善

一覧にあるスキル対応のエージェントはどれも、インストールされた `references/evolution-workflow.md` と `scripts/wiki_evolve.py` を使い、自然言語で `/wiki:learn` と `/wiki:evolve` のワークフローを実行できます。スラッシュコマンドそのものがあるのは Claude Code だけです。記録とライフサイクル管理のツールは、Python の標準ライブラリだけを使います。共通の評価用アダプターは、9つのホストすべてに対応しています。ただし、ホストごとに、インストールされているバージョン、認証、使えるツール、報告される使用量の集計が異なります。教訓を記録するために、新しくエージェントとの連携を作る必要はありません。[経験の記録とスキルの改善](./evolution.md)で、実行要件と実地検証の結果を確認できます。
