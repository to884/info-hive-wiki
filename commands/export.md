---
description: プロジェクト wiki の指定記事・関連記事・原本を、他の利用者にも渡せる ZIP にまとめる。
argument-hint: "<記事> [ほかの記事] [--only] [--without-raw]"
argument-description: "記事のスラッグまたは wiki 内の相対パス。--only は指定記事に限定し、--without-raw は原本を省く。"
---

入力は $ARGUMENTS。送り元として指定されたプロジェクトの wiki と raw の根を、プロジェクトの指示と作業領域の設定から確認する。個人 wiki を送り元に切り替えない。`SCHEMA.md` がなければ `/wiki:init` を案内する。別ユーザーのファイルシステムにアクセスする必要はない。

1. `<B>` は `${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_bundle.py`。引数は個別の値として渡す。次の形で対象を調べる。

   ```text
   python "<B>" plan --wiki "<WIKI_ROOT>" --raw-root "<RAW_ROOT>" --page "<記事>"
   ```

   `--page` は繰り返せる。既定は指定記事から本文の wikilink と frontmatter の出典・概念・エンティティの参照を1段たどる。必要な記事を個別に選ぶ場合は `--only` と複数の `--page` を使う。PDF や開発資料など raw の根の外にある出典は、その原本を渡すと利用者が指定した場合に限り `--allow-raw "<そのファイルまたはディレクトリ>"` を足す。原本を省く場合は `--without-raw` を使う。
2. 記事、原本（ディレクトリ内のファイルと Web raw の画像を含む）、サイズ、同梱しない参照を示す。原本の内容や記事本文、図への参照、ファイル名に共有したくない情報がないか利用者と確かめる。原本欄の絶対パスは配布用の相対パスに変わる。任意の本文や原本の中身の伏せ字は自動では行わない。
3. 範囲が決まったら、同じ引数の `plan` を `pack` に替え、`--expect-sha256 "<plan_sha256>" --zip "<新規 ZIP の絶対パス>"` を足す。既存ファイルがある場合は別名を使う。作成後は ZIP のパス、指紋、受け手が使う `/wiki:import` を伝える。ZIP の送付は利用者が選んだ経路で行う。
