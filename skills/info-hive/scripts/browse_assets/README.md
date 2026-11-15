# 閲覧（browse）の画面の資産

閲覧の画面の CSS と JavaScript を置く。ページは CSP（`style-src 'self'; script-src 'self'`）の下で描くので、どれもファイルとして読み込む。

| ファイル | 中身 |
| --- | --- |
| `style.css` | 閲覧の画面の独自の CSS |
| `app.js` | ナビゲーションを組み立て、データの世代を確かめるスクリプト |
| `fluent2.css` | Fluent 2 の Web 版のデザインを、React を使わない CSS で再現したもの（非公式）。下の「同梱した Fluent 2 の CSS」を参照 |
| `licenses/` | 同梱したファイルのライセンスの文書。閲覧の出力には含めない |

## 同梱した Fluent 2 の CSS

`fluent2.css` は、スキル `fluent2-html-replica`（to884/fluent2-html-replica の `skills/fluent2-html-replica/`）に同梱された `assets/fluent2.css` を、中身を書き換えずに写したものである。プラグインは、このファイルだけを読み、スキルの場所には依存しない。

| 項目 | 値 |
| --- | --- |
| 写した元 | スキル `fluent2-html-replica` の `assets/fluent2.css`（スキルの `computedHash` は `14bc59994c3d16920cc9ffd1e4b4d124fe32b1bcc7624fbcdd9778a0b511cb29`） |
| 基準の版 | `@fluentui/react-components` 9.75.2、`@fluentui/tokens` 1.0.0-alpha.24、microsoft/fluentui の commit `f9720c35ba893c9630f4c0389d07dc6d6fd6068d` |
| 大きさ | 625,811 バイト |
| SHA-256 | `0b393938b1b5349f2436780eda73d8786e730bc793f4c3fc103ecf6913e8b4bb` |
| 写した日 | 2026年11月14日 |
| ライセンス | MIT（[licenses/fluent2-html-replica.LICENSE](licenses/fluent2-html-replica.LICENSE)）。元になった Fluent UI（MIT、Copyright (c) Microsoft Corporation）と Fluent UI System Icons（MIT）の表示は [licenses/fluent2-html-replica.THIRD-PARTY-NOTICES.txt](licenses/fluent2-html-replica.THIRD-PARTY-NOTICES.txt) にある。同じ表示が `fluent2.css` の先頭のコメントにも入っているので、閲覧の出力に写した `fluent2.css` にも表示が付いて回る |

- このプラグインと `fluent2.css` は Microsoft の製品ではなく、Microsoft の提携、推薦、承認を受けたものでもない。Fluent、Microsoft、Segoe は Microsoft Corporation の商標である。
- 書体は同梱しない。`fluent2.css` は、フォントスタックに Segoe UI と日本語の書体（Yu Gothic UI、Meiryo UI ほか）の名前を並べるだけである。Segoe UI のない環境では、並びの次の書体で表示する。
- スキルの `fluent2.js` は同梱しない。要素の `style` に書き込むので、閲覧の画面の約束（要素の `style` を変えない）に反するからである。部品のふるまいが要るときは、`app.js` が受け持つ。
- `fluent2.css` は生成物なので、手で直さない。閲覧の独自の規則は `style.css` に書き、`fluent2.css` のあとに読む。

## 写し直すとき

1. スキル `fluent2-html-replica` を更新し、`SKILL.md` の基準の版と、`references/08-plain-html.md` の「互換性の注意」を読む。CSS 変数の名前は Fluent の内部の実装で、版で変わりうる。
2. スキルの `assets/fluent2.css` を、このディレクトリの `fluent2.css` に写す。`LICENSE` と `THIRD-PARTY-NOTICES.txt` も、`licenses/` の同じ名前のファイルに写す。
3. 上の表の基準の版、大きさ、SHA-256、写した日を書き換える。
4. 開発リポジトリの閲覧の試験（`tests/wiki/test_browse*.py` と `tests/wiki/browse/`）を流し、明るい配色と暗い配色の画面を確かめる。
