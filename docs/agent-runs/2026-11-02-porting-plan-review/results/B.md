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
