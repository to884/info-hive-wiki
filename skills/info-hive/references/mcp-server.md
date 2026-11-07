# wiki の MCP サーバー

wiki のプラグインは、検索のためのローカルの MCP サーバーを持つ。埋め込みモデルの読み込み、埋め込み、索引の書き込みは、このサーバーだけが行う。wiki の Markdown は書き換えない。

この文書は、サーバーの仕組み、ツール、置き場所、困ったときの確かめ方をまとめる。コマンド（`/wiki:query`、`/wiki:lint`、`/wiki:stats`、`/wiki:graph`、`/wiki:ingest` のグラフの lint）は、まずこのサーバーのツールを使い、使えないときは CLI（`wiki_search.py` など）に戻る。使い分けの規則は `SKILL.md` の「MCP のツールと CLI の使い分け」にある。

## 仕組み

```text
ホスト（Claude Code など）
  └─ 入口（wiki_mcp_launch.py、.mcp.json で登録。依存を持たない）
        └─ stdio の中継（wiki_mcp_relay.py）
              └─ HTTP（127.0.0.1、トークン付き）
                   wiki の MCP サーバー（wiki_mcp_server.py、1 台に 1 つ）
                     ├─ 埋め込み: cl-nagoya/ruri-v3-130m（ONNX fp32、onnxruntime）
                     ├─ ベクトル索引: usearch（HNSW、f16、コサイン）
                     └─ 語彙検索: Tantivy の BM25（wiki の bigram のトークン）
```

- ホストはセッションごとに入口を起動する。入口は中継の環境を確かめ（壊れていれば入れ直し）、中継を動かす。中継は、動いているサーバーがあればそれを使い、なければ起動する。セッションがいくつあっても、サーバーは 1 つで、モデルも 1 回だけ読み込む。
- サーバーは中継から切り離して起動するので、セッションが終わっても残り、次のセッションが使う。要求がない状態が 15 分続くと、自分で止まる。次の呼び出しで中継が起動し直す。
- サーバーは 127.0.0.1 だけで待ち受ける。起動ごとに作るトークンを Bearer で確かめ、Host ヘッダーが `127.0.0.1:<ポート>` でない要求を断る。
- サーバーの依存（約 1 GB）とモデル（約 530 MB）は、最初に使うときに取得する。最初の呼び出しは数分かかることがある。
- uv が依存を入れている途中でプロセスが止められると（セッションの終わりなど）、uv の環境が作りかけのまま残ることがある。入口は中継の環境を、中継はサーバーの環境を、`uv sync --script` で確かめ、失敗したら `--reinstall` で 1 回だけ入れ直す。

## ツール

| ツール | 中身 |
| --- | --- |
| `query` | ハイブリッド検索（BM25 と埋め込みを RRF で融合）。結果の形は `wiki_search.py --json` と同じ |
| `get_page` | 1 ページの frontmatter（日本語の別名のキーを英語にそろえたもの）と本文 |
| `list_index` | `index.md` の本文と `indexes/` の一覧。`name` を渡すと `indexes/` のそのファイル |
| `lint` | 構造の lint（`wiki_lint.py --json` と同じ結果）と、オントロジーがあればグラフの lint（`wiki_graph_lint.py --json`）。検査するだけで直さない |
| `stats` | 規模、構成、リンク密度の集計（`wiki_stats.py --json` と同じ結果）と、索引の状態とサーバーの版 |
| `graph` | グラフ（`graph/graph.sqlite`）への問い合わせ（`wiki_graph_query.py --json` と同じ結果）。DB は読み取り専用で開く |
| `index_chunks` | rag などからチャンクを受け取り、wiki の外の索引に加える |

`index_chunks` 以外のツールには、`wiki` に wiki のルートの絶対パスを渡す。`lint`、`stats`、`graph` は、同じ名前のスクリプトの関数をサーバーの中で呼ぶ。結果のキーも CLI の `--json` と同じ英語のままである。

### query

| 引数 | 既定 | 説明 |
| --- | --- | --- |
| `query` | （必須） | 検索語。日本語は分かち書きせずに渡す |
| `wiki` | | wiki のルート |
| `top` | 10 | 返す行の数 |
| `type`、`tags`、`since` | | frontmatter の `type`、タグ、更新日（YYYY-MM-DD）で絞り込む |
| `per_page` | 2 | 1 ページから返すセクションの最大数 |
| `collection`、`query_tokens`、`docs`、`max_distance` | | `index_chunks` で入れたチャンクを検索するとき（下を参照） |

結果の `index` は、索引のセクションの数と埋め込み済みの数を示す。初回や大きな変更の直後は、埋め込みを裏で進めながら答えるので、`state` が `embedding` になり、`warning` に理由が入る。そのあいだも語彙検索は wiki 全体を対象にする。

### lint

| 引数 | 既定 | 説明 |
| --- | --- | --- |
| `soft_cap`、`hard_cap` | 100、200 | ページの大きさの上限の行数（`--soft-cap`、`--hard-cap`） |
| `soft_char_cap`、`hard_char_cap` | 10000、25000 | 空白を除いた文字数の上限（`--soft-char-cap`、`--hard-char-cap`）。`SCHEMA.md` が別の値を決めている wiki では、その値を渡す |
| `required_fm` | `type`、`title`、`tags`、`created`、`updated` | 必須の frontmatter のフィールドの配列 |
| `suggest_pages`、`suggest_min` | 偽、5 | 多くのページに出てくるのに専用のページがない語を挙げる |
| `graph` | 真 | グラフの lint も行う |

結果の `graph` の `state` は次のどれかになる。

| `state` | 意味 |
| --- | --- |
| `checked` | `findings` に `wiki_graph_lint.py --json` と同じ結果が入る |
| `disabled` | オントロジー（`graph/ontology.yaml`）がない。`message` は CLI の 3 つの入口と同じ文面 |
| `error` | オントロジーを読めない。`message` に理由 |
| `skipped` | `graph` が偽 |

判定モデルを使う検査（`wiki_graph_lint.py --decide`）は含めない。

### stats

結果は `wiki_stats.py --json` と同じ形に、次の 2 つを足したものである。

- `index`: 索引のセクションの数（`sections`）、埋め込み済みの数（`embedded`）、残り（`pending`）、状態（`ready`、裏で埋め込み中の `embedding`、まだ埋め込みを始めていない `pending`）、置き場所（`dir`）、埋め込みモデル（`model`）
- `server`: サーバーのコードの版（`version`）、プロトコルの版、pid

索引をまだ開いていない wiki では、開いて wiki の変更を取り込む。埋め込みは始めない。`query` を呼ぶと始まる。

### graph

| `command` | 要る引数 | 中身 |
| --- | --- | --- |
| `neighbors` | `node` | 1 ホップ先のノード |
| `edges` | `subject`、任意の `predicate` | 主語から出るエッジ |
| `path` | `from_node`、`to_node`、任意の `max_depth`（既定 6） | 有向の最短経路 |
| `facts` | `about` | 出るエッジと入るエッジ |

- ノード ID（`concept:消費税` など）は NFC にそろえてから探す。
- オントロジーがない wiki、`graph.sqlite` がない wiki、見つからないノードはエラーになる。DB は `wiki_graph_extract.py` が作る。このツールは作らない。
- グラフはナビゲーション用の索引である。重要な主張は、エッジの `source` のページで確かめる（`references/graph-workflow.md`）。

### index_chunks

rag がチャンクを渡すためのツールである。wiki の Markdown は変えない。索引は collection ごとに分け、wiki の索引とは混ぜない。

| 引数 | 説明 |
| --- | --- |
| `collection` | 索引の名前。英小文字、数字、`_`、`-` の 1〜64 文字 |
| `tokenizer` | 語彙検索のトークンの作り方の名前（例: `sudachi-c-normalized-v1`）。collection を作ったときの値と違えば失敗する |
| `generation` | 世代。整数 |
| `chunks` | 各要素に `id`（collection の中で一意）、`text`、`tokens`（文字列の配列）、`doc`（文書の識別子）、任意の `metadata`、任意の `vector` か `vector_b64`（ruri-v3-130m の 512 次元。リトルエンディアンの float32 を base64 にしたもの） |
| `delete_docs` | 丸ごと消す文書の識別子 |
| `reset` | 真なら collection を作り直す（`tokenizer` を変えるとき） |

- 語彙検索のトークンは呼び出し側が作る。サーバーは分かち書きの辞書を持たない。検索するときも、`query` に同じ作り方の `query_tokens` を渡す。
- ベクトルがないチャンクは、サーバーが「検索文書: 」の接頭辞を付けて埋め込む。
- 渡したチャンクの `doc` ごとに、`generation` より古い世代の行を消す。同じ `id` を渡すと置き換える。
- 1 回の要求の本文は 64 MiB まで。ベクトルつきなら 1,000〜2,000 件ずつに分けて渡す。
- 同じ `id` と `generation` で渡し直しても結果は変わらない。中継は、サーバーが呼び出しの途中で止まると、つなぎ直して同じ呼び出しを 1 回だけやり直す。

## 置き場所

| OS | 場所 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\info-hive\` |
| macOS、Linux | `$XDG_DATA_HOME/info-hive/`（未設定なら `~/.local/share/info-hive/`） |

その下に次を置く。

- `server.json`: 動いているサーバーのポート、トークン、pid、コードの版。中継だけが読む。
- `index/<wiki のハッシュ>/`: wiki ごとの索引。wiki のルートの絶対パスから作る。ユーザースコープとプロジェクトスコープの wiki は別の索引になる。wiki の中の `.wiki-cache/` には置かない。
- `index/collections/<名前>/`: `index_chunks` の索引
- `models/`: 埋め込みモデルのファイル（固定の revision から取得し、sha256 を確かめる）
- `logs/server.log`: サーバーのログ

索引はいつ消してもよい。次に使うときに作り直す（wiki の再埋め込みは、大きな wiki では時間がかかる）。

## 環境変数

| 変数 | 説明 |
| --- | --- |
| `INFO_HIVE_DATA_DIR` | 置き場所を変える（検証用） |
| `INFO_HIVE_MCP_IDLE_SECONDS` | 要求がなくなってから止まるまでの秒数（既定 900） |
| `INFO_HIVE_EMBED_THREADS` | 埋め込みに使うスレッドの数（既定は onnxruntime の既定で、全コア） |

## MCP が使えないとき

サーバーの依存を取得できない、uv がない、などで MCP が使えないときは、今の CLI がそのまま使える。

```bash
uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --json
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --json --no-embed
```

CLI の埋め込み検索は、MCP のサーバーとは別のモデル（multilingual-e5-small）と別の索引（wiki の `.wiki-cache/`）を使う。

## 困ったとき

- サーバーが起動しない: `logs/server.log` を見る。`uv run --script wiki_mcp_launch.py --ensure` を実行すると、サーバーを確かめ（なければ起動し）、トークンを除いた `server.json` の中身を表示する。
- プラグインを更新した: 中継は `server.json` のコードの版を見て、古いサーバーを止めて起動し直す。
- uv の環境が壊れた（`Failed to read metadata from installed package` のような uv のエラーが出る）: 次の 2 つを実行して入れ直す。入口とサーバーは、起動のたびに同じことを自動で試す。

  ```bash
  uv sync --reinstall --script "<プラグインのパス>/skills/info-hive/scripts/wiki_mcp_relay.py"
  uv sync --reinstall --script "<プラグインのパス>/skills/info-hive/scripts/wiki_mcp_server.py"
  ```

- 直したのにツールが出ない: Claude Code は、接続に失敗した MCP のサーバーを覚えていて、しばらく起動しないことがある（`~/.claude/mcp-needs-auth-cache.json` に `plugin:wiki:wiki` が入る）。`/mcp` で wiki のサーバーをつなぎ直す。
- Claude Code 以外のホスト（Codex など）: `CLAUDE_PLUGIN_ROOT` を持たないので、`uv run --script <プラグインのパス>/skills/info-hive/scripts/wiki_mcp_launch.py` を stdio の MCP サーバーとして登録する。
