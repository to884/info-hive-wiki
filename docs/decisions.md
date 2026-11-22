# 判定モデル

判定モデルは、決められた選択肢から答えを選ぶ小さなモデルです。コードが選択肢を用意し、モデルが 1 つ選んで確率を返します。LLM Wiki では、5 つのチェックに使えます。

> **既定では無効**
>
> 判定レイヤーは任意で、既定では無効です。判定レイヤーがなくても、wiki は同じように動きます。判定バックエンドを有効にするまで、どこにも何も送られません。

## 規則

- 判定モデルがページを書くことはなく、書き込みを止めることもありません。書き込み時のチェックは、lint の検出結果かレビューフラグを追加するだけです。
- 読み取りの処理はフェイルオープンで動きます。エラー、タイムアウト、無効な回答のときは、`failed_open: true` を付けた通常の結果が返ります。
- ページの執筆、マージ、要約と、質問への回答は、引き続きメインのエージェントが行います。frontmatter、型、日付のチェックも、引き続きコードが行います。
- すべての出力に、バックエンド、モデル、レイテンシー、閾値が記録されます。
- 評価（eval）に合格していないチェックは実験的なものです。その結果はヒントとして扱います。

## 5つのチェック

| チェック | 使われる場所 | 問い | 得られるもの |
|---|---|---|---|
| D1 関連度ゲート | 検索 | 上位 20 行のそれぞれについて、このセクションはクエリに答える役に立つか、それとも単語が共通しているだけか | 閾値を下回る行が除かれます。結果が空なら、wiki に答えがないことがエージェントに伝わります。 |
| D2 証拠チェック | グラフの lint | 記録された証拠がソースページに見つからない場合、型付きエッジを裏づける文があるか。あればどの文か | lint の検出結果 `evidence_unsupported` |
| D3 矛盾のスクリーニング | `wiki_decide.py contradictions` | 同じエンティティについての2つのセクションは、互いを裏づけるか、矛盾するか、詳細を補うか、無関係か | エージェントが読むべきペア |
| D4 取り込み時の候補絞り込み | `wiki_decide.py shortlist` | 検索の各候補について、このソースはそのページに情報を加えるか、内容を変えるか | 先に読むべきページの順位付きリスト |
| D5 信頼できないテキストのスクリーニング | `wiki_decide.py screen` | このテキストは AI システムへの指示を含んでいるか | 閾値を超えると `flagged: true` になります。ファイルは変更しません。 |

## バックエンド

| バックエンド | 実行場所 | モデル | キー | データの送り先 |
|---|---|---|---|---|
| `local` | 手元のマシンの `llama-server` | Liquid `d1-2B`（GGUF） | 不要 | 手元のマシンから出ない |
| `typesafe` | TypeSafe API | `jev-1.14.0` | `TYPESAFE_API_KEY` | TypeSafe に送られる |
| `openrouter` | OpenRouter | `typesafe/jev-1.14` | `OPENROUTER_API_KEY` | OpenRouter と TypeSafe に送られる |
| `liquid` | Liquid API | `d1.1` | `LIQUID_API_KEY` | Liquid に送られる |

## プライバシー、ライセンス、コスト

各チェックがバックエンドに送る内容は次のとおりです。

| チェック | 送られるテキスト |
|---|---|
| D1 | クエリと、候補の各セクションのタイトル、見出し、先頭から最大 1,500 文字 |
| D2 | エッジと、ソースページの最大 60 文 |
| D3 | 2つのセクション |
| D4 | ソースと候補の各セクションの、先頭 2,000 文字 |
| D5 | ページのテキスト |

- TypeSafe: 顧客のデータを学習に使いません。データを保持しない設定は、エンタープライズプランでのみ使えます。
- Liquid API: API のトラフィックをどれだけ保持するかは明示されていません。
- ローカルの `llama-server`: テキストが手元のマシンから出ることはありません。`127.0.0.1` だけにバインドしてください。

ライセンス: Liquid d1 のモデルには LFM Open License v1.0 が適用されます。無料で使えるのは、年間売上高が 1,000 万ドル未満の組織に限られます。それを超える場合は、Liquid から商用ライセンスを取得します。

料金（ベンダー公表値、2026年10月時点）:

| バックエンド | 料金 |
|---|---|
| Jev（TypeSafe、OpenRouter） | 入力 100 万トークンあたり $0.042 |
| Liquid API `d1.1` | 入力 100 万トークンあたり $0.02 |
| ローカルの `d1-2B` | 無料 |

ふるい分けを行う検索1回で、約 20 件のリクエストが送られます。各リクエストには1つのセクションが入るので、検索1回で数千トークンの入力を使います。

## ローカルで有効にする

ローカルのバックエンドを起動します。キーは不要で、データは手元のマシンから外に出ません。

```bash
llama-server -hf LiquidAI/d1-2B-GGUF --host 127.0.0.1 --port 8080
export INFO_HIVE_DECISIONS=local
python skills/info-hive/scripts/wiki_decide.py check
```

または、外部の API のバックエンドを選びます。

```bash
export INFO_HIVE_DECISIONS=typesafe        # off | local | typesafe | openrouter | liquid
export TYPESAFE_API_KEY=...
```

任意: `INFO_HIVE_DECISIONS_URL` でベース URL を、`INFO_HIVE_DECISIONS_MODEL` でモデルを変えられます。`wiki_decide.py` のフラグ `--backend`、`--model`、`--url`、`--timeout` は、環境変数より優先されます。

チェックを使います。

```bash
uv run --script skills/info-hive/scripts/wiki_search.py "query" --json --decide
uv run --script skills/info-hive/scripts/wiki_graph_lint.py wiki/ --decide
python skills/info-hive/scripts/wiki_decide.py contradictions --wiki wiki/
python skills/info-hive/scripts/wiki_decide.py shortlist raw/new-source.md --wiki wiki/
python skills/info-hive/scripts/wiki_decide.py screen raw/new-clipping.md
```

`--decide` を付けなければ、検索とグラフの lint の出力は変わりません。

## 先に評価する

評価には、`eval/decisions/` 以下の合成フィクスチャだけを使います。テストのために、非公開の wiki のテキストを外部のバックエンドに送ってはなりません。

```bash
python eval/decisions/run_eval.py --backend fake                  # ネットワークを使わない
python eval/decisions/run_eval.py --backend local --out results.json
```

各バックエンドを、単純なベースライン（ふるい分けなし、またはテキストの完全一致）と比べます。閾値は、評価用に取り分けたデータ（held-out split）だけをもとに決めます。自分のクエリでテストするときは、`local` バックエンドを使います。

### 測定結果（2026-10-29）

既定の閾値は、`local` バックエンドで合成の評価を1回実際に実行した結果から決めました。構成は llama.cpp b11976 と `LiquidAI/d1-2B-GGUF:Q8_0`、語彙ベースの検索で、リクエストは 1,179 件、エラーは 0 件でした。生の数値は移植元のリポジトリのコミット `2d24ccf` にある `eval/decisions/results-local-d1-2b.json` に記録されています。測定結果のファイルは、このリポジトリには含まれていません。詳しくは [UPSTREAM.md](../UPSTREAM.md) を参照してください。

| チェック | 閾値 | 取り分けた半分での結果 |
|---|---|---|
| D1 関連度ゲート | 0.65 | 答えのないクエリで行が返る割合: 100% から 0% に。Recall@5: 1.0 から 1.0 に。 |
| D2 証拠チェック | 0.55 | 適合率 1.0、再現率 1.0 |
| D3 矛盾 | 0.5 | 適合率 1.0、再現率 1.0 |
| D5 信頼できないテキスト | 0.7 | 適合率 1.0、再現率 1.0 |

フィクスチャが小さいので、これらの数値は楽観的です。Jev は、実行時に有料のクレジットがなかったため測定していません。チェックを有効にする前に、使うバックエンドで評価を実行してください。
