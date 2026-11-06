# wiki の MCP サーバー

wiki のプラグインは、検索のためのローカルの MCP サーバーを持つ。埋め込みモデルの読み込み、埋め込み、索引の書き込みは、このサーバーだけが行う。wiki の Markdown は書き換えない。

この文書は、サーバーの仕組み、ツール、置き場所、困ったときの確かめ方をまとめる。コマンド（`/wiki:query` など）はまだ今の CLI（`wiki_search.py`）を使う。MCP のツールへの移行は後の版で行う。

## 仕組み

```text
ホスト（Claude Code など）
  └─ stdio の中継（wiki_mcp_relay.py、.mcp.json で登録）
        └─ HTTP（127.0.0.1、トークン付き）
             wiki の MCP サーバー（wiki_mcp_server.py、1 台に 1 つ）
               ├─ 埋め込み: cl-nagoya/ruri-v3-130m（ONNX fp32、onnxruntime）
               ├─ ベクトル索引: usearch（HNSW、f16、コサイン）
               └─ 語彙検索: Tantivy の BM25（wiki の bigram のトークン）
```

- ホストはセッションごとに中継を起動する。中継は、動いているサーバーがあればそれを使い、なければ起動する。セッションがいくつあっても、サーバーは 1 つで、モデルも 1 回だけ読み込む。
- サーバーは中継から切り離して起動するので、セッションが終わっても残り、次のセッションが使う。要求がない状態が 15 分続くと、自分で止まる。次の呼び出しで中継が起動し直す。
- サーバーは 127.0.0.1 だけで待ち受ける。起動ごとに作るトークンを Bearer で確かめ、Host ヘッダーが `127.0.0.1:<ポート>` でない要求を断る。
- サーバーの依存（約 1 GB）とモデル（約 530 MB）は、最初に使うときに取得する。最初の呼び出しは数分かかることがある。

## ツール

| ツール | 中身 |
| --- | --- |
| `query` | ハイブリッド検索（BM25 と埋め込みを RRF で融合）。結果の形は `wiki_search.py --json` と同じ |
| `get_page` | 1 ページの frontmatter（日本語の別名のキーを英語にそろえたもの）と本文 |
| `list_index` | `index.md` の本文と `indexes/` の一覧。`name` を渡すと `indexes/` のそのファイル |
| `index_chunks` | rag などからチャンクを受け取り、wiki の外の索引に加える |

`query`、`get_page`、`list_index` には、`wiki` に wiki のルートの絶対パスを渡す。

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

## MCP が使えないとき

サーバーの依存を取得できない、uv がない、などで MCP が使えないときは、今の CLI がそのまま使える。

```bash
uv run --script "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --json
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_search.py" "<検索語>" --wiki "<WIKI_ROOT>" --json --no-embed
```

CLI の埋め込み検索は、MCP のサーバーとは別のモデル（multilingual-e5-small）と別の索引（wiki の `.wiki-cache/`）を使う。

## 困ったとき

- サーバーが起動しない: `logs/server.log` を見る。`uv run --script wiki_mcp_relay.py --ensure` を実行すると、サーバーを確かめ（なければ起動し）、トークンを除いた `server.json` の中身を表示する。
- プラグインを更新した: 中継は `server.json` のコードの版を見て、古いサーバーを止めて起動し直す。
- Claude Code 以外のホスト（Codex など）: `CLAUDE_PLUGIN_ROOT` を持たないので、`uv run --script <プラグインのパス>/skills/info-hive/scripts/wiki_mcp_relay.py` を stdio の MCP サーバーとして登録する。
