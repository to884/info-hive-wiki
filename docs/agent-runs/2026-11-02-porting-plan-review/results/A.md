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
