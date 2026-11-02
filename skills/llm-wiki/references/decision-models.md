# 判定モデル（任意）

判定モデルは、選択肢が決まった質問に答える小さなモデルである。たとえば「このセクションはクエリへの回答に役立つか？ A か B」「このエッジを裏づける文はどれか？ s1、s2、どれでもない」といった質問である。選択肢はコードが用意し、モデルはその中から1つを選んで確信度を返す。

判定レイヤーは既定では無効で、なくても wiki は同じように動く。ユーザーが求めた場合にだけ有効にする。

## 規則

- 判定がページを書き込むことも、書き込みを止めることもない。読み取りの処理は fail-open で動く。書き込み時のチェックは、lint の検出結果かレビューフラグを追加するだけである。
- fail-open とは、エラー、タイムアウト、無効な回答のときに、ツールが通常の結果を返し、`failed_open: true` を報告することである。
- ユーザーがそのバックエンドを有効にしていない限り、ホスト型のバックエンドに wiki のテキストを送ってはならない。
- どの出力にも、バックエンド、モデル、`latency_ms`、閾値を記録する。
- ページの書き込み、統合、要約、質問への回答、スラッグの作成、frontmatter のチェックに判定モデルを使ってはならない。これらはコードかメインのエージェントが行う。

## チェック

| チェック | 使う場所 | 質問 | 結果 |
|---|---|---|---|
| D1 関連度ゲート | 検索（ローカルとホスト型） | 上位 20 行のそれぞれについて、このセクションはクエリへの回答に役立つか（A）、単語が共通しているだけか（B）？ | P(A) が閾値を下回る行を除く。結果が空なら、wiki に答えはない。 |
| D2 証拠のチェック | グラフの lint（ローカルとホスト型） | 型付きエッジの証拠のテキストが、そのソースページに見つからない。エッジを裏づける文はどれか、それともどれでもないか？ | lint の検出結果 `evidence_unsupported`、またはレビューフラグ |
| D3 矛盾の洗い出し | ローカルの lint（`wiki_decide.py contradictions`） | 同じエンティティについての2つのセクションは、裏づけ合うか、矛盾するか、新しい詳細を加えるか、無関係か？ | 読むべきペアの一覧。判断はエージェントが行う。 |
| D4 取り込み候補の絞り込み | ローカルの取り込み（`wiki_decide.py shortlist`） | 検索で見つかった候補のそれぞれについて、このソースはこのページに情報を加えるか、内容を変えるか？ | 先に読むべきページの順位付きの一覧 |
| D5 信頼できないテキストの洗い出し | ホスト型での `sources/` への書き込み、ローカルの `wiki_decide.py screen` | このテキストは AI システムに指示を出しているか？ | レビューフラグ `untrusted_text`。コミットはそのまま適用される。 |

評価に合格していないチェックは実験的なものである。その結果は事実ではなく、手がかりとして使う。

## バックエンド

| バックエンド | 実行場所 | モデル | キー | テキストがマシンの外に出るか |
|---|---|---|---|---|
| `local` | `127.0.0.1:8080` の `llama-server` と `LiquidAI/d1-2B-GGUF` | `d1-2B` | なし | 出ない |
| `typesafe` | `https://api.typesafe.ai` | `jev-1.14.0` | `TYPESAFE_API_KEY` | 出る（TypeSafe へ） |
| `openrouter` | `https://openrouter.ai/api` | `typesafe/jev-1.14` | `OPENROUTER_API_KEY` | 出る（OpenRouter と TypeSafe へ） |
| `liquid` | `https://api.liquid.ai/decisions` | `d1.1` | `LIQUID_API_KEY` | 出る（Liquid へ） |
| `hosted` | ホスト型 wiki のサーバー（`/api/v1/decide`） | サーバーの設定による | wiki のトークン | 出る（自分の Worker とそのバックエンドへ） |
| `workers-ai` | サーバーのみ: Workers AI 上の Jev | `typesafe/jev-1.14.0` | なし（AI Gateway のクレジットが必要） | Cloudflare の中にとどまる |

補足:

- `workers-ai` には、Cloudflare AI Gateway のクレジット（統合請求）が必要である。クレジットがないと、呼び出しは `2021: Insufficient AI Gateway credits` で失敗する。その場合 wiki は fail-open で動き、ゲートもフラグもかからない。
- サーバーで `typesafe`、`openrouter`、`liquid` を使う場合は、キーを Worker のシークレットとして追加する: `npx wrangler secret put TYPESAFE_API_KEY`。
- `llama-server` は `127.0.0.1` だけにバインドする。
- ライセンス: Liquid の d1 モデルは LFM Open License v1.0 である。無償で使えるのは年間売上 1,000 万ドルまでで、それを超える場合は Liquid から商用ライセンスを受ける必要がある。
- 料金（ベンダー公表値、2026 年 10 月）: Jev は入力 100 万トークンあたり 0.042 ドル、Liquid API の `d1.1` は入力 100 万トークンあたり 0.02 ドル。ローカルで使う場合は無料である。
- プライバシー: TypeSafe がデータを一切保持しないのは、エンタープライズプランだけである。Liquid は API のデータ保持について明らかにしていない。Workers AI 上の Jev はデータを保持しない。各チェックがどのテキストを送るかをユーザーに伝える（D1: クエリとセクションのテキスト、D2: エッジとページの文、D3: 2つのセクション、D4: ソースの冒頭と候補のセクション、D5: ページのテキスト）。

## ローカルで有効にする

バックエンドは、環境変数かフラグで設定する。既定は `off` である。

```bash
export LLM_WIKI_DECISIONS=local          # off | local | typesafe | openrouter | liquid | hosted
export LLM_WIKI_DECISIONS_URL=http://127.0.0.1:8080   # 任意。ベース URL
export LLM_WIKI_DECISIONS_MODEL=d1-2B                 # 任意。モデルを上書きする
python scripts/wiki_decide.py check                   # バックエンドを試す
```

チェックは次のように使う。

```bash
uv run --script scripts/wiki_search.py "query" --json --decide [--decide-pool 20] [--decide-threshold 0.65]
uv run --script scripts/wiki_graph_lint.py wiki/ --decide [--decide-threshold 0.55]
python scripts/wiki_decide.py contradictions --wiki wiki/ [--max-pairs 40]
python scripts/wiki_decide.py shortlist raw/<source-file> --wiki wiki/ [--top 30]
python scripts/wiki_decide.py screen <file>
```

`wiki_decide.py` のグローバルなフラグは、`--backend`、`--model`、`--url`、`--timeout`、`--profile` である。閾値のフラグは、各サブコマンドの `--help` を参照。`--decide` を付けると、検索の JSON にトップレベルの `decision` ブロックと、各行の `decision` フィールドが加わる。`--decide` を付けなければ、出力は変わらない。

## ホスト型 wiki で有効にする

wiki の設定は、所有者が `/setup` ページか `python3 scripts/wiki_remote.py settings --set <key>=<value>` で変更する。設定は次のとおり。

| 設定 | 意味 | 既定値 |
|---|---|---|
| `decisions.enabled` | 判定レイヤーを有効にする | `false` |
| `decisions.backend` | `workers-ai`、`typesafe`、`openrouter`、`liquid` のいずれか | `workers-ai` |
| `decisions.model` | 固定するモデルの ID | バックエンドの既定 |
| `decisions.search_gate` | `decide: true` を付けた検索だけでなく、すべての検索にゲートをかける | `false` |
| `decisions.screen_writes` | `sources/` 以下の新しいページや置き換えたページに D5 を実行する | `false` |
| `decisions.relevance_threshold` | 検索の行を残すための P(関連あり) | `0.65` |
| `decisions.screen_threshold` | レビューフラグを作るための P(信頼できない) | `0.7` |
| `decisions.timeout_ms` | 1回の呼び出しのタイムアウト | `4000` |

エージェントは、`wiki_search` には `decide: true` を、`wiki_remote.py search` と `lint` には `--decide` を渡す。レビューフラグは、`wiki_lint` の出力と `/ui/<wiki>/review` ページに出る。フラグは `wiki_remote.py review --resolve <id> --status accepted|dismissed` で解決する。

## 使う前に評価する

評価には、合成したフィクスチャだけを使う。評価のために、非公開の wiki のテキストをホスト型のバックエンドに送ってはならない。

```bash
python eval/decisions/run_eval.py --backend fake            # ネットワークを使わない
python eval/decisions/run_eval.py --backend local --out results.json
```

バックエンドごとに分けて報告する。閾値は、取り分けておいた分割のデータだけをもとに決める。
