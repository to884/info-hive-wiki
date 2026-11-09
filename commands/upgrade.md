---
description: 既存のユーザースコープまたはプロジェクトの LLM Wiki を、現在のプラグインのバージョンにアップグレードする。
argument-hint: "[--user] [--wiki-dir <name>] [--raw-dir <name>]"
argument-description: "`--user` を付けるとユーザースコープの wiki を対象にし、付けなければ現在のプロジェクトの wiki を対象にする。`--wiki-dir` と `--raw-dir` は、既定と違うディレクトリ名で作った wiki を指すときに使う。"
---

選んだ wiki を、現在のプラグインのバージョンにアップグレードする。`info-hive` スキルを使う。

引数（あれば）: $ARGUMENTS

`--global` は `--user` に改名して受け付けなくなったので、`--global` が渡されたら何も実行せず、`--user` を使うように伝えて止める。

1. `<WIKI_ROOT>` と `<RAW_ROOT>` は、`--user`、明示されたパス、もっとも近いプロジェクトの指示、グローバルなエージェントの指示の順に探し、どれにもなければ現在のプロジェクトの `wiki/` と `raw/` とする。wiki があることを確認する。ない場合は、代わりに対応する `/wiki:init --user` か `/wiki:init` を提案する。
2. ベースディレクトリと、特定したルートとちょうど一致する wiki と raw の相対パスを指定して、`init_wiki.py` を実行する。ユーザースコープの標準の構成（`~/info-hive/wiki/` と `~/info-hive/raw/`）では `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/init_wiki.py" "<absolute-home-directory>/info-hive" --upgrade` を使う。以前の既定（`~/wiki/` と `~/wiki/raw/`）で作った wiki は `"<absolute-home-directory>" --wiki-dir wiki --raw-dir wiki/raw --upgrade` のまま使い、移動はしない。プロジェクトの既定の構成では、プロジェクトルートの絶対パスに `--upgrade` を付ける。プラグインのディレクトリに対して実行してはならない。実行環境の JSON が `"status": "ready"` を示していない限り、アップグレードの完了を報告してはならない。
3. スクリプトの "Upgrade required: <SCHEMA.md>" の出力を読む。そこに挙がっている、SCHEMA.md に欠けているセクションごとに、次のことを行う。
   - `skills/info-hive/assets/SCHEMA.md.template` を開き、見出しが一致するセクションを探す。
   - そのセクションの内容をユーザーに示し、`str_replace` でユーザーの SCHEMA.md に追記することを提案する。セクションごとに承認を待つ。SCHEMA.md はユーザーと一緒に改訂してきたものなので、黙って変更してはならない。
   - ユーザーの SCHEMA.md が、追記すると衝突するような形でカスタマイズされている場合は、衝突をユーザーに示し、どう解決するか（見送る、末尾に追記する、手作業でマージする）を聞く。
4. すべてのセクションをマージするか見送ったら、次の点を伝える。
   - グラフ層はオプトイン方式である。`graph:` の frontmatter がない既存のページもそのまま有効で、何も壊れない。
   - ページでグラフ層を使い始めるときは、明示的なソースが裏づけている場合に限って、型付きの `graph.relationships[]` を追加する。詳しくは `skills/info-hive/references/graph-workflow.md` を参照。
   - グラフの lint と抽出は、PyYAML のバージョンを固定したメタデータを持ち、`uv run --script` で実行される。手作業での `pip install` は不要である。
   - `/wiki:ingest` に URL を渡すと、`wiki_fetch.py` で Web ページを取り込める。既存の wiki に移行作業は要らない。画像は `raw/assets/<スラッグ>/` に保存する。`raw/assets/` は `init_wiki.py` がすでに作っている。
   - コードブロックとインラインのコードの中の `[[...]]` を、wikilink として数えなくなった。グラフ層を使っている wiki は、`wiki_graph_extract.py` でグラフを作り直すよう伝える（作り直すまで、MCP の `graph` は古い結果を返す）。検索は、最上位の `README.md` と `assets/` の中の Markdown を対象にしなくなった。
   - 取り込みで、ソースの要約ページに `## 主張と行き先` の表を残すようになった。抜き出した主張ごとに、どのページへ送ったか（`create`、`update`、`source_only`）と理由を書く。既存のソースのページに移行作業は要らず、次に取り込むソースから表が付く。`init_wiki.py` は既存の `.page-template.md` を上書きしないので、テンプレートの `## 関係するページ` の直前にこのセクションを足すことを提案する。
   - 知識ページに図表を入れられるようになった。`--upgrade` で `wiki/assets/`（svg の図の置き場所）ができる。既存のページに移行作業は要らない。ページの大きさは、コードフェンスの中身と画像だけの行を除いて数えるようになったので、mermaid の図の多いページは、以前より小さく数えられる。

   **移植元の llm-wiki で作った wiki から移るときの注意点**（該当する場合は、これもあわせて伝える）:
   - 既存のページ、frontmatter、ファイル名に移行作業は要らない。英語のスラッグや英語の見出しのページも、そのまま使える。
   - 埋め込みモデルが、英語専用の `BAAI/bge-small-en-v1.5` から多言語の `intfloat/multilingual-e5-small`（約 0.25 GB）に変わった。`wiki/.wiki-cache/embeddings.sqlite` に記録したモデル名が違うので、アップグレードのセットアップで、すべてのセクションのベクトルが作り直される。パースキャッシュ（`search-index.json`）も、形式の番号が違うので作り直される。どちらも派生データなので、知識は失われない。
   - モデルのファイルは `~/.cache/info-hive/fastembed/` に置く。llm-wiki のキャッシュ（`~/.cache/llm-wiki/`）は読みも上書きもしないので、llm-wiki を使わなくなったなら、ユーザーが削除してよい。
   - 語彙ベースの検索（BM25）が日本語に対応した。日本語のクエリでも結果が返るようになる。
   - ページの大きさを、行数に加えて空白を除いた文字数でも判定するようになった（目安 4,000 文字、上限 8,000 文字）。長い日本語のページが、新しく lint で指摘されることがある。
   - 判定レイヤーの環境変数は `LLM_WIKI_DECISIONS*` から `INFO_HIVE_DECISIONS*` に変わった。古い名前は読まない。
   - ホスト型 wiki（`/wiki:migrate`、`/wiki:init --hosted`、`wiki_remote.py`）は info-hive にはない。ホスト型 wiki に移した wiki は、ローカルのフォルダーに戻してからアップグレードする。
   - プロバイダーを使っていた旧来の `embeddings.jsonl` キャッシュ（llm-wiki の v2 系）は無視される。削除を提案してよいが、既存のファイルなのでユーザーの承認を得てから削除する。
   - frontmatter の文字列の値は二重引用符で囲む決まりになった。既存のページはそのまま読めるが、lint が `unquoted_strings` として指摘する。`init_wiki.py` は既存の `.page-template.md` を上書きしないので、テンプレートの `type` と `tags` の例を引用符つきに直すことを提案する。

   **検索について伝えること**:
   - コマンド（`/wiki:query`、`/wiki:lint`、`/wiki:stats`、`/wiki:graph`）は、まず wiki の MCP サーバーのツールを使う。サーバーは cl-nagoya/ruri-v3-130m の ONNX、usearch、Tantivy で検索し、索引は wiki の外（Windows は `%LOCALAPPDATA%\info-hive\index\`）に置く。初めて使うときは、依存（約 1 GB）とモデル（約 530 MB）を取得するので数分かかる。ツールが使えないときは、下の CLI に戻る。詳しくは `skills/info-hive/references/mcp-server.md` にある。
   - CLI（`wiki_search.py`）のセクション検索の既定は、ローカルのハイブリッド検索である。FastEmbed と sqlite-vec による埋め込み検索の結果を、RRF で BM25 の結果と統合する。どちらの経路にも、API キー、リモートのエンドポイント、プロバイダーへの同意、クエリごとの課金はない。
   - `wiki/.wiki-cache/embeddings.sqlite` には、セクションの位置情報、内容のハッシュ、ベクトルが保存される。新しいセクションや変更されたセクションは差分だけベクトル化し、削除されたセクションは取り除く。スキーマやモデルが変わったときは自動的に作り直す。環境変数 `INFO_HIVE_EMBED_MODEL` で `intfloat/multilingual-e5-large`（約 2.24 GB）に切り替えたときも同じである。
   - 初回のセットアップとアップグレードのたびに、`uv` を通じて `setup_wiki.py` が自動で実行され、バージョンを固定した実行環境一式をインストールし、すべてのベクトルを同期する。
   - `python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --no-embed` を使うと、依存パッケージを使わない語彙ベースの BM25 に固定できる。パッケージがない、モデルの初期化でエラーが出た、sqlite-vec を読み込めなかった、といった場合も語彙ベースの検索に切り替わり、クエリの出力は壊れない。
   - 検索はセクション単位で、セクションの見出し（`§` の行）を表示する。ページ単位の語彙ベースの順位付けにするには `--granularity page` を指定する。
   - スクリプトが「ローカルの埋め込み検索（FastEmbed と sqlite-vec）」の欠落を報告した場合は、現在のテンプレートの `- Semantic backend: local FastEmbed + sqlite-vec` の行を、内容を示して承認を得たうえで、既存の `## Retrieval` セクションにマージする。llm-wiki の SCHEMA.md にこの行があっても、モデル名が `BAAI/bge-small-en-v1.5` のままなら、`intfloat/multilingual-e5-small` に直すことを提案する。
5. 選んだ wiki について、ユーザースコープまたはプロジェクトのエージェントのメモリに書かれた記述が古い `/wiki:init` で設定されたもので、グラフ層に触れていない場合は、次のような1行の案内を追加するかをユーザーに聞く。

   > 関係を問う質問では、wiki の MCP サーバーのツール `graph`（使えなければ `info-hive` スキルの `wiki_graph_query.py`）で `wiki/graph/graph.sqlite` も参照できる。

   追加する内容を示して承認を得てから、`str_replace` で書き込む。ユーザーが「見送る」と答えたら、それ以上勧めない。
6. `wiki_graph_extract.py` は自動で実行しない。`graph:` メタデータを持つページが1つもないうちは、実行しても何も得られない。ユーザーの準備ができたときの次の手順として伝えておく。

7. **経験の記録とスキルの改善（llm-wiki の v3.2.0 で加わった機能）:** init と upgrade は、`.experience-template.json`、`.pattern-template.md`、`.evolution/README.md` を、既存のファイルを上書きせずに追加する。`## Skill evolution` のスキーマセクションは、任意のマージとして提案する。経験の記録とスキルの改善はオプトイン方式で、既存のページやコマンドに移行作業は要らない。実験のアーカイブは、`.wiki-cache/` と違って消えずに残る。`/wiki:learn`、`/wiki:evolve`、パイロットスイート、検証・適用・ロールバックを明示的に行う流れについては、`references/evolution-workflow.md` を案内する。

8. **判定レイヤー（llm-wiki の v4.0.0 で加わった機能）の注意点**（これもあわせて伝える）:
   - 既存の wiki に移行作業は要らない。ファイル、スクリプト、検索、コマンドは、これまでどおり動く。
   - 任意の判定レイヤーを使うと、検索結果を関連度でふるい分けたり、グラフの弱い証拠や信頼できないテキストにフラグを立てたりできる。既定では無効で、ユーザーがバックエンドを有効にするまで、どこにもテキストを送らない。詳しくは `skills/info-hive/references/decision-models.md` を参照。
   - Paperclip 連携（`integrations/paperclip/`）は llm-wiki の v4.0.0 で削除され、info-hive にもない。必要なユーザーは llm-wiki の v3.2.x を使い続ける。スキルと wiki のファイルには影響しない。
