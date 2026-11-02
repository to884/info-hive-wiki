# リポジトリの指示

info-hive は、Claude Code プラグイン llm-wiki（praneybehl/llm-wiki-plugin v4.0.1）を日本語に特化して移植したものである。作業の前に [docs/porting-plan.html](docs/porting-plan.html) の方針と段階を読み、それに従う。移植元との関係は [UPSTREAM.md](UPSTREAM.md) に記録する。

## 応答

- ユーザーへの応答は日本語で書く。コード、コマンド、パス、識別子は原文のまま書く。

## 名前

- プラグイン名は `wiki`、マーケットプレイス名とスキル名は `info-hive` である。コマンドは `/wiki:<名前>` の形で起動する。
- スキルの本体は `skills/info-hive/` にある。コマンドからは `${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/...` で参照する。

## 日本語と機械が読む文字列

- 人とエージェントが読む文章（文書、コメント、出力メッセージ）は日本語で書く。
- frontmatter のキー、`type`・`kind` の値、グラフの述語名、`--json` の出力のキー、コマンドラインの引数名、`SCHEMA.md` のアップグレード検出用の見出しは英語のまま残す。frontmatter のキーと `type`・`kind` の値は日本語の別名でも読めるようにするが、スクリプトとテンプレートが書くときは英語を使う（計画の方針 B）。
- ファイルの読み書きと子プロセスとの入出力は UTF-8 を明示する。Windows の既定は cp932 である。

## 移植元の修正

- 移植元の修正を取り込んだら、[UPSTREAM.md](UPSTREAM.md) の表に 1 行追加する。
- info-hive の修正を移植元に送ることはしない。

## リリース

- この指示でいう「公開」は、Claude Code プラグイン、skills.sh ディレクトリのスキル、GitHub リリースの公開だけを指す。npm への公開は、ユーザーが明示的に求めない限り含めない。
- ユーザーに見えるプラグインやスキルの変更は、マージ前に SemVer のバージョンを決める。バージョンは `.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json` の 2 か所、`CHANGELOG.md` をまとめて更新する。スキルには独自のバージョンがなく、プラグインのバージョンを引き継ぐ。
- リリースのコミットが `main` に入り、`vX.Y.Z` のタグが push され、GitHub リリースが作成され、マーケットプレイスからの導入を確かめるまでは、リリースの完了を報告しない。
