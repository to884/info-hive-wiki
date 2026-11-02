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
