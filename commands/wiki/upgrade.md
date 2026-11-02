---
description: 既存のグローバルまたはプロジェクトの LLM Wiki を、現在のプラグインのバージョンにアップグレードする。
argument-hint: "[--global] [--wiki-dir <name>] [--raw-dir <name>]"
---

選んだ wiki を、現在のプラグインのバージョンにアップグレードする。`llm-wiki` スキルを使う。

引数（あれば）: $ARGUMENTS

1. `<WIKI_ROOT>` と `<RAW_ROOT>` は、`--global`、明示されたパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` と `raw/` とする。wiki があることを確認する。ない場合は、代わりに対応する `/wiki:init --global` か `/wiki:init` を提案する。
2. ベースディレクトリと、特定したルートとちょうど一致する wiki と raw の相対パスを指定して、`init_wiki.py` を実行する。標準のグローバル構成では `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/init_wiki.py" "<absolute-home-directory>" --wiki-dir wiki --raw-dir wiki/raw --upgrade` を使う。プロジェクトの既定の構成では、プロジェクトルートの絶対パスに `--upgrade` を付ける。プラグインのディレクトリに対して実行してはならない。実行環境の JSON が `"status": "ready"` を示していない限り、アップグレードの完了を報告してはならない。
3. スクリプトの "Upgrade required: <SCHEMA.md>" の出力を読む。そこに挙がっている、SCHEMA.md に欠けているセクションごとに、次のことを行う。
   - `skills/llm-wiki/assets/SCHEMA.md.template` を開き、見出しが一致するセクションを探す。
   - そのセクションの内容をユーザーに示し、`str_replace` でユーザーの SCHEMA.md に追記することを提案する。セクションごとに承認を待つ。SCHEMA.md はユーザーと一緒に改訂してきたものなので、黙って変更してはならない。
   - ユーザーの SCHEMA.md が、追記すると衝突するような形でカスタマイズされている場合は、衝突をユーザーに示し、どう解決するか（見送る、末尾に追記する、手作業でマージする）を聞く。
4. すべてのセクションをマージするか見送ったら、次の点を伝える。
   - グラフ層はオプトイン方式である。`graph:` の frontmatter がない既存のページもそのまま有効で、何も壊れない。
   - ページでグラフ層を使い始めるときは、明示的なソースが裏づけている場合に限って、型付きの `graph.relationships[]` を追加する。詳しくは `skills/llm-wiki/references/graph-workflow.md` を参照。
   - グラフの lint と抽出は、PyYAML のバージョンを固定したメタデータを持ち、`uv run --script` で実行される。手作業での `pip install` は不要である。

   **v3.0.0 の注意点**（これもあわせて伝える）:
   - 既存のページと frontmatter に移行作業は要らない。プロバイダーを使っていた旧来の `embeddings.jsonl` キャッシュは無視される。ベクトルはすべて生成し直せるものなので、削除してかまわない。
   - セクション検索の既定は、ローカルのハイブリッド検索になった。FastEmbed の `BAAI/bge-small-en-v1.5` と sqlite-vec による検索結果を、RRF で BM25 の結果と統合する。API キー、リモートのエンドポイント、プロバイダーへの同意、クエリごとの課金は、もう一切ない。
   - `wiki/.wiki-cache/embeddings.sqlite` には、セクションの位置情報、内容のハッシュ、ベクトルが保存される。新しいセクションや変更されたセクションは差分だけベクトル化し、削除されたセクションは取り除く。スキーマやモデルが変わったときは自動的に作り直す。
   - 初回のセットアップとアップグレードのたびに、`uv` を通じて `setup_wiki.py` が自動で実行され、バージョンを固定した実行環境一式をインストールし、すべてのベクトルを同期する。モデルのファイルは `~/.cache/llm-wiki/fastembed/` に置かれる。
   - `python "${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/scripts/wiki_search.py" "<query>" --wiki "<WIKI_ROOT>" --no-embed` を使うと、依存パッケージを使わない語彙ベースの BM25 に固定できる。パッケージがない、モデルの初期化でエラーが出た、sqlite-vec を読み込めなかった、といった場合も語彙ベースの検索に切り替わり、クエリの出力は壊れない。
   - 検索は引き続きセクション単位で、セクションの見出し（`§` の行）を表示する。ページ単位の語彙ベースの順位付けに戻すには `--granularity page` を指定する。
   - 現在のテンプレートにある `[3.0.0] Local semantic retrieval` マーカーを、内容を示して承認を得たうえで、既存の `## Retrieval` セクションにマージする。
5. 選んだ wiki について、グローバルまたはプロジェクトのエージェントのメモリに書かれた記述が古い `/wiki:init` で設定されたもので、グラフ層に触れていない場合は、次のような1行の案内を追加するかをユーザーに聞く。

   > Relational queries can also consult `wiki/graph/graph.sqlite` via `wiki_graph_query.py` from the `llm-wiki` skill.

   追加する内容を示して承認を得てから、`str_replace` で書き込む。ユーザーが「見送る」と答えたら、それ以上勧めない。
6. `wiki_graph_extract.py` は自動で実行しない。`graph:` メタデータを持つページが1つもないうちは、実行しても何も得られない。ユーザーの準備ができたときの次の手順として伝えておく。

7. **v3.2.0:** init と upgrade は、`.experience-template.json`、`.pattern-template.md`、`.evolution/README.md` を、既存のファイルを上書きせずに追加する。`## Skill evolution` のスキーマセクションは、任意のマージとして提案する。経験の記録とスキルの改善はオプトイン方式で、既存のページやコマンドに移行作業は要らない。実験のアーカイブは、`.wiki-cache/` と違って消えずに残る。`/wiki:learn`、`/wiki:evolve`、パイロットスイート、検証・適用・ロールバックを明示的に行う流れについては、`references/evolution-workflow.md` を案内する。

8. **v4.0.0 の注意点**（これもあわせて伝える）:
   - ローカルの wiki に移行作業は要らない。ローカルのファイル、スクリプト、検索、コマンドは、これまでどおり動く。
   - ホスト型のバックエンドを、任意で使えるようになった。MCP、JSON API、OAuth、Web のリーダーを備えた1つの Cloudflare Worker である。この wiki をそこへ移すには `/wiki:migrate` を、別のエージェントを接続するには `/wiki:init --hosted <url>` を実行する。詳しくは `skills/llm-wiki/references/hosted-mode.md` を参照。
   - 任意の判定レイヤーを使うと、検索結果を関連度でふるい分けたり、グラフの弱い証拠や信頼できないテキストにフラグを立てたりできる。既定では無効で、ユーザーがバックエンドを有効にするまで、どこにもテキストを送らない。詳しくは `skills/llm-wiki/references/decision-models.md` を参照。
   - Paperclip 連携（`integrations/paperclip/`）は削除された。必要なユーザーは v3.2.x を使い続ける。スキルと wiki のファイルには影響しない。
   - この wiki がホスト型 wiki の `pull` ミラーである（`.llm-wiki-remote.json` がある）場合は、init や upgrade による書き込みを行ってはならない。サーバー側でホスト型 wiki をアップグレードしてから、もう一度 pull する。
