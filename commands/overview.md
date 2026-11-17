---
description: 指定したページだけから図を中心にした概要を作り、本文と別のサイドカーに保存する。
argument-hint: "<page> [--refresh] | --status | --remove <page>"
argument-description: "概要を作るページ、作り直し、一覧の表示、または削除。"
---

入力: $ARGUMENTS

## 0. wiki を決める

`info-hive` スキルに従い、設定されている wiki のルート `<WIKI_ROOT>` を特定する。
`<WIKI_ROOT>/SCHEMA.md` がなければ、初期化された wiki ではない。何も書かずに止まり、ユーザースコープなら `/wiki:init --user`、プロジェクトなら `/wiki:init` を案内する。
別の wiki に黙って切り替えたり、`SCHEMA.md` を自分で作ったりしない。スクリプトが未初期化と返したときも同じである。
SCHEMA.md があれば読む。

## 1. 要求をファイルで渡す

**利用者の引数をシェルのコマンド行に入れない。** ページ名を引用符で囲んで渡す方法も使わない。
`--status` だけなら、次を呼んで一覧を伝え、終わる。

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_overview.py" status "<WIKI_ROOT>"
```

作成と削除では、要求のファイルへ Write する前に、次を呼ぶ。

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_overview.py" begin "<WIKI_ROOT>"
```

`ready` の `request_path` に、Write ツールで UTF-8 の JSON を書く。シェルのリダイレクトや echo は使わない。
作るときの例は次の形で、`page` は利用者の指定、`generator` は使っているモデルの表示名にする。

```json
{"format": 2, "action": "create", "page": "エージェントのメモリの階層", "refresh": false, "generator": "GPT-6 Astra"}
```

`--refresh` は `refresh: true` にする。`--remove <page>` は `action: "remove"` にして、次を呼び、結果を伝えて終わる。

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_overview.py" remove "<WIKI_ROOT>"
```

作るときは、要求を書いたあとで次を呼ぶ。

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_overview.py" prepare "<WIKI_ROOT>"
```

- `exists` は、すでに新しい概要があると伝えて終わる。
- `not_found` は、ページが見つからないと伝えて終わる。
- `ambiguous` は、`candidates` を見せて利用者に選んでもらう。`begin` からやり直し、要求の `page` の代わりに、選んだ識別子をそのまま `id` に書く。短い名前で再検索しない。
- `error` は、理由を伝えて止まる。経路の検査を回避して書かない。

## 2. 下書きを作る

`ready` が返ったら、`${CLAUDE_PLUGIN_ROOT}/skills/info-hive/references/overview-workflow.md` を Read ツールで読む。
続いて `page_copy` を Read し、そのページだけから概要を作る。
`draft_dir` に、Write ツールで `overview.md` と必要な SVG を置く。最終の `assets/overview/` に自分で書かない。

## 3. 検査して保存する

`work_id` が小文字の16進数16桁であることを確かめ、次を呼ぶ。ページ名や利用者の追加の文字列は渡さない。

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_overview.py" check "<WIKI_ROOT>" --work=<work_id>
```

`rejected` は `problems` の指す下書きを直し、`check` をやり直す。`ok` のあとで保存する。

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_overview.py" write "<WIKI_ROOT>" --work=<work_id>
```

- `written` は `sidecar` のパスを伝える。`page_changed` が真なら、保存前の読み取り時点で元のページが変わっていたことも伝える。
- `page_unreadable` は、保存しておらず下書きが残っていることを伝える。ページを再び読めるようにしたら、同じ `work_id` で `write` をやり直せる。
- `page_gone` は、保存していないことを伝え、元のページの扱いを利用者に確認する。
- `rejected` は、検査と同じに直して `check` からやり直す。

概要の保存では、ページの本文、インデックス、グラフ、`log.md` を更新しない。
保存した概要は `/wiki:browse` で記事の最初の見出しの直後に表示する。元ページが変わった概要には警告が付き、閉じた状態で表示する。自動では作り直さない。

概要が壊れてページの識別子を読めず、wiki に読まなかった記事のリンクがある場合は、`remove --orphan` で消さない。復旧時は元ページとの対応と不要であることを人が確認したうえで、そのサイドカーだけを手で削除する。本文や図を手で編集して修復しない。
