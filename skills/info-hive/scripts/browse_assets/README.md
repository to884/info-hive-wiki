# 閲覧（browse）の画面の資産

閲覧画面の表示と静的サイトの書き出しに使うファイルです。閲覧画面は、ページの CSP（`style-src 'self'; script-src 'self'`）に従って、CSS と JavaScript を外部ファイルとして読み込みます。

| ファイル | 中身 |
| --- | --- |
| `style.css` | 閲覧の画面の独自の CSS |
| `app.js` | ナビゲーションを組み立て、データの世代を確かめるスクリプト |
| `fluent2.css` | Fluent 2 の Web 版のデザインを、React を使わない CSS で再現したもの（非公式）。下の「同梱した Fluent 2 の CSS」を参照 |
| `figures.js` | 本文の Mermaid のフェンスを図として描くスクリプト（親の側）。サーバー方式でのみ動きます（ADR-0030）。 |
| `mermaid-frame.html`、`mermaid-frame.js` | Mermaid を動かす sandbox の枠の文書と、その中のスクリプト。サーバーがトークンのない `/_frame/<版>/` から配り、静的サイトには含めません。 |
| `mermaid.tiny.js` | Mermaid の束。下の「同梱した Mermaid」を参照 |
| `render-pele.mjs` | 静的の書き出し時に Node.js 20 以上で図を読み、明暗 2 配色の SVG を生成する |
| `pele.mjs` | 静的の書き出し時にだけ使う Pele の束。下の「同梱した Pele」を参照 |
| `licenses/` | 同梱したファイルのライセンスの文書。閲覧の出力には含めません。 |

## 同梱した Fluent 2 の CSS

`fluent2.css` は、スキル `fluent2-html-replica`（to884/fluent2-html-replica の `skills/fluent2-html-replica/`）に同梱された `assets/fluent2.css` を、中身を変えずにコピーしたファイルです。プラグインは同梱したこのファイルだけを読みます。スキルの場所には依存しません。

| 項目 | 値 |
| --- | --- |
| 写した元 | スキル `fluent2-html-replica` 0.1.5（タグ `v0.1.5`、commit `638ad7109577671d3acd8ed5173594006116fc3c`）の `assets/fluent2.css`（スキルの `computedHash` は `326ec308208eefadb3c371d3b5f746a0d88a8c0d903ff0583d17a27ba8ca7faf`） |
| 基準の版 | `@fluentui/react-components` 9.75.2、`@fluentui/tokens` 1.0.0-alpha.24、microsoft/fluentui の commit `f9720c35ba893c9630f4c0389d07dc6d6fd6068d` |
| 大きさ | 626,488 バイト |
| SHA-256 | `05d35361e0340e1029507beb90ce280a1db76a473e7aaf0d896436bf1f231fba` |
| 写した日 | 2026年11月16日 |
| ライセンス | MIT（[licenses/fluent2-html-replica.LICENSE](licenses/fluent2-html-replica.LICENSE)）。元になった Fluent UI（MIT、Copyright (c) Microsoft Corporation）と Fluent UI System Icons（MIT）の表示は [licenses/fluent2-html-replica.THIRD-PARTY-NOTICES.txt](licenses/fluent2-html-replica.THIRD-PARTY-NOTICES.txt) にあります。同じ表示が `fluent2.css` の先頭のコメントにも入っています。閲覧の出力にコピーした `fluent2.css` にも表示が残ります。 |

- このプラグインと `fluent2.css` は Microsoft の製品ではなく、Microsoft の提携、推薦、承認を受けたものでもありません。Fluent、Microsoft、Segoe は Microsoft Corporation の商標です。
- 書体は同梱しません。`fluent2.css` は、フォントスタックに Segoe UI と日本語の書体（Yu Gothic UI、Meiryo UI ほか）の名前を並べるだけです。Segoe UI のない環境では、並びの次の書体で表示します。
- スキルの `fluent2.js` は同梱しません。要素の `style` に書き込むので、閲覧の画面の約束（要素の `style` を変えない）に反するからです。部品のふるまいが要るときは、`app.js` が受け持ちます。
- `fluent2.css` は生成物なので、手で直しません。閲覧の独自の規則は `style.css` に書き、`fluent2.css` のあとに読みます。

## 写し直すとき

1. スキル `fluent2-html-replica` を更新し、`SKILL.md` の基準の版と、`references/08-plain-html.md` の「互換性の注意」を読みます。CSS 変数の名前は Fluent の内部実装で、版によって変わることがあります。
2. スキルの `assets/fluent2.css` を、このディレクトリの `fluent2.css` に写します。`LICENSE` と `THIRD-PARTY-NOTICES.txt` も、`licenses/` の同じ名前のファイルに写します。
3. 上の表の基準の版、大きさ、SHA-256、写した日を書き換えます。
4. 開発リポジトリの閲覧の試験（`tests/wiki/test_browse*.py` と `tests/wiki/browse/`）を実行し、明るい配色と暗い配色の画面を確かめます。

## 同梱した Mermaid

`mermaid.tiny.js` は、npm の `@mermaid-js/tiny` 12.3.0 の `dist/mermaid.tiny.js` を、中身を変えずにコピーしたファイルです。Mermaid の全機能を含むファイル（`mermaid.min.js`）は、elkjs（EPL-2.0 OR GPL-3.0-or-later）を含むため使いません。tiny で描画できないマインドマップ、アーキテクチャ図、KaTeX の数式、`layout: elk` はコードとして表示します。

| 項目 | 値 |
| --- | --- |
| 写した元 | `https://registry.npmjs.org/@mermaid-js/tiny/-/tiny-12.3.0.tgz` の `package/dist/mermaid.tiny.js`（tarball の integrity は `sha512-90BO2kQvmQWj9YjBX2IsLHCw61rN69dPTn5wFfDu+b9aT7qtzfzTA3wpKWeaRAKK85refSo9How5EOeY39DWfA==`。レジストリの公表値と一致した） |
| 大きさ | 2,852,578 バイト |
| SHA-256 | `c41f5236091b83f524c4940f97a16b2b14a76f21ff84aea4fd6b66421d8a2a9a` |
| 枠の版（`FRAME_VERSION`） | `mermaid-12.3.0-1` |
| 写した日 | 2026年11月17日 |
| ライセンス | MIT（[licenses/mermaid.LICENSE](licenses/mermaid.LICENSE)）。束に含まれる依存の表示は [licenses/mermaid.THIRD-PARTY-NOTICES.txt](licenses/mermaid.THIRD-PARTY-NOTICES.txt) にあります。Mermaid の MIT の表示は、閲覧の出力に必ず入る `mermaid-frame.js` の先頭のコメントにもあります。 |

- `.gitattributes` で、`mermaid.tiny.js` を `-text` にしています。改行の変換を受けず、上の SHA-256 のまま持ちます。
- 枠の 3 つのファイル（`mermaid-frame.html`、`mermaid-frame.js`、`mermaid.tiny.js`）は、サーバーが `/_frame/<枠の版>/` から、キャッシュを許して配ります。どれかを変えたら、枠の版を上げます。枠の版は、`wiki_browse_core.py` の `FRAME_VERSION`、`figures.js` の `FRAME_VERSION`、この表の 3 か所にあり、試験が一致を確かめます。

### Mermaid を写し直すとき

1. `npm pack @mermaid-js/tiny@<版>` で tarball を取り、integrity をレジストリの公表値と照らします。全部入りの束に替えるときは、束の中の依存のライセンス（とくに elkjs）を確かめます。
2. `package/dist/mermaid.tiny.js` を `mermaid.tiny.js` に、`package/LICENSE` を `licenses/mermaid.LICENSE` に写します。`licenses/mermaid.THIRD-PARTY-NOTICES.txt` は、その版の package.json の依存のライセンスの文書から作り直します。
3. 上の表の版、integrity、大きさ、SHA-256、写した日を書き換えます。枠の版を 3 か所で上げます。
4. 開発リポジトリの閲覧の試験（`tests/wiki/test_browse*.py` と `tests/wiki/browse/figures.test.mjs`）を、5 つのブラウザーで実行します。

## 同梱した Pele

`pele.mjs` は、npm の `pele` 1.13.0 の `dist/pele.js` を、バイト列を変えずにコピーしたファイルです。静的サイトでは、HTML から生成済みの SVG を参照します。Pele のバンドルと `render-pele.mjs` は書き出し先に含めません。

| 項目 | 値 |
| --- | --- |
| 写した元 | [`pele-1.13.0.tgz`](https://registry.npmjs.org/pele/-/pele-1.13.0.tgz) の `package/dist/pele.js` |
| 大きさ | 729,332 バイト |
| SHA-256 | `37bea5005aa6d2dde929032ced5d2958cf642667cbc1fe675f9dfacf4ba15a51` |
| ライセンス | Pele の MIT と Lucide アイコンの ISC（[licenses/pele.LICENSE](licenses/pele.LICENSE)） |

更新するときは `npm pack pele@<版>` で tarball を取り、integrity をレジストリの公表値と照らします。束とライセンス文書を写し、表の値を更新して、開発リポジトリの `tests/wiki/test_browse.py` と `tests/wiki/browse/figures.test.mjs` を実行します。
