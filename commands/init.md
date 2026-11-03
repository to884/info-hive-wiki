---
description: 個人用のグローバルな LLM Wiki、またはこのプロジェクトの LLM Wiki を初期化する。
argument-hint: "[--global] [--wiki-dir <name>] [--raw-dir <name>]"
---

新しい LLM Wiki を初期化する。`info-hive` スキルを使う。

引数（あれば）: $ARGUMENTS

1. `skills/info-hive/references/retrieval-setup.md` を開き、そこにあるセットアップの質問を1回のやり取りでまとめて聞く。質問するのは、グローバルとプロジェクトのどちらにするか、wiki と raw のパス、モデルキャッシュの場所、グラフを使うか、エージェントのメモリとの連携の5点である。`--global` が指定されていれば、ユーザーが別のパスを指定しない限り、`~/wiki/` と `~/wiki/raw/` を選んだものとして扱う。
2. 初期化では、`uv` を通じてローカルの実行環境一式をインストールし、動作を確認することを説明する。対象は、バージョンを固定した FastEmbed、sqlite-vec、PyYAML、`intfloat/multilingual-e5-small` モデル（約 0.25 GB）、パースキャッシュ、既存の全セクションのベクトルである。API キー、プロバイダーへの同意、クエリごとの料金は不要で、wiki やクエリのテキストを外部に送ることもない。
3. ユーザーが答えたら、選んだ場所に対して初期化スクリプトを実行する。プロジェクトの wiki では `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/init_wiki.py" "<absolute-project-root>"` を使う。標準のグローバル構成では `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/init_wiki.py" "<absolute-home-directory>" --wiki-dir wiki --raw-dir wiki/raw` を使う。プラグインのディレクトリに対して実行してはならない。実行環境の JSON が `"status": "ready"` を示していない限り、セットアップの完了を報告してはならない。
4. 生成された `SCHEMA.md` の内容をユーザーと一緒に確認し、場所を記したブロックを選んだパスに設定する。ページの種類、タグ、ワークフローの規約をカスタマイズしたいかをユーザーに聞く。
5. `references/agent-memory-integration.md` に従い、構成に合ったエージェントのメモリとの連携を提案する。グローバルな wiki なら、ユーザーレベルの固定パスを使ってグローバルな指示に書き、プロジェクトの wiki なら、相対パスを使ってプロジェクトの指示に書く。追加する内容をそのまま示し、追記するか、新しく作るか、見送るかを聞く。承認なしに書き込んではならない。
6. この時点ではまだソースを取り込まない。このコマンドの役目は、ユーザーが内容を理解したうえでセットアップを終えることだけである。
