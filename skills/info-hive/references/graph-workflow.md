# グラフのワークフロー

グラフ層は、Markdown の wiki の上に任意で作るコンパイル済みのインデックスである。wiki を置き換えるものではない。`wiki/graph/` の下に wiki と並べて置き、いつでも Markdown から作り直せる。目的は、出所の裏づけを持つ型付きの関係を機械でクエリできるようにしつつ、正本は Markdown のままにすることである。

この文書が、グラフの有効化、更新、検査、問い合わせ、作り直しの手順の正本である。取り込み、問い合わせ、lint、アップグレードの各手順とコマンドは、グラフの段を呼ぶ時点と結果の扱いだけを書き、規約はここを参照する。

## グラフ層が有効かどうか

グラフ層が有効かどうかは、オントロジー `wiki/graph/ontology.yaml` があるかだけで決める。`graph.sqlite` などの生成物の有無では決めない。

- **オントロジーがある**: グラフ層は有効である。下の手順に従う。
- **オントロジーがない**: グラフ層を導入する前の wiki である。extract、lint、query のスクリプトは、どれも何も書き出さずに、同じ文面を標準エラーに書いて終了コード 1 で止まる。オントロジーを消した wiki に古い `graph.sqlite` が残っていても、query はそれに答えない。オントロジーのファイルをでっち上げてはならない。グラフ層を使いたいなら、`/wiki:upgrade` か、`assets/ontology.yaml.template` を `wiki/graph/ontology.yaml` にコピーすることを提案する。使わないなら、グラフの段を飛ばして進める。
- **オントロジーを読めない**（YAML の誤り、最上位がマッピングでない。`[]` や `false` も含む）: extract と lint は終了コード 2 で止まる。ユーザーにオントロジーの修正を頼む。query は PyYAML を使わないので中身を読まず、ファイルがあれば DB に問い合わせる。

`--ontology` で別のオントロジーを指定したときは、そのファイルの有無で同じように決める。query に `--db` で DB を明示したときは、オントロジーを確かめずにその DB を使う（`extract --out` で別の場所に作った場合のため）。

終了コードのまとめ: 0 は成功、1 は必要な入力がない（wiki のディレクトリ、オントロジー、DB、ノード）、2 は環境か入力の形式の問題（PyYAML がない、オントロジーを読めない）である。

## グラフが表すもの

extract で得られるエッジは、次の3種類である。

1. ページの frontmatter の `graph.relationships[]` で宣言した型付きの意味のエッジ。例: `founded`、`proposed`、`depends_on`。それぞれ、明示的な `source`（ソースページのスラッグ）、`evidence` の引用、`confidence`（high/medium/low）、`status`（current/historical/proposed/disputed/superseded）を持つ。extract がこれらを作り出すことはない。
2. `mentions` エッジ。本文の `[[wikilink]]` 1つにつき1本できる（ページごとに重複は除く）。確信度は `low` である。目的のページを早く見つけるのには役立つが、型付きの関係の証拠として引用してはならない。
3. `sourced_from` エッジと `summarizes_raw` エッジ。`sourced_from` は、ソースページ以外のページの frontmatter にある `sources:` リストの値1つにつき1本でき、ソースページを指す。値は、wikilink と同じ規則で、スラッグ（`paper`）、パス（`sources/paper`）、`[[sources/paper]]` のどれで書いてもよい。読み方は wiki の lint の `unresolved_sources` と同じで、lint が source でないページを指すと挙げる値からも、ページが 1 つなら `sourced_from` ができる。`summarizes_raw` は、ソースページの `raw:` フィールド1つにつき1本でき、raw ファイルのパスを（文字列リテラルの）目的語とする。

どのエッジも、主語はそれを書いたページ自身のノードであり、エッジの `page` はそのページである。別のページに同じ内容の関係を書けば、別のエッジになる（エッジの ID に、ディスク上のページのパスが入る）。`graph.relationships` の項目のうち、`predicate` と `object` があり、目的語が曖昧でない（下の段落）もの 1 つからは、必ず 1 本のエッジができる。同じページで `status` や `valid_to` だけが違う 2 件も、まったく同じ 2 件も、別のエッジになる。`contradicts` と `supersedes` に書いたエッジの ID は、エッジの ID の計算に入れないので、互いのエッジの ID を書いても、それぞれの ID は変わらない。エッジの ID を書いた参照か未知の欄だけが違う 2 件は、ページの中の順番で ID が決まり、並べ替えると入れ替わりうるので、グラフの lint が `order_dependent_edge_ids` に挙げる。`contradicts` と `supersedes` の値は、ノード ID、ソースページのスラッグ、抽出したエッジの ID のどれかに当たればよく、どれにも当たらなければ、グラフの lint が `broken_contradicts` と `broken_supersedes` に挙げる。エッジの ID は、16 進 24 桁の形をしていても、抽出したエッジにない値なら挙がる。

ページへの参照（本文の wikilink と `sources:` の値）は、当たるページが 1 つのときだけエッジにする。`a/Foo.md` と `b/Foo.md` のように、別のディレクトリに同じスラッグのページがあるとき、パスのない `[[Foo]]` と `sources:` の `Foo` は、どちらのページにも寄せず、エッジを作らない。パスを付けた `[[a/Foo]]` と `sources:` の `a/Foo` は、そのページを指す。`sources:` の値で、当たるページが複数あるものと、ないものは、グラフの lint の `ambiguous_sources` と `unresolved_sources` が挙げる（ソースページの `sources:` は `sourced_from` を作らないが、この 2 つは挙げる）。重なったスラッグは、wiki の lint の `duplicate_slugs` が挙げる。

別のページが同じノード ID を持つとき（同じ `graph.node_id` を付けたか、型が同じで既定の node_id を使う同じスラッグのページ）、ノードは最初のページから 1 つだけでき、どのページの関係も、そのノードを主語にして残る。そのノード ID を目的語にした型付きの関係は、どのページのノードか決められないので、エッジにしない。グラフの lint は、重なったノード ID を `duplicate_node_ids` に、エッジにしなかった関係を `ambiguous_object_refs` に挙げる。別の `graph.node_id` を付けて分ける。

## frontmatter のスキーマ

例の人物と会社は架空のものである。

```yaml
graph:
  node_id: person:山田太郎            # 任意。既定は <node_type>:<slug>
  node_type: person                  # 任意。既定はオントロジーを使って type/kind から決まる
  canonical: true                    # 複数のスラッグが同じエンティティの別名になっているとき、正本に印を付ける
  aliases: [山田, やまだたろう, yamada@example.com]
  relationships:
    - predicate: founded
      object: company:やまだ製作所
      source: 山田太郎インタビュー
      evidence: "2015 年に、やまだ製作所を一人で立ち上げた。"
      confidence: high
      status: current
      # 任意:
      # valid_from: 2015-04-01
      # valid_to: 2026-03-01
      # notes: "..."
      # raw_ref: "raw/山田太郎インタビュー.md#L42"
      # contradicts: <node-id-or-edge-id>
      # supersedes: <node-id-or-edge-id>
```

関係に必須のフィールドは、`predicate`、`object`、`source`、`evidence`、`confidence`、`status` である。

### 値の読み方

グラフのスクリプト（extract と lint）は、frontmatter の最上位の欄（`type`、`kind`、`title`、`tags`、`sources`、`raw` など）を、wiki の lint、検索、統計と同じパーサーで読む。値は、書かれた文字のままの文字列か、その配列になる。ただし、ノードの `title` は、検索と同じく、配列なら空でない要素を「, 」でつないだ文字にし、値がなければスラッグにする。ノードの `page_type`、`kind`、`created`、`updated` も、配列なら同じく要素をつなぐ（`type` と `kind` の要素は日本語の別名を英語に直す）。ノードの種類（`node_type`）は、そうしてそろえた `type` と `kind` から決める。`sources: [true]` は `true` というページ、`sources: [007]` は `007` というページを指し、`kind: 2024` は文字の `2024` である。wiki の lint が解決する `sources:` は、グラフでも同じページに解決する。`raw:` を配列で書くと、値 1 つにつき `summarizes_raw` が 1 本できる。

`graph:` の欄だけは PyYAML で読む。そのうち、ページやノードを指す欄（`node_id`、`aliases`、関係の `object`、`source`、`contradicts`、`supersedes`）は、引用符のない `true`、`no`、数、日付も、書かれた文字で読む（`source: 007` は `007`、`object: no` は `no`）。`null`、`~`、値なしは、値がないものとして扱う。文字の `null` を指すには、`"null"` と引用符で囲む。ほかの欄（`canonical`、`valid_from`、`notes` など）は、YAML の値で読む。

次のページは、グラフの lint が挙げる。抽出も、件数を標準エラーに書く。

- `unreadable_graph`: `graph:` を書いたのに読めなかったページ。型付きの関係は 1 つもできない。`reason` が `yaml_error` なら、frontmatter が YAML として読めない（`title: a: b` のように引用符のない値に `: ` がある、タブがある）か、`graph:` の中に `valid_from: 2026-13-01` のような組み立てられない値がある。`not_mapping` なら、`graph:` の値がマッピングでない。`graph:` の外の値は、frontmatter が YAML として構文を読めれば、どう書いてあっても `graph:` の読み方に影響しない（`created: 2026-13-01` があっても読む）。
- `unread_fields`: `type`、`kind`、`sources`、`raw`（日本語の別名を含む）のうち、YAML としては値があるのに、wiki の lint のパーサーが読めない書き方のもの（`field`）。アンカー（`&`、`*`）を付けた配列、キーの次の行に書いた `[a, b]`、`? type` の形のキー、最上位のマージキー（`<<`）だけで来た値である。lint、検索、統計と同じく、グラフもその値を読まない（`sources:` なら `sourced_from` ができない）。配列は 1 行の `["a", "b"]` で書く。字下げの違うブロックの配列（`- "a"`）、複数行にまたがる `[a,` … `]`、引用符つきのキー（`"type": x`）、`type : x` は、wiki 0.9.7 から lint のパーサーが読むので、挙がらない。

`evidence` には、ソースの文をそのまま写す（言い換えない、訳さない）。英語のソースは英語のまま引く。引用は 1 文か、文の一部にとどめる。`--decide` を付けた lint は、引用がソースページの本文にあるかを照合する。照合では、大文字と小文字の違いと、日本語の文字どうしの間の改行や空白は吸収するが、英数字の間の空白は残し、全角と半角の違いは吸収しない。ソースが全角の「ＡＩ」なら、引用も「ＡＩ」と書く。

`node_id` の形式は `<node_type>:<slug>` である。既定値は、オントロジーの `maps_from` ブロックを使って、ページの `type`/`kind` から決まる。スラッグが日本語なら、ノード ID も `concept:消費税` や `organization:国税庁` のように日本語を含む。lint、extract、query はこれをそのまま扱い、NFC にそろえてから照合する。英語の固有名詞のページは `person:andrej-karpathy` のように英小文字のスラッグのままでよい。

`decision`、`claim`、`raw` は明示的にだけ使うノードの種類である。wiki のページを持たないので、エッジの目的語としてだけ現れる。型付きエッジがこれらのどれかを指していると、`wiki_graph_lint.py` は「リンク切れの目的語の参照」として指摘する。この指摘は、(a) そのページを作るか、(b) `explicit_only: true` を付けてオントロジーに追加し、lint が引き続きその参照を指摘することを受け入れるまで続く。

## オントロジー

`wiki/graph/ontology.yaml` は、グラフ層の取り決めである。次のものを宣言する。

- `node_types[*].maps_from`: ページの `type`/`kind` をノードの種類にどう対応させるか。`type` と `kind` の値は、ページの値と同じく、書かれた文字で比べる（`kind: 2024` は、ページの `kind: 2024` に当たり、`kind: 2024.0` には当たらない）。
- `predicates[*]`: 使ってよい述語。それぞれ `subject_types`、`object_types`、`requires_evidence` を持つ。`"*"` は、主語と目的語のどちらの側でもワイルドカードになる。

分野に固有の新しい述語が必要になったら、オントロジーを編集する。`wiki_graph_lint.py` を再実行して検証する。既存の型付きエッジが合わなくなっていれば、それも検出される。

## 型付きエッジを追加するか、ふつうの `[[wikilink]]` にするか

次の場合は、型付きエッジを追加する。

- 特定のソースが、その関係を明示的に述べている。
- 証拠として引用できる文がある。
- その述語が、あとのクエリで意味を持つ（「誰が何を創業したか」「山田太郎は何を提案したか」）。

次の場合は、ふつうの `[[wikilink]]` を使う。

- 関係が暗黙的である、雰囲気でしかない、または留保を付けたい。
- その主張を、1つのソースの引用に結びつけられない。
- どちらにしても述語が `mentions` になる。

迷ったら、wikilink を書き、型付きエッジはスキップする。lint は証拠の欠落を指摘するが、控えめな主張をとがめることはない。

## 手順

検査（lint）と問い合わせ（query）は、wiki の MCP サーバーのツール `lint` と `graph` が使えればそちらで行い、使えないときは下のスクリプトを実行する。使い分けは `SKILL.md` の「MCP のツールと CLI の使い分け」にある。更新（extract）は wiki の `graph/` に書き込むので、いつもスクリプトで実行する。

コマンドの `<skill-root>` は、このスキルのディレクトリ（`SKILL.md` のあるディレクトリ）の実際のパスに置き換える。書き方は `SKILL.md` の「スクリプトのパス」にある。`wiki/` は実際の wiki のディレクトリに置き換える。lint は `<wiki>` を省くと、作業領域の設定（`.info-hive/workspace.json`）の `dirs.wiki`、設定がなければ `./wiki` を使う。extract と query は `<wiki>` を省けない。

### 検査（lint）

```bash
uv run --script "<skill-root>/scripts/wiki_graph_lint.py" wiki/ [--json] [--decide]
```

ツールの `lint` は、構造の lint の結果と一緒に、同じ検査の結果を `graph` に入れて返す（`state` が `checked` なら `findings`、オントロジーがなければ `disabled`、読めなければ `error`）。`--decide` が要るときはスクリプトを使う。

型付きのメタデータを、オントロジーと照らして検査する。ファイルは編集しない。検出結果は編集案として示し、型付きの `graph.relationships` を黙って書き換えてはならない。承認された修正を適用したら、extract を実行する。`--decide` は、任意の判定レイヤーで証拠を確かめる（`decision-models.md`）。

lint を実行するのは、`graph.relationships[]` を足したか直したとき、オントロジーを編集したとき、ユーザーが lint を頼んだときである。型付きの関係に触れていない更新では、lint を飛ばしてよい。

### 更新（extract）

```bash
uv run --script "<skill-root>/scripts/wiki_graph_extract.py" wiki/ [--out <dir>] [--formats jsonl,sqlite,graphml] [--ontology <path>]
```

**グラフ層が有効な wiki で、ページを足した、消した、名前を変えた、または書き換えたときは、必ず extract を実行する。** extract が作るエッジは `graph.relationships[]` だけから来るのではない。本文の `[[wikilink]]`（`mentions`）、frontmatter の `sources:`（`sourced_from`）、ソースページの `raw:`（`summarizes_raw`）からも作り、ノードの題名、種類、タグ、別名も frontmatter から読む。そのため、型付きの関係を足さず、新しいページも作らない取り込みでも、既存のページの本文や `sources:` を直せばグラフは変わる。オントロジーの `node_types` の `maps_from` を変えたときも、ノードの種類と ID が変わるので extract を実行する。extract を飛ばしてよいのは、wiki のページもオントロジーも 1 つも変えなかったときだけである。

extract は、型付きの関係があればその前に lint を通す。lint の検出結果があっても extract は動くが、型の誤ったエッジもそのままグラフに入る。

取り込みのあとで extract を実行したら、`log.md` の取り込みの行の下に、次のサブ行を追記する。

`   グラフ: ノード +N、型付きエッジ +M（述語: founded、contains_product、...）`

### 作り直しと中断

extract は、選んだ形式の成果物（`nodes.jsonl`、`edges.jsonl`、`graph.sqlite`、`graph.graphml`）を、まずすべて同じディレクトリの一時ファイル（`<名前>.<プロセス番号>.tmp`）に書く。書き終えてから、`graph.sqlite`、`nodes.jsonl`、`edges.jsonl`、`graph.graphml` の順に元のファイルと置き換える。書いている途中で止まっても、前の成果物が壊れずに残り、query を続けられる。新しい内容にするには、extract をもう一度実行する。

同じ出力先の extract は、ロック（`<出力先>/.extract.lock`、OS のファイルロック）で 1 つずつ動かす。後から始めた extract は、先の extract が終わるまで最長 120 秒待ち、ロックを取ってからページを読む。そのため、2 つの extract の成果物が混ざらない。ロックはプロセスが止まると OS が外す。ロックを持つ extract は、止まった実行が残した一時ファイル（SQLite の `-journal` なども含む）を消す。`wiki/graph/.gitignore` のひな形は、一時ファイルとロックのファイルを無視する。

Windows では、ほかのプロセスが `graph.sqlite` を開いていると置き換えに失敗する。extract は少し待ってやり直し、それでも置き換えられなければ一時ファイルを消し、終了コード 1 で止まる。`graph.sqlite` を最初に置き換えるので、このときはほかの成果物も前のまま残る。`graph.sqlite` を置き換えたあとで JSONL や GraphML の置き換えに失敗した場合と、置き換えと置き換えのあいだでプロセスが止まった場合だけは、新旧の成果物が混ざる。そのときは extract をもう一度実行する。

成果物はいつでも消して作り直せる。手で直してはならない。Markdown を直して extract を実行する。

### 問い合わせ（query）

```bash
python "<skill-root>/scripts/wiki_graph_query.py" wiki/ neighbors --node company:やまだ製作所
python "<skill-root>/scripts/wiki_graph_query.py" wiki/ edges    --subject person:山田太郎 [--predicate founded]
python "<skill-root>/scripts/wiki_graph_query.py" wiki/ path     --from person:山田太郎 --to concept:消費税 [--max-depth 6]
python "<skill-root>/scripts/wiki_graph_query.py" wiki/ facts    --about company:やまだ製作所
```

`--json` は、lint と query のどちらでも使える。query は標準ライブラリだけで動くので、`python` で実行する。

ツールの `graph` では、`command` に `neighbors`、`edges`、`path`、`facts` のどれかを渡し、引数は `node`、`subject`（と任意の `predicate`）、`from_node` と `to_node`（と任意の `max_depth`）、`about` を使う。結果は `--json` と同じ形である。ツールは `graph.sqlite` を読み取り専用で開き、作らない。オントロジーがない wiki、`graph.sqlite` がない wiki、見つからないノードはエラーになる。

関係を問う質問（「X とつながっているものは？」「Y を提案したのは誰？」「A から B への経路をたどって」）では、次のようにする。

1. いつもどおりインデックスを読む。
2. `graph.sqlite` が古い（`log.md` にある最新の `## [YYYY-MM-DD] ingest |` の行より前に作られた）なら、先に extract で作り直す。
3. 候補のページの周りにある型付きエッジを問い合わせる。`neighbors`、`edges`、`facts` がもっとも役に立つ。
4. 関係するノードやエッジの元になっている wiki ページを読む。重要な主張については、グラフの行だけをもとに答えてはならない。`evidence` フィールドは手がかりであって、正本ではない。
5. 引用には、グラフの行ではなく wiki ページへの `[[wikilinks]]` を使う。

### 有効化

新しい wiki では、`init_wiki.py` が `graph/ontology.yaml`、`graph/README.md`、`graph/.gitignore` を置く。グラフ層を導入する前の wiki では、`/wiki:upgrade`（`init_wiki.py --upgrade`）が足りないファイルを足す。アップグレードは、既存のオントロジーを書き換えず、extract も自動では実行しない。

## 生成される成果物の扱い

| ファイル | 正本か | 既定の管理方法 |
|------|-----------|------------------|
| `wiki/graph/ontology.yaml` | 正本。手で編集する | git で管理する |
| `wiki/graph/nodes.jsonl` | 生成物 | 任意。`wiki/graph/.gitignore` では無視していない。PR でグラフの差分を見たいなら git で管理する |
| `wiki/graph/edges.jsonl` | 生成物 | 上と同じ |
| `wiki/graph/graph.sqlite` | 生成物 | 既定で gitignore する（大きいバイナリーのため） |
| `wiki/graph/graph.graphml` | 生成物 | 既定で gitignore する |

初期化で作られる `wiki/graph/.gitignore` は、`graph.sqlite` と `graph.graphml` を無視する。チームで別の方針にしたいなら、このファイルを編集する。

## 避けるべきパターン

- 証拠のない型付きエッジ。グラフ層の意味がまったくなくなる。lint が指摘するので、指摘を黙らせてはならない。
- `nodes.jsonl`、`edges.jsonl`、`graph.sqlite` を手で編集する。Markdown を編集し、作り直す。
- 型付きエッジを「合わせる」ために、オントロジーの項目をでっち上げる。オントロジーは分野の実態を表すべきもので、無理に作ったエッジを取り繕うためのものではない。適切な `subject_types`/`object_types` を付けて述語を追加するか、`mentions` を使う。
- 回答の中で、グラフの行を証拠として扱う。必ず wiki ページを引用する。グラフは、どの wiki ページを読むべきかを教えてくれただけである。
- ページを書き換えたあとで、作り直すのを忘れる。型付きの関係に触れていなくても、wikilink や `sources:` が変わればグラフは変わる。グラフが気づかないうちに wiki とずれていく。
- オントロジーがないのに、extract の結果がないことを「グラフが空」と受け取る。終了コード 1 は、グラフ層を導入していないことを示す。
