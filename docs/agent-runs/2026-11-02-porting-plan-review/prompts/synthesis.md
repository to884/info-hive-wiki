あなたは総括の書き手です。
同じタスクに 4 体のエージェントが独立して取り組み、その後、各エージェントが自分以外の成果をレビューしました。
成果とレビューを読み、作者（このリポジトリの所有者）が判断できる総括を 1 本書いてください。
あなたの最終メッセージだけが `summary.md` として保存されます。
エージェントは記号で呼びます（エージェント A、エージェント B、エージェント C、エージェント D）。記号とモデルの対応は末尾に表として付けるので、本文ではモデル名で優劣を語らないでください。

## タスク

# タスク

4 モデルで計画をレビューする。

## 対象

- 計画書: `C:\Works\info-hive\docs\porting-plan.html`（HTML。ブラウザーで開かずにソースを読めばよい）
- 移植元: `C:\Works\llm-wiki-plugin`（Claude Code プラグイン llm-wiki v4.0.1。作業ツリーに未コミットの日本語訳がある）

計画書は、llm-wiki-plugin を日本語に特化して `info-hive` リポジトリへ移植する手順をまとめたものである。

## レビューの観点

1. 事実の誤り: 計画書が述べるコードの挙動、行番号、ファイル名、モデルの仕様を、移植元のコードと照合して確かめる。
2. 抜け: 日本語で壊れる・劣化するのに計画書に載っていない箇所、移植に必要なのに段階に入っていない作業。
3. 段階の順序と依存: P0〜P6 の順序と依存関係に無理がないか。
4. 完了の判定とリスク: 判定できない条件、足りないリスク。
5. 方針 A〜J への意見。承認待ちの 4 項目（7 節）と、作者が質問への回答で決めた方針 C〜J のどちらも批判の対象にしてよい。反対するときは、代わりの案と理由を示す。

## 成果に含めるもの

- 指摘の一覧。各指摘に、重大度（高・中・低）、計画書の該当箇所（節や段階）、根拠（移植元のファイルと行番号）、直し方を付ける。
- 重大度の高い順に並べる。
- 根拠を確かめられなかった指摘は、その旨を明記する。

リポジトリのファイルは書き換えない。


## 成果

### エージェント A

## 結論

計画は大筋で妥当です。方針 A〜J の多くにも賛成します。ただし、P0 に着手する前に直すべき点が 3 つあります。

- **キャッシュの世代管理**: frontmatter の解析器・トークン化・分割を変えても、既存の解析キャッシュとベクトル索引が古い結果を返し続けます。計画にこの対処がありません。
- **英語の非劣化条件**: 既定の埋め込みモデルを多言語の小型モデルに替えると、「英語の成績が下がっていない」という完了条件と衝突するおそれが高いです。しかも `--gate` は相対比較なので、劣化を検出できません。
- **単漢字のクエリ**: 方針 E の bigram の定義のままでは、「税」「株」「鍵」のような単漢字のクエリが文書中の熟語（「消費税」など）に当たりません。

このほか、事実の誤りや内部の矛盾が見つかりました。

- 行番号は v4.0.1 ではなく作業ツリーの値です。
- 方針 H の「第 1 段階」と P4 が食い違っています。
- P0 の前提に挙がっている承認待ちの項目が、7 節の一覧と一致しません。
- 方針 C の「設定ファイル」は、ホスト型を削ると使い道がなくなります。
- 付録から漏れている箇所があります。

指摘は重大度の高い順に下に並べます。

### 指摘の一覧（重大度順）

| # | 重大度 | 該当箇所 | 指摘 | 根拠 | 直し方 |
|---|---|---|---|---|---|
| 1 | 高 | P1・P3・リスク、完了判定「既存の wiki を変換なしで」 | 解析キャッシュは `schema: 1` で固定されています。ファイルの sha256 が一致すると、キャッシュにある `meta` と `sections` をそのまま使います。このため P1 で解析器（「、」区切り、NFKC、BOM）を直しても、既存の wiki では古い解析結果が黙って使われます。ベクトル索引の鍵（`section_content_hash`）もモデル名と本文だけでできているので、e5 の接頭辞や段落分割を入れても再計算されません。 | `wiki_search.py:120-135`（schema 1）、`:229-234`（sha256 が一致すると再利用）、`:409-411`（ハッシュ＝モデル名＋本文）、`:60` `VECTOR_INDEX_SCHEMA = "2"`、`:447-460` | P1 と P3 の作業に「解析キャッシュの schema を上げる」「`VECTOR_INDEX_SCHEMA` を上げ、索引の meta に接頭辞・分割方式・トークン化の版を含める」を加えます。リスク表にも載せます。完了判定には「キャッシュありで P1 の前後を比べ、結果が非キャッシュ時と一致する」を加えます（既存の `cache_invariant` を日本語の評価セットでも回す）。 |
| 2 | 高 | 方針 F、完了判定「英語の --gate が通り、成績が下がっていない」 | `--gate` は「section ≥ page」「hybrid ≥ section」「hybrid の誤検出率 ≤ section」という相対比較だけで、移植前の値とは比べません。gate が通っても劣化は否定できません。さらに、多言語の小型モデルは英語専用の bge-small-en-v1.5 より英語の精度が落ちる見込みが高く（推測。MTEB の一般的な傾向による）、「下がっていない」を厳密に満たせない可能性があります。依存がないと hybrid の比較を飛ばす点も問題です。 | `eval/retrieval/run_eval.py:195-199`（hybrid を飛ばす）、`:213-221`（相対比較だけの gate）、`wiki_search.py:59` | P2 で v4.0.1 の英語の評価値（recall@5・@10、MRR、誤検出率）を絶対値で記録し、許容する劣化幅（例: recall@10 で −0.03 まで）を完了判定に書きます。CI では hybrid を必須にし、飛ばしたら失敗にします。許容幅に収まらない場合に備えて、英語だけの wiki では bge-small-en を選べる設定を用意する案も検討します。 |
| 3 | 高 | 方針 E、P3 | 「1 文字だけの並びは 1 文字で」という定義では、文書中の「消費税」は「消費」「費税」になり、クエリ「税」の 1-gram と一致しません。日本語には単漢字の語（税、株、鍵、型、値など）が多くあります。文字種の境目（「の機」のような、ひらがなと漢字にまたがる bigram）の扱いも書かれていません。 | 方針 E の本文。コードはまだありません。現行の実装は `wiki_search.py:57, 94-95` | トークン化の仕様を次のように詳しく書きます。①文字種（漢字・ひらがな・カタカナ。々、ー、拡張 B の 𠮷 を含む）の連続で区切る。②漢字は unigram と bigram の両方を索引に入れる。③ひらがなだけの bigram は、索引から外すかどうかを P3 で評価する。P2 の境界ケースに「単漢字のクエリ」を加えます。 |
| 4 | 中 | 方針 H と P4 | 方針 H は「D2 の文分割（。！？）と空白の扱いは第 1 段階で直す」と書いていますが、付録と P4 では D2 を P4 に置いています。P2 で `test_decisions.py` に足す日本語のケースは、P4 が終わるまで失敗します。 | 計画 3 節の H、4 節の P4、8 節「D2 の文分割と照合 … P4」 | 「第 1 段階」が初回リリースのことなのか P1 のことなのかを明記します。P2 で足すテストは、期待する失敗（expectedFailure）として入れます。 |
| 5 | 中 | 方針 C、P0、完了判定「設定ファイルと環境変数が移植元と別」 | `~/.config/llm-wiki/remote.json` と `LLM_WIKI_CONFIG` を使うのは、ホスト型の `wiki_remote.py` と hosted バックエンドだけです。方針 G でこれらを削ると、info-hive には設定ファイルがなくなります。改名の対象として本当に共有されている状態は、モデルのキャッシュ `~/.cache/llm-wiki/fastembed` です。 | `wiki_remote.py:14, 223-227`、`wiki_decide.py:16, 176-189, 215-217`、`wiki_search.py:424-428`、`retrieval-setup.md:26, 44`、`scaling-playbook.md:46`、`commands/wiki/upgrade.md:25` | `LLM_WIKI_CONFIG` は改名せず、`LLM_WIKI_URL` と `LLM_WIKI_TOKEN` と一緒に削ります。キャッシュの場所は `~/.cache/info-hive/fastembed` にします。方針 F の「大きいモデルを設定で選べる」の手段（例: 環境変数 `INFO_HIVE_EMBED_MODEL`）をここで決めます。 |
| 6 | 中 | P0「CI に Windows のジョブを追加」、完了判定「CI が Linux と Windows の両方で通る」 | P0 の時点では文字コードがまだ直っていないので、Windows のジョブは失敗します。既存のテストには POSIX 前提のものがあります（shebang 付きの偽の実行ファイルを chmod して起動する）。評価用のコードにも、`encoding` を指定しない読み書きや `text=True` が多く残っています。計画の付録はスクリプトだけを数えていて、テスト側の修正が抜けています。 | `eval/evolution/test_adapters.py:71, 104, 193`、`eval/retrieval/run_eval.py:193`（`QUERIES.read_text()`）、`eval/decisions/test_decisions.py:40-42, 488-490`、`eval/evolution/test_evolution.py:41-95` | Windows のジョブは P1 で追加します（P0 では失敗を許容する扱いにします）。P1 の作業に「eval/ 配下の読み書きと subprocess を UTF-8 にする」と「POSIX 前提のテストを Windows では飛ばすか書き直す」を加えます。 |
| 7 | 中 | 8 節「ensure_ascii」（見た目に分類）、P1 | 評価ループは `json.dumps(pattern["sources"])` を、ensure_ascii の既定のまま frontmatter の `sources:` に書き込みます。日本語のファイル名は `\u6a5f…` になり、手書きの frontmatter 解析器はこれをデコードしません。表示の問題ではなく、データが壊れます。 | `wiki_evolve_loop.py:452, 458, 659`、`wiki_search.py:81-83` | 重大度を「壊れる」に上げます。ここは P1 の最後ではなく、P1 の最初に直します。書き込む先はファイルなので、cp932 で落ちる心配はありません。 |
| 8 | 中 | 8 節「encoding を指定しない読み書き」 | `wiki_evolve_loop.py:429` の `path.write_text(encode(record))` が一覧から漏れています。`encode` は ensure_ascii=False なので、Windows では cp932 で書かれます。直後の `capture()` は `read_json`（UTF-8 で読む）なので、日本語を含む記録で失敗します。`wiki_evolve_agent.py:208` の `text=True` も漏れています（影響は小さい）。 | `wiki_evolve_loop.py:427-430`、`wiki_evolve.py:45-50, 153-154` | 一覧に加えます。P1 の完了の確認は、行番号の一覧ではなく、`grep` で `encoding=` のない `read_text`／`write_text`／`open`／`text=True` が 0 件になることで判定します。 |
| 9 | 中 | P2「test_decisions.py に日本語のケースを足す」 | 判定テストで使う偽のバックエンドは、`[a-z0-9]+` で語の重なりを計算します。日本語の主張と根拠は重なりが 0 になるので、日本語のケースを足しても判定ロジックを検証できません。 | `eval/decisions/fake_backend.py:34-39` | P2 か P4 に「fake_backend のトークン化を方針 E と同じものにする」を加えます。 |
| 10 | 中 | P1（frontmatter・正規化）、方針 B | frontmatter の解析器が 2 系統あります。検索・lint・統計は手書きの解析器、グラフの 2 本は PyYAML です。「、」区切りや NFKC を手書き側だけに入れると、グラフ側とタグの解釈が食い違います。リンクの正規化も 5 本のスクリプトに複製されています。計画の一覧にも `wiki_stats.py` の `link_stem` が入っていません。 | `wiki_graph_extract.py:103`、`wiki_graph_lint.py:81`（yaml.safe_load）、`wiki_search.py:65-91`、`wiki_lint.py:81-108`、`wiki_stats.py:25-`（link_stem） | 共通のモジュール（例: `wiki_text.py`。正規化、トークン化、frontmatter の後処理、リンクの鍵）を作って各スクリプトから import します。`setup_wiki.py` が `wiki_search` を import している前例があるので、`uv run --script` でも動きます。グラフ側は、YAML で読んだ値にも同じ後処理をかけます。 |
| 11 | 中 | 4 節の依存（P5 は P0 のあとならよい） | P5 で書き直す文書には、P3 と P4 の結果が入ります。モデル名と次元数（`SCHEMA.md.template:90`、`retrieval-setup.md:18, 43, 62`。62 行目は「モデルが bge-small-en-v1.5 で 384 次元」を確認手順として書いている）や、lint の項目名とページの上限（`page-conventions.md:51-53`、`ingest-workflow.md:23, 75, 92`、`commands/wiki/stats.md:8`）です。 | 左の各ファイルと行 | P5 の前提を「P0（下書きの開始）、P3 と P4（確定）」に直します。 |
| 12 | 中 | 5 節「日本語の評価セットで P2 の基準を上回る」 | 基準はほぼ 0 件なので、この条件は何をしても満たせます。20〜30 ページ・約 30 問の評価セット（英語版は 21 ページ・32 問）では、1 問で recall が約 3 ポイント動き、小さな差は誤差に埋もれます。同じ人が書いた評価セットでモデルとしきい値を調整するので、過学習のおそれもあります。 | `eval/retrieval/queries.json`（32 問）、`eval/retrieval/corpus/`（21 ページ）。計算は 1/32 ≈ 0.031 | 完了判定には絶対値の目標を書きます（値は P2 のあとで決めると明記）。クエリを調整用と検証用に分け、しきい値は調整用だけで決めます。「0.3 GB 程度」も「0.35 GB 以下」のように判定できる形にします。 |
| 13 | 中 | 7 節（スキルのディレクトリの改名、承認待ち） | 「スキル名だけ変えてディレクトリは残す」という代案は、Agent Skills の仕様（name は親ディレクトリ名と一致させる）に反する可能性があります（記憶にもとづく。今回は確認していません）。改名するなら、`eval/evolution/workflows/` にある移植元のパスと sha256 の記録もずれます。`build_suite.py` は `git show 06369b2:skills/llm-wiki/...` を実行するので、移植元の履歴がないと動きません。 | `eval/evolution/workflows/build_suite.py:24`、`source-manifest.json`、`frozen-runtime.patch` | 改名には賛成です。`eval/evolution/workflows/` は移植元の研究の記録なので、取り込まずに UPSTREAM.md から参照するよう P0 に加えます。 |
| 14 | 低〜中 | P4「lint の項目名を日本語にする」 | `--json` のキー（`orphans`、`broken_links` など）まで訳すと、出力を読むエージェントやテストとの契約が壊れ、方針 B に反します。 | `wiki_lint.py:153-165, 279-291` | 訳すのは表示用のラベル（`render_text` の中）だけで、JSON のキーは訳さないと明記します。方針 B の「変えてはいけないもの」にも、スクリプトの JSON 出力のキーを加えます。 |
| 15 | 低 | P0 の前提 | 「7 節の承認待ちの 4 項目（方針 A、C、G、J）」とありますが、7 節の承認待ちは A、B、スキルのディレクトリ名、docs サイトの 4 つです。C、G、J は確定済みです。 | 計画 4 節の P0 と 7 節 | 前提を「方針 A・B、スキルのディレクトリ名、docs サイト」に直します。 |
| 16 | 低 | 8 節の冒頭「行番号は v4.0.1 時点の値」 | 行番号は、未コミットの訳を含む作業ツリーの値です。HEAD（2d24ccf）とは、訳で docstring の行数が変わった 9 本でずれます。たとえば HEAD では `Empty query` が 732 行目で作業ツリーでは 731 行目、`wiki_lint.py` の `line_count` は HEAD 135 行目と作業ツリー 130 行目です。 | `git archive HEAD` と作業ツリーを比べた結果。行数は init_wiki 262→260、wiki_lint 367→362 など | 「未コミットの訳を含む作業ツリーの行番号」と書き直します。方針 J で取り込むのは作業ツリーなので、値はそのまま使えます。 |
| 17 | 低 | 2 節「変えてはいけないもの」の「アップグレード用の見出し 6 つ」 | 6 つの目印のうち見出しは 4 つ（Skill evolution、Optional graph metadata、Graph layer、Retrieval）です。残りの 2 つは部分文字列で照合する箇条書きの行（`Graph lint + extract`、`- Semantic backend: local FastEmbed + sqlite-vec`）です。 | `init_wiki.py:41-78, 101-104`、`SCHEMA.md.template:90, 120` | 「見出し 4 つと照合用の文字列 2 つ」に直します。P5 で、この 2 行の英語の前置きを残すよう書きます。P3 でモデル名を変えても、この行の前置きは変えません。 |
| 18 | 低 | 2 節「Python は訳の前後で構文木が同一」 | 同一なのは docstring を除いた場合だけです。13 本のスクリプトが `argparse` の `description=__doc__` を使っているので、`--help` はすでに日本語になっていて、— を含みます。cp932 のパイプへ出すと落ちます。 | AST の比較（全 29 ファイルで、docstring 込みだと不一致、docstring を除くと一致）。`wiki_lint.py:2, 337`、`wiki_search.py:910` など | 記述を直します。P1 の完了判定に「全スクリプトの `--help` を cp932 のパイプで実行して落ちない」を加えます。 |
| 19 | 低 | P0「29 ファイルにある `${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki/…`」 | 29 という数を再現できませんでした。`${CLAUDE_PLUGIN_ROOT}/skills/llm-wiki` を含むファイルは 10 本です。`skills/llm-wiki` を含むファイルは、server/ と research/ を除いて 53 本、さらに docs/ を除くと 37 本です。`llm-wiki` を含むファイルは 91 本です。 | `grep -rl` の結果 | 作業量は数ではなく、`grep` で 0 件になることで判定します。 |
| 20 | 低 | 8 節「判定の hosted バックエンド」 | `wiki_decide.py` の 176 行目（`read_remote_profile` の定義）、215-217 行目（`client_from_config` の hosted の分岐）、681 行目（`--profile`）が漏れています。 | `wiki_decide.py` の該当行 | 一覧に加えるか、P0 の最後の検索に `hosted`、`profile`、`wiki_remote` を含めます。 |
| 21 | 低 | P6「バージョンを書く場所（package.json …）」 | docs サイトを外す推奨（7 節）を採ると、`package.json` はドキュメント用のパッケージなので不要になります。`pnpm-lock.yaml`、`deploy-docs.yml`、CI の docs ジョブ、`docs/.vitepress/config.ts` の版も同じです。 | `package.json`（name: llm-wiki-plugin-docs）、`CONTRIBUTING.md:134-145`、`wiki-tests.yml` の docs ジョブ | docs サイトについての決定に合わせて、P6 のバージョンの場所を plugin.json と marketplace.json の 2 か所と CHANGELOG に絞ります。 |
| 22 | 低 | 5 節「ホスト型への参照が残っていない」 | P6 で CHANGELOG に削った機能を書くので、CHANGELOG と UPSTREAM.md には `/wiki:migrate` などが必ず残ります。 | 計画 P6 | 判定から CHANGELOG と UPSTREAM.md を除外すると明記します。 |
| 23 | 低 | 方針 D | NFC では `ＡＩ.md` と `AI.md` が別のスラッグのままです。Windows の大文字小文字を区別しないファイルシステム、Windows で禁止の文字、Linux のファイル名の上限 255 バイト（日本語で約 85 文字）についても書かれていません。 | `wiki_lint.py:167-171`（重複スラッグの判定は完全一致） | lint に「NFKC と casefold で衝突するスラッグ」の警告を加えます。P5 のスラッグの規則に、禁止文字と長さの上限を書きます。 |
| 24 | 低 | 2 節「4 か月で 6 リリース」 | CHANGELOG では 2026-07-02 以降に 9 リリースあります（2.0.5〜2.0.7、3.0.0、3.1.0、3.1.1、3.2.0、4.0.0、4.0.1）。コミット数の 36 は合っています（マージを含む）。 | `git log --since=2026-08-02`、CHANGELOG の見出し | 数え方を明記します。結論（更新が速い）は変わりません。 |
| 25 | 低 | 1 節と P0（範囲） | README にある Codex・Cursor などへの導入（`npx skills add`）と、評価ループの他エージェント用アダプターを残すかどうかが決まっていません。 | `README.md:43, 58-61, 88` | 範囲を 1 節に書きます。 |

## 根拠

- **キャッシュ（#1）**: `collect_pages` は、キャッシュの `sha256` がファイルと一致すると `meta`、`links`、`sections` をキャッシュから使い、`parse_frontmatter` を呼びません（`wiki_search.py:229-234`）。キャッシュを捨てる条件は、`schema != 1` のときだけです（`:127`）。ベクトル索引は、`semantic_meta` の schema、model、dimension が変わったときだけ作り直されます（`:447-460`）。
- **gate（#2）**: `run_eval.py:213-221` の比較は、page・section・hybrid の 3 方式どうしの比較だけで、過去の値を一切参照しません。
- **トークン化（#3）**: 現行は `TOKEN_RE = [a-z0-9]+`（`wiki_search.py:57`）なので、日本語だけのクエリはトークンが 0 個になり、731 行目で「Empty query.」を出して終わります。埋め込み検索には進みません。ここは計画の記述どおりです。
- **D2（#4）**: `SENTENCE_END_RE = (?<=[.!?])\s+`（`wiki_decide.py:355`）、`s[:max_chars]`（300 文字。`:401`）、段落内の改行を `" ".join(para)` で連結する処理（`:375`）を確かめました。計画の症状の説明は正しいです。
- **設定ファイル（#5）**: `.config/llm-wiki` という文字列は、`wiki_remote.py` にしか出てきません（grep の結果）。
- **計画の記述のうち確かめて正しかったもの**:
  - TOKEN_RE、`LOCAL_EMBED_MODEL = "BAAI/bge-small-en-v1.5"`、`MAX_COSINE_DISTANCE = 0.35`
  - frontmatter の正規表現が ASCII の `:` しか受け付けないこと、BOM 付きで frontmatter が一致しないこと（`wiki_lint.py:83`、`wiki_search.py:56`）
  - Shift_JIS を読み飛ばすこと（`wiki_search.py:238-241` は黙って飛ばす。`wiki_lint.py` は read_errors として報告する）
  - 大文字で始まる語句だけを提案すること（`wiki_lint.py:38`）
  - `text.split()` で語数を数えること（`wiki_stats.py:100`）
  - 行数の上限がスクリプトに直接書かれていること（`wiki_lint.py:130`、`wiki_stats.py:146`）
  - 未コミットの訳が 94 ファイル、コマンドが 10 本、references が 12 本であること
  - 判定レイヤーが既定で無効なこと（`wiki_decide.py:203`）。方針 H の「無効のまま」は現状の動作で、新たな対策ではありません。

## 検討した代替案

- **方針 A（コピーして独立させる）**: 独立させることには賛成です。ただし、ファイルのコピーより、フォークを履歴ごと info-hive に取り込み、`upstream` を remote として残す方法を推します。マージはせず、必要な修正だけを `git cherry-pick` や `git diff 2d24ccf..upstream/vX` で取り込めます。`build_suite.py:24` のように移植元の履歴を前提にするコードも動き続けます。履歴を持たない独立したリポジトリにする理由が、AGENTS.md の運用ルール以外に見当たりません。
- **方針 B**: 賛成です。対象に、照合用の文字列 2 つと、各スクリプトの `--json` 出力のキーを加えるべきです（#14、#17）。
- **方針 C**: `/wiki:*` を維持することには賛成です。設定ファイルの項目は、モデルのキャッシュの場所の改名に置き換えます（#5）。
- **方針 D**: 賛成です。NFKC での衝突を警告する lint を加えます（#23）。
- **方針 E**: bigram を既定にすることには賛成です。辞書が要らず、依存も増えません。ただし、漢字の unigram を併用することと、文字種で区切ることを仕様に入れます（#3）。形態素解析だけにする案は、依存と Windows での導入の手間が増えるので採りません。
- **方針 F**: 候補に、日本語に特化した小型モデル（例: cl-nagoya/ruri-v3-30m・70m。長い入力に対応し、0.3 GB 以下に収まる見込み。サイズ、ONNX 化できるか、fastembed の `add_custom_model` で動くかは今回確かめていません）を加えて比べる価値があります。MiniLM の入力上限の 128 トークンを避けられる可能性があります。一方で、英語の成績は落ちる見込みなので、#2 の許容幅とあわせて判断します。
- **方針 G**: 賛成です。
- **方針 H**: 賛成ですが、段階の記述を直します（#4）。
- **方針 I**: 賛成です。数える範囲（frontmatter とコードブロックを含めるか）を定義します。
- **方針 J**: 賛成です。P0 に「訳を取り込んだ状態で、既存のテストが Linux で通ることを確かめる」を加えます。docstring が `--help` に出るので、動作が変わっているためです（#18）。
- **docs サイト**: 外す推奨に賛成です。外すなら、関連するファイル（package.json、pnpm-lock、deploy-docs.yml、docs ジョブ）も P0 で一緒に削ります（#21）。

## 前提と不確実性

- 次の点は確かめていません。
  - fastembed 0.8.0 のモデル一覧とサイズ（計画の記述を信用しています）。
  - fastembed が MiniLM を実際に何トークンで切るか。tokenizer の `model_max_length` が 512 なら、128 トークンで切らずに、学習範囲の外の長さを入力している可能性があります（推測）。P3 で実測が必要です。
  - fastembed の MiniLM が量子化された版かどうか。118M パラメーターを fp32 で持つと約 0.47 GB になり、0.22 GB という数字と合いません（推測）。e5-small を比べるときも、量子化した版で条件をそろえる必要があります。
- Claude Code でプラグイン名と `/wiki:*` の名前空間がどう決まるかは、動かして確かめていません。移植元の README の記述から、名前空間は `commands/wiki/` というディレクトリから来ると推測しています。
- Agent Skills の仕様が「name は親ディレクトリ名と一致させる」と定めている点は、記憶にもとづいていて、今回は確認していません（#13）。
- 作者の判断が要る点は次の 4 つです。
  - 英語の劣化をどこまで許容するか（#2）
  - 方針 H の「第 1 段階」の意味（#4）
  - 他のエージェントへの導入を範囲に含めるか（#25）
  - 履歴ごと取り込むか（方針 A の代案）

## 参照

- 計画書: `C:\Works\info-hive\docs\porting-plan.html`（HTML のテキストを抽出して全文を読んだ）
- 移植元の作業ツリー（`C:\Works\llm-wiki-plugin`）:
  - `skills/llm-wiki/scripts/`: wiki_search.py（1-135、160-345、395-640、720-740、910、928）、wiki_lint.py（全体）、wiki_stats.py（全体）、wiki_decide.py（1-30、55-225、340-410、505-660、676-681）、wiki_evolve.py（45-85、153-180、240-325、565-580）、wiki_evolve_loop.py（425-475、655-668）、init_wiki.py（36-160）、setup_wiki.py（全体）、wiki_graph_extract.py（53-140）、wiki_graph_lint.py（60-112、327-400、490-526）
  - `skills/llm-wiki/SKILL.md`（frontmatter）、`assets/SCHEMA.md.template`、`references/retrieval-setup.md`・`page-conventions.md`・`ingest-workflow.md` などの該当行、`commands/wiki/ingest.md`
  - `eval/retrieval/run_eval.py`（140-235）、`queries.json`、`corpus/`、`eval/decisions/fake_backend.py`（20-40）、`eval/evolution/test_adapters.py`（60-80、193）、`eval/evolution/workflows/build_suite.py`、`source-manifest.json`
  - `.github/workflows/wiki-tests.yml`、`.claude-plugin/plugin.json`・`marketplace.json`、`package.json`、`CONTRIBUTING.md`（134-208）、`README.md` と `CHANGELOG.md` の該当行
- 移植元の HEAD（2d24ccf）: `git archive` で一時ディレクトリに展開し、行番号と Python の構文木（docstring 込みと docstring を除いた場合）を作業ツリーと比べた。
- `git log` と `git status` で、コミット数、リリース、未コミットのファイル数を確かめた。

### エージェント B

照合が終わりました。結果をまとめます。

## 結論

計画書の事実認定は大半が正確で、主要な主張（トークン化、空クエリで埋め込み検索に進まないこと、モデルとしきい値、encoding 未指定、D2 の文分割、テンプレートのマーカー、訳の構文木同一性、94 ファイル、36 コミット）はすべて移植元のコードで確認できました。方針 B・D・E・G・H・I・J の方向性には賛成です。

直すべき点は 3 つに集約されます。

1. **完了条件「CI が Windows でも通る」は、今の段階構成では達成できません。** 評価ループのテストが symlink、chmod、shebang 付き実行ファイル、`ps` コマンドに依存しており、文字コードを直しても Windows では落ちます。対応する作業がどの段階にもありません。
2. **方針 C と P0 の「設定ファイルを `~/.config/info-hive/` に」「`LLM_WIKI_CONFIG` を改名」は対象が存在しません。** どちらもホスト型 wiki 専用で、方針 G で削る側です。残る名前付きの場所はモデルキャッシュ `~/.cache/llm-wiki/fastembed/` だけで、こちらは計画に載っていません。
3. **方針 A は「ファイルをコピーする」ではなく「git 履歴を持ち込んで独立させる」に変えるべきです。** 履歴がないと移植元の修正を `git cherry-pick` できず手で移植することになり、`eval/evolution/workflows/build_suite.py` は `git show 06369b2…` を呼ぶので動かなくなります。

そのほか中程度の指摘として、P5 が実際には P3・P4 の結果に依存すること、P0 の前提に書かれた承認待ち項目が 7 節と食い違うこと、bigram で 1 文字クエリが一致しないこと、frontmatter の 「、」区切りを受け付けるとグラフ側の PyYAML と解析が食い違うこと、「変えてはいけないもの」に抜けがあることを挙げます。付録の行番号は v4.0.1 ではなく訳済み作業ツリーの値です。

## 根拠

指摘を重大度の高い順に並べます。行番号は特記のない限り `git show HEAD:…`（v4.0.1、2d24ccf）の値です。

**1. Windows CI の完了条件が達成できない**（高、5 節・P0・P1）
- 根拠: `eval/evolution/test_adapters.py:71,104,106,193` は shebang 付きの偽コマンドを `chmod(0o755)` し `symlink_to` し、`ps -eo` を呼びます。`eval/evolution/test_evolution.py:148` も `symlink_to` を使います。`skills/llm-wiki/scripts/wiki_evolve.py:105` はマニフェストに `st_mode & 0o777` を記録するため、Windows では Linux と値が変わります。`wiki_evolve.py:291,298,317` は `start_new_session` と `killpg` を POSIX 限定で使っています。Windows のシンボリックリンク作成は開発者モードか管理者権限が要ります。
- 直し方: 完了条件を「Windows のジョブでは retrieval・lint・link_resolution・decisions のテストを通す。evolution のテストは Linux のみ」と書き換えるか、P4 に「evolution テストの Windows 対応」を作業として追加する。前者を推奨します。日本語対応の目的に evolution の Windows 対応は含まれません。

**2. 設定ファイルと `LLM_WIKI_CONFIG` の改名は対象が存在しない**（中、方針 C・P0・7 節）
- 根拠: `~/.config/llm-wiki/remote.json` を読むのは `wiki_remote.py:12,61` だけです。`LLM_WIKI_CONFIG` を読むコードは `wiki_remote.py:61` と、hosted バックエンド用の `wiki_decide.py:177-190`、そのテスト `eval/decisions/test_decisions.py:176-265` のみです。いずれも方針 G で削る範囲です。一方、`wiki_search.py:425-429` の既定モデルキャッシュ `~/.cache/llm-wiki/fastembed` と環境変数 `FASTEMBED_CACHE_PATH` は残りますが、計画のどこにも出てきません。文書側では `commands/wiki/upgrade.md:25`、`references/retrieval-setup.md:26,44`、`scaling-playbook.md:46`、`SCHEMA.md.template:91` が触れています。
- 直し方: 方針 C から設定ファイルの項を外し、`LLM_WIKI_CONFIG` は改名ではなく削除の一覧へ移す。残る環境変数は `LLM_WIKI_DECISIONS`、`_URL`、`_MODEL` の 3 つだけです。モデルキャッシュの既定を `~/.cache/info-hive/fastembed/` にするか、移植元と共有するかを P0 の決定事項に加える。モデルが変わるので共有の利点はなく、分離を推奨します。

**3. 方針 A はコピーではなく履歴ごと取り込むべき**（中、方針 A・P0・6 節）
- 根拠: 方針 A は「移植元のスクリプト修正は必要なものだけ個別に取り込む」としていますが、共通の履歴がなければ `git cherry-pick` が使えず、毎回手で差分を移植することになります。また `eval/evolution/workflows/build_suite.py:11,24` は固定コミット `06369b2…` を `git show` で取り出すため、履歴のないリポジトリでは動きません。
- 直し方: info-hive の main を 2d24ccf から開始し（upstream を remote に追加して分岐）、以後マージはせず cherry-pick だけ行う方針にする。「独立して育てる」という結論は変えずに済み、MIT の著作権表示も履歴で裏づけられます。既存の初回コミット（AGENTS.md）は上に積み直すか捨てます。

**4. P5 の前提が足りない**（中、4 節 P5）
- 根拠: P5 で書き直す `references/retrieval-setup.md`、`commands/wiki/init.md:14`（モデル名 `BAAI/bge-small-en-v1.5` を明記）、`SCHEMA.md.template:90`（モデル名と次元数）は P3 で決める既定モデルに依存します。lint の項目名の日本語化は P4 の作業です。計画は「P4 と P5 は P1 のあとなら並べて進められる」としています。
- 直し方: P5 を「文書の構成と運用規約の書き直し」と「P3・P4 の結果の反映」に分け、後者を P6 の直前に置く。

**5. P0 の前提に書かれた承認待ち項目が 7 節と矛盾する**（中、P0・7 節）
- 根拠: P0 の前提は「承認待ちの 4 項目（方針 A、C、G、J）」ですが、7 節の承認待ちは「A、B、スキルのディレクトリ名、docs サイト」で、C・G・J は確定済みです。
- 直し方: P0 の前提を 7 節に合わせる。

**6. bigram だけでは 1 文字のクエリが一致しない**（中、方針 E・P3）
- 根拠: 方針 E は「1 文字だけの並びは 1 文字で」としています。クエリ「犬」はトークン「犬」になりますが、本文の「犬が」は bigram「犬が」にしかならず、一致しません。漢字 1 字の用語（税、法、脳など）は検索できなくなります。
- 直し方: 索引側で漢字の unigram も登録する、またはクエリが 1 文字のときだけ索引側の bigram を前方一致で展開する。前者が単純です。あわせて文字クラスの仕様を書いてください。長音「ー」と踊り字「々」は Hiragana・Katakana・CJK 統合漢字のブロック外（U+30FC は片仮名ブロック内ですが U+3005 は CJK 記号ブロック）なので、漏れると「人々」が「人」になります。かな・漢字の境界で bigram を切らないことも明記が要ります。

**7. frontmatter の 「、」区切りを受け付けると解析器が食い違う**（中、P1）
- 根拠: lint と search は正規表現の簡易パーサー（`wiki_lint.py:100-104`、`wiki_search.py:78-82`）ですが、`wiki_graph_extract.py:104-105` と `wiki_graph_lint.py` は `yaml.safe_load` で読みます。lint・search だけが 「、」を区切りにすると、同じページのタグがグラフ側では 1 つの文字列になります。
- 直し方: 「、」を区切りとして受け付けず、lint で「インライン配列に全角読点があります」と指摘する。NFKC による比較だけを入れる。

**8. 「変えてはいけないもの」と「自由に変えられる」の線引きに抜け**（中、2 節・方針 B・P5）
- 根拠: `wiki_evolve.py:57-60` は経験・提案 ID を `[a-z0-9][a-z0-9-]{0,63}` に制限しています。`wiki_evolve_loop.py:461-473` は index.md に `- [[slug]]` 行、log.md に `- YYYY-MM-DD: Captured training iteration …` 行を書き、`if line not in old` で重複を判定します。同 `:436-438,451-454,459,658-660` は `Task:`、`Outcome:`、`Scope:`、`## Observation from …` という英語本文のページを生成します。計画は「ログ行の書式…はどのスクリプトも解析していないので自由に変えられます」としていますが、書く側のスクリプトはあります。
- 直し方: ID の ASCII 制限を「変えてはいけないもの」に加える。P5 でログ行・索引行の書式を変えるなら、P4 に `wiki_evolve_loop.py` の生成文字列の日本語化を加える。

**9. ホスト型削除の対象に `test_decisions.py` の hosted テストが漏れている**（中、方針 G・P0・8 節）
- 根拠: 計画は `test_decisions.py:62` だけを挙げていますが、`:28,78,171-229,245-266` に hosted バックエンドのテストが 5 件あり、`wiki_remote.py` と `LLM_WIKI_URL/TOKEN/CONFIG` に依存します。削除後は ImportError で落ちます。文書側は作業ツリーで 19 ファイルが「ホスト型」に触れています（commands 9 本、SKILL.md、references 7 本、README、CONTRIBUTING）。
- 直し方: 8 節の該当行を `test_decisions.py:28,62,78,171-266` に広げ、文書は grep の結果で件数を管理する。

**10. cp932 の完了条件は CI では検証できない**（中、5 節・P0）
- 根拠: GitHub の Windows ランナーの既定コードページは cp1252 です。cp1252 は UTF-8 の日本語バイト列をほぼ例外なく復号してしまうので、読み込みの不具合は文字化けとして静かに通ります。「—」(U+2014) は cp1252 には含まれ cp932 には含まれないため、出力で落ちるケースも CI では再現しません。
- 直し方: Windows ジョブに `PYTHONUTF8=0` と `PYTHONIOENCODING=cp932` を設定し、さらに「scripts 配下の `open`・`read_text`・`write_text` 呼び出しがすべて `encoding=` を持つ」ことを静的に検査するテストを追加する。これなら判定できます。

**11. 付録の行番号は v4.0.1 ではなく訳済み作業ツリーの値**（低、8 節）
- 根拠: 付録は「行番号は v4.0.1 時点の値」としていますが、`wiki_lint.py:38,95,99,130,149,303,356`、`wiki_stats.py:100,134,146`、`wiki_decide.py:14-15,355,516,548,633`、`init_wiki.py:86,91,118`、`wiki_graph_extract.py:121,128,574`、`wiki_graph_lint.py:105,526`、`test_link_resolution.py:111` は作業ツリーの行で、HEAD では 1〜5 行ずれます（例: HEAD の `CAPITALIZED_PHRASE_RE` は 39 行目、`TOKEN_RE` 以外の lint 行は +5）。`wiki_search.py` と `wiki_evolve*.py` は両方で一致します。
- 直し方: 注記を「訳済み作業ツリーの値」に直す。

**12. 移植元のリリース数**（低、2 節）
- 根拠: CHANGELOG によると 2026-07-20 以降に 2.0.3〜2.0.7、3.0.0、3.1.0、3.1.1、3.2.0、4.0.0、4.0.1 の 11 リリースがあります。メジャー 2 回は正しいです。コミット 36 件は一致しました。
- 直し方: 「4 か月で 11 リリース」に直す。結論は変わりません。

**13. 細部の事実**（低）
- `wiki_lint.py:126-133` は読めないファイルを `read_errors` として報告します。「Shift_JIS のファイルは黙って読み飛ばされる」は search・stats・graph には当たりますが lint には当たりません。
- 「bigram で索引が大きくなり、キャッシュが重くなる」について、`wiki_search.py:243-256` の解析キャッシュはセクション本文のみを保持し、トークンは保存しません。BM25 は `build_bm25`（337 行）で毎回構築されます。重くなるのは実行時メモリと計算で、キャッシュファイルではありません。
- `wiki_search.py:673` の検索 JSON 出力はすでに `ensure_ascii=False` です。2 節の「json.dumps が日本語を \uXXXX にする」は decide・lint・graph_lint・graph_query・evolve_loop に限られ、search は逆に今 cp932 で落ちる側です。
- SCHEMA の照合マーカー 6 つのうち、見出しは 4 つで、`init_wiki.py:64,76` の 2 つは箇条書きの接頭文字列です。訳済みテンプレート `SCHEMA.md.template:90,120` は接頭を保っており問題ありませんが、P3 でモデル名を書き換えるとき `- Semantic backend: local FastEmbed + sqlite-vec` の接頭を崩すと `--upgrade` が全 wiki で欠落を報告します。P5 の「見出し 6 つ」を「見出し 4 つと箇条書き接頭 2 つ」に直してください。
- 「29 ファイルにある `skills/llm-wiki/…` のパス」は確かめられませんでした。HEAD で `skills/llm-wiki/` を含むファイルは 65 で、server・research・docs・eval/remote・migrate.md を除くと 35 前後です。P0 の完了判定は数ではなく「grep で 0 件」にしてください。
- `eval/decisions/fake_backend.py:34` は `[a-z0-9]+` でトークン化して関連度を偽装します。P2 で `test_decisions.py` に日本語ケースを足すなら、この偽バックエンドも方針 E と同じトークン化にしないと全件 0 点になります。
- 8,000 文字の数え方に frontmatter とコードブロックを含めるかが未定義で、完了条件「8,000 文字を超えるページが指摘される」を判定できません。本文のみ、コードブロック込みと定めてください。

**方針 F について確認できたこと**
- fastembed 0.8.0 の `paraphrase-multilingual-MiniLM-L12-v2` は `qdrant/…-onnx-Q` の量子化版 `model_optimized.onnx` で 0.22 GB、384 次元です。評価は量子化済み重みに対して行うことになります。fastembed 側の切り詰めは 512 トークンで、128 トークンは学習時の上限という計画の記述は正確です。
- `multilingual-e5-large` は 2.24 GB、説明に「Prefixes: necessary」とあり、`text_embedding_base.py:31-60` の `query_embed`・`passage_embed` は接頭辞を付けません。計画どおり `wiki_search.py:478-487` で付与する実装が必要です。`add_custom_model` は `text_embedding.py:43` に存在します。
- `section_content_hash`（411 行）と `semantic_meta`（448-452 行）がモデル名を含むため、モデルを設定可能にしたときもこの 2 か所に設定値を通せば既存索引の再構築は自動です。

## 検討した代替案

- **コマンド名前空間を `/hive:*` に変える**: `commands/wiki/` のサブディレクトリ名が名前空間になるため、ディレクトリ名を変えれば衝突は消えます。ただし Q1 で `/wiki:*` 維持が決まっており、README の注記で足りると判断して採りませんでした。
- **形態素解析を既定にする**: 辞書依存が増え、uv の単一スクリプト実行と相性が悪いです。計画どおり評価後の任意機能でよいと判断しました。
- **ホスト型 wiki を「無効化」だけして残す**: 保守対象が減らず、英語前提の TypeScript 実装と仕様を揃える負担が残るため、削除に賛成します。
- **日本語特化モデルを既定候補に加える**: cl-nagoya の Ruri 系は 0.3 GB 以下の構成がありますが、fastembed 向け ONNX の有無を確かめられなかったため、P3 の比較候補への追加提案にとどめます。

## 前提と不確実性

- 行番号の照合は `git show HEAD:` と作業ツリーの両方で行いました。計画書の HTML はソースから本文を抽出して読み、ブラウザーでは開いていません。
- 「訳の前後で構文木が同一」は、docstring を除いた AST を 29 ファイルで比較して SAME を確認しました。docstring は訳されているので、厳密には文字列リテラルの一部は変わっています。
- Claude Code のプラグインコマンドの名前空間規則は、手元の plugin-dev スキルの参照文書（「サブディレクトリ名が名前空間になる」）に依拠しています。実機で両プラグインを同時に入れて衝突を確かめてはいません。
- Windows ランナーの既定コードページが cp1252 であることは一般的な事実として扱いました。GitHub のイメージを直接確かめてはいません。
- MiniLM の「日本語で 200 文字前後」は計画書の推定で、トークナイザーで測ってはいません。
- 「29 ファイル」の数え方は作者の定義が不明なため、誤りとは断定していません。
- 作者の判断が要る点: 完了条件から evolution テストの Windows 対応を外すか、方針 A を履歴持ち込みに変えるか、モデルキャッシュの既定パスを分けるか、bigram に unigram を併用するか。

## 参照

- 計画書: `C:\Works\info-hive\docs\porting-plan.html`
- 移植元（`C:\Works\llm-wiki-plugin`、HEAD 2d24ccf と作業ツリー）
  - `skills/llm-wiki/scripts/wiki_search.py`（57-62, 73-95, 100, 161-168, 215-256, 337-351, 373-395, 405-433, 478-487, 565-621, 663-673, 729-732, 767-820）
  - `skills/llm-wiki/scripts/wiki_lint.py`（全文）、`wiki_stats.py`（全文）
  - `skills/llm-wiki/scripts/wiki_decide.py`（1-30, 55-70, 110-190, 354-436, 505-560, 563-660）
  - `skills/llm-wiki/scripts/wiki_evolve.py`（48-62, 165-175, 250-300, 570-578）、`wiki_evolve_loop.py`（82-90, 242-250, 436-475, 492-498, 564-570, 655-666）、`wiki_evolve_agent.py`、`wiki_evolve_stream.py`
  - `skills/llm-wiki/scripts/init_wiki.py`（1-125, 137-151）、`setup_wiki.py`、`test_link_resolution.py`
  - `skills/llm-wiki/scripts/wiki_graph_extract.py`、`wiki_graph_lint.py`、`wiki_graph_query.py`
  - `skills/llm-wiki/assets/SCHEMA.md.template`、`decision-questions.json`、`skills/llm-wiki/SKILL.md`
  - `commands/wiki/*.md`、`skills/llm-wiki/references/*.md`（hosted 記述の grep）
  - `eval/decisions/test_decisions.py`、`fake_backend.py`、`eval/evolution/test_adapters.py`、`test_evolution.py`、`eval/evolution/workflows/build_suite.py`、`eval/retrieval/run_eval.py`、`test_search_contracts.py`、`test_setup.py`
  - `.github/workflows/wiki-tests.yml`、`deploy-docs.yml`、`.claude-plugin/marketplace.json`、`plugin.json`、`package.json`、`CHANGELOG.md`、`docs/.vitepress/config.ts`
  - `git log`、`git diff --stat HEAD`
- fastembed 0.8.0（uv キャッシュ `AppData/Local/uv/cache/archive-v0/TqvgikJc_VCmDkgm/fastembed`）: `text/pooled_embedding.py`、`text/text_embedding_base.py`、`text/text_embedding.py`、`text/multitask_embedding.py`
- plugin-dev スキル `command-development/references/plugin-features-reference.md`（コマンドの名前空間規則）The two background file searches that were stopped were no longer needed. Both were looking for the installed fastembed package, which I had already located in the uv cache and inspected before they timed out. Nothing in the review above depends on them, and the final report stands as delivered.

### エージェント C

照合はほぼ終わりました。高い重大度の指摘は、検索品質の完了判定、Claude Code のコマンド名前空間、D2 の過剰な正規化、標準入力の文字コードです。モデルの入力長は、元モデルの設定と FastEmbed で使う ONNX 版を区別し、確認できた範囲を明記します。
## 結論

**現状のまま実装に進む前に、計画の修正が必要です。**
指摘は高 4 件、中 8 件、低 1 件です。

特に、検索の回帰判定、コマンド名前空間、D2 の引用照合、Windows の標準入力について、計画どおりに実装しても要件を満たせない箇所があります。
独立した移植、日本語スラッグ、小さい多言語モデルを評価して選ぶという方向は支持します。

ファイルの変更、ビルド、テストの実行は行っていません。

## 根拠

以下の `S/` は、移植元の `C:\Works\llm-wiki-plugin\skills\llm-wiki\scripts\` を表します。
行番号は、未コミットの日本語訳を含む現在の作業ツリーのものです。

1. **【高】検索の完了条件では、品質の維持・改善を判定できない**

   **該当箇所:** P2、P3、5 節「英語の評価セットの `--gate` が通り、成績が下がっていない」。

   **根拠:** `eval/retrieval/run_eval.py:213–227` のゲートは、同じ実装の page・section・hybrid を比較しています。変更前の成績との比較はありません。また、同ファイル `140–151, 197–201` は、埋め込みの初期化に失敗すると hybrid を省略します。したがって、検索全体が悪化した場合や、埋め込みが動かない場合にも通り得ます。

   P2 の基準測定にも問題があります。`S/wiki_search.py:729–732` は日本語クエリで標準出力を空にして終了しますが、評価側の `run_eval.py:95–96, 119–121` は JSON を必須としているため、そのままでは「0 件」ではなく JSON 解析エラーになります。

   **直し方:** P2 に評価ハーネスの整備を追加し、空出力による既知の失敗を記録できるようにします。変更前の英語成績を固定し、変更後との比較を別ゲートにしてください。日本語には Recall、MRR、偽陽性率の合格値を設定し、モデル・しきい値の選定用と最終判定用のクエリを分けます。埋め込みを検証する CI では、hybrid の省略を失敗扱いにします。

2. **【高】方針 C のコマンド名は、現在の Claude Code の仕様と一致しない**

   **該当箇所:** 方針 C、P0、P6、6 節のコマンド衝突リスク。

   **根拠:** 移植元の `.claude-plugin/plugin.json:2` はプラグイン名を指定し、`commands/wiki/query.md:1–4` には名前を上書きする設定がありません。現在の公式仕様では、`commands/db/migrate.md` は `/<plugin>:db:migrate` になります。したがって、その構成でプラグイン名を変更すると、通常は `/info-hive:wiki:query` となり、`/wiki:query` の維持にはなりません。[Claude Code 公式「Commands」](https://code.claude.com/docs/en/plugins/components#commands)

   **直し方:** 対応する Claude Code のバージョンを決め、P0 でコマンドの登録名を確認してください。推奨は `commands/` を平らにして `/info-hive:query` などにそろえる案です。`/wiki:*` が必須なら、別途エイリアス等の実現方法を検証し、配布手順まで計画に含めます。「両プラグインが必ず衝突する」という説明も見直してください。

   **不確実性:** インストール済み Claude Code での実測はしていません。上記は現在の公式仕様との照合です。

3. **【高】D2 で空白をすべて除くと、異なる引用を根拠として通してしまう**

   **該当箇所:** 方針 H、P4 の NFKC・空白除去。

   **根拠:** `S/wiki_decide.py:358–365` は引用の包含を判定し、`418–423` は一致すればモデルを呼ばずに `supported=True, confidence=1.0` を返します。計画の空白除去を適用すると、例えば `now here` と `nowhere` は両方とも `nowhere` になります。日英混在を許す要件に対し、語境界を失う変更です。

   **直し方:** 原文一致、NFC と空白圧縮による一致、日本語文字間の改行だけを処理した一致を段階的に扱ってください。英単語間・数字間の空白は保持します。互換文字を同一視する NFKC を「原文の引用と一致した」という判定に使う範囲も限定します。誤一致を拒否する負例を完了条件に加えてください。

   文分割では、日本語句点の後に空白がなくても分割する必要があります。現在の正規表現は `S/wiki_decide.py:355` の `(?<=[.!?])\s+` なので、文字クラスへの句読点追加だけでは `前文。後文。` を分割できません。

4. **【高】P1 に標準入力の UTF-8 化が抜けている**

   **該当箇所:** P1、5 節の Windows 入出力条件。

   **根拠:** P1 は標準出力・標準エラーだけを対象にしています。しかし、`S/wiki_decide.py:640`、`wiki_evolve_agent.py:223`、`wiki_evolve_claude.py:44`、`wiki_evolve_api.py:130` は標準入力から JSON を読みます。Windows のパイプは、UTF-8 モード等がなければシステムのロケール文字コードを使います。[Python 3.12 公式「標準ストリーム」](https://docs.python.org/3.12/library/sys.html#sys.stdin)

   **直し方:** CLI の標準入力も UTF-8 と定義し、読み取り開始前に対応させてください。子プロセスへの `PYTHONUTF8=1` は、直接起動された親プロセスには効きません。未エスケープの日本語・`𠮷` を含む JSON を、UTF-8 のバイト列として直接流し込む検証を追加します。

5. **【中】読点によるリスト分割が、検索・lint とグラフで食い違う**

   **該当箇所:** P1 の「frontmatter のリストを `、` でも区切る」、方針 B。

   **根拠:** `S/wiki_search.py:65–91` と `wiki_lint.py:81–108` は独自パーサーですが、`wiki_graph_extract.py:95–108` は PyYAML を使います。`tags: [機械学習、深層学習]` は YAML では単一の値です。独自パーサーだけ変更すると、検索では 2 タグ、グラフでは 1 タグになります。引用符付きの日本語タイトルや `sources` に読点が含まれる場合にも、機械的分割は意味を変えます。

   **直し方:** 初回は ASCII の YAML 区切りを維持し、誤記が疑われる箇所を lint で案内する案を推奨します。独自拡張を採用するなら、引用符を考慮した共通仕様を定め、全パーサーで同じ意味になることを確認してください。

6. **【中】パースキャッシュの移行・無効化が計画にない**

   **該当箇所:** P1、P3、5 節の既存 wiki 互換性。

   **根拠:** `S/wiki_search.py:127–129, 161–166` はパースキャッシュのスキーマを `1` としています。同ファイル `230–245` はファイルのハッシュが同じなら、旧実装で作った metadata・links・sections を再利用します。BOM 対応や解析・正規化規則を直しても、既存ページの内容が変わらなければ旧解析結果が残ります。

   一方、ベクトル索引は `435–461` でモデル名・次元変更を検出して再構築するため、「モデルを変更しても索引が必ず残る」という問題ではありません。

   **直し方:** パース規則を変える際のスキーマ更新を P1 に追加してください。埋め込みの接頭辞・分割方式・モデルの版を変える場合も識別できるようにし、旧キャッシュあり／なしで検索結果が一致する条件を追加します。

7. **【中】NFC 正規化後の衝突と、実ファイルへの復帰方法が未定義**

   **該当箇所:** 方針 D、P1、P2、6 節。

   **根拠:** `S/wiki_search.py:181–189` はパスを辞書に格納します。`wiki_graph_extract.py:194–199` は重複ノードを先勝ちで省略し、`wiki_graph_query.py:50–71` はノード ID を完全一致で検索します。比較キーだけを NFC にすると、正規化後に同名となる複数ファイルや、既存の明示的な `graph.node_id`・`source` との対応を決める必要があります。

   **直し方:** 正規化した比較キーと実際のパスを別々に保持し、複数候補ができたら曖昧なリンクとして報告してください。NFC/NFD の対応だけでなく、両方のファイルが共存するケース、グラフの明示 ID・出典参照・CLI 引数まで検証対象に含めます。Windows で作成できない名前や末尾の空白等の制約も命名規則に明記します。

8. **【中】MiniLM の設定値と、FastEmbed での実際の入力上限を混同している**

   **該当箇所:** 方針 F、P2、P3、6 節。

   **根拠:** 元の Sentence Transformers モデルの設定は `max_seq_length: 128` です。[元モデルの設定](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/sentence_bert_config.json)  
   ただし FastEmbed 0.8.0 は別の量子化 ONNX 配布物を使用し、その登録情報には 512 トークンとあります。[FastEmbed の登録定義](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/text/pooled_embedding.py)  
   実際の切り詰め長は tokenizer の設定から決定されます。[FastEmbed の tokenizer 読み込み](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/common/preprocessor_utils.py)

   **直し方:** 学習・推奨長と実行時の切り詰め長を分けて記載します。「日本語 200 文字」を固定境界にせず、実際の tokenizer で測り、セクション後半だけに正解があるクエリを使って評価してください。長い段落もあるため、分割するなら段落単位だけでなくトークン数による上限が必要です。

   **未確認:** ONNX 配布物の tokenizer 設定を取得できなかったため、実行時上限が実際に 128／512 のどちらかは断定しません。0.22 GB も登録上の値であり、総ダウンロード量の実測ではありません。

9. **【中】評価ループの照合修正が、別の主要な照合箇所を取りこぼしている**

   **該当箇所:** 方針 H、P4、8 節の `wiki_evolve.py:260`。

   **根拠:** `S/wiki_evolve.py:259–281` は回答キーワードの照合ですが、ループ本体の `wiki_evolve_loop.py:73–89` は、ジャッジの引用を原文への完全一致で検証します。こちらでは NFC/NFD や改行の差で引用が不一致になると、校正・根拠判定が失敗します。`wiki_evolve.py` の変更だけでは直りません。

   **直し方:** 回答、引用、引用元パスで必要な比較規則を分け、ループ側も調査・修正対象に追加してください。過剰な一致を許さない方針は指摘 3 と共通です。日本語の校正用正例・負例と、提案・評価・適用・復元までの検証を完了条件に加えます。実モデルの日本語精度を後回しにする場合は、初回の保証範囲を明示します。

10. **【中】bigram 案は 1 文字の検索語を拾えない**

    **該当箇所:** 方針 E、P2、P3。

    **根拠:** 計画は「1 文字だけの並び」に限り unigram を作ります。この規則では、クエリ `癌` は `["癌"]`、本文 `肺癌治療` は `["肺癌", "癌治", "治療"]` となり、共通トークンは 0 個です。`S/wiki_search.py:336–370, 768–770` の BM25 では、語彙検索の得点が付きません。これは計画した規則からの推論で、実行結果ではありません。

    **直し方:** 1 文字クエリ用の検索方法を用意するか、対応する最小文字数を明示してください。長音符、繰り返し記号、拡張漢字、日英の境界を含む語も評価対象にします。形態素解析との比較は、平均成績だけでなく短い固有名詞・専門語で分けて評価します。

11. **【中】docs サイトを外す場合の CI・依存関係の整理が不足している**

    **該当箇所:** P0、P6、7 節の docs サイト案。

    **根拠:** `.github/workflows/wiki-tests.yml:49–59` にはサーバーとは独立した docs ジョブがあります。`package.json:17–25` の実行スクリプト・依存は VitePress 用です。`deploy-docs.yml:44–56` も docs の存在を前提にしています。サーバージョブと公開用 workflow だけを外しても、docs の検証ジョブが残ります。

    **直し方:** docs 削除と同じ段階で、docs ジョブ、公開 workflow、npm スクリプト、依存・ロックファイル、README のリンクをまとめて整理してください。サイトを外すなら、P6 のバージョン管理対象に `package.json` を残す理由も再検討します。

12. **【中】互換性のために残すものは「見出し 6 つ」ではない**

    **該当箇所:** 2 節、方針 B、P5。

    **根拠:** `S/init_wiki.py:42–78` の 6 マーカーは、見出し 4 個と本文の文字列 2 個です。後者は `Graph lint + extract` と `- Semantic backend: local FastEmbed + sqlite-vec` です。`101–105` は前者を行一致、後者を部分文字列で検出します。現在の翻訳済みテンプレートでも、`assets/SCHEMA.md.template:90, 120` に両文字列が保持されています。

    **直し方:** 6 マーカーを正確に列挙し、それぞれの照合方法を記載してください。見出しだけを保護する実装にしないこと、新規テンプレートで不足判定が出ないこと、既存スキーマのアップグレードが冪等であることを検証します。

13. **【低】依存関係の図・本文・検証順序をそろえる必要がある**

    **該当箇所:** 4 節、P2、P4、P5、P6。

    **根拠:** 4 節の図と本文は P5 を P1 後としますが、P5 自体の前提は P0 です。また、P2 で日本語の判定テストを追加する一方、P4 は P1 だけに依存します。P5 のページサイズ・引用・検索の説明は P3/P4 の最終仕様にも依存します。関連する実装境界は `S/wiki_search.py:278–333`、`wiki_decide.py:355–405`、`wiki_lint.py:219–222` です。

    **直し方:** P4 の回帰ケースは修正前に用意し、P2 の完了物に依存させるか P4 冒頭へ移してください。P5 の執筆は並行しても、最終整合確認は P3/P4 後とします。P6 に「コード・指示・テンプレートを使った一連の操作の確認」を追加してください。

## 検討した代替案

| 方針・承認待ち項目 | 意見と代替案 |
|---|---|
| A：コピーして独立 | 支持。ただしフォークでも上流マージは義務ではなく、コピー自体が衝突を解決するわけではありません。起点コミットと採用パッチを記録する運用を重視します。 |
| B：機械用キーを英語で維持 | 支持。指摘 12 の本文マーカーや、評価用 ID の制約も保護対象として列挙します。 |
| C：名前とコマンド | `/info-hive:*` への統一を推奨。`/wiki:*` の維持は実現方法を確認してから確定します。 |
| D：日本語スラッグ | 支持。実ファイルの改名による強制統一より、比較キーと実パスを分け、衝突を検出する案を採ります。 |
| E：bigram | 初期方式として支持。短語の扱いを定め、SudachiPy の採否を最終評価用データで判断します。 |
| F：0.3 GB 程度 | 支持。登録上のサイズだけで決めず、実ファイル・精度・入力長を確認します。小さいモデルの失敗を、大きいモデルの案内だけで合格扱いにはしません。 |
| G：ホスト関連を削除 | ローカル専用製品にするなら支持。ただし「サーバーを提供しない」と「既存サーバーにも接続しない」は別の製品判断です。後者も今回の範囲であることを明記します。 |
| H：判定・評価ループを残す | 支持。ただし D2 の正規化を限定し、評価ループ自体の日本語検証も行います。モデル精度の検証延期とは分けて扱います。 |
| I：4,000／8,000 文字 | 初期値として支持。既存の行数用 CLI 引数の意味を維持し、文字数の設定を別に設ける案が明確です。 |
| J：未コミット訳を取り込む | 支持。コピー前に訳の差分・ハッシュを保存し、取り込んだ集合を再現できるようにします。 |
| スキルのディレクトリ改名 | 名前をそろえる案を支持。テスト・CI・生成用ツールのパスも対象にします。過去の測定資料は、現行手順と区別して保存します。 |
| docs サイトを外す | 初回の保守範囲を絞る案を支持。指摘 11 の整理と、README から必要情報へ到達できる確認を同時に行います。 |

## 前提と不確実性

- 移植元の HEAD は `2d24ccfbbd46e77a431ddf2b1b54042d18576217` と確認しました。現在の作業ツリーを実装の照合対象としています。
- ビルド、モデルのダウンロード、検索評価、Claude Code へのインストールは実施していません。コードから導いた挙動と実測結果を区別しています。
- 未コミット訳の全ファイルについて、翻訳前後の AST 一致を再検証していません。計画書のその主張は追認していません。
- MiniLM の実行時 tokenizer 上限、総ダウンロード量、日本語検索・判定精度は未確認です。
- 「既存 wiki を変換なしで使える」は、Markdown 正本を維持し、派生キャッシュの再構築は許す意味と解釈しました。キャッシュまで変更不可なら、別の移行設計が必要です。
- 方針 A〜J と承認待ち項目はレビュー対象として評価しました。承認・実装を行ったものではありません。
- 指定された `docs\agent-runs\2026-11-02-porting-plan-review` 配下は読んでいません。

## 参照

- [計画書](C:/Works/info-hive/docs/porting-plan.html)：2〜8 節。
- 移植元 `C:\Works\llm-wiki-plugin\`：
  - [wiki_search.py](C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/wiki_search.py)：解析、キャッシュ、リンク解決、検索、ベクトル索引。
  - [wiki_decide.py](C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/wiki_decide.py)：D2、標準入力、検索候補取得。
  - [wiki_evolve.py](C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/wiki_evolve.py)、[wiki_evolve_loop.py](C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/wiki_evolve_loop.py)：ID、回答・引用照合、評価・生成処理。
  - `skills/llm-wiki/scripts/wiki_evolve_agent.py`、`wiki_evolve_claude.py`、`wiki_evolve_api.py`、`wiki_evolve_stream.py`：標準入出力、プロンプト、子プロセス。
  - `skills/llm-wiki/scripts/wiki_lint.py`、`wiki_stats.py`、`wiki_graph_extract.py`、`wiki_graph_lint.py`、`wiki_graph_query.py`：解析、文字数、リンク・グラフ処理。
  - `skills/llm-wiki/scripts/init_wiki.py`、`setup_wiki.py`：テンプレート、更新マーカー、実行環境準備。
  - `eval/retrieval/run_eval.py`、`test_embedding_mode.py`、`test_setup.py`：評価ゲート、索引・セットアップの既存検証。
  - `.claude-plugin/plugin.json`、`marketplace.json`、`.github/workflows/wiki-tests.yml`、`deploy-docs.yml`、`package.json`。
  - `skills/llm-wiki/SKILL.md`、`assets/SCHEMA.md.template`、`references/ingest-workflow.md`、`page-conventions.md`、`commands/wiki/query.md`、`init.md`。
  - `AGENTS.md`、`LICENSE`。Git の HEAD・作業ツリー差分情報。
- 外部仕様：本文でリンクした Claude Code、Python 3.12、FastEmbed 0.8.0、Sentence Transformers の公式文書・ソース。

### エージェント D

モデル仕様にも修正が必要です。計画書の「128トークン」はSentence Transformers側の設定値ですが、採用予定のFastEmbed 0.8.0は別のONNX配布物を使い、登録情報では512トークンとしています。加えて、アップグレードで保存すべき対象は「見出し6個」ではなく「見出し4個と本文中のマーカー2個」でした。これらを根拠と修正案付きで整理します。
## 結論

**計画の方向性は妥当ですが、現状の完了条件では日本語対応の失敗を見逃します。高重大度の5件を修正してから実装を進めるべきです。**

特に、P2の基準測定が現行コードでは途中終了すること、検索ゲートが移植前からの劣化を検出しないこと、旧キャッシュで修正が無効になることが重要です。P1の標準入力対応と、P4の引用照合にも修正が必要です。

以下は独立したレビューです。ファイル変更、ビルド、他エージェントの成果の参照は行っていません。

## 根拠

以下、`S/` は `C:\Works\llm-wiki-plugin\skills\llm-wiki\scripts\`、`E/` は `C:\Works\llm-wiki-plugin\eval\` を表します。行番号は未コミットの日本語訳を含む作業ツリーのものです。

1. **【高】P2の日本語検索の基準測定は「0件」ではなく、JSON解析エラーになる。**

   **該当箇所:** P2、P3への依存関係。

   **根拠:** `S/wiki_search.py:729–732` はトークンが空なら標準エラーへ `Empty query.` を書き、JSONを出さずに戻ります。一方、`E/retrieval/run_eval.py:95–96, 118–121` は標準出力を無条件に `json.loads()` へ渡します。したがって、日本語だけのクエリでは `json.loads("")` となり、計画にある「現状の成績を記録する（ほぼ0件）」を既存ハーネスで実施できません。

   **直し方:** P2の前に空結果のJSON契約を修正するか、基準測定専用のハーネスで「検索不能」と「正常な0件」を別に記録してください。基準測定のために先に変更した箇所も明示します。

2. **【高】既存の `--gate` は、移植前からの検索劣化も埋め込みの不稼働も保証しない。**

   **該当箇所:** P2・P3、5節「完了の判定」。

   **根拠:** `E/retrieval/run_eval.py:213–227` が比較するのは、同一実装の `section` 対 `page`、`hybrid` 対 `section` です。移植前の値とは比較しません。また、同ファイル `140–151, 197–201` はハイブリッド検索が動かなければ評価から外します。MRRは算出されますが、ゲートでは検査されません。

   例えば `page=0.9、section=0.9` から両方 `0.1` に下がっても、`section ≥ page` は成立します。これは条件式からの例示であり、実測値ではありません。

   **直し方:** 移植前の英語指標を保存し、許容差を明記して比較してください。日本語には正例・負例と独立した最終評価セットを用意し、recall、MRR、偽陽性率の目標をP2で固定します。必須の埋め込み評価では `mode == "hybrid"` を要求し、スキップを失敗として扱う必要があります。

3. **【高】旧パースキャッシュの無効化が計画にないため、BOMやfrontmatterの修正が既存wikiで効かない。**

   **該当箇所:** P1・P3、5節「既存wikiを変換なしで利用」。

   **根拠:** `S/wiki_search.py:118–158, 161–168` のキャッシュ形式は `schema=1`。同ファイル `230–245` はファイルのSHA-256が一致すると、保存済みの `meta`・`links`・`sections` を使い、デコードやfrontmatter解析をやり直しません。旧実装でBOM付きページのメタデータを空として保存していた場合、コードだけ直してもその状態が残ります。標準の検索手順も `commands/wiki/query.md:17` で `--cache` を使用しています。

   **直し方:** P1にパーサーの版による無効化を追加し、「移植元が作成済みのキャッシュを持つwiki」で受入試験をしてください。なお、ベクトル索引には既にモデル名・次元・スキーマ変更時の再作成があります（`S/wiki_search.py:447–461`）。こちらは接頭辞や分割方式などの変更も再作成条件に含めるべきです。

4. **【高】P1の文字コード対策には、標準入力が抜けている。**

   **該当箇所:** P1、5節のWindows入出力条件。

   **根拠:** 計画は標準出力・標準エラーと子プロセスの設定を指定していますが、`S/wiki_decide.py:639–642` は `sys.stdin.read()`、`S/wiki_evolve_agent.py:218–223` は `json.load(sys.stdin)` を使います。ほかにも `wiki_evolve_claude.py:44`、`wiki_evolve_api.py:130` に同じ入口があります。親を経由しない直接実行では、子に渡す `PYTHONUTF8=1` では対応できません。

   **直し方:** JSONを受け取る標準入力もUTF-8と定義し、各入口で設定してください。受入試験には、`PYTHONUTF8` なしでUTF-8の日本語JSONをパイプ入力するケースを加えます。

5. **【高】D2で空白をすべて削除すると、別の文章を「確実な引用」と認定する。**

   **該当箇所:** 方針H、P4の「空白をすべて除いてから照合」。

   **根拠:** `S/wiki_decide.py:358–365` は現在、連続空白を単一空白へ畳んで比較しています。同ファイル `418–423` は引用一致ならモデルを呼ばず、`supported=True、confidence=1.0` を返します。提案方式では `"now here"` と `"nowhere"` がともに `"nowhere"` となります。また、`S/wiki_graph_lint.py:357–359` は引用一致した関係をD2の検証対象から除きます。

   **直し方:** 英数字間の語境界は保持し、日本語文字間の改行など、許容する差だけを吸収してください。肯定例だけでなく、空白の有無で意味・識別子が変わる負例も必要です。NFKCも引用原文とは区別して扱い、許容範囲を明文化すべきです。

6. **【中】NFC対応がファイル名とwikilinkに偏り、グラフの明示参照まで届いていない。**

   **該当箇所:** 方針D、P1・P4、8節。

   **根拠:** `S/wiki_graph_extract.py:193–194` は既定ノードIDをスラッグから作りますが、`278–299` は `relationships.object`・`source` をそのまま出力します。`S/wiki_graph_lint.py:242–244, 271–274, 285–291` はこれらを文字列一致で検証し、`S/wiki_graph_query.py:50–71` もSQLで完全一致させます。スラッグだけNFC化しても、NFDの明示参照は残り得ます。

   **直し方:** `node_id`、`object`、`source`、`contradicts`、`supersedes`、問い合わせ側IDについて、同一性の規則を決めてください。正規化後のキーと実際のファイルパスを分離し、正規化衝突を上書きせず報告することも必要です。グラフ抽出・lint・queryを通した受入条件を追加します。

7. **【中】frontmatterの「、」区切り対応は、検索とグラフで異なる解釈を生む。**

   **該当箇所:** P1、方針B。

   **根拠:** `S/wiki_search.py:65–90` と `S/wiki_lint.py:81–108` は簡易パーサーですが、`S/wiki_graph_extract.py:95–108` と `S/wiki_graph_lint.py:74–86` はPyYAMLを使います。前者だけを変更すると、`tags: [機械学習、深層学習]` の要素数が処理系によって異なります。また、引用符付きの `"研究、開発"` を単純に分割すると、本来1個の値を壊します。

   **直し方:** 初回はASCIIカンマまたはYAMLのブロックリストを正規形式として案内し、曖昧な記法をlintで指摘する案を推奨します。受け付けるなら、引用符を考慮した共通処理を検索・lint・グラフに適用してください。

8. **【中】MiniLMの「128トークンで切れる」という説明は、採用予定の実行系と混同している。**

   **該当箇所:** 方針F、P2・P3、6節。

   **根拠:** 移植元は `S/wiki_search.py:430, 478–487` でFastEmbedを使用します。元モデルのSentence Transformers設定は `max_seq_length=128` ですが、FastEmbed 0.8.0の登録情報はQdrantのONNX配布物を参照し、512トークンの切り詰めと記載しています。FastEmbedのローダーも `sentence_bert_config.json` ではなく、`tokenizer_config.json` から上限を取得します。[元モデルの設定](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/sentence_bert_config.json)、[FastEmbedのモデル登録](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/text/pooled_embedding.py)、[トークナイザー読込処理](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/common/preprocessor_utils.py)。

   **直し方:** モデルの学習条件、Sentence Transformersの設定、実際に使うONNX版の上限を分けて記載してください。「日本語200文字」を固定境界にせず、採用するトークナイザーで実測して評価ケースを作ります。今回、配布物の実ファイルと推論時の切り詰めは確認できていません。

9. **【中】評価ループの引用照合は、計画にある `wiki_evolve.py` の修正だけでは変わらない。**

   **該当箇所:** 方針H、P4、8節。

   **根拠:** `S/wiki_evolve.py:259–281` の `normalize()` は旧形式の回答採点に使われます。一方、`S/wiki_evolve_loop.py:73–79` の `grounded_verdict()` は `quote in source` の直接比較で、`normalize()` を呼びません。同ファイル `82–90, 321–322` はこの判定を校正と本評価に使用します。改行やUnicode表現の差による引用不一致は残ります。

   **直し方:** 評価ループの引用検証も対象に追加してください。ただし、成果物の完全一致チェックまで一律に緩めず、引用照合と厳密な検証を区別します。日本語の校正正例・負例と、ループ全体の受入試験が必要です。

10. **【中】方針Eのbigramだけでは、1文字クエリが長い日本語列に一致しない。**

    **該当箇所:** 方針E、P2・P3。

    **根拠:** 計画は「1文字だけの並びは1文字」としています。この定義なら、文書「本を読む」のトークンは `{本を, を読, 読む}`、クエリ「本」は `{本}` で、共通部分は空です。`S/wiki_search.py:360–366` は存在しない語を得点化せず、`768–770` は0点の結果を除外します。これは提案方式に基づく推論で、実装後の実測ではありません。

    **直し方:** 1文字検索を保証するか決め、保証するなら短いクエリ用の文字一致などを追加します。P2には1文字語に加え、長音符・繰返し記号・補助平面漢字を含むケースも置き、対象文字範囲を明文化してください。

11. **【中】保存すべきSCHEMAマーカーは「見出し6個」ではない。**

    **該当箇所:** 2節、方針B、P5。

    **根拠:** `S/init_wiki.py:42–79` の6項目は、見出し4個と本文マーカー2個です。後者は `Graph lint + extract` と `- Semantic backend: local FastEmbed + sqlite-vec`。同ファイル `101–104` も、見出しには行一致、それ以外には部分文字列一致を使います。実際のテンプレートでは `assets/SCHEMA.md.template:90, 120` にあります。

    **直し方:** 「見出し6個」を「検出マーカー6個」に改め、文字列を列挙してください。翻訳後に新規初期化したSCHEMAをアップグレード検査して、不足を誤報しないことをP5の完了条件にします。

12. **【中】プラグイン改名後も `/wiki:*` を維持できるという前提は、P0で検証が必要。**

    **該当箇所:** 方針C、P0・P6、6節の衝突リスク。

    **根拠:** 移植元の `.claude-plugin/plugin.json:2` は `name: "llm-wiki"`、コマンドは `commands/wiki/query.md` などに配置されています。しかし現行公式仕様では、コンポーネントはプラグイン名の名前空間に入り、明示的なコマンド定義もプラグイン接頭辞を持ちます。ディレクトリを残すだけで `/wiki:*` が保たれるとは確認できません。[Claude Codeのmanifest仕様](https://code.claude.com/docs/en/plugins-reference#commands)。

    **直し方:** 対応するClaude Codeの版と導入方法を決め、改名した最小構成で実際のコマンド名をP0で確認してください。維持できなければ `/info-hive:*` に統一する案を推奨します。衝突の有無も実際の公開名から判断すべきです。今回はCLI上の登録名を実測していません。

13. **【中】方針Gで削除する設定を、方針Cでは改名して残そうとしている。**

    **該当箇所:** 方針C・F・G、P0、5節。

    **根拠:** `LLM_WIKI_CONFIG` と `~/.config/llm-wiki/remote.json` の実体は `S/wiki_remote.py:59, 223–227` にあり、`S/wiki_decide.py:176–188` もホスト型の接続設定として利用しています。ローカル判定は環境変数から設定されます（同ファイル `194–218`）。一方、埋め込みモデルは `S/wiki_search.py:59, 430` の定数です。

    **直し方:** ホスト型設定の削除と、ローカル向け設定の新設を分けてください。後者を新設するなら、モデル名・しきい値・設定の優先順位をP0で定義してP3へ渡します。単なるパス改名では、方針Fの「設定でモデルを選べる」は実現しません。

14. **【低】「Pythonの構文木が同一、文字列リテラルも不変」は不正確。**

    **該当箇所:** 2節「手元の状態」、方針J。

    **根拠:** 読み取り専用の比較で、変更ファイル94件、変更Pythonファイル29件を確認しました。29件すべてで通常の `ast.dump(ast.parse(...))` はHEADと異なり、文字列式を除外した比較では差がありませんでした。例えば `S/wiki_search.py:9–40` のモジュールdocstringは日本語化され、`main()` の `description=__doc__` にも使われます（同ファイル `909–910`）。

    **直し方:** 「docstring等の文字列式を除外した構文木では差がない」と、比較方法を正確に書いてください。docstring変更は `--help` の表示にも影響します。

15. **【低】段階の前提説明と依存図が一致していない。**

    **該当箇所:** 方針H、4節、P0・P5。

    **根拠:** 計画書 `551–553行` はP5がP1に依存すると記載しますが、P5本文 `683行` の前提はP0です。P0の `597行` は「承認待ち4項目」に方針A・C・G・Jを挙げますが、7節の4項目はA・B・スキル改名・docsです。また方針Hの「第1段階」のD2修正は、段階表ではP4に置かれています。

    **直し方:** P5は草案をP0後、確定をP1・P3・P4の仕様確定後と分けると整合します。P0の承認対象を7節と一致させ、「第1段階」がP1なのか初回リリースなのかも明記してください。この指摘の根拠は計画書内部であり、移植元コードではありません。

## 検討した代替案

- **方針Aの独立コピー:** 採用可能です。上流へ還元しないことと、履歴を残さないことは別ですが、自動マージを続ける必要はありません。起点コミットだけでなく、取り込んだ未コミット訳の差分またはハッシュ一覧も保存する案を推奨します。起点コミットだけでは方針Jの状態を再現できません。
- **方針Bの機械可読キー維持:** 賛成です。frontmatter、述語、JSON出力キー、CLI引数まで対象を明記し、日本語化は表示ラベルと説明へ限定します。キーの日本語化は既存コードとの整合を失うため採りません。
- **スキルディレクトリ改名:** 採用可能です。実際に `E/retrieval/run_eval.py:38`、`E/evolution/test_loop.py:9` などにも固定パスがあるため、コマンド文書以外も含む参照検査が条件です。
- **docsサイトの初回除外:** 賛成です。ただし情報の削除とは分け、README・referencesへ残す内容を一覧化し、`.github/workflows/wiki-tests.yml` のdocsジョブも整合させます。
- **方針D・E・F:** 日本語スラッグ、依存の少ないbigram、小型モデルの評価による選定は維持できます。一律のNFKCによるファイル名変更、形態素解析の無条件必須化、評価前のモデル確定は採りません。
- **方針G:** ローカル機能に限定するなら関連クライアントの削除は整合します。「サーバーを提供しない」だけから削除が必然とはいえませんが、今回の対象範囲を明示すれば合理的です。
- **方針H:** 判定モデルを既定で無効にする案は妥当です。ただし「日本語で検証済み」と「任意機能として残すだけ」を分ける必要があります。未評価の判定・評価ループまで、1節の「どれも正しく動く」に含めるのは避けるべきです。
- **方針I:** 4,000／8,000文字を初期値とする案は採用可能です。本文だけかfrontmatter・コードブロックも含むかを定義し、境界値と行数制限との組合せを検査してください。
- **方針J:** 既存訳の利用に賛成です。移植前の状態を再現できる記録を先に確保し、取り込み確認後に元の作業ツリーを退避する順序を推奨します。

## 前提と不確実性

- 移植元HEADは `2d24ccfbbd46e77a431ddf2b1b54042d18576217`、未コミット変更は94ファイルで、計画の記述と一致しました。
- コードの読解と構文木比較を実施しました。プログラムの機能試験、モデルのダウンロード・推論、ビルドは実施していません。
- MiniLMの実際の切り詰め長、ダウンロード総量、日本語検索精度、Claude Code上の公開コマンド名は未実測です。登録情報・公式仕様と計画の不一致を指摘しています。
- bigramの1文字検索については、計画に記載された分割方式をそのまま実装するという仮定に基づきます。
- 作者の判断が必要なのは、検索品質の合格値、短いクエリの保証範囲、引用照合で許容する差、コマンド名、任意の判定機能を初回リリースでどこまで保証するかです。

## 参照

- [移植計画書](/C:/Works/info-hive/docs/porting-plan.html)
- [移植元のAGENTS.md](/C:/Works/llm-wiki-plugin/AGENTS.md)
- 検索・導入: [wiki_search.py](/C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/wiki_search.py)、[setup_wiki.py](/C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/setup_wiki.py)、[init_wiki.py](/C:/Works/llm-wiki-plugin/skills/llm-wiki/scripts/init_wiki.py)
- lint・統計・グラフ: `S/wiki_lint.py`、`wiki_stats.py`、`wiki_graph_extract.py`、`wiki_graph_lint.py`、`wiki_graph_query.py`
- 判定・評価ループ: `S/wiki_decide.py`、`wiki_evolve.py`、`wiki_evolve_loop.py`、`wiki_evolve_agent.py`。関連入口・設定の確認として `wiki_evolve_api.py`、`wiki_evolve_claude.py`、`wiki_evolve_stream.py`、`wiki_remote.py`
- 評価・CI: [retrieval/run_eval.py](/C:/Works/llm-wiki-plugin/eval/retrieval/run_eval.py)、`E/retrieval/test_embedding_mode.py`、`E/evolution/test_loop.py`、`E/decisions/test_decisions.py`、`.github/workflows/wiki-tests.yml`
- 配布・指示: `.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`、`commands/wiki/query.md`、`skills/llm-wiki/assets/SCHEMA.md.template`
- Git差分対象のPython29ファイルは、追加で構文木比較のために読み取りました。
- 外部一次資料: [FastEmbed v0.8.0ソース](https://github.com/qdrant/fastembed/tree/v0.8.0/fastembed)、[MiniLMのSentence Transformers設定](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/sentence_bert_config.json)、[Claude Code manifest仕様](https://code.claude.com/docs/en/plugins-reference)


## 相互レビュー

### エージェント A

## エージェント B のレビュー

### 長所
- Windows の CI を通すうえでの障害を、もっとも具体的に挙げています。symlink、chmod、shebang 付きの偽コマンド、`ps`、マニフェストに `st_mode & 0o777` を記録すること（`wiki_evolve.py:105`）の 5 つです。直し方として「evolution のテストは Linux だけ」と範囲を絞る案を出していて、現実的です。
- GitHub の Windows ランナーの既定は cp1252 なので、cp932 の不具合が CI では再現しない、という指摘は 4 案の中で B だけです。encoding の付け忘れを静的に検査する案も、判定できる形になっていて有効です。
- 3 つの指摘は正確で、方針 A の代案（履歴ごと取り込み、cherry-pick だけを行う）も妥当です。3 つとは、`LLM_WIKI_CONFIG` と設定ファイルはホスト型でしか使われないこと、モデルのキャッシュ `~/.cache/llm-wiki/fastembed` の扱いが計画に抜けていること、`build_suite.py` が `git show 06369b2` に依存することです。
- fastembed の実物を uv のキャッシュで調べ、次の 3 点を確かめています。MiniLM は量子化版の `-onnx-Q` であること、登録上の切り詰めは 512 トークンであること、e5 の接頭辞は自前で付ける必要があること。
- 次の点は B だけが拾っています。評価ループの ID が ASCII に制限されていること、`wiki_evolve_loop.py` が index.md と log.md に英語の行を生成し、重複を判定していること、`test_decisions.py` の hosted のテストが 5 件あること。

### 短所
- 重大度の高い不具合を 4 つ見落としています。
  - 解析キャッシュが古いままになる問題
  - P2 の基準を測ると JSON の解析で失敗する問題
  - 標準入力の UTF-8 化
  - D2 で空白をすべて除くと、別の文を引用と誤認する問題
- コマンドの名前空間について、「サブディレクトリ名が名前空間になる」を根拠に `/wiki:*` を維持できると判断しています。しかし公式の仕様は「プラグインのすべての部品をプラグイン名の下の名前空間に置く」と書いています（後述）。この前提は検証されていません。
- リリース数の訂正も誤っています。B は「11 リリース、メジャー 2 回は正しい」としていますが、CHANGELOG では 2026-07-20 の 2.0.0〜2.0.7 の 8 件と、3.0.0〜4.0.1 の 6 件を合わせて 14 件です。メジャーも 2.0.0、3.0.0、4.0.0 の 3 回です。
- 英語の成績が劣化するおそれ（`--gate` が相対比較であること）に触れていません。
- `PYTHONIOENCODING=cp932` が変えるのは標準入出力だけで、`open()` の既定の文字コードは再現できません。CI での再現策としては不完全です。

### 検証した事実
- 一致しました。`test_evolution.py:148`（`symlink_to`）、`wiki_evolve.py:105`（`st_mode & 0o777`）、`test_adapters.py:104-106`（chmod と symlink）、`test_decisions.py:78, 171, 212, 245`（hosted と wiki_remote）。
- fastembed の登録情報も一致しました。uv のキャッシュにある `text/pooled_embedding.py:50-59` は「512 input tokens truncation」、`size_in_GB=0.22`、`qdrant/…-onnx-Q`、`model_optimized.onnx` です。
- `commands/wiki/init.md:14` にモデル名があるとしていますが、実際は 15 行目でした（軽微）。
- リリース数は食い違いました（CHANGELOG の 113〜166 行で 2.0.x が 8 件）。

## エージェント C のレビュー

### 長所
- 重大度の高い穴を 3 つ見つけています。いずれも私が見落としたものです。
  - **P2 の基準を測れない**: `wiki_search.py` は日本語のクエリで標準出力を空にしたまま終わります。`run_eval.py:95-96` はそれを無条件に `json.loads` するので、「ほぼ 0 件」を記録する前に解析エラーで止まります。
  - **D2 で空白をすべて除く案は危険**: `now here` と `nowhere` が一致してしまいます。一致すると、`check_evidence` はモデルを呼ばずに `supported=True, confidence=1.0` を返します（`wiki_decide.py:418-422`）。誤った根拠が確定してしまいます。
  - **標準入力が UTF-8 でない**: `wiki_decide.py:640`、`wiki_evolve_agent.py:223`、`wiki_evolve_claude.py:44`、`wiki_evolve_api.py:130`。
- 「`(?<=[.!?])\s+` に句点を足すだけでは、空白のない `前文。後文。` を分割できない」という指摘も的確です。
- 評価ループの引用の照合 `grounded_verdict` が `quote in source` を直接比べていて、計画が挙げた `wiki_evolve.py:260` を直しても変わらない、という指摘も正しいです。
- 公式の仕様に照らして、`/wiki:*` を維持できるという前提を疑っています。

### 短所
- Windows の CI を妨げる POSIX 依存のテストを挙げていません。
- `LLM_WIKI_CONFIG` と設定ファイルの対象がなくなることも挙げていません。
- 評価ループの書き込みにある 2 つの不具合を見落としています。`json.dumps` が frontmatter を `\u` 表記で壊すこと（`wiki_evolve_loop.py:452, 458, 659`）と、`:429` で encoding を指定していないことです。
- `fake_backend.py` のトークン化が ASCII だけである点も見落としています。
- コマンド名について「通常は `/info-hive:wiki:query` になる」と書いています。これはプロジェクト用コマンドの規則からの推論で、プラグインのサブディレクトリの扱いは公式文書に書かれていません。C は不確実性を明記していますが、重大度を高とする根拠はやや強すぎます。
- 方針 A を「コピーでも上流のマージは義務ではない」とだけ評価していて、`build_suite.py` が移植元の履歴に依存する点を見ていません。
- 構文木が同一という主張を検証していません（本人も明記しています）。

### 検証した事実
- 一致しました。`run_eval.py:95-96`（`json.loads(stdout)["results"]`）、標準入力を読む 4 か所、`wiki_decide.py:418-422` の判定の短絡、`wiki_evolve_loop.py:73-79`（`e['quote'] in sources[...]`）。
- 公式文書を取得して確かめました（https://code.claude.com/docs/en/plugins-reference）。原文は「Claude Code namespaces every component under it」で、プラグイン名を接頭辞にする規則は確認できました。ただし、プラグインの `commands/` のサブディレクトリの扱いと、接頭辞を省略できるかどうかは書かれていません。C の結論は方向として正しく、具体的な名前は確かめられませんでした。

## エージェント D のレビュー

### 長所
- 重大度の高い指摘 5 件（P2 の JSON 解析エラー、gate が移植前と比べない、解析キャッシュ、標準入力、D2 の空白の除去）は、どれもコードで裏づけられています。並びも妥当です。
- C にない指摘が 3 つあります。
  - NFC の正規化がグラフの明示的な参照（`object`、`source`、`node_id`）まで及んでいないこと
  - 方針 C が、方針 G で削る設定を改名して残そうとしている矛盾と、それではモデルを設定で選べない点
  - 構文木が同一という主張を、docstring を含む比較と除く比較の両方で検証したこと
- 方針 J に「取り込んだ訳の差分かハッシュを保存しておかないと、出発点を再現できない」と付け加えていて、実務上有用です。
- gate の欠陥を、数値の例（0.9 から 0.1 に下がっても通る）で示していて、作者に伝わりやすいです。

### 短所
- Windows の CI の POSIX 依存、評価ループの frontmatter の破損と `:429`、`fake_backend` の ASCII 前提を見落としています。どれも C と同じ穴です。
- 単漢字のクエリについて、「保証するかを決める」とするだけで、漢字の unigram を併用するといった具体策を示していません。
- 英語の劣化の許容幅を、多言語モデルの性質と結びつけて論じていません。
- コマンド名の指摘は C と同じく、実際のコマンド名を確かめていません（本人も明記しています）。

### 検証した事実
- 一致しました。高の 5 件の根拠となる行、構文木の比較（私の比較と同じ結果）、計画書の内部の矛盾（P0 の前提と方針 H）。
- グラフの明示的な参照の行（`wiki_graph_extract.py:278-299`、`wiki_graph_lint.py:242-291`）は、今回は開いていません。確かめていません。

## 自分の結論の見直し

結論を改めます。私の指摘のうち、解析キャッシュ、英語の劣化と相対比較の gate、単漢字のクエリは維持します。そのうえで、次の点を加えて訂正します。

1. **高に加える指摘が 3 つ**
   - **P2 の基準の測定が解析エラーになる**（C・D の指摘）。P2 の作業に、評価ハーネスの空出力への対応を加えるべきです。
   - **標準入力の UTF-8 化**（C・D の指摘）。P1 に含めるべきです。
   - **D2 で空白をすべて除く案**（C・D の指摘）。英数字のあいだの空白は残し、日本語の文字のあいだの改行だけを吸収する案に替えます。私は D2 の症状が計画の記述どおりであることを確かめただけで、計画の直し方が危険だという点は見落としていました。
2. **中〜高に加える指摘が 1 つ**: コマンドの名前空間です。私は「`/wiki:*` はディレクトリから来る」と推測していました。公式の仕様ではすべての部品がプラグイン名の下に置かれるので、この推測は根拠が弱いです。P0 で、改名した最小構成のプラグインを入れて実際のコマンド名を確かめ、それを方針 C の確定条件にすべきです。
3. **中に加える指摘**: 評価ループの `grounded_verdict`、グラフの明示的な参照の NFC 化、句点のあとに空白がない場合の文の分割です。
4. **訂正が 2 つ**
   - MiniLM について、fastembed の登録情報は 512 トークンの切り詰めで、量子化版です。計画の「冒頭しか反映されない」は誤りの可能性が高く、正しくは「学習時の上限（128 トークン）を超える入力をそのまま与える」です。今の bge も、キャッシュ名から量子化版（`bge-small-en-v1.5-onnx-q`）と分かります。
   - リリース数は、私の 9 件も誤りでした。正しくは 14 件で、メジャーは 3 回です。
5. **B から取り入れる点**: CI は cp932 を再現できないので、Windows の完了判定は静的な検査で補うべきです。frontmatter のリストで「、」を区切りとして受け付けず、lint で指摘する案（B・C・D）にも賛成します。私の「共通のモジュールで同じ後処理をかける」より、変更が少なく安全です。

## 順位と確信度

1. **エージェント D**: 重大度の高い指摘 5 件がすべてコードで裏づけられています。そのうえ、グラフの参照、設定の矛盾、構文木の検証まで扱っていて、計画を直す順序がもっとも正確です。
2. **エージェント C**: D とほぼ同じ高の指摘を持っています。設定の矛盾と構文木の検証がない分だけ D に劣ります。
3. **自分**: 解析キャッシュ、英語の劣化、単漢字のクエリ、評価ループの書き込みの不具合、`fake_backend`、POSIX 依存のテストなど、他の案にない抜けを多く拾いました。一方で、P2 の測定の失敗、標準入力、D2 の過剰な正規化という高の指摘を 3 つ落とし、リリース数も誤りました。
4. **エージェント B**: Windows の CI と方針 A についての分析は最良です。ただし高の指摘を 4 つ落とし、コマンド名とリリース数の判断も誤っています。

確信度は中です。D と C の差は小さく、私と B の差も、どの指摘を重く見るかで入れ替わります。コマンド名の実際の挙動は誰も確かめていません。

### エージェント B

## Summary

All four pages loaded (HTTP 200). No fetch failed. I also fetched `https://code.claude.com/docs/en/plugins/components` because it is the page that holds the Commands section and the subdirectory rule. The plugins-reference and overview pages link to it as "Plugin components," though not to a `#commands` anchor.

Pages read:
- https://code.claude.com/docs/en/plugins-reference
- https://code.claude.com/docs/en/plugins
- https://code.claude.com/docs/en/plugins/components
- https://agentskills.io/specification

## 1. Plugin command invocation and subdirectories

Yes, the prefix is the plugin name. From the plugins-reference page, under `commands`:

> "In the object map, each key becomes the command name after the plugin prefix. For example, `"about"` in plugin `deploy-tools` runs as `/deploy-tools:about`."

From the components page, under "Commands":

> "Save a command at `commands/<file>.md` and it becomes `/<plugin>:<file>`. A subdirectory adds a segment, so `commands/db/migrate.md` is `/my-plugin:db:migrate`."

So a plugin named `llm-wiki` with `commands/wiki/query.md` is invoked as **`/llm-wiki:wiki:query`**. The other three forms (`/wiki:query`, `/llm-wiki:query`, and the reading that matches them) are not what the docs describe.

Caveats:
- The standard-layout table describes `commands/` as "Flat Markdown command files," and only the components page explains nesting.
- The components page says "Commands are the older format, and skills supersede them for new work."

## 2. Same command name in two plugins

None of the pages I read addresses two plugins defining a command with the same name. The only namespacing text is from the plugins-reference page:

> "Claude Code namespaces every component under it, so an agent `reviewer` in plugin `deploy-tools` appears as `deploy-tools:reviewer`."

Because the plugin name is part of every invocation, commands from plugins with different names should not collide. The conflict rules I did find cover other component types:
- Hooks: "When another enabled plugin has the same name, one of the two registers its `hooks/hooks.json` hooks and the other's are left out."
- LSP: "when two enabled servers claim the same extension, the first registered handles those files and the other isn't used for them, whether the servers come from one plugin or two."
- Settings: "Two plugins set the same key: the value from the plugin loaded last applies."

The `loading` page, which the docs link for same-name plugins, was not part of this request and I did not fetch it.

## 3. Does `name` have to match the skill directory?

**Agent Skills spec** (https://agentskills.io/specification), under "`name` field":

> "Must match the parent directory name"

The same list also requires 1–64 characters, only lowercase letters, numbers and hyphens, no leading or trailing hyphen, and no `--`.

**Claude Code docs** do not state this requirement. The components page (under "Skills") says:

> "**Command name**: `/<plugin>:<directory>`, so `skills/review/SKILL.md` in `my-plugin` is `/my-plugin:review`. If you set `name` in the frontmatter, it replaces the last segment and the plugin prefix stays."

The docs treat frontmatter `name` as an override of the directory-derived name, not as something that must equal the directory. The agents section says the same thing in a different form: "`name: audit` in `agents/review/security.md` loads as `my-plugin:review:audit`."

So the spec requires the match, and the Claude Code docs describe `name` as an override without mentioning the requirement. A plugin skill whose `name` differs from its directory would conform to Claude Code's documented behavior but not to the spec. I did not fetch the linked skills page ("how a skill gets its command name"), which may say more.公式文書の取得と、コードの照合が終わりました。結果をまとめます。

## エージェント A のレビュー

### 長所
- **解析キャッシュの世代管理の抜けを最初に挙げた。** `wiki_search.py` の `collect_pages` は sha256 が一致すると保存済みの `meta`・`sections` を使い、frontmatter を再解析しません。P1 の BOM・NFKC・区切りの修正が既存 wiki に効かないという指摘は正しく、私を含む他の全員が見落としていた最重要の抜けの一つです。
- **`--gate` が相対比較だけである点と、日本語の評価基準が「ほぼ 0 → 何でも上回る」になる点を、統計の粒度（1 問で約 3 ポイント）まで含めて示した。** 完了条件を判定可能にする具体案も付いています。
- **frontmatter の `sources:` に書かれる `\uXXXX` がデータ破壊だと見抜いた。** `wiki_evolve_loop.py:451-452,458,659` は既定の `ensure_ascii` で書き、手書きパーサーはエスケープを戻しません。計画はこれを「見た目」に分類しており、重大度の誤りを正しています。
- **`description=__doc__` のため `--help` はすでに日本語で、— を含む点**、**`package.json` が docs 専用のパッケージである点**、**CHANGELOG に `/wiki:migrate` が必ず残り完了条件と衝突する点**、**JSON 出力キーを訳してはいけない点**は、いずれも他の 3 体にない実用的な指摘です。
- 方針 A の代案として「履歴ごと取り込み、upstream を remote に残す」を提示しており、私と同じ結論に独立に至っています。

### 短所
- **リリース数の訂正が誤っている。** A は「2026-07-02 以降 9 リリース」としましたが、CHANGELOG には 2026-07-20 付で 2.0.0〜2.0.7 の 8 本と 3.0.0 があり、合計は 14 本です。私の最初の数（11）も誤りで、2.0.0〜2.0.2 を落としていました。
- **コマンド名前空間の前提を検証していない。** A は計画と同じく `/wiki:*` が維持できると仮定しています。公式文書では `commands/wiki/query.md` は `/<plugin>:wiki:query` になります（後述）。
- **標準入力の文字コードを見落としている。** `wiki_decide.py:641`、`wiki_evolve_agent.py:223`、`wiki_evolve_claude.py:44`、`wiki_evolve_api.py:130` の `sys.stdin` 読み取りは P1 の対象外のままです。
- fastembed の仕様（量子化版か、何トークンで切るか）を未確認のまま推測で書いています。推測の内容自体は正しく、推測と明記している点は誠実です。
- 25 件を 1 枚の表に詰めており、重大度の根拠が読み取りにくい箇所があります。

### 検証した事実
- 解析キャッシュの再利用: `wiki_search.py:233-237`（HEAD）で一致。schema 判定は `:127` のみで一致。
- `--gate` の相対比較: `eval/retrieval/run_eval.py:217-223` で一致。`hybrid_available()`（`:144-152`）が依存欠如時に hybrid を外す点も一致。
- `QUERIES.read_text()` の encoding 未指定: `run_eval.py:199` で一致。`test_decisions.py:40-42,488-490` の `text=True` も一致。
- `wiki_evolve_loop.py:429` の `write_text(encode(record))` の encoding 未指定、`wiki_evolve_agent.py:208` の `text=True`: 一致。
- hosted の漏れ: `wiki_decide.py:216-218`（`client_from_config` の分岐）、`:682`（`--profile`）: 一致。A の行番号は作業ツリー値で 1 行ずれます。
- Agent Skills 仕様の「`name` は親ディレクトリ名と一致」: 仕様書（agentskills.io）で一致。Claude Code の文書は `name` を上書きとして扱い、一致を要求していません。A の「記憶にもとづく」注記は正確でした。
- `package.json` の name が `llm-wiki-plugin-docs`: 一致。
- 評価コーパス 21 ページ・32 問: ページは `SCHEMA.md` を含めて 21 ファイル、本文ページは 20。問数は確かめていません。

## エージェント C のレビュー

### 長所
- **コマンド名前空間の前提の誤りを、公式文書を根拠に 高 で挙げた唯一のレビュー。** 私が取得した公式文書は「`commands/db/migrate.md` は `/my-plugin:db:migrate`」と明記しており、計画の「プラグイン名を変えても `/wiki:*` が残り、移植元と衝突する」という前提は文書上成り立ちません。方針 C と 6 節のリスクの根拠が崩れるため、重大度 高 は妥当です。
- **P2 の基準測定が「0 件」ではなく JSON 解析エラーになる点を発見した。** `wiki_search.py:729-732` は標準出力を空にして正常終了し、`run_eval.py:96,100` は無条件に `json.loads` します。P2 を既存ハーネスで実行できないという指摘は正確です。
- **標準入力の UTF-8 化の抜け**、**`grounded_verdict` の完全一致**（`wiki_evolve_loop.py:77-78` の `quote in source`）、**D2 で空白を全削除すると `now here` と `nowhere` が一致する**という 3 点は、計画の P1・P4 の修正案が目的を達しないことを示す具体的な反例です。
- 「。」で分割するには、現在の正規表現 `(?<=[.!?])\s+` が句読点の後の空白を要求するため、文字クラスに「。」を足すだけでは足りないという細部まで押さえています。

### 短所
- **計画の事実認定の検証が薄い。** 訳の構文木同一性、94 ファイル、コミット数、リリース数、fastembed の量子化・サイズは確認していません。「行番号は作業ツリーの値」と正しく前置きしていますが、計画の「v4.0.1 時点」という誤記を指摘していません。
- **Windows CI の POSIX 依存（symlink、chmod、shebang、`ps`）を挙げていない。** 完了条件「CI が Windows で通る」が達成できない問題は見えていません。
- **設定ファイルと `LLM_WIKI_CONFIG` がホスト型専用で、方針 C の改名対象が存在しない点を挙げていない。**
- 方針 G への「サーバーを提供しないことと接続しないことは別」というコメントは正しいですが、計画はすでに 7 節で「解釈」と明示しており、新しい情報ではありません。
- 低重大度の掃き出し（P0 前提の矛盾など）は #13 に圧縮されており、作者が修正箇所を特定しにくいです。

### 検証した事実
- 公式文書（code.claude.com/docs/en/plugins/components）: 「`commands/<file>.md` は `/<plugin>:<file>` になる。サブディレクトリは区切りを 1 つ足し、`commands/db/migrate.md` は `/my-plugin:db:migrate`」。C の主張と一致。ただし移植元 README は `/wiki:ingest` と書いており、CLI 上の実測は誰も行っていません。
- `run_eval.py:96,100` の無条件 `json.loads`: 一致。
- `grounded_verdict` の完全一致: `wiki_evolve_loop.py:77-78` で一致。
- 標準入力の 4 か所: 一致。
- D2 の引用一致で `confidence=1.0` を返す: `wiki_decide.py:422-423` で一致。`wiki_graph_lint.py:360` が一致した辺を D2 から除く: 一致。
- fastembed の MiniLM 登録情報「512 input tokens truncation」: 手元の uv キャッシュの `pooled_embedding.py` で一致。配布物は `qdrant/…-onnx-Q` の量子化版でした。

## エージェント D のレビュー

### 長所
- **C とほぼ同じ高重大度 5 件（JSON 解析エラー、gate、キャッシュ、標準入力、D2 の空白）を独立に確認し、さらに計画書の内部矛盾を行番号付きで示した。** 計画書の 551-553 行（P5 は P1 後）、683 行（P5 の前提は P0）、597 行（P0 の前提に C・G・J）、530 行（方針 H の「第 1 段階」）はすべて一致しました。
- **グラフの明示参照（`graph.node_id`、`relationships.object`、`source`、`contradicts`、`supersedes`）が NFC 正規化の対象から漏れている**ことを、抽出・lint・query の 3 スクリプトの完全一致箇所まで示しています。計画の方針 D はファイル名とリンクしか見ていないため、有効な補完です。
- 構文木の比較を 29 ファイルで自ら実施し、「docstring を除けば同一」と計画の記述を正確に言い直しています。私の結果と一致します。
- 方針 C と G の矛盾（削除する設定を改名しようとしている）を、`client_from_config` の行まで示して指摘しています。

### 短所
- **コマンド名前空間を 中 に据えている。** 公式文書の記述どおりなら、方針 C の前提（`/wiki:*` 維持、衝突リスク）が崩れるので、C と同じく 高 が適切です。
- **Windows CI の POSIX 依存を挙げていない。** 文字コード以外の理由で Windows ジョブが通らない点が抜けています。
- A が見つけた `--help` の日本語化、`package.json` の性格、CHANGELOG と完了条件の衝突、JSON キーの保護、`fake_backend.py` のトークン化、frontmatter `sources` の `\uXXXX` 破壊を挙げていません。
- 内容と構成が C と大きく重なり、独立した追加価値は上記の 3 点に絞られます。
- fastembed の登録情報は URL 引用のみで、手元での確認はしていません（本文で明記しており誠実です）。

### 検証した事実
- 計画書の行番号 530、551-553、597、683: 一致。
- `wiki_graph_extract.py:195-196`（明示 ID）、`:283-296`（`str(obj)`、`source` をそのまま出力）、`wiki_graph_lint.py:244,275,289-292`（完全一致で検証）、`wiki_graph_query.py:50-72`（SQL の完全一致）: 一致。
- `client_from_config` の hosted 分岐（HEAD 216-218）と `read_remote_profile`（177-192）: 一致。
- 構文木比較: 私の結果（docstring を除いて 29 ファイル SAME）と一致。

## 自分の結論の見直し

結論を改めます。私のレビューは事実認定の照合（fastembed の実物確認、構文木、hosted テストの漏れ、設定ファイルの所在、Windows の POSIX 依存）では堅実でしたが、計画の完了条件を無効にする 3 つの大穴を見落としていました。

- **追加する高重大度の指摘（3 件）**
  1. 解析キャッシュの世代管理がない。P1 の解析器修正は、`.wiki-cache/search-index.json` を持つ既存 wiki に効きません。P1 で schema を上げ、P3 で `VECTOR_INDEX_SCHEMA` と索引 meta に接頭辞・分割方式を含める作業が必要です。
  2. 検索の完了条件が判定できない。`--gate` は方式どうしの相対比較で、hybrid は依存欠如時に静かに外れ、P2 の基準測定は空出力で JSON 解析エラーになります。移植前の絶対値を保存し、許容幅と日本語の合格値を書き、hybrid の省略を失敗にする必要があります。
  3. 方針 C の前提が公式文書と食い違う。`commands/wiki/*.md` は `/<plugin>:wiki:*` になるため、プラグイン名を変えれば `/info-hive:wiki:*` になり、移植元との衝突も起きません。私は plugin-dev スキルの文言と移植元 README から `/wiki:*` を既定の挙動と判断しましたが、公式文書を優先します。P0 で実測し、`commands/` を平らにして `/info-hive:*` にそろえる案を推奨に変えます。Q1 の決定は前提ごと見直しが要ります。
- **追加する中重大度の指摘**: 標準入力の UTF-8 化（P1）、`grounded_verdict` の完全一致（P4 の対象を `wiki_evolve_loop.py:73-79` に広げる）、D2 の空白全削除は英数字間の語境界を失うので許容する差を限定する、「。」分割は空白を要求しない正規表現にする、frontmatter `sources:` の `\uXXXX` はデータ破壊として P1 の最初に直す。
- **訂正**: リリース数は「4 か月で 6」ではなく 14 本（2026-07-20 付の 2.0.0〜2.0.7 と 3.0.0、以降の 5 本）です。私の「11」も誤りでした。結論（更新が集中する）は変わりません。
- **維持する独自の指摘**: Windows の評価テストが symlink・chmod・shebang・`ps` に依存し文字コードを直しても通らないこと、GitHub の Windows ランナーは cp1252 で cp932 の条件を再現できないこと、`wiki_evolve_loop.py` が index・log 行と英語本文のページを書くため「ログ行の書式は自由に変えられる」は過大であること、方針 A は履歴ごと取り込む形にすべきこと、fastembed の MiniLM が量子化版であること。
- **方針への意見の変更**: 方針 C は「`/info-hive:*` に統一、P0 で実測」に変更。方針 H は「D2 の正規化を限定し、評価ループの引用照合も対象に含める」に変更。他は維持します。

## 順位と確信度

1. **エージェント A**。網羅性と独自発見の数で最も優れています。キャッシュ、gate、frontmatter の `\uXXXX` 破壊、`--help`、`package.json`、CHANGELOG と完了条件の衝突、JSON キーの保護は、いずれも検証して正しく、作者がそのまま計画に反映できる形です。欠点は名前空間の未検証とリリース数の誤りですが、全体の価値を覆しません。
2. **エージェント C**。公式文書に基づく名前空間の指摘、JSON 解析エラー、標準入力、`grounded_verdict`、D2 の反例と、計画の修正案が目的を達しない箇所を的確に突いています。計画の事実認定の照合が薄い点で A に劣ります。
3. **エージェント D**。C と同じ高重大度 5 件に加え、グラフの明示参照と計画書の内部矛盾を行番号で示しました。C との重複が大きく、名前空間の重大度を低く見た点で C の下に置きます。
4. **自分**。事実の照合（fastembed の実物、構文木、hosted テスト、設定ファイル、Windows の POSIX 依存）は最も細かく行いましたが、キャッシュ・gate・標準入力・名前空間という計画の成否を左右する 4 点を見落としました。見落としの重さを考えると最下位が妥当です。

確信度: **中**。1 位と 2 位の順序は、独自発見の数と検証の正確さで A が上という判断ですが、名前空間の前提崩れを 高 で捉えた C を 1 位とする見方にも合理性があります。名前空間の挙動は公式文書との照合のみで、CLI 上の実測は 4 体とも行っていません。

### エージェント C

## エージェント A のレビュー

調査範囲が広く、私が見落とした実装上の問題も拾っています。ただし、キャッシュ・Git・評価指標について説明の修正が必要です。

以下、`S/` は `C:\Works\llm-wiki-plugin\skills\llm-wiki\scripts\`、`E/` は同リポジトリの `eval\` を表します。行番号は訳済み作業ツリーのものです。

### 長所

- Windows 対応を製品コードだけでなく、テスト・偽バックエンドまで追っている点が優れています。
- `wiki_evolve_loop.py:429` の一時 JSON 書き込みから、UTF-8 固定の読み込みへ至る経路を指摘しています。単なる一覧の漏れではなく、失敗する理由を説明できています。
- 未コミット訳、過去の測定資料、設定ファイルの削除、文書の確定時期まで含め、移植作業の抜けを広く確認しています。

### 短所

- **キャッシュの説明が広すぎます。** トークンは毎回作り直されるため、トークン化変更だけで旧トークンが残るわけではありません。段落分割も、本文・locator が変わればベクトルの再計算につながります。問題は、旧解析結果の再利用と、入力文字列に現れない埋め込み設定変更に分けるべきです。
- **「共通履歴がなければ cherry-pick できない」は誤りです。** 必要なのは変更元コミットを取得できることで、main が共通祖先を持つことではありません。履歴の保持は有用ですが、必須にする根拠にはなりません。[Git 公式仕様](https://git-scm.com/docs/git-cherry-pick)
- `sources` の日本語エスケープ問題は発生条件の説明が不足しています。通常生成される出典名は ASCII です。日本語パスが入り得る場所と、既存状態から引き継ぐ場合を区別する必要があります。
- 標準入力の文字コード、P2 の空 JSON、D2 の過剰な空白除去という重大な問題を取りこぼしています。
- 多言語モデルの英語精度低下は仮説として扱うべきです。未測定の許容幅を、そのまま完了条件に採用することには賛成しません。

### 検証した事実

| 主張 | 結果と根拠 |
|---|---|
| 一時 JSON の文字コードが不整合 | **一致。** `S/wiki_evolve_loop.py:427–430` は encoding 未指定で書き、`S/wiki_evolve.py:45–50, 153–154` は UTF-8 固定で読みます。cp932 環境で日本語を含めると不整合になります。 |
| 偽バックエンドが ASCII 前提 | **一致。** `E/decisions/fake_backend.py:33–39, 56–64` は `[a-z0-9]+` の重なりを使います。ただし「判定全件が 0 点」ではなく、日本語だけの語彙重なりが 0 になるという表現が正確です。 |
| 解析キャッシュに版管理が必要 | **一致。** `S/wiki_search.py:232–245` は旧 metadata・links・sections を再利用します。一方、`:265, 331` でトークンは再生成されます。 |
| 日本語出典を JSON エスケープすると簡易パーサーが復号しない | **条件付きで一致。** `S/wiki_evolve_loop.py:452, 458, 659` と `S/wiki_search.py:81–85` に不整合があります。ただし新規生成名は `wiki_evolve_loop.py:421, 431` で ASCII になります。日本語の保存先では、同 `:436` の `raw` パスにも同種の問題があります。 |
| 32 問なので 1 問で約 3.1 ポイント変化 | **食い違い。** 集計すると正例 27、負例 5 です。`E/retrieval/run_eval.py:113–136` は別々に平均します。正例 1 問の最大寄与は `1/27 ≈ 3.70` ポイント、偽陽性率は `1/5 = 20` ポイントです。 |
| CHANGELOG に対象期間の 9 リリース | **食い違い。** `CHANGELOG.md:9–166` には 2026-07-20 以降の版の見出しが 14 個あります。公開済み GitHub Releases の件数は未確認です。 |

## エージェント B のレビュー

Windows と設定削除の指摘は有用ですが、採用案の一部が元の要件を狭めており、Git とコマンド仕様にも誤った前提があります。

### 長所

- Windows のテストに必要な追加作業を、計画の完了条件と結び付けています。
- ホスト型設定を削除する一方で改名しようとしている矛盾を、設定の利用箇所から説明しています。
- ログや索引には「読む側」だけでなく「生成する側」があるという指摘は、P4・P5 の整合確認に役立ちます。

### 短所

- **評価ループ全体を Windows 検証から外す推奨には反対です。** 計画の 1 節は評価ループも日本語対応の対象としています。除外するなら仕様変更であり、テストを通すための整理では済みません。移植可能な検証を残し、OS 固有の検査だけを分ける案が適切です。
- `ps`、`killpg`、`start_new_session` を Windows の障害として一括列挙していますが、既に OS 条件分岐があります。権限ビットを記録することも、同一 OS 内の比較だけなら直ちに障害にはなりません。
- A と同じく、共通履歴を cherry-pick の必須条件としている点は誤りです。
- コマンドのサブディレクトリだけから `/wiki:*` を維持できると判断した点は、現在の公式仕様との照合が不足しています。
- `PYTHONIOENCODING=cp932` は標準ストリームの設定であり、encoding 未指定のファイル読み込みまで cp932 にする手段ではありません。提案した CI 設定だけで全条件を検証できるとはいえません。

### 検証した事実

| 主張 | 結果と根拠 |
|---|---|
| Windows 向けにテストの修正が必要 | **一致。** `E/evolution/test_adapters.py:71–106` は shebang、chmod、拡張子のない symlink コマンドを使います。`test_evolution.py:148` にも symlink 作成があります。 |
| `ps` の検査も Windows で実行される | **食い違い。** `E/evolution/test_adapters.py:193–208` は `os.name == 'posix'` と実行可否で保護されています。実装側の `S/wiki_evolve.py:291, 298–321` にも Windows 分岐があります。 |
| ホスト型を削ると既存設定ファイルも不要になる | **一致。** `S/wiki_remote.py:223–227` の設定を `wiki_decide.py:176–190` が hosted 用に利用しています。モデルキャッシュは別途 `wiki_search.py:424–428` にあります。 |
| 履歴なしでは `build_suite.py` が動かない | **条件付きで一致。** `E/evolution/workflows/build_suite.py:10, 24` は固定コミットの存在を要求します。ただし過去の評価資料を再生成するツールであり、製品の通常実行すべてが履歴を要求するわけではありません。 |
| `/wiki:*` の維持はディレクトリ名だけで決まる | **公式仕様と食い違い。** `.claude-plugin/plugin.json:2` と `commands/wiki/query.md` の構成に対し、公式仕様はプラグイン名を接頭辞に含めます。対象バージョンでの実測は未実施です。[Claude Code 公式仕様](https://code.claude.com/docs/en/plugins/components#commands) |
| 対象期間の CHANGELOG は 11 リリース | **食い違い。** A と同じく、版の見出しとして数えるなら 14 個です。内訳は 2.0 系 8 個、3 系 4 個、4 系 2 個で、`8 + 4 + 2 = 14` です。 |
| FastEmbed の実行時上限は 512 トークン | **登録情報は一致、実動作は確かめられず。** ローダーは配布物の `tokenizer_config.json` を使います。登録情報だけでは実動作まで断定できません。[ローダーの実装](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/common/preprocessor_utils.py) |

なお、Windows ランナーの既定が cp1252 という主張は、今回も対象イメージでは確認できていません。

## エージェント D のレビュー

重大な失敗経路の選び方と、確認済み事実・推論・未実測事項の区別が、3 案の中で最も適切です。

### 長所

- P2 の基準測定が途中終了すること、検索ゲートが過去との比較になっていないことを、別の問題として明確に説明しています。
- 標準入力と D2 の誤一致を、実際の入口・早期 return まで追っています。修正案にも負例が含まれています。
- パースキャッシュとベクトル索引を区別し、既存の再構築機構を認めています。
- MiniLM の元モデル設定と ONNX 実行系を区別し、実ファイルを確認できていないことを明記しています。

### 短所

- Windows の既存テストの移植作業、偽バックエンドの ASCII 前提、一時 JSON の encoding 漏れを取りこぼしています。
- 「評価ループも検証する」という提案には賛成ですが、そのために必要なテスト基盤の修正まで具体化できていません。
- SCHEMA マーカーの呼び方の誤りは、現在の訳済みテンプレートでは文字列が保存されているため、重大度は低でもよいと考えます。実際に壊れる問題との優先順位を、もう少し明確にできます。

### 検証した事実

| 主張 | 結果と根拠 |
|---|---|
| 日本語クエリの基準測定が JSON 解析で失敗する | **一致。** `S/wiki_search.py:729–732` は JSON を出さず、`E/retrieval/run_eval.py:95–96, 119–121` は無条件に解析します。 |
| gate は変更前の成績を比較せず、hybrid を省略できる | **一致。** `E/retrieval/run_eval.py:140–151, 197–221`。 |
| 標準入力の UTF-8 対応が不足 | **一致。** `S/wiki_decide.py:640`、`wiki_evolve_agent.py:223` に直接の標準入力読み込みがあります。 |
| 空白の全削除は引用の誤一致を生む | **一致。** `S/wiki_decide.py:358–365, 418–423`。`now here` と `nowhere` が同じ文字列になり、モデルによる判定を経ずに通ります。これは処理規則からの推論です。 |
| ループの引用検証は `wiki_evolve.py` の修正だけでは変わらない | **一致。** `S/wiki_evolve_loop.py:73–89` は独立した直接比較を使います。 |
| 翻訳前後の AST は docstring を除けば同一 | **一致。** 今回、変更 Python 29 ファイルを再比較しました。通常の AST は 29 件すべて異なり、正規の docstring だけを除くと差は 0 件でした。 |
| 実際の MiniLM 切り詰め長は未確認 | **適切な留保。** 元設定・FastEmbed 登録情報・ローダーが別の情報源であることを確認しました。推論時の実測はしていません。 |

## 自分の結論の見直し

**「計画を修正してから進める」という結論は維持し、指摘を追加・修正します。**

私の初回案では、Windows のテスト基盤、一時 JSON の書き込み、削除される設定ファイルを十分に追えていませんでした。次を追加します。

- **高・P1:** `wiki_evolve_loop.py:429` の書き込みと `wiki_evolve.py:45–46` の読み込みを UTF-8 でそろえる。日本語の経験記録が通ることを確認する。
- **中・P1／P4:** Windows 用の偽コマンド起動方法と symlink 検査を整備する。評価ループ全体を除外せず、OS 固有部分を分ける。
- **中・P2／P4:** 偽バックエンドに日本語ケースを追加する。製品側と同じトークン化を使うだけでは共通の欠陥を見逃すため、期待応答を固定したケースも用意する。
- **中・方針 C／F／G:** ホスト型設定の削除と、モデル選択用設定の新設を区別する。モデルキャッシュの分離方針も決める。
- **低・2 節／付録:** AST 比較条件、行番号の基準、P0 の承認対象、リリース数の数え方を明記する。

また、私が中としていた**旧パースキャッシュの無効化は高へ引き上げます**。通常の検索手順がキャッシュを使うため、修正済みコードでも既存 wiki に効果が出ない問題は、完了判定を直接損ないます。

一方、履歴を main に取り込むこと、英語精度の許容低下を直ちに導入すること、Windows の評価ループ検証を全面除外することは採用しません。いずれも、確認された不具合から必然的に導かれる解決策ではありません。

今回はソース読解・集計・AST 比較だけを行いました。ファイル変更、ビルド、機能試験は行わず、指定された他エージェント成果の保存先も読んでいません。

## 順位と確信度

初回成果の順位です。指摘数より、重大な問題の捕捉、根拠の正確さ、修正案が元の要件を守るかを重視しました。

| 順位 | 案 | 理由 |
|---|---|---|
| 1 | エージェント D | 重大な失敗経路を押さえ、既存機構と未確認事項を正確に区別している。 |
| 2 | エージェント A | 調査範囲が広く、今回追加すべき実装・テスト上の抜けを最も多く提示した。説明の過大化と一部の誤認は修正が必要。 |
| 3 | 自分 | 主要な設計・判定上の問題は捕捉したが、Windows テスト基盤と評価ループの具体的な encoding 漏れを見落とした。 |
| 4 | エージェント B | 有用な追加指摘がある一方、Git の前提誤りと、検証対象の縮小による解決案が評価を下げる。 |

**確信度は中です。** ソースで確認できる比較には根拠がありますが、Windows CI、Claude Code の登録名、モデル推論は未実測です。特に 2 位と 3 位は、網羅性と重大問題の捕捉をどちらに重く置くかで入れ替わり得ます。

### エージェント D

Windowsテストの抜けは、エージェントA・Bの有効な追加指摘でした。ただし `ps` の実行には既にPOSIXの条件分岐があり、その点ではBの説明が広すぎます。また、CHANGELOGの該当期間の見出しは14件で、Aの9件・Bの11件とは一致しません。問題の発見と、説明・修正案の正確さを分けて評価します。
## エージェント A のレビュー

**網羅性が最も高く、追加調査の材料として有用です。ただし、キャッシュの説明と集計値には修正が必要です。**

以下、`S/` は移植元の `skills/llm-wiki/scripts/`、`E/` は `eval/` を指します。特記しない行番号は日本語訳を含む作業ツリーのものです。

### 長所

- **実装だけでなくテスト側の移植漏れまで調べています。** Windowsの偽実行ファイル、判定用の偽バックエンド、履歴に依存する評価資料は、私の前回レビューで具体的に扱えていなかった重要な指摘です。
- **`wiki_evolve_loop.py:429` の文字コード不整合を特定した点が優れています。** 日本語をエスケープしないJSONを既定文字コードで書き、直後にUTF-8として読む経路まで示しています。
- P5の文書確定がP3・P4に依存すること、docs削除後の配布ファイル整理、変更履歴に旧機能名を残す必要など、実装後の整合性にも目が届いています。
- 未確認のモデル仕様やAgent Skills仕様を、確認済みの事実と区別しています。

### 短所

- **【中】キャッシュ問題を広く言いすぎています。** トークンはキャッシュに保存されず毎回生成され、モデル変更時にはベクトル索引を再構築します。分割変更で検索対象文字列が変われば、そのベクトルも更新対象です。「トークン化・分割を変えても古い結果を返し続ける」と一括りにせず、旧パース結果の再利用と、埋め込み前処理の変更検出漏れを分けるべきです。
- **【中】偽バックエンドに本番と同じトークン化を使わせる案は、検証の独立性を弱めます。** 同じ不具合を両側で共有する可能性があります。日本語の固定応答を返すスタブと、トークン化自体の独立した期待値テストを分ける案が適切です。
- **【低】数値の根拠に誤りがあります。** recallの分母は全32問ではなく正例27問です。また、指定期間のCHANGELOG見出しは9件ではありません。後述のとおり14件です。
- 「英語精度が落ちる見込みが高い」は推測と明記されていますが、採用候補の実測がありません。許容劣化幅の例を、そのまま合格値に採用すべきではありません。

### 検証した事実

| 判定 | 主張と確認結果 |
|---|---|
| **一致** | `E/evolution/test_adapters.py:71–106` にshebang・chmod・symlinkを使う偽コマンドがあります。Windows対応作業の追加は必要です。 |
| **一致** | `S/wiki_evolve_loop.py:429–430` は既定文字コードで書いてから `capture()` を呼びます。`S/wiki_evolve.py:45–50, 153–154` はUTF-8読込と `ensure_ascii=False` を使用しています。 |
| **一致** | `E/decisions/fake_backend.py:33–39` はASCIIトークンしか扱いません。日本語の語彙重複による模擬判定には対応が必要です。 |
| **一部食い違い** | パースキャッシュの問題は正しいですが、トークンは `S/wiki_search.py:265` で再生成されます。モデル変更の再構築は同ファイル `447–461`、内容変更の更新は `505–518` にあります。 |
| **食い違い** | `E/retrieval/queries.json` は正例27問・負例5問です。`run_eval.py:122–136` より、正例1問のrecallが0から1になった場合の平均変化は `1/27 ≈ 3.70ポイント`。偽陽性率は負例1問で `1/5 = 20ポイント` 変わります。 |
| **食い違い** | `CHANGELOG.md:9–166` には対象期間の版見出しが14件あります。実際の公開リリース数とは区別が必要ですが、「CHANGELOGでは9件」は誤りです。 |
| **追加確認できた** | スキル名と親ディレクトリ名の一致は、記憶だけの懸念ではなく現在の[Agent Skills仕様](https://agentskills.io/specification#name-field)に明記されています。 |

## エージェント B のレビュー

**Windows対応と履歴依存の発見は有用ですが、重要な提案に誤った前提があり、そのまま採用するには修正が必要です。**

### 長所

- **Windows CIの問題を独立した移植作業として扱った点が重要です。** 文字コードを直せばすべて解決するという計画上の見落としを指摘しています。
- ホスト型設定の削除と、残るモデルキャッシュの扱いを区別しています。
- パースキャッシュにトークンが保存されないこと、モデル変更時にはベクトル索引が再構築されることなど、実装の細部を正確に押さえています。
- 量子化ONNX版を実際の評価対象として認識している点は、モデル名だけで比較するより適切です。

### 短所

- **【高】「共通履歴がないとcherry-pickできない」は誤りです。** 必要なコミットオブジェクトを取得すれば、共通祖先のないブランチにも変更を適用できます。独立コピーでは競合解決が増える可能性はありますが、履歴をmainへ統合する必然性はありません。[Git公式仕様](https://git-scm.com/docs/git-cherry-pick)は既存コミットの変更を適用する操作として定義しています。既存の初回コミットを捨てる案まで進める根拠としては不十分です。
- **【中】Windowsの評価ループを対象外にする推奨は、要求の縮小です。** 計画書354行は評価ループを対応範囲に含め、715行はWindowsですべてのスクリプトの入出力を要求しています。対象外にするなら、技術的な修正案ではなく製品要件の変更として提示すべきです。
- **【中】`ps` をWindowsでの失敗原因に含めた説明は不正確です。** 実際にはPOSIX条件で保護されています。問題のある偽実行ファイルと、既にOS分岐済みの処理を分ける必要があります。
- **【中】`PYTHONIOENCODING=cp932` ではファイル読書きの既定文字コードまで再現できません。** 対象は標準ストリームです。Windowsランナーのロケールを確認し、標準入出力とファイルI/Oを別々に検証すべきです。[Python公式仕様](https://docs.python.org/3.12/using/cmdline.html#envvar-PYTHONIOENCODING)。
- **【中】コマンド名前空間の根拠が現行公式仕様と食い違います。** サブディレクトリだけで `/wiki:*` が決まるという説明では、プラグイン接頭辞が抜けています。

### 検証した事実

| 判定 | 主張と確認結果 |
|---|---|
| **一致** | `E/evolution/workflows/build_suite.py:10, 24` は固定コミットを `git show` で取得します。コピー移植時に、この生成ツールの扱いを決める必要があります。ただし、mainの履歴統合以外にも、別checkout・必要オブジェクトの取得・固定資料としての分離という解決方法があります。 |
| **一部食い違い** | Windowsの偽コマンド問題は確認できました。一方、`E/evolution/test_adapters.py:193–196` は `os.name == 'posix'` で `ps` の実行を制限しています。 |
| **一致** | docs専用の `package.json:17–25` と、独立した `.github/workflows/wiki-tests.yml:49–59` のdocsジョブが存在します。サイト除外時の整理が必要です。 |
| **食い違い** | 現行公式仕様では `commands/wiki/query.md` は通常 `/<plugin>:wiki:query` です。Cの説明と一致します。[Claude Code公式仕様](https://code.claude.com/docs/en/plugins/components#commands)。 |
| **食い違い** | CHANGELOG上の対象期間の見出しは11件ではなく14件です。また、HEADの `wiki_search.py` の `Empty query.` は732行、作業ツリーでは731行であり、「両方で一致」も正しくありません。 |
| **確かめられず** | GitHub Windowsランナーの既定コードページを常にcp1252とする主張は、指定イメージを確認していないため追認しません。 |
| **一部確認** | FastEmbed 0.8.0の[登録定義](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/text/pooled_embedding.py)に量子化版・384次元・0.22 GB・512トークンの記載があります。ただし、これだけでは「学習時の上限が128」という主張や、実配布物の動作まで証明できません。 |

## エージェント C のレビュー

**3案の中で、重大な不具合の説明と検証範囲の区別が最も安定しています。統合レビューの土台に適しています。**

### 長所

- **検索ゲート、空JSON、D2の誤一致、標準入力を、実際の失敗経路に結び付けています。** 症状だけでなく、どの条件で失敗するかが明確です。
- **名前空間について現在の一次資料を使っています。** A・Bの推測やローカル参照文書より強い根拠です。
- キャッシュ問題を「モデル変更でも必ず残る」と誇張せず、既存の再構築処理を認めています。
- モデルの設定値・登録情報・実測を区別し、取得できなかったトークナイザー設定を未確認と明記しています。
- D2について、句読点の追加だけでは空白のない日本語文を分割できないことまで指摘しています。

### 短所

- **【中】Windowsテストの移植費用が抜けています。** 標準入力の修正は重要ですが、それだけでは既存CIをWindowsへ広げられません。A・Bが見つけた偽実行ファイルの問題を追加すべきです。
- **【中】日本語判定テストを追加する際の偽バックエンドの制約を見落としています。** `E/decisions/fake_backend.py:33–39` のASCII限定を考慮しないと、提案したD2検証の準備が不足します。
- **【中】方針CとGの設定ファイルの矛盾を拾えていません。** ホスト型専用設定を削除することと、ローカルのモデル選択設定を新設することを分ける必要があります。
- 評価ループの検証を広げる提案は妥当ですが、契約を確認するオフライン試験と、実モデルの日本語精度評価をどこまで初回に含めるかは、さらに具体化が必要です。

### 検証した事実

| 判定 | 主張と確認結果 |
|---|---|
| **一致** | `S/wiki_search.py:729–732` は日本語だけのクエリでJSONを出さず戻ります。`E/retrieval/run_eval.py:95–96` はJSON必須で、そのままでは基準測定が失敗します。 |
| **一致** | `S/wiki_decide.py:355` の文境界は空白を要求します。文字クラスへの「。！？」追加だけでは `前文。後文。` を分割できません。 |
| **一致** | `S/wiki_decide.py:362–365, 418–423` は引用一致で判定を省略し、確信度1.0を返します。空白全削除による誤一致は実装上の重要なリスクです。 |
| **一致** | `S/wiki_evolve_loop.py:73–89` の引用検証は直接の部分文字列比較です。`wiki_evolve.py` の正規化変更だけでは変わりません。 |
| **一致** | 現行Claude Code仕様はプラグイン接頭辞とサブディレクトリ名の両方を使います。Cの `/info-hive:wiki:query` という説明は仕様と一致します。ただし実機登録は未検証です。 |
| **未確認という扱いが適切** | FastEmbedは[トークナイザー設定から上限を取得](https://raw.githubusercontent.com/qdrant/fastembed/v0.8.0/fastembed/common/preprocessor_utils.py)します。今回も実配布物の設定と推論結果は確認しておらず、128／512の実動作を断定しない判断を支持します。 |

## 自分の結論の見直し

**「計画を修正してから実装すべき」という結論は維持します。ただし、私のレビューにはWindowsテストと評価用コードの調査が不足していました。次を追加・修正します。**

1. **Windowsテストの移植を高優先度の作業として追加します。**  
   P1またはP4に、偽コマンドの起動方法、symlinkを使う検証、テスト自身の文字コード対応を明記します。評価ループ全体を無条件にWindows対象外にする案は採りません。

2. **P2に評価ハーネス・偽バックエンドの整備を追加します。**  
   空JSON問題に加え、日本語の固定応答を扱うテスト基盤を用意します。本番トークン化と同じ実装を使うこと自体を合格根拠にはしません。

3. **`wiki_evolve_loop.py:429` の直接的な不整合を追加します。**  
   「全読書きを直す」という抽象的な指示だけでなく、書いた直後に別の文字コードで読み直す具体的な失敗経路を受入試験に含めます。

4. **コマンド名の指摘を強めます。**  
   前回は「維持できるか未証明」としましたが、今回確認した現行仕様では通常 `/info-hive:wiki:*` になります。対応版を決め、P0で公開名を検証する必要があります。

5. **方針Aは独立コピーも選択肢として残します。**  
   固定コミット依存の評価生成ツールは整理対象に加えますが、cherry-pickのためにmainへ上流履歴を統合する必要はありません。未コミット訳の保存・再現性の確保は引き続き必要です。

6. **行番号・集計値の注記を厳密にします。**  
   作業ツリーとHEADの行番号を区別します。リリースについて確認できたのはCHANGELOGの14見出しであり、GitHub上の公開リリース数ではありません。

ファイル変更・ビルド・機能テストは行っていません。禁止された成果ディレクトリも読んでいません。今回の検証は、ソース・Gitオブジェクトの読み取り、件数計算、公式仕様との照合です。

## 順位と確信度

| 順位 | 案 | 評価 |
|---|---|---|
| **1位** | **エージェントC** | 重大な失敗経路を正確に説明し、仕様・推論・未実測の境界が明確。 |
| **2位** | **エージェントA** | 調査範囲が広く、他案にない有効な移植漏れを発見。キャッシュ説明と数値は修正が必要。 |
| **3位** | **自分の前回案** | 主要な指摘の根拠は堅いが、Windowsテスト・偽バックエンド・履歴依存の調査が不足。 |
| **4位** | **エージェントB** | 有効な発見はあるが、Gitの前提、コマンド仕様、Windows対応の範囲変更に問題がある。 |

**1位の理由は、指摘の多さよりも、実装判断を誤らせにくいことです。** Cを土台に、AのWindows・評価基盤・文字コードの具体的な発見を加える構成を推奨します。

**順位の確信度は中です。** 特にAと自分の順位は、網羅性と記述の正確さのどちらを重く見るかで変わります。コードで確認した一致・食い違いへの確信度は高く、モデルの実精度、実際のWindows CI、インストール後のコマンド名は未検証です。


## 規則

- 票数ではなく根拠の質で優劣を付ける。複数が同じ結論でも、同じ資料を写しただけなら独立に同じ結論へ至ったとは言えない。
- レビューで確かめられた事実（一致、食い違い）を、レビュー前の主張より優先する。
- 正面から割れた論点は根拠で決める。決められないなら、決めるために何が要るかを書く。
- 作者の判断が要ることは、選択肢とそれぞれの帰結を並べる。
- 日本語で書き、結論を先に書く。

## 最終メッセージの形式

```markdown
# <タスクの主題> の総括

## 推奨
（決定を 1〜2 文で）

## 批評で動いたこと
- （他の成果を読んで結論を改めたエージェントと、その内容）
- （最も鋭い反論と、それが決定的だったか）

## 推奨案が残る理由
- （レビューで確かめられた根拠）

## エージェントごとの要約
（エージェント A、エージェント B、エージェント C、エージェント D の結論を 1 段落ずつ。レビューで受けた指摘のうち当たっていたものを添える）

## 残るリスクと未確定事項
- （作者の判断が要ること。選択肢と帰結）

## 記号とモデルの対応
| 記号 | モデル |
| --- | --- |
| A | Claude Opus 5.5（`claude-opus-5-5`） |
| B | Claude Fable 5.1（`claude-fable-5-1`） |
| C | GPT-6.1 Sol（`gpt-6.1-sol`） |
| D | GPT-6 Astra（`gpt-6-astra`） |
```
