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
