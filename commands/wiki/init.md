---
description: 個人用のグローバルな LLM Wiki、またはこのプロジェクトの LLM Wiki を初期化する。ホスト型の wiki にこのエージェントを接続することもできる。
argument-hint: "[--global] [--wiki-dir <name>] [--raw-dir <name>] | --hosted <url>"
---

新しい LLM Wiki を初期化するか、ホスト型の wiki に接続する。`llm-wiki` スキルを使う。

引数（あれば）: $ARGUMENTS

引数に `--hosted <url>` が含まれている場合は、下の「ホスト型 wiki への接続」に従い、ローカルの手順はスキップする。

## ローカルの wiki

1. `skills/llm-wiki/references/retrieval-setup.md` を開き、そこにあるセットアップの質問を1回のやり取りでまとめて聞く。質問するのは、グローバルとプロジェクトのどちらにするか、wiki と raw のパス、モデルキャッシュの場所、グラフを使うか、エージェントのメモリとの連携の5点である。`--global` が指定されていれば、ユーザーが別のパスを指定しない限り、`~/wiki/` と `~/wiki/raw/` を選んだものとして扱う。
2. 初期化では、`uv` を通じてローカルの実行環境一式をインストールし、動作を確認することを説明する。対象は、バージョンを固定した FastEmbed、sqlite-vec、PyYAML、`BAAI/bge-small-en-v1.5` モデル、パースキャッシュ、既存の全セクションのベクトルである。API キー、プロバイダーへの同意、クエリごとの料金は不要で、wiki やクエリのテキストを外部に送ることもない。
3. ユーザーが答えたら、選んだ場所に対して初期化スクリプトを実行する。プロジェクトの wiki では `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/init_wiki.py" "<absolute-project-root>"` を使う。標準のグローバル構成では `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/init_wiki.py" "<absolute-home-directory>" --wiki-dir wiki --raw-dir wiki/raw` を使う。プラグインのディレクトリに対して実行してはならない。実行環境の JSON が `"status": "ready"` を示していない限り、セットアップの完了を報告してはならない。
4. 生成された `SCHEMA.md` の内容をユーザーと一緒に確認し、場所を記したブロックを選んだパスに設定する。ページの種類、タグ、ワークフローの規約をカスタマイズしたいかをユーザーに聞く。
5. `references/agent-memory-integration.md` に従い、構成に合ったエージェントのメモリとの連携を提案する。グローバルな wiki なら、ユーザーレベルの固定パスを使ってグローバルな指示に書き、プロジェクトの wiki なら、相対パスを使ってプロジェクトの指示に書く。追加する内容をそのまま示し、追記するか、新しく作るか、見送るかを聞く。承認なしに書き込んではならない。
6. この時点ではまだソースを取り込まない。このコマンドの役目は、ユーザーが内容を理解したうえでセットアップを終えることだけである。

## ホスト型 wiki への接続（`--hosted <url>`）

最初に `skills/llm-wiki/references/hosted-mode.md` を読む。この手順は、すでにデプロイされている wiki にこのエージェントを接続するものであり、サーバーはデプロイしない。サーバーをデプロイしたい場合は、「Hosted wiki」のドキュメントページを案内する。

1. ユーザーが使っているエージェントのクライアントと、トークンと OAuth のどちらで接続するかを聞く。OAuth を使うのは、そのクライアントがリモートの MCP サーバーへの OAuth サインインに対応している場合だけである。答えに応じて、次のどちらかの手順に進む。

### トークンの場合

2. このエージェント用のトークンをユーザーに求める。トークンは `<url>/setup` か `wiki_remote.py token create` で、スコープを `read,write` にして作るよう伝える（raw をアップロードするなら `raw:write` も加える）。`ADMIN_TOKEN` は貼り付けないよう伝える。
3. 接続情報を保存する: `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py" login --url "<url>" --token -`。トークンはユーザーが標準入力から入力する。トークンを表示したり、プロジェクト内のファイルやメモリファイルに書き込んだりしてはならない。
4. `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py" status` を実行し、wiki の ID、スコープ、ページ数、検索モードを報告する。コマンドが失敗したら、そこで止める。
5. `python3 "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_remote.py" mcp-config <client>` を実行する（`<client>` は `claude-code`、`codex`、`cursor`、`gemini`、`opencode`、`omp`、`pi`、`generic` のいずれか）。出力と、それをどこに書くかを示し、ユーザーが承認してから追加する。環境変数 `LLM_WIKI_TOKEN` を設定するよう伝える。

### OAuth の場合

2. MCP サーバーの URL `<url>/mcp` を、`Authorization` ヘッダーなしでクライアントに追加する。設定の変更内容をそのまま示し、ユーザーが承認してから追加する。`wiki_remote.py login` は実行しない。OAuth サインインで発行されるトークンはクライアントに渡るもので、`wiki_remote.py` には渡らないためである。
3. クライアントからサインインを始めるよう伝える（Claude Code なら `/mcp`、Pi なら `pi mcp login llm-wiki` など）。クライアントは、自身のリクエストパラメーターを付けて、サーバーの認可ページをブラウザーで開く。`<url>/authorize` を直接開くよう伝えてはならない。クライアントからのリクエストがないと、そのページではサインインを完了できない。
4. 認可ページで、ユーザーは `ADMIN_TOKEN` か使い捨てのペアリングコードでクライアントを承認し、wiki とアクセス権を選ぶ。ペアリングコードは `<url>/setup` か `wiki_remote.py pair --label <label> --scopes read,write` で作る。
5. 新しく追加した MCP サーバー経由で `wiki_overview` を呼び出し、結果から wiki の ID、ページ数、検索モードを報告する。呼び出しが失敗したら、そこで止める。

### 共通の手順

6. `references/agent-memory-integration.md` にあるホスト型 wiki 用の記述を、URL を埋めた状態で提案する。追加する文面をそのまま示し、追記するか、新しく作るか、見送るかを聞く。承認なしに書き込んではならない。同じ wiki についてローカルの wiki 用の記述がすでにある場合は、置き換えを提案する。
7. 移行したいローカルの wiki がある場合は、`/wiki:migrate` を提案する。このコマンドでは何もインポートしない。
