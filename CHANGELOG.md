# 変更履歴

このプラグインの主な変更は、すべてこのファイルに記録する。

書式は [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) にもとづき、このプロジェクトは [Semantic Versioning](https://semver.org/spec/v2.0.0.html) に従う。

## [Unreleased]

### Changed

- **互換性のない変更:** `/wiki:init` と `/wiki:upgrade` の `--global` を `--user` に改名した。`--global` は受け付けず、渡されたときは何も実行せずに `--user` を使うよう伝える。作られる場所（`~/info-hive/wiki/` と `~/info-hive/raw/`）は変わらない。文書の「個人用のグローバルな wiki」も「ユーザースコープの wiki」に改めた。エージェントのグローバルな指示と、スキルのグローバルなインストールの呼び方は変えていない。
- `/wiki:help` の一覧を、コードブロックのプレーンテキストではなく、書式付きの Markdown で表示するようにした。コマンド名は太字のインラインコード、引数の書式はインラインコードにするので、`<質問>` の山かっこや `|` も崩れない。`--json` の出力は変わらない。

## [0.1.0] - 2026-11-04

移植元 praneybehl/llm-wiki-plugin v4.0.1 を日本語の wiki 向けに移植した、最初のリリース。移植元のバージョンごとの変更履歴は [docs/upstream-changelog.md](docs/upstream-changelog.md) にある。

### Added

- `/wiki:help` を追加した。`commands/*.md` の frontmatter から、コマンド名・引数・説明・引数の説明の一覧を `wiki_help.py` で生成して表示する。引数の説明は、各コマンドの frontmatter に足した `argument-description` に書く。
- `wiki_lint.py` は、`created`・`updated`・`ingested` が `YYYY-MM-DD` の形でなければ `non_iso_dates` として指摘する。
- `wiki_lint.py` は、二重引用符で囲まれていない文字列の値を `unquoted_strings` として指摘する。
- `wiki_encoding.py` を加えた。テキストのソースの文字コード（UTF-8、Shift_JIS（CP932）、EUC-JP、ISO-2022-JP など）を判定し、`--output` で UTF-8 に変換したものを別のファイルに書き出す。元のファイルは書き換えない。`/wiki:ingest` の手順に加えた。

### Changed

- 移植元 [praneybehl/llm-wiki-plugin](https://github.com/praneybehl/llm-wiki-plugin) v4.0.1（コミット `2d24ccf`）を起点に、プラグイン名を `wiki`、スキル名とマーケットプレイス名を `info-hive` にした。コマンドは `commands/` の直下に置き、`/wiki:<名前>` の形で起動する。スキルのディレクトリを `skills/llm-wiki/` から `skills/info-hive/` に、判定レイヤーの環境変数を `LLM_WIKI_DECISIONS*` から `INFO_HIVE_DECISIONS*` に、モデルのキャッシュを `~/.cache/llm-wiki/` から `~/.cache/info-hive/` に変えた。移植元との関係は `UPSTREAM.md` に記録している。
- `/wiki:init --global` の既定を、`~/wiki/` と `~/wiki/raw/` から `~/info-hive/wiki/` と `~/info-hive/raw/` に変えた。標準のグローバル構成の初期化とアップグレードは、ベースディレクトリに `~/info-hive` を渡すだけでよい（`--wiki-dir` と `--raw-dir` は既定の `wiki` と `raw`）。`init_wiki.py` は、ベースディレクトリがなくても親のディレクトリがあれば作る。親もないときは止める。以前の既定で作った wiki は移動せず、今までどおり `--wiki-dir wiki --raw-dir wiki/raw` で指す。
- frontmatter の文字列の値は、配列の要素も含めて二重引用符で囲む決まりにした（`type: "source"`、`tags: ["税制", "間接税"]`）。日付と数値は囲まない。lint・検索・統計の frontmatter の解析を `wiki_text.py` の共通のパーサーにまとめ、引用符の中のカンマで配列を分けず、引用符の外の行末コメントを捨て、`\"` と `\\` を戻すようにした。評価ループが書くページとテンプレートも引用符つきにした。検索のパースキャッシュは自動で作り直される。
- リポジトリを `to884/info-hive` から `to884/info-hive-wiki` に移した。旧リポジトリはアーカイブする。
- マーケットプレイスの定義（`.claude-plugin/marketplace.json`）を、別のリポジトリ [to884/info-hive-marketplace](https://github.com/to884/info-hive-marketplace) に移した。マーケットプレイス名は `info-hive` のままで、`/plugin install wiki@info-hive` で入れる。
- 旧リポジトリをマーケットプレイスとして追加していた場合は、`/plugin marketplace remove info-hive` のあとに `/plugin marketplace add to884/info-hive-marketplace` と `/plugin install wiki@info-hive` を実行し直す。
- `npx skills add` と `git clone` で指定するリポジトリも `to884/info-hive-wiki` になる。
- 検索のトークン化を日本語に対応させた。NFKC で正規化して小文字にしたあと、文字種の境界で区切り、英数字は語単位、かなと漢字は文字 bigram にする。漢字は 1 文字単位のトークンも加え、「税」のような単漢字のクエリが「消費税」に当たるようにした。英数字だけのテキストのトークンは変わらない。
- 日本語のトークンを含むクエリでは、IDF で重みを付けたクエリの被覆率が 0.25 に満たないページを、語彙ベースの結果から外すようにした。文字 bigram が関係のないページと偶然に一致するのを防ぐためである。
- 既定の埋め込みモデルを、英語専用の `BAAI/bge-small-en-v1.5` から多言語の `intfloat/multilingual-e5-small`（fp16 の ONNX、約 0.25 GB）に替えた。クエリとセクションに `query: ` と `passage: ` の接頭辞を付け、コサイン距離の上限を 0.35 から 0.193 にした。既存の `embeddings.sqlite` は、次の検索で自動的に作り直される。環境変数 `INFO_HIVE_EMBED_MODEL` でモデルを選べるようにした。大きいモデルとして `intfloat/multilingual-e5-large`（約 2.24 GB、距離の上限 0.19）も選べる。
- fastembed を読み込む前に `HF_HUB_DISABLE_SHARED_BLOBS=1` を設定するようにした。huggingface_hub 1.33 の共有の置き場所では、重みを `model.onnx_data` に分けたモデルを onnxruntime 1.31 が読み込めないためである。
- 語彙側のトークンが 0 個のクエリでも、ハイブリッド検索では埋め込み検索を行うようにした。
- `wiki_lint.py` は、空白を除いた文字数でもページの大きさを判定するようにした。目安は 4,000 文字、上限は 8,000 文字で（`--soft-char-cap`、`--hard-char-cap`）、行数の上限と、どちらかを超えたら指摘する。`--json` の `oversized_hard`・`oversized_soft` の各項目に `chars` を加えた。
- `wiki_lint.py --suggest-pages` に、3 文字以上のカタカナ語と、3 文字以上の漢字の複合語を加えた。
- `wiki_stats.py` に文字数を加え、大きいページを文字数の多い順に並べるようにした。
- D2 の文分割を「。！？」に対応させ、引用の照合で日本語の文字どうしの間の改行と空白を吸収するようにした。英数字の間の空白は区別する。評価ループの引用と答えの照合も同じ規則にした。
- 評価ループが wiki に書くページ、index と log の行、`PURPOSE.md` の文面を日本語にした。
- スクリプトの出力、`--help`、lint の報告を日本語にした。表は全角の文字を 2 桁と数えてそろえる。`--json` のキーは英語のまま残している。
- SKILL.md、コマンド、references、テンプレートを、日本語の wiki の運用として書き直した。
  - SKILL.md の起動条件（`description`）に、日本語の依頼の例を加えた。
  - スラッグには日本語を使ってよいとし、英字は小文字にしてハイフンでつなぐこと、空白と使えない記号、NFC での照合を書いた。
  - ページの大きさを、空白を除いた文字数（目安 4,000 文字、上限 8,000 文字）と行数で書くようにした。語数の目安はやめた。
  - 引用の書き方（本文では「」で囲む、グラフの `evidence` と判定・評価ループの根拠はソースの文をそのまま写す）と、照合の規則を書いた。
  - ログ行の説明とサブ行を日本語で書くようにした。操作名（`ingest`、`query`、`lint` など）は英語のまま残す。
  - 英語の wiki を前提にした例文を、日本語の wiki の例に替えた。
  - 埋め込みモデルの記述を `intfloat/multilingual-e5-small` に直し、日本語のクエリを分かち書きせずに渡せることを書いた。
  - `/wiki:upgrade` に、移植元の llm-wiki で作った wiki から移るときの注意点を書いた。
- テンプレートの見出しを日本語にした。`SCHEMA.md` のアップグレード検出に使う目印（`## Skill evolution`、`## Optional graph metadata`、`## Graph layer`、`## Retrieval` と、`Graph lint + extract`、`- Semantic backend: local FastEmbed + sqlite-vec` の行）だけは英語のまま残している。評価ループが `index.md` と `log.md` を新しく作るときの見出しも、テンプレートに合わせた。
- オントロジーのテンプレートの述語の説明を日本語にした。述語名とノードの種類は変えていない。

### Removed

- ホスト型 wiki を提供しない。移植元の `server/`（Cloudflare Worker）、`wiki_remote.py`、`references/hosted-mode.md`、`/wiki:migrate`、`/wiki:init --hosted`、`eval/remote/`、`docs/hosted.md` と、環境変数 `LLM_WIKI_URL`・`LLM_WIKI_TOKEN`・`LLM_WIKI_CONFIG` を削った。wiki はローカルのディレクトリだけを扱う。
- ドキュメントのサイト（VitePress）と GitHub Pages へのデプロイのワークフローを削り、`docs/` の Markdown だけを残した。
- 移植元の調査メモ `research/` と、移植元の英語の評価セットでの測定記録（`eval/decisions/results-*.json`）は取り込んでいない。
- Paperclip 連携は移植元の v4.0.0 で削除されており、info-hive にもない。

[Unreleased]: https://github.com/to884/info-hive-wiki/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/to884/info-hive-wiki/releases/tag/v0.1.0
