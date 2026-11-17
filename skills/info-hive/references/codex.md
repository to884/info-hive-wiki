# Codex での wiki の操作

スクリプトのパスの書き方は `SKILL.md` の「スクリプトのパス」にある。

## プラグイン一式がある場合

Codex のプラグインは `.codex-plugin/plugin.json` からスキルと MCP を読み込む。`commands/` の Markdown は Codex のスラッシュコマンドとしては登録されない。

`$wiki:info-hive <操作> <引数>` を受け取ったら、`uv run --quiet --script "<skill-root>/scripts/wiki_codex.py" <操作>` で共通の手順を読む。このスクリプトは引数の値を受け取らず、子プロセスも起動しない。表示された `<利用者の引数>` に当たる値は、依頼から読み取る。スクリプトや MCP に渡すときは、それぞれの引数として扱い、シェルの文字列にそのまま埋め込まない。

| 依頼 | 読む手順 | 主な処理 |
| --- | --- | --- |
| 初期化 | `init` | 保存先の確認、初期化、実行環境の準備、`AGENTS.md` との連携 |
| 資料の取り込み | `ingest` | raw、要約、関連ページ、索引とログ、グラフの更新 |
| 質問 | `query` | 索引を先に読む、MCP か CLI で検索、出典と確信度を示す |
| 点検 | `lint` | 構造とグラフの検査、内容の確認、修正案 |
| 統計 | `stats` | 規模、リンク、分割の目安 |
| 関係の問い合わせ | `graph` | 型付きの関係、経路、根拠 |
| 閲覧 | `browse` | ローカルの閲覧サーバーの起動・停止、静的 HTML |
| 経験を記録 | `learn` | 検証したタスクの経験 |
| 手順を改善 | `evolve` | 予算を決めた評価、提案、適用・ロールバック |
| 既存の wiki を更新 | `upgrade` | 既存の SCHEMA.md を保って差分を提案 |
| 使い方 | `help` | 一覧または `help query` などの個別のヘルプ |

自然言語の依頼でも、対応する操作の手順を先に読む。操作名が分からないときは `help` で一覧を見る。`<skill-root>` は、このファイルの一つ上の `SKILL.md` があるディレクトリである。`<plugin-root>` は、その二つ上にあるプラグインのルートである。

## Codex に合わせる点

- MCP のツール名は接続に表示された名前を使う。`wiki` には対象の wiki の絶対パスを渡す。結果が 0 件でも同じ検索を CLI でやり直さない。接続できない場合の CLI への戻り方は `SKILL.md` と共通の手順に従う。
- Read / Write / Edit / `str_replace` の指定は、Codex の読み取りと差分編集で行う。既存のページ全体を書き直さない。承認を得る指定や原本を保つ規則も同じである。
- Web の raw は `references/web-raw.md` の W1〜W8 を自分で行う。翻訳が必要なら日本語の raw を作り、W8 の JSON の `completed: true` を確認してから後段へ進む。原文の保存と日本語要約で代替せず、進められない場合は理由と未完了の状態を伝える。Claude CLI や Sonnet のエージェントを呼ばない。翻訳者と補足には、自分に示された実際のモデル名を書く。分からなければ推測しない。
- `ingest session` は、実行中の Codex の会話を自動で取り込む。対象の ID と範囲は下の「会話履歴の取り込み」のとおりに決める。利用者が明示した Claude Code の JSONL も、既存の確認と伏せ字の手順で扱える。
- メモリとの連携先は、プロジェクトなら `AGENTS.md`、ユーザー共通なら Codex のホームの `AGENTS.md`（既定は `~/.codex/AGENTS.md`）である。wiki の保存先と、プラグインの導入先は別である。
- `browse` は Codex のデスクトップアプリでブラウザのペインが使える場合は `--no-open` で起動して URL を開く。CLI では既定のブラウザを使う。終了するときは `browse --stop` を使い、ホストの終了に伴う停止だけを当てにしない。
- `rag@info-hive` の自動導入や Codex 対応を前提にしない。rag のツールがなければ、PDF は `references/ingest-workflow.md` の通常の PDF の手順で取り込む。

## 会話履歴の取り込み

`$wiki:info-hive ingest session` で実行中の会話、`$wiki:info-hive ingest session:<ID>` または `codex:<ID>` で指定したチャットを取り込む。`wiki_session.py` の S1〜S10 の確認、伏せ字、承認、raw の書き込み、要約の順に進める。Codex CLI 0.166.0 の読み取り API で確認している。

1. 実行中の会話は `python "<skill-root>/scripts/wiki_session.py" locate --host codex --current --json` で決める。指定した ID は `--current` の代わりに `--session-id "<ID>"` を渡す。`CODEX_THREAD_ID` を優先し、なければ `CODEX_SESSION_ID` を使う。どちらもなければ `list --host codex --json` で候補のメタデータだけを見せ、利用者に選んでもらう。会話の名前や「最新」という理由だけで対象を選ばない。
2. 戻り値の `transcript`（`codex:<ID>`）を `--transcript` に渡す。実行中の会話では戻り値の `before` を、最初の `inspect` から以後ずっと `--before` に付ける。取り込みを依頼したターン全体より前に範囲を固定するためである。確認中の応答や新しい依頼が増えても、`before` を取り直さない。
3. `inspect` で範囲を確認したら、共通の S2〜S10 に進む。履歴は毎回メモリ上で読み、承認した範囲のハッシュが変われば書かずに止まる。会話の未加工の JSON、表示した本文、伏せる候補はファイルに保存しない。履歴を読むためにモデルのターンを始める必要はない。

対象は、人が使う CLI、IDE、デスクトップのチャットである。`exec` や子エージェントの記録を人との会話と推定しない。読み取るのは、このホストが保存したユーザーメッセージと公開されたアシスタントの本文である。思考、ツール結果、システムや開発者の指示、他のエージェントの会話、通知や圧縮の本文は含めない。Codex の応答では、当時のモデルを API から確かめられなければ「不明」とし、現在選んでいるモデルを過去の発言へ当てはめない。

従来の JSONL とデータベースの履歴は、Codex の API が読み分ける。SQLite を直接開いたり、未加工の履歴を JSONL に変換して保存したりしない。古い履歴を読む際に Codex 自身が内部の索引を補うことはあるが、この取り込み処理は会話の内容を編集せず、モデルのターンも起動しない。対応する読み取り API がない版、読めない履歴、曖昧な範囲では止まり、別の会話に切り替えない。

## スキルだけがある場合

`npx skills add` などでスキルだけを導入した配置には、プラグインの `commands/` と `.mcp.json` は含まれない。`wiki_codex.py` に手順を捏造させたり、プラグインのルートを推測して探させたりしない。完全な操作手順と MCP の自動登録が必要なら、プラグイン一式の導入を案内する。

スキルだけでも、初期化は `SKILL.md` と `references/retrieval-setup.md`、取り込みは `references/ingest-workflow.md`、検索は `references/query-workflow.md`、点検は `references/lint-workflow.md`、グラフは `references/graph-workflow.md`、経験の記録と改善は `references/evolution-workflow.md` に従って、同梱の CLI で行える。プラグインの MCP がなければ CLI を使う。
