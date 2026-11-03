# コントリビュートの手引き

このプラグインの改善に関心を持ってくれたことに感謝する。以下の取り決めは、コントリビュートを滞りなく取り込むためのもので、参加を制限するためのものではない。小さな PR も歓迎する。

info-hive は、Praney Behl の [llm-wiki-plugin](https://github.com/praneybehl/llm-wiki-plugin) v4.0.1 を日本語向けに移植したものである。移植元とは独立して開発しており、info-hive への修正を移植元に送ることはしない。起点のコミットと取り込んだ修正は [UPSTREAM.md](./UPSTREAM.md) に記録する。

## 受け付けやすいコントリビュート

コントリビュートを受け付けやすい場所は4つある。スキル本体（`skills/info-hive/SKILL.md` と references）、同梱のスクリプト（`skills/info-hive/scripts/`）、スラッシュコマンド（`commands/`）、ドキュメント（README、CHANGELOG、`docs/`、このファイル）である。どれについても、不具合の修正と小さな改善はそのまま受け入れやすい。

大きな変更（初期化で作るページの種類の追加、lint のチェックの追加、references の構成の変更、既定のスキーマの変更など）は、コードを書く前に issue を立てて、変更の形を相談してほしい。このスキルは意図して特定の設計を選んでいる。アトミックなページ、インデックスを先に読むナビゲーション、設定としてのスキーマなどである。「改善」のつもりの変更が、意図したトレードオフを崩すこともあるので、先に issue で話しておけば手戻りが減る。

合わないと思われるのは、Markdown を正本とする方針から離れる機能や、ローカルのフォルダーで動くモードを外部のサービスに依存させる機能である。別の正本のデータベースや、2つ目のサービスが必要な案は、このリポジトリへの PR より、別のプロジェクトとして進めるほうが向いている。その方向を探るなら、README で紹介している実装（OmegaWiki、Synthadoc など）が参考になる。

## 開発環境の準備

フォークをクローンし、ローカルのチェックアウトを指す形で、Claude Code のセッションにプラグインをインストールする。

```bash
git clone https://github.com/<your-fork>/info-hive
cd info-hive
```

続いて Claude Code で、ローカルのパスをマーケットプレイスとして追加し、インストールする。

```
/plugin marketplace add /absolute/path/to/info-hive
/plugin install wiki@info-hive
```

変更したら、`/plugin marketplace update` で更新してからインストールし直す。ビルドの手順はない。スキル、コマンド、スクリプトは、ファイルシステムにあるものがそのまま動く。

同梱の Python スクリプトは Python 3.10 以上を対象にする。改善のワークフロー、語彙ベースの検索、lint、stats、グラフのクエリは標準ライブラリだけを使う。実行環境のセットアップ、ハイブリッド検索、グラフのコンパイルは、uv を通じて、バージョンを固定した既存の PEP 723 の依存パッケージを使う。新しい補助の処理には標準ライブラリを優先し、改善のワークフローには依存パッケージを加えない。

## 変更を確かめる

提出する前に、関係するリポジトリ内のテストを実行し、変更したワークフローを使い捨ての wiki で試す。

```bash
uv run --with fastembed==0.8.0 --with sqlite-vec==0.1.9 --with PyYAML==6.0.3 python -m unittest discover -s eval/retrieval -p 'test_*.py'
python skills/info-hive/scripts/test_link_resolution.py
python eval/evolution/test_evolution.py
python eval/evolution/test_loop.py
python eval/evolution/test_adapters.py
python eval/retrieval/run_eval.py --gate
python eval/retrieval/run_eval.py --set ja --gate
python eval/decisions/test_decisions.py
```

検索の変更では、`eval/retrieval/baselines/` に保存した基準値とも比べる。`--gate` は方式どうしの相対比較しかしないので、英語の成績が全体に下がっても通る。基準値を取り直すときは、ハイブリッド検索の依存パッケージを入れて `--save` を付ける。

```bash
uv run --python 3.12 --with PyYAML==6.0.3 --with fastembed==0.8.0 --with sqlite-vec==0.1.9 python eval/retrieval/run_eval.py --save eval/retrieval/baselines/en.json
uv run --python 3.12 --with PyYAML==6.0.3 --with fastembed==0.8.0 --with sqlite-vec==0.1.9 python eval/retrieval/run_eval.py --set ja --save eval/retrieval/baselines/ja.json
```

判定レイヤーのテストは偽のバックエンドを使う。非公開の wiki のテキストを、外部の判定バックエンドに送ってはならない。

改善のワークフローのテストは、決まった結果を返すランナーでライフサイクルの動作を確かめるもので、モデルの品質が上がったことを確かめるものではない。実際のモデルを使う試験には、ランナーとモデルを明示して選び、推論の予算を決める必要がある。その結果は別に記録する。

手作業で行う最小限のスモークテストは次のとおり。

```bash
mkdir /tmp/test-wiki && cd /tmp/test-wiki
python /path/to/info-hive/skills/info-hive/scripts/init_wiki.py .
# wiki/sources/ や wiki/concepts/ などの下に、Markdown のページを手でいくつか作る
# わざと問題を入れておく（孤立ページ、リンク切れの [[wikilink]]、frontmatter の欠落）
python /path/to/info-hive/skills/info-hive/scripts/wiki_lint.py wiki/
python /path/to/info-hive/skills/info-hive/scripts/wiki_stats.py wiki/
python /path/to/info-hive/skills/info-hive/scripts/wiki_search.py "your query terms"
```

スキル本体（`SKILL.md` や references）を変えたときにもっとも役立つのは、実際に取り込んでみることである。テスト用のプロジェクトの `raw/` に論文や記事を置き、`/wiki:ingest <path>` を実行し、できあがったものを読む。ワークフローがずれて出力の質が落ちているなら、スキルを直す必要がある。

スラッシュコマンドを変えたときは、新しいプロジェクトにプラグインをインストールし、コマンドを最初から最後まで実行して確かめる。

## スキルの文章の書き方

スキル本体と references は、一定の文体と構成で書いている。新しく書く文章も、既存の文章にそろえる。

references のファイルは、箇条書きを最小限にした文章で書く。ばらばらの要点を並べるのではなく、理由でつないで説明する。これは意図した選択である。スキルを読む LLM にとっては、箇条書きの断定より、つながった説明のほうが役に立つ。箇条書きが3つか4つより多く続くようなら、文章で書いたほうがよくないかを考え直す。

指示は命令の形で書き（「ソースを読む」「先にスキーマを確かめる」）、理由は説明の形で書く（「大きさに上限を設ける理由は……」）。本当に譲れない制約でない限り、大文字の MUST や NEVER のような強い言い方は避け、制約がある理由を説明する。

まだ確かめられていない主張は、言い切らない。「このパターンは、積み重なっていく文章の調査には向いているが、関係の多いデータには向かない」は、根拠を示せる主張である。「このパターンは最良の知識管理の方法である」はそうではない。

## スクリプトの書き方

同梱のスクリプトには、守っておきたい共通の取り決めがある。

どのスクリプトも、先頭の docstring に目的、使い方、実行例を書く。新しいスクリプトも同じ形にし、`python script.py --help` で役に立つ説明が出るようにする。

出力は、既定では人が読む形にし、プログラムから使うための `--json` を任意で付けられるようにする（今は `wiki_lint.py` にある。ほかのスクリプトにも必要に応じて加えてよい）。

診断用のスクリプトは、検出結果を報告するだけで、修正は適用しない。初期化とセットアップ、明示的に呼び出す改善のライフサイクルは、ドキュメントに書いた成果物を書き込む。改善の候補は、合格してレビューを済ませた変更を明示的に適用するまで隔離しておく。スナップショットと測定結果は、あとから確認できる状態で残す。

ライフサイクルの新しい補助の処理は、標準ライブラリで書く。依存パッケージが必要な検索とグラフの処理には、バージョンを固定した既存の実行環境を使い回す。

## スキーマの変更と互換性のない変更

変更によっては、ユーザーがすでに作った wiki に影響する。必須の frontmatter のフィールドを加える、既定のページの種類の名前を変える、インデックスの形式を変える、といった変更である。これらは互換性のない変更であり、次のものが必要になる。

PR に移行の手順を書く（スクリプト、手作業の手順、またはその両方）。

`CHANGELOG.md` の `### Changed` か `### Breaking` の見出しの下に、はっきり書く。

SemVer に沿ってバージョンを上げる。古い wiki と互換性があればマイナー、なければメジャーを上げる。

互換性を保てる変更（妥当な既定値を持つ任意の frontmatter のフィールドを加えるなど）にできるなら、互換性のない形より、そちらを選ぶ。

## PR のチェックリスト

PR を出す前に、次を確かめる。

- [ ] 影響を受ける同梱のスクリプトと、既存の回帰テストが、小さなテスト用の wiki で通る。
- [ ] `python -c "import json; json.load(open('.claude-plugin/plugin.json', encoding='utf-8')); json.load(open('.claude-plugin/marketplace.json', encoding='utf-8'))"` が成功する。
- [ ] Claude Code でプラグインを読み込める（`/plugin marketplace add <local-path>`、`/plugin install`、エラーが出ない）。
- [ ] `CHANGELOG.md` の `[Unreleased]` に変更を書いた。この PR でバージョンを準備する場合は、そのリリースの見出しの下に書いた。
- [ ] `SKILL.md` の frontmatter や `plugin.json` の説明文を変えた場合、1024 文字未満に収まっている。
- [ ] SKILL.md の説明文を変えた場合、スキルが呼び出される範囲がはっきり良くなっている（関係する言い回しが増える、呼び出されるべきときに呼び出されないことが減る）。説明文は、Claude がスキルを使うかどうかを決める主な手がかりなので、言葉の選び方が大事である。

## issue

不具合の報告でもっとも役に立つのは、問題を起こしたコマンドや自然言語の依頼、関係する wiki の一部（共有してよければ、数ページかディレクトリ全体）、期待した結果と実際の結果である。スクリプトの不具合なら、正確なコマンドラインと出力（またはスタックトレース）も添える。

機能の要望では、求める機能だけでなく、その背景にある使い方も書いてほしい。要望された機能そのものより、同じ問題への別のやり方のほうが合っていることもある。

## リリース（メンテナー向け）

### バージョンを書く場所

リリースのバージョンは、JSON の3つのフィールドと、CHANGELOG の見出しに書く。**すべて一緒に上げる**。`claude plugin validate` は、プラグインのマニフェストとマーケットプレイスの項目でバージョンが食い違うと、エラーにする。

| 場所 | フィールド | 備考 |
|----------|-------|-------|
| `.claude-plugin/plugin.json` | `version` | 正本。Claude Code は、インストールしたバージョンをまずここから読む。 |
| `.claude-plugin/marketplace.json` | `metadata.version` | マーケットプレイス全体のバージョン。 |
| `.claude-plugin/marketplace.json` | `plugins[0].version` | プラグインごとの項目。`plugin.json` と一致させる。 |
| `CHANGELOG.md` | `## [X.Y.Z] - YYYY-MM-DD` の見出しと、`[X.Y.Z]: …/releases/tag/vX.Y.Z` のリンクの行 | 人が読むための記録。 |

`skills/info-hive/SKILL.md` には、バージョンのフィールドを**置かない**。スキルのバージョンは、それを含むプラグインの `plugin.json` で決まる。`name` か `description` を変えるとき以外は、SKILL.md の frontmatter に手を付けない。

同梱のスクリプトとスラッシュコマンドにも、バージョンの情報はない。プラグインのバージョンがそれらも表す。

### SemVer の方針

- **メジャー**（`1.0.0` → `2.0.0`）: wiki の構造、frontmatter のスキーマ、スクリプトのコマンドラインのフラグに対する互換性のない変更で、既存の wiki を手作業で移行しないと使えなくなるもの。
- **マイナー**（`0.3.0` → `0.4.0`）: 既存の wiki が無視できる機能の追加（新しいコマンド、任意の frontmatter、新しいスクリプト）。古い wiki を使っている人が、カスタマイズを失わずに新しいファイルを冪等に取り込めるよう、必ず `--upgrade` の流れを用意する（または `init_wiki.py` の既存の流れを広げる）。
- **パッチ**（`0.3.0` → `0.3.1`）: 不具合の修正、ドキュメントだけの変更、外から見た動作が変わらない内部の整理。

互換性を保てる形（妥当な既定値を持つ新しい frontmatter のフィールドなど）にできるなら、互換性のない変更より、そちらを選ぶ。

### リリースの手順

1. `CHANGELOG.md` の **`[Unreleased]` の項目を**、新しい `## [X.Y.Z] - YYYY-MM-DD` の見出しの下に**移す**。末尾に `[X.Y.Z]: https://github.com/to884/info-hive/releases/tag/vX.Y.Z` の行を加え、`[Unreleased]: https://github.com/to884/info-hive/compare/vX.Y.Z...HEAD` に更新する。
2. 次の場所で **`version` を `X.Y.Z` に上げる**。
   - `.claude-plugin/plugin.json`
   - `.claude-plugin/marketplace.json`（`metadata.version` と `plugins[0].version` の両方）
3. 古い wiki を使っている人が取り逃がす**ファイルやスキーマのセクションをリリースで加える場合**は、`skills/info-hive/scripts/init_wiki.py` を広げる。
   - 新しいテンプレートのファイルを `template_map` に加える（冪等である。`init_wiki.py` は、すでにあるファイルを飛ばす）。
   - SCHEMA.md に新しいセクションを加えるなら、`SCHEMA_SECTION_MARKERS` に目印を加え、`--upgrade` が手作業のマージとして示すようにする。
   - 手作業の手順が変わるなら、`/wiki:upgrade` のスラッシュコマンドを更新する。
4. **マニフェストを検証する**（バージョンの食い違いとスキーマの誤りを見つける）。
   ```bash
   claude plugin validate .
   ```
   `✔ Validation passed` と表示されればよい。
5. 一時的なフィクスチャで、同梱のスクリプトの**スモークテストを行う**（`init_wiki.py` で一時ディレクトリに wiki を作り、ページをいくつか置き、lint、extract、query を実行する）。グラフを変えた場合は、検出結果の種類ごとにそれを起こすフィクスチャのページを用意し、lint のすべての分岐を通す。
6. **2つに分けてコミットする**。
   ```bash
   git commit -m "feat: <1行の要約>"      # 中身の変更
   git commit -m "release: vX.Y.Z"        # バージョンの更新と CHANGELOG
   ```
7. **push してタグを付ける**。
   ```bash
   git push origin main
   git tag -a vX.Y.Z -m "vX.Y.Z: <1行の要約>"
   git push origin vX.Y.Z
   ```
   タグは単純な `vX.Y.Z` の形にする。`claude plugin tag` コマンドを使うと `wiki--vX.Y.Z` の形のタグができる。これでも Claude Code からのインストールには使えるが、このリポジトリでは `vX.Y.Z` にそろえる。
8. **GitHub のリリースを作る**。
   ```bash
   gh release create vX.Y.Z --repo to884/info-hive --title "vX.Y.Z: <要約>" \
     --notes "$(cat <<'EOF'
     <リリースノート。ふつうは CHANGELOG の項目を短くしたもの>
     EOF
     )"
   ```

### 利用者が新しいバージョンを取り込む方法

- **Claude Code:** `/plugin marketplace update` のあとに `/plugin install wiki@info-hive` を実行する。Claude Code は、解決した `version` をキャッシュしているものと比べ、違っていれば更新する。
- **`npx skills add` でインストールしたほかのエージェント:** `npx skills update info-hive` を実行するか、最初に使った `npx skills add to884/info-hive -a <agent>` をもう一度実行する。
- **以前のバージョンで作った既存の wiki:** `/wiki:upgrade` を実行する（コマンドラインからなら `python /path/to/info-hive/skills/info-hive/scripts/init_wiki.py /absolute/path/to/project --upgrade`）。スクリプトのパスと対象のプロジェクトは別にしておく。アップグレードは、ファイルについては冪等で、SCHEMA.md のマージは手順を追って確認しながら進める。

### リリースのたびに変えなくてよいもの

- `skills/info-hive/SKILL.md` の frontmatter（バージョンのフィールドはない）
- `commands/` のスラッシュコマンドのファイル（バージョンのフィールドはない）
- `skills/info-hive/references/` のリファレンス（新しい内容を加える変更でない限り）
- `skills/info-hive/assets/` のテンプレート（新しいテンプレートを加える変更でない限り）
- `LICENSE`、`README.md`（利用者向けの概要が変わらない限り）

## ライセンス

コントリビュートした内容は、このプロジェクトの [MIT ライセンス](./LICENSE)で提供することに同意したものとする。
