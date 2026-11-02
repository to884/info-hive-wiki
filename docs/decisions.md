---
title: 判定モデル
description: 検索結果の関連度によるふるい分け、グラフの証拠のチェック、信頼できないテキストのスクリーニングを行う、任意の小さな判定モデル。バックエンド、プライバシー、ライセンス、コストも扱う。
---

# 判定モデル {#decision-models}

判定モデルは、選択肢の決まった問いに答える小さなモデルである。選択肢はコードが用意し、モデルはその中から1つを選んで確率を返す。LLM Wiki では、判定モデルを5つのチェックに使える。

::: info 既定では無効
判定レイヤーは任意で、既定では無効である。判定レイヤーがなくても、wiki は同じように動く。判定バックエンドを有効にするまで、どこにも何も送られない。
:::

## 規則 {#rules}

- 判定モデルがページを書くことはなく、書き込みを止めることもない。書き込み時のチェックは、lint の検出結果かレビューフラグを追加するだけである。
- 読み取りの処理はフェイルオープンで動く。エラー、タイムアウト、無効な回答のときは、`failed_open: true` を付けた通常の結果が返る。
- ページの執筆、マージ、要約と、質問への回答は、引き続きメインのエージェントが行う。frontmatter、型、日付のチェックも、引き続きコードが行う。
- すべての出力に、バックエンド、モデル、レイテンシー、閾値が記録される。
- 評価（eval）に合格していないチェックは実験的なものである。その結果はヒントとして扱う。

## 5つのチェック {#the-five-checks}

| チェック | 使われる場所 | 問い | 得られるもの |
|---|---|---|---|
| D1 関連度ゲート | 検索（ローカルとホスト型） | 上位 20 行のそれぞれについて、このセクションはクエリに答える役に立つか、それとも単語が共通しているだけか | 閾値を下回る行が除かれる。結果が空なら、wiki に答えがないことがエージェントに伝わる。 |
| D2 証拠チェック | グラフの lint（ローカルとホスト型） | 型付きエッジの証拠のテキストが、そのソースページに出てこない。エッジを裏づける文はどれか、あるいはないか | lint の検出結果 `evidence_unsupported`、ホスト型 wiki ではレビューフラグ |
| D3 矛盾のスクリーニング | ローカル（`wiki_decide.py contradictions`） | 同じエンティティについての2つのセクションは、互いを裏づけるか、矛盾するか、詳細を補うか、無関係か | エージェントが読むべきペア |
| D4 取り込み時の候補絞り込み | ローカル（`wiki_decide.py shortlist`） | 検索の各候補について、このソースはそのページに情報を加えるか、内容を変えるか | 先に読むべきページの順位付きリスト |
| D5 信頼できないテキストのスクリーニング | ホスト型での `sources/` へのコミット、ローカルの `wiki_decide.py screen` | このテキストは AI システムへの指示を含んでいるか | レビューフラグ `untrusted_text`。コミットはそのまま適用される。 |

## バックエンド {#backends}

| バックエンド | 実行場所 | モデル | キー | データの送り先 |
|---|---|---|---|---|
| `local` | 手元のマシンの `llama-server` | Liquid `d1-2B`（GGUF） | 不要 | 手元のマシンから出ない |
| `typesafe` | TypeSafe API | `jev-1.14.0` | `TYPESAFE_API_KEY` | TypeSafe に送られる |
| `openrouter` | OpenRouter | `typesafe/jev-1.14` | `OPENROUTER_API_KEY` | OpenRouter と TypeSafe に送られる |
| `liquid` | Liquid API | `d1.1` | `LIQUID_API_KEY` | Liquid に送られる |
| `workers-ai` | ホスト型 wiki 専用: Workers AI 上の Jev | `typesafe/jev-1.14.0` | 不要。ただし AI Gateway のクレジットが必要 | Cloudflare の中にとどまる |
| `hosted` | ローカルのスクリプトが、あなたのホスト型 wiki を呼び出す | ホスト型 wiki の設定による | wiki のトークン | あなたの Worker と、そのバックエンドに送られる |

ホスト型で最初に選ぶ候補は、Workers AI 上の Jev である。

::: warning Workers AI にはクレジットが必要
`workers-ai` バックエンドは、Cloudflare AI Gateway の統合請求を通じて `typesafe/jev-1.14.0` を呼び出す。アカウントには AI Gateway のクレジットが必要である。クレジットがないと、呼び出しは `2021: Insufficient AI Gateway credits` で失敗する。その場合、wiki はフェイルオープンで動き、検索はふるい分けられず、フラグも作られない。
:::

ほかのホスト型バックエンドでは、キーを Worker のシークレットとして追加する。

```bash
cd server
npx wrangler secret put TYPESAFE_API_KEY     # または OPENROUTER_API_KEY、LIQUID_API_KEY
```

## プライバシー、ライセンス、コスト {#privacy-licence-and-costs}

各チェックがバックエンドに送る内容は次のとおりである。

| チェック | 送られるテキスト |
|---|---|
| D1 | クエリと、候補の各セクションのタイトル、見出し、先頭から最大 1,500 文字 |
| D2 | エッジと、ソースページの最大 60 文 |
| D3 | 2つのセクション |
| D4 | ソースと候補の各セクションの、先頭 2,000 文字 |
| D5 | ページのテキスト |

- TypeSafe: 顧客のデータを学習に使わない。データを保持しない設定は、エンタープライズプランでのみ使える。
- Liquid API: API のトラフィックをどれだけ保持するかは明示されていない。
- Workers AI 上の Jev: データを保持しない。
- ローカルの `llama-server`: テキストが手元のマシンから出ることはない。`127.0.0.1` だけにバインドすること。

ライセンス: Liquid d1 のモデルには LFM Open License v1.0 が適用される。無料で使えるのは、年間売上高が 1,000 万ドル未満の組織に限られる。それを超える場合は、Liquid から商用ライセンスを取得する。

料金（ベンダー公表値、2026年10月時点）:

| バックエンド | 料金 |
|---|---|
| Jev（TypeSafe、OpenRouter、Workers AI） | 入力 100 万トークンあたり $0.042 |
| Liquid API `d1.1` | 入力 100 万トークンあたり $0.02 |
| ローカルの `d1-2B` | 無料 |

ふるい分けを行う検索1回で、約 20 件のリクエストが送られる。各リクエストには1つのセクションが入るので、検索1回で数千トークンの入力を使う。

## ローカルで有効にする {#turn-it-on-locally}

ローカルのバックエンドを起動する（キーは不要で、データはマシンから出ない）。

```bash
llama-server -hf LiquidAI/d1-2B-GGUF --host 127.0.0.1 --port 8080
export LLM_WIKI_DECISIONS=local
python skills/llm-wiki/scripts/wiki_decide.py check
```

または、ホスト型のバックエンドを選ぶ。

```bash
export LLM_WIKI_DECISIONS=typesafe        # off | local | typesafe | openrouter | liquid | hosted
export TYPESAFE_API_KEY=...
```

任意: `LLM_WIKI_DECISIONS_URL` でベース URL を、`LLM_WIKI_DECISIONS_MODEL` でモデルを変えられる。`wiki_decide.py` のフラグ `--backend`、`--model`、`--url`、`--timeout` は、環境変数より優先される。

チェックを使う。

```bash
uv run --script skills/llm-wiki/scripts/wiki_search.py "query" --json --decide
uv run --script skills/llm-wiki/scripts/wiki_graph_lint.py wiki/ --decide
python skills/llm-wiki/scripts/wiki_decide.py contradictions --wiki wiki/
python skills/llm-wiki/scripts/wiki_decide.py shortlist raw/new-source.md --wiki wiki/
python skills/llm-wiki/scripts/wiki_decide.py screen raw/new-clipping.md
```

`--decide` を付けなければ、検索とグラフの lint の出力は変わらない。

## ホスト型 wiki で有効にする {#turn-it-on-for-a-hosted-wiki}

`/setup` を開いてサインインし、その wiki の判定の設定を変える。または、管理者トークンを使って CLI で設定する。

```bash
python3 skills/llm-wiki/scripts/wiki_remote.py settings --set decisions.enabled=true
python3 skills/llm-wiki/scripts/wiki_remote.py settings --set decisions.backend=workers-ai
```

| 設定 | 意味 | 既定値 |
|---|---|---|
| `decisions.enabled` | 判定レイヤーを有効にする | `false` |
| `decisions.backend` | `workers-ai`、`typesafe`、`openrouter`、`liquid` のいずれか | `workers-ai` |
| `decisions.model` | 固定するモデルの ID | バックエンドの既定値 |
| `decisions.search_gate` | `decide: true` を付けた検索だけでなく、すべての検索をふるい分ける | `false` |
| `decisions.screen_writes` | `sources/` 以下で新しく作られたページや置き換えられたページをスクリーニングする | `false` |
| `decisions.relevance_threshold` | 検索の行を残すのに必要な確率 | `0.65` |
| `decisions.screen_threshold` | `untrusted_text` フラグを立てる確率 | `0.7` |
| `decisions.timeout_ms` | 1回の呼び出しのタイムアウト | `4000` |

レビューフラグは、`wiki_lint` の結果と `/ui/<wiki>/review` に表示される。そこで承認か却下をするか、`wiki_remote.py review --resolve <id> --status accepted` を使う。

## 先に評価する {#evaluate-first}

評価には、`eval/decisions/` 以下の合成フィクスチャだけを使う。テストのために、非公開の wiki のテキストをホスト型のバックエンドに送ってはならない。

```bash
python eval/decisions/run_eval.py --backend fake                  # ネットワークを使わない
python eval/decisions/run_eval.py --backend local --out results.json
```

各バックエンドを、単純なベースライン（ふるい分けなし、またはテキストの完全一致）と比べる。閾値は、評価用に取り分けたデータ（held-out split）だけをもとに決める。自分のクエリでテストするときは、`local` バックエンドを使う。

### 測定結果（2026-10-29） {#measured-results-2026-10-29}

既定の閾値は、`local` バックエンドで合成の評価を1回実際に実行した結果から決めた。構成は llama.cpp b11976 と `LiquidAI/d1-2B-GGUF:Q8_0`、語彙ベースの検索で、リクエストは 1,179 件、エラーは 0 件だった。生の数値は `eval/decisions/results-local-d1-2b.json` にある。

| チェック | 閾値 | 取り分けた半分での結果 |
|---|---|---|
| D1 関連度ゲート | 0.65 | 答えのないクエリで行が返る割合: 100% から 0% に。Recall@5: 1.0 から 1.0 に。 |
| D2 証拠チェック | 0.55 | 適合率 1.0、再現率 1.0 |
| D3 矛盾 | 0.5 | 適合率 1.0、再現率 1.0 |
| D5 信頼できないテキスト | 0.7 | 適合率 1.0、再現率 1.0 |

フィクスチャが小さいので、これらの数値は楽観的である。Jev は、実行時に有料のクレジットがなかったため測定していない。チェックを有効にする前に、使うバックエンドで評価を実行すること。
