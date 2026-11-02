先ほどのタスクに、あなたを含む 4 体のエージェントが独立して取り組みました。
あなた以外の 3 体の成果を渡します。エージェントは「エージェント A」のように記号で呼び、モデル名は明かしません。
あなたの最終メッセージだけがレビューとして保存されます。

## タスク（再掲）

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


## 他のエージェントの成果

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


## 規則

- 各成果について、長所と短所の両方を書く。自分の案を守るためではなく、相手が優れている点を認めるレビューが役に立つ。
- 短所は、誤った前提、見落とし、リスク、隠れた費用、未検証の主張として具体的に書く。
- リポジトリを読めば確かめられる主張は確かめ、結果（一致、食い違い、確かめられず）を書く。
- `docs\agent-runs\2026-11-02-porting-plan-review` の配下は読まない。
- 日本語で書き、結論を先に書く。

## 最終メッセージの形式

1. `## エージェント X のレビュー`（3 体ぶん）。各節に `### 長所`、`### 短所`、`### 検証した事実` を置く
2. `## 自分の結論の見直し`：他の成果を読んで結論を改めるか。改めるなら何をどう、改めないならなぜ
3. `## 順位と確信度`：自分を含む 4 案の順位、1 位の理由、確信度（高・中・低）
