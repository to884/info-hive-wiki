# グラフのワークフロー

グラフ層は、Markdown の wiki の上に任意で作る**コンパイル済みのインデックス**である。wiki を置き換えるものではない。`wiki/graph/` の下に wiki と並べて置き、いつでも Markdown から作り直せる。目的は、出所の裏づけを持つ型付きの関係を機械でクエリできるようにしつつ、正本は Markdown のままにすることである。

`wiki/graph/ontology.yaml` がない場合、その wiki はグラフ層を導入する前のものである。extract、lint、query は実行せず、オントロジーのファイルをでっち上げてもならない。グラフ層の追加を提案するか、グラフ層なしで進める。

## グラフが表すもの

extract で得られるエッジは、次の3種類である。

1. ページの frontmatter の `graph.relationships[]` で宣言した**型付きの意味のエッジ**。例: `founded`、`proposed`、`depends_on`。それぞれ、明示的な `source`（ソースページのスラッグ）、`evidence` の引用、`confidence`（high/medium/low）、`status`（current/historical/proposed/disputed/superseded）を持つ。extract がこれらを作り出すことはない。
2. **`mentions` エッジ**。本文の `[[wikilink]]` 1つにつき1本できる（ページごとに重複は除く）。確信度は `low` である。目的のページを早く見つけるのには役立つが、型付きの関係の証拠として引用してはならない。
3. **`sourced_from`** エッジ。ソースページ以外のページの frontmatter にある `sources:` リストのスラッグ1つにつき1本でき、ソースページを指す。**`summarizes_raw`** エッジ。ソースページの `raw:` フィールド1つにつき1本でき、raw ファイルのパスを（文字列リテラルの）目的語とする。

## frontmatter のスキーマ

```yaml
graph:
  node_id: person:praney-behl       # 任意。既定は <node_type>:<slug>
  node_type: person                  # 任意。既定はオントロジーを使って type/kind から決まる
  canonical: true                    # 複数のスラッグが同じエンティティの別名になっているとき、正本に印を付ける
  aliases: [Praney, praney@example.com]
  relationships:
    - predicate: founded
      object: company:seedblocks
      source: praney-founder-context-dump
      evidence: "Solo technical founder and sole director..."
      confidence: high
      status: current
      # 任意:
      # valid_from: 2025-01-15
      # valid_to: 2026-03-01
      # notes: "..."
      # raw_ref: "raw/founder-dump.md#L42"
      # contradicts: <node-id-or-edge-id>
      # supersedes: <node-id-or-edge-id>
```

関係に必須のフィールドは、`predicate`、`object`、`source`、`evidence`、`confidence`、`status` である。

`node_id` の形式は `<node_type>:<slug>` である。既定値は、オントロジーの `maps_from` ブロックを使って、ページの `type`/`kind` から決まる。`decision`、`claim`、`raw` は明示的にだけ使うノードの種類である。wiki のページを持たないので、エッジの目的語としてだけ現れる。型付きエッジがこれらのどれかを指していると、`wiki_graph_lint.py` は「リンク切れの目的語の参照」として指摘する。この指摘は、(a) そのページを作るか、(b) `explicit_only: true` を付けてオントロジーに追加し、lint が引き続きその参照を指摘することを受け入れるまで続く。

## オントロジー

`wiki/graph/ontology.yaml` は、グラフ層の取り決めである。次のものを宣言する。

- `node_types[*].maps_from`: ページの `type`/`kind` をノードの種類にどう対応させるか。
- `predicates[*]`: 使ってよい述語。それぞれ `subject_types`、`object_types`、`requires_evidence` を持つ。`"*"` は、主語と目的語のどちらの側でもワイルドカードになる。

分野に固有の新しい述語が必要になったら、オントロジーを編集する。`wiki_graph_lint.py` を再実行して検証する。既存の型付きエッジが合わなくなっていれば、それも検出される。

## 型付きエッジを追加するか、ふつうの `[[wikilink]]` にするか

次の場合は、型付きエッジを追加する。

- 特定のソースが、その関係を明示的に述べている。
- 証拠として引用できる文がある。
- その述語が、あとのクエリで意味を持つ（「誰が何を創業したか」「Stephanie は何を提案したか」）。

次の場合は、ふつうの `[[wikilink]]` を使う。

- 関係が暗黙的である、雰囲気でしかない、または留保を付けたい。
- その主張を、1つのソースの引用に結びつけられない。
- どちらにしても述語が `mentions` になる。

迷ったら、wikilink を書き、型付きエッジはスキップする。lint は証拠の欠落を指摘するが、控えめな主張をとがめることはない。

## extract / lint / query の流れ

```bash
# まず型付きメタデータを検証する。lint は控えめに動き、ファイルを編集しない。
uv run --script scripts/wiki_graph_lint.py wiki/

# nodes.jsonl、edges.jsonl、graph.sqlite、graph.graphml にコンパイルする。
uv run --script scripts/wiki_graph_extract.py wiki/

# グラフをたどる。
python scripts/wiki_graph_query.py wiki/ neighbors --node product:konvy
python scripts/wiki_graph_query.py wiki/ edges    --subject person:stephanie-emmanouel
python scripts/wiki_graph_query.py wiki/ path     --from person:praney-behl --to product:konvy
python scripts/wiki_graph_query.py wiki/ facts    --about product:konvy
```

`--json` は、lint と query のどちらのコマンドでも使える。

## 取り込みのワークフローとの組み合わせ

標準の取り込みのワークフローの手順 6 のあと（必要な箇所だけの更新と、ソースページの作成のあと）に、次のことを行う。

1. 取り込むページに新しい型付きエッジを追加した場合は、`wiki_graph_lint.py` を実行する。extract の前に、検出結果をユーザーと一緒に仕分ける。
2. `wiki_graph_extract.py` を実行して、コンパイル済みの成果物を更新する。
3. `log.md` の取り込みの項目の下に、次のサブ行を追記する。
   `   graph: +N nodes, +M typed edges (predicates: founded, contains_product, ...)`

取り込むページに `graph.relationships[]` がなく、新しいページも作らなかった場合は、どちらもスキップする。グラフ層は変わらないからである。

## クエリのワークフローとの組み合わせ

ユーザーの質問が関係を問うものらしい場合（「X とつながっているものは？」「Y を提案したのは誰？」「A から B への経路をたどって」）は、次のようにする。

1. いつもどおりインデックスを読む。
2. `wiki/graph/graph.sqlite` があり、最新のログの項目より新しい場合は、候補のページの周りにある型付きエッジをクエリする。`neighbors`、`edges`、`facts` がもっとも役に立つ。
3. 関係するノードやエッジの元になっている wiki ページを読む。重要な主張については、グラフの行だけをもとに答えてはならない。`evidence` フィールドは手がかりであって、正本ではない。
4. 引用には、グラフの行ではなく wiki ページへの `[[wikilinks]]` を使う。

`graph.sqlite` が古い（`log.md` にある最新の取り込みより古い）場合は、先に作り直してからクエリする。

## 生成される成果物の扱い

| ファイル | 正本か | 既定の管理方法 |
|------|-----------|------------------|
| `wiki/graph/ontology.yaml` | 正本。手で編集する | git で管理する |
| `wiki/graph/nodes.jsonl` | 生成物 | 任意。`wiki/graph/.gitignore` では無視していない。PR でグラフの差分を見たいなら git で管理する |
| `wiki/graph/edges.jsonl` | 生成物 | 上と同じ |
| `wiki/graph/graph.sqlite` | 生成物 | 既定で **gitignore** する（大きいバイナリーのため） |
| `wiki/graph/graph.graphml` | 生成物 | 既定で **gitignore** する |

初期化で作られる `wiki/graph/.gitignore` は、`graph.sqlite` と `graph.graphml` を無視する。チームで別の方針にしたいなら、このファイルを編集する。

## 避けるべきパターン

- **証拠のない型付きエッジ。** グラフ層の意味がまったくなくなる。lint が指摘するので、指摘を黙らせてはならない。
- **`nodes.jsonl`、`edges.jsonl`、`graph.sqlite` を手で編集する。** Markdown を編集し、作り直す。
- **型付きエッジを「合わせる」ために、オントロジーの項目をでっち上げる。** オントロジーは分野の実態を表すべきもので、無理に作ったエッジを取り繕うためのものではない。適切な `subject_types`/`object_types` を付けて述語を追加するか、`mentions` を使う。
- **回答の中で、グラフの行を証拠として扱う。** 必ず wiki ページを引用する。グラフは、どの wiki ページを読むべきかを教えてくれただけである。
- **取り込みのあとで、作り直すのを忘れる。** グラフが気づかないうちに wiki とずれていく。取り込んだら extract を実行する、と習慣にしておく。
