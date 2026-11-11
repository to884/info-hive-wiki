# 判定モデル（任意）

判定モデルは、選択肢が決まった質問に答える小さなモデルである。たとえば「このセクションはクエリへの回答に役立つか？ A か B」「このエッジを裏づける文はどれか？ s1、s2、どれでもない」といった質問である。選択肢はコードが用意し、モデルはその中から1つを選んで確信度を返す。

コマンドの `<skill-root>` は、このスキルのディレクトリ（`SKILL.md` のあるディレクトリ）の実際のパスに置き換える。書き方は `SKILL.md` の「スクリプトのパス」にある。

判定レイヤーは既定では無効で、なくても wiki は同じように動く。ユーザーが求めた場合にだけ有効にする。

判定モデルに渡す質問文（`assets/decision-questions.json`）は英語のままで、日本語の wiki でどれだけ正しく判定できるかはまだ確かめていない（移植計画の方針 H）。評価用のフィクスチャも英語だけである。日本語の wiki で有効にすると、関係のある結果を誤って捨てるおそれがある。

## 規則

- 判定がページを書き込むことも、書き込みを止めることもない。読み取りの処理は fail-open で動く。書き込み時のチェックは、lint の検出結果を出すか、フラグを立てるだけである。
- fail-open とは、エラー、タイムアウト、無効な回答のときに、ツールが通常の結果を返し、`failed_open: true` を報告することである。
- ユーザーがそのバックエンドを有効にしていない限り、外部のバックエンドに wiki のテキストを送ってはならない。
- どの出力にも、バックエンド、モデル、`latency_ms`、閾値を記録する。
- ページの書き込み、統合、要約、質問への回答、スラッグの作成、frontmatter のチェックに判定モデルを使ってはならない。これらはコードかメインのエージェントが行う。

## チェック

| チェック | 使う場所 | 質問 | 結果 |
|---|---|---|---|
| D1 関連度ゲート | 検索 | 上位 20 行のそれぞれについて、このセクションはクエリへの回答に役立つか（A）、単語が共通しているだけか（B）？ | P(A) が閾値を下回る行を除く。結果が空なら、wiki に答えはない。 |
| D2 証拠のチェック | グラフの lint | 型付きエッジの証拠のテキストが、そのソースページに見つからない。エッジを裏づける文はどれか、それともどれでもないか？ | lint の検出結果 `evidence_unsupported` |
| D3 矛盾の洗い出し | lint（`wiki_decide.py contradictions`） | 同じエンティティについての2つのセクションは、裏づけ合うか、矛盾するか、新しい詳細を加えるか、無関係か？ | 読むべきペアの一覧。判断はエージェントが行う。 |
| D4 取り込み候補の絞り込み | 取り込み（`wiki_decide.py shortlist`） | 検索で見つかった候補のそれぞれについて、このソースはこのページに情報を加えるか、内容を変えるか？ | 先に読むべきページの順位付きの一覧 |
| D5 信頼できないテキストの洗い出し | `wiki_decide.py screen` | このテキストは AI システムに指示を出しているか？ | 出力の `flagged` が true になる。ページの書き込みは止めない。 |

評価に合格していないチェックは実験的なものである。その結果は事実ではなく、手がかりとして使う。

### D2 の照合と文の分け方

グラフの lint は、型付きエッジの `evidence` がソースの要約ページの本文に含まれているかを先に照合する。含まれていれば、判定モデルを呼ばずに裏づけありとする（`confidence` は 1.0）。照合の規則は次のとおりで、評価ループで判定役の引用を確かめるとき（`grounded_verdict`）も同じ規則を使う（`scripts/wiki_text.py` の `quote_in_text`）。

- 大文字と小文字を区別せず、空白の並びを 1 つにまとめる。
- 日本語の文字（かな、漢字、和文の記号、全角の英数字）どうしの間の改行と空白は取り除く。行をまたいだ日本語の文も一致する。
- 英数字の間の空白は残す。空白をすべて除くと、「now here」と「nowhere」を同じとみなしてしまうためである。
- NFKC はかけない。全角と半角の違いは吸収しないので、ソースが全角の「ＡＩ」なら `evidence` も「ＡＩ」と書く。

このため、`evidence` にはソースの文をそのまま写す。言い換えたり訳したりせず、1 文か文の一部にとどめる。

照合で見つからないエッジだけを判定モデルに渡す。そのとき、ソースの要約ページの本文を文に分け、番号を付けて渡す。英語の文は「.!?」とそのあとの空白で分け、日本語の文は「。！？」のあとで分ける（空白がなくても分ける）。句点のあとに閉じ括弧（」』）〉》】〕）が続くときは閉じ括弧のあとで分け、「？！」のように句点が続くときは最後の句点のあとで分ける。コードブロックは除く。渡すのは最大 60 文で、1 文は 300 文字で切る（`decision-questions.json` の `max_sentences` と `max_sentence_chars`）。

## バックエンド

| バックエンド | 実行場所 | モデル | キー | テキストがマシンの外に出るか |
|---|---|---|---|---|
| `local` | `127.0.0.1:8080` の `llama-server` と `LiquidAI/d1-2B-GGUF` | `d1-2B` | なし | 出ない |
| `typesafe` | `https://api.typesafe.ai` | `jev-1.14.0` | `TYPESAFE_API_KEY` | 出る（TypeSafe へ） |
| `openrouter` | `https://openrouter.ai/api` | `typesafe/jev-1.14` | `OPENROUTER_API_KEY` | 出る（OpenRouter と TypeSafe へ） |
| `liquid` | `https://api.liquid.ai/decisions` | `d1.1` | `LIQUID_API_KEY` | 出る（Liquid へ） |

補足:

- `llama-server` は `127.0.0.1` だけにバインドする。
- ライセンス: Liquid の d1 モデルは LFM Open License v1.0 である。無償で使えるのは年間売上 1,000 万ドルまでで、それを超える場合は Liquid から商用ライセンスを受ける必要がある。
- 料金（ベンダー公表値、2026 年 10 月）: Jev は入力 100 万トークンあたり 0.042 ドル、Liquid API の `d1.1` は入力 100 万トークンあたり 0.02 ドル。ローカルで使う場合は無料である。
- プライバシー: TypeSafe がデータを一切保持しないのは、エンタープライズプランだけである。Liquid は API のデータ保持について明らかにしていない。各チェックがどのテキストを送るかをユーザーに伝える（D1: クエリとセクションのテキスト、D2: エッジとページの文、D3: 2つのセクション、D4: ソースの冒頭と候補のセクション、D5: ページのテキスト）。

## 有効にする

バックエンドは、環境変数かフラグで設定する。既定は `off` である。

```bash
export INFO_HIVE_DECISIONS=local          # off | local | typesafe | openrouter | liquid
export INFO_HIVE_DECISIONS_URL=http://127.0.0.1:8080   # 任意。ベース URL
export INFO_HIVE_DECISIONS_MODEL=d1-2B                 # 任意。モデルを上書きする
python "<skill-root>/scripts/wiki_decide.py" check                   # バックエンドを試す
```

チェックは次のように使う。

```bash
uv run --script "<skill-root>/scripts/wiki_search.py" "軽減税率の対象" --json --decide [--decide-pool 20] [--decide-threshold 0.65]
uv run --script "<skill-root>/scripts/wiki_graph_lint.py" wiki/ --decide [--decide-threshold 0.55]
python "<skill-root>/scripts/wiki_decide.py" contradictions --wiki wiki/ [--max-pairs 40]
python "<skill-root>/scripts/wiki_decide.py" shortlist raw/<source-file> --wiki wiki/ [--top 30]
python "<skill-root>/scripts/wiki_decide.py" screen <file>
```

`wiki_decide.py` のグローバルなフラグは、`--backend`、`--model`、`--url`、`--timeout` である。閾値のフラグは、各サブコマンドの `--help` で確かめる。`--decide` を付けると、検索の JSON にトップレベルの `decision` ブロックと、各行の `decision` フィールドが加わる。`--decide` を付けなければ、出力は変わらない。

## 使う前に評価する

評価には、合成したフィクスチャだけを使う。評価のために、非公開の wiki のテキストを外部のバックエンドに送ってはならない。

次のコマンドは、プラグインのリポジトリ（info-hive-wiki）のルートで実行する。`eval/` はスキルではなくリポジトリにある。

```bash
python eval/decisions/run_eval.py --backend fake            # ネットワークを使わない
python eval/decisions/run_eval.py --backend local --out results.json
```

バックエンドごとに分けて報告する。閾値を決めるチェックでは、`run_eval.py` がフィクスチャを調整用（`tune`）と確認用（`test`）に分ける。閾値は調整用のデータだけで決め、確認用のデータで成績を確かめる。
