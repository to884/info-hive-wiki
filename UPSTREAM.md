# 移植元の記録

info-hive は、[praneybehl/llm-wiki-plugin](https://github.com/praneybehl/llm-wiki-plugin)（MIT ライセンス、作者 Praney Behl）を日本語向けに移植したものである。移植元の履歴は取り込まず、ファイルをコピーして独立させた（[移植計画](https://github.com/to884/info-hive-dev/blob/main/docs/porting-plan.html)の方針 A）。

## 起点

| 項目 | 値 |
| --- | --- |
| リポジトリ | https://github.com/praneybehl/llm-wiki-plugin |
| バージョン | v4.0.1 |
| コミット | `2d24ccfbbd46e77a431ddf2b1b54042d18576217` |
| 取り込んだ日 | 2026-11-02 |

起点のコミットに、手元のフォーク（`to884/llm-wiki-plugin`）にあった未コミットの日本語訳 94 ファイルを重ねた状態を取り込んだ。訳を入れた各ファイルの SHA-256 は 開発用のリポジトリの [docs/upstream/translation-hashes.txt](https://github.com/to884/info-hive-dev/blob/main/docs/upstream/translation-hashes.txt) に残してある。

## 取り込まなかったもの

| 移植元のパス | 理由 |
| --- | --- |
| `server/` | ホスト型 wiki を提供しないため（方針 G） |
| `skills/llm-wiki/scripts/wiki_remote.py`、`skills/llm-wiki/references/hosted-mode.md`、`commands/wiki/migrate.md`、`eval/remote/`、`docs/hosted.md` | 同上 |
| `docs/.vitepress/`、`docs/public/`、`docs/v3/`、`package.json`、`pnpm-lock.yaml`、`.github/workflows/deploy-docs.yml` | docs サイトを持たず、`docs/` の Markdown だけを残すため |
| `research/` | 移植元の調査メモで、移植版には当てはまらないため |
| `eval/decisions/results-fake.json`、`eval/decisions/results-local-d1-2b.json` | 移植元の英語の評価セットでの測定記録。判定レイヤーのしきい値の根拠として移植元で参照できる |
| `eval/evolution/workflows/build_suite.py`、`eval/evolution/workflows/frozen-runtime.patch` | 移植元の固定コミットを `git show` で読むツールとその入力。スイートを作り直すときは移植元の起点のコミットで実行する |
| `AGENTS.md` | 開発の指示は開発用のリポジトリ（to884/info-hive-dev）に置くため |

## 取り込み後の主な変更

- プラグイン名を `wiki`、マーケットプレイス名とスキル名を `info-hive` にした。コマンドは `commands/` の直下に移し、`/wiki:query` の形で起動する。
- スキルのディレクトリを `skills/llm-wiki/` から `skills/info-hive/` に改名した。
- 判定レイヤーの環境変数を `LLM_WIKI_DECISIONS*` から `INFO_HIVE_DECISIONS*` に、モデルのキャッシュを `~/.cache/llm-wiki/` から `~/.cache/info-hive/` に変えた。

日本語への対応の内容は [移植計画](https://github.com/to884/info-hive-dev/blob/main/docs/porting-plan.html) と `CHANGELOG.md` にまとめる。

## 移植元の修正を取り込む手順

移植元に役立つ修正が入ったら、必要なものだけを取り込む。履歴を共有していないので、パスの違い（`skills/llm-wiki/` と `skills/info-hive/`、`commands/wiki/` と `commands/`）に注意する。

```bash
git remote add upstream https://github.com/praneybehl/llm-wiki-plugin.git   # 初回だけ
git fetch upstream
git log --oneline 2d24ccf..upstream/main
git cherry-pick <コミット>   # 衝突したら、パスを読み替えて手で直す
```

取り込んだら、下の表に 1 行追加する。

## 取り込んだ移植元の修正

| 日付 | 移植元のコミット | 内容 | info-hive のコミット |
| --- | --- | --- | --- |
| （まだない） | | | |
