# wiki のページ、索引、ログを書く

通常の取り込みと `query`・`learn` の保存は、`scripts/wiki_apply.py` を使う。以下の `<A>` はそのスクリプトの絶対パスである。原本は、それぞれの操作の raw 保存手順で先に用意する。別プロジェクトの wiki に回答を保存するときは、保存先の wiki に対応する raw の根を使う。

ライブ wiki では `<WIKI_ROOT>/raw/` も原本の根に使える。公開版では wiki の外に独立した原本の根を用意する。

## 読む版を固定する

wiki が初期化済みであることを確かめ、`python "<A>" view --wiki "<WIKI_ROOT>"` を実行する。JSON の `read_root` から規約、索引、記事、グラフの定義を読む。公開版の場合、MCP の読み取りには元の `wiki` パスと `release_id` を渡し、CLI の読み取りには元の wiki パスと `--release-id <ID>` を渡す。ライブでは `release_id` は `null` になる。原本の保存や専用のキャッシュを使うツールには元の `<WIKI_ROOT>` を渡す。版の照合に失敗したら、エラーを報告して止まる。

## 差分を承認して適用する

1. `python "<A>" begin --wiki "<WIKI_ROOT>" --raw-root "<RAW_ROOT>" --name "<作業の説明>"` を実行する。公開版は読むときに得た `--release-id <ID>` を添える。JSON の `apply_dir` を以下の `<D>` に使う。
2. 新しいページは `python "<A>" new --apply-dir "<D>" --path "sources/<スラッグ>.md"`、既存のページは `python "<A>" edit --apply-dir "<D>" --path "concepts/<スラッグ>.md"` で登録する。返された `path` にだけ書く。既存ページはその写しの必要な箇所だけを直す。セッションの保存では、承認された範囲と伏せ字を下書きにも適用する。
3. 新しいページごとに索引の項目を登録する。分割済みなら対応する `indexes/<type>.md` を指定する。既存の末尾に足す場合は `--section` を省く。対象の見出しが複数ある場合は、読み直して `--nth` に何番目かを指定する。

   ```text
   python "<A>" index-add --apply-dir "<D>" --file "index.md" --section "## ソース" --line "- [[<スラッグ>]]: <要約>"
   ```

4. `python "<A>" log-entry --apply-dir "<D>" --op ingest --title "<ソースの題名>"` でログを登録する。保存する回答は `query`、経験は `learn`、Web raw の警告を確認した記録は `prepare` を使う。補足は `--line "<補足>"` を繰り返し指定する。字下げはスクリプトが付ける。公開版では、差分の判断に使った既存ページや索引を `depends-on --apply-dir "<D>" --path "<相対パス>"` で登録できる。特に、本文や frontmatter にリンクを載せない根拠を登録する。
5. `python "<A>" diff --apply-dir "<D>"` でページ、索引、ログの差分と `plan_sha256` を出す。差分を利用者に示して承認を得る。承認のあとに内容を変えた場合は、差分を出し直す。
6. `python "<A>" apply --apply-dir "<D>" --expect-sha256 "<承認された plan_sha256>"` を実行する。公開版では PyYAML が必要なので、`uv run --with pyyaml==6.0.3 python "<A>" apply …` の形でも実行できる。保留なら公開が確定するまで後続の仕上げを待つ。成功したら結果の `files` を照合する。`already_applied` は同じ承認が適用済みであることを示す。公開版の読み直しは結果の `release_id` で固定する。ログ項目の位置は `log_entries` の `heading` と `nth` で分かる。

後の処理結果を既存のログ項目に添えるときは、別の `begin` を行い、`log-sub --apply-dir "<D>" --entry "<heading>" --nth <nth> --line "<補足>"` を登録する。同じ手順で差分を承認して適用する。

## 公開版で保留になった場合

承認済みの変更が保留になったときは、`state: "pending"` と終了コード 4 を返す。`python "<A>" pending --wiki "<WIKI_ROOT>"` で計画と理由を見つける。再試行には `uv run --with pyyaml==6.0.3 python "<A>" retry --wiki "<WIKI_ROOT>" --plan-id "<plan_id>" --expect-sha256 "<承認された plan_sha256>"` を使う。承認時の下書きと指紋は wiki 側に記録してある。再試行は中断した公開を復旧し、完了済みなら同じ公開版を返す。

別の保存が先に公開されても、変更対象と根拠の版が同じなら同じ承認で続行できる。対象ページ、根拠のページ、定義が変わった場合は保留の理由を確認し、現在の版を読み直して別の `begin` から新しい差分を承認する。

利用者が保留分を取り消すと決めたら、`python "<A>" discard-save --wiki "<WIKI_ROOT>" --plan-id "<plan_id>" --expect-sha256 "<承認された plan_sha256>"` で取り消す。すでに公開した保存は取り消しの対象外とする。

## ライブで保留になった場合

同じページが先に更新された場合は `rebase --apply-dir "<D>" --path "<相対パス>"` を使う。`path` に最新のページが、`previous` に直す前の下書きが入る。変更を手で当て直し、新しい差分を確認する。

中断で書き手の記録が残った場合は、利用者が `python "<A>" unlock --wiki "<WIKI_ROOT>"` を実行する。実行中の書き手がいれば外せない。そのあと `python "<A>" recover --wiki "<WIKI_ROOT>" --live` を実行する。復旧用の日誌がある場合は、次の `apply` でも先に処理される。

日誌にない内容が見つかった場合は、道具が途中まで適用した変更を戻し、食い違ったファイルを保全する。`recover --wiki "<WIKI_ROOT>" --live --show` で対象と理由を確認する。利用者が現在の内容を残すと決めたら `--discard <plan_id>` で日誌を退避し、必要な直しを新しい計画で用意する。

原本の根を移動した場合も、`--show` で復旧に必要なパスを確認できる。根を元の場所に戻してから `unlock` と `recover` を実行する。

待ち時間を超えた計画は `python "<A>" pending --wiki "<WIKI_ROOT>"` で調べられる。対象の作業用ディレクトリと承認した指紋を使い、同じ `apply` を再実行できる。

## 運用者が公開版からライブに切り戻す

1. `python "<A>" plan-return-live --wiki "<WIKI_ROOT>" --raw-root "<RAW_ROOT>"` の出力を JSON ファイルに保存する。`plan.before` と `plan.after` で変更・追加・削除されるパスを確かめ、`release_id` と `plan_sha256` を固定して承認する。公開版かライブの基準に変化があれば計画を採り直す。切り戻し前に、未適用の承認済みの公開保存を `pending` で確認し、再試行するか、利用者が取り消すと決めたものを `discard-save` で片づける。
2. `python "<A>" return-live --wiki "<WIKI_ROOT>" --plan "<計画を保存した JSON>" --expect-sha256 "<承認した plan_sha256>"` を実行する。中断した場合は同じコマンドか `python "<A>" recover --wiki "<WIKI_ROOT>"` を実行する。原本の根を移動した場合は、計画に記録された場所へ戻してから再試行する。管理記録やファイルが計画と食い違う場合はエラーを報告し、記録を手で外して切り替えない。
3. `view` の `release_id` が `null` になり、`read_root` がライブの根を指すことを確認する。版 ID を固定した取得や閲覧は引き続きその版を使う。同じ切り戻しを再試行すると `already_returned` が返る。

切り戻しは、公開開始時と最後の公開版で基準を照合できる wiki に使う。ライブにだけあるファイルを残し、公開開始時に同じパスの内容が変わっていたファイルは、元の内容を `.wiki-cache/apply/return-preimages/<plan_id>/` に保全してから書き戻す。公開中にライブで手作業の変更が入った場合や、公開後の追加先がライブ専用ファイルと重なる場合は、先に内容を照合する。書き手と読み手は切り戻しと再公開の記録を扱える版を使う。

## 運用者が切り戻したライブから再公開する

1. `uv run --with pyyaml==6.0.3 python "<A>" plan-republish-live --wiki "<WIKI_ROOT>" --raw-root "<RAW_ROOT>" --out "<新しい候補ディレクトリ>"` の出力を JSON ファイルに保存する。前の公開版と、切り戻し後に共通の書く段で保存したページ・索引・ログが候補に入る。新しい添付など別途公開するものは `--include "<wiki 内の相対パス>"` で指定する。旧形式の切り戻し記録で公開範囲を判定できないパスは、内容を確認して `--include` または `--exclude` を指定する。以前からライブ専用のファイルは、ライブで編集しても公開範囲に加わらない。
2. 候補の追加・変更・削除の一覧、Markdown の差分と指紋を確認して承認する。`pending` にライブの承認済み差分があれば、先に再試行する。利用者が古い承認を取り消すと決めた場合は、`discard-live-pending --wiki "<WIKI_ROOT>" --plan-id <ID> --expect-sha256 <承認の指紋>` を使う。`python "<A>" republish-live --wiki "<WIKI_ROOT>" --source "<候補ディレクトリ>" --plan "<計画.json>" --expect-sha256 "<承認した plan_sha256>"` を実行する。
3. 中断した場合は同じコマンドか `python "<A>" recover --wiki "<WIKI_ROOT>"` で続ける。書き込み中は `view` の既定の読み取り先がライブを示し、以前の版指定も使える。完了後は `view` で新しい `release_id` を照合する。承認後にライブの内容が変わった場合は、新しいディレクトリに候補を作り直す。

ライブ保存の指紋が公開版から連続しないファイルは、差分を見て `--include` で公開を指定する。未公開のファイルで公開範囲に加えないものは `--exclude` に渡す。共有の索引とログも追加・変更の一覧と Markdown の差分で確認する。

## 運用者が別ディスクの記録と照合して復元する

1. バックアップを取る前に、確定済みの公開版で `python "<A>" init-restore-ledger --wiki "<WIKI_ROOT>" --ledger "<別ディスクの専用ディレクトリ>"` を実行する。原本も保全する場合は `--raw-root "<RAW_ROOT>"` を足す。wiki・raw と別のボリュームを使い、同じ物理ディスクの別パーティションへの配置は避ける。専用ディレクトリは運用用のアクセス権で保護する。配信・保存プロセスの `INFO_HIVE_RESTORE_LEDGER` にも同じディレクトリの絶対パスを設定する。この環境変数を使うプロセスは、独立記録に結び付いている wiki 専用とする。
2. wiki、原本、管理領域をバックアップする。独立した記録は保存の成功応答より先に更新される。公開版の切り替えやライブへの切り戻しが確定するまでは、直前の確認済み公開版を読める。旧固定版は `python "<A>" retire --wiki "<WIKI_ROOT>" --release-id "<旧版 ID>"` で取り下げる。取り下げは独立した記録を先に確定する。
3. 配信を止めて wiki を復元する。wiki の実体 ID とパス、独立した記録のマウント位置を維持し、独立した記録は最新の状態を使う。`python "<A>" reconcile-restore --wiki "<WIKI_ROOT>"` を実行する。`retirements_reapplied` に補った取り下げ、`incomplete_receipts` に欠落または不一致の完了記録が出る。
4. `python "<A>" reconcile-restore --wiki "<WIKI_ROOT>" --restore-content` は、`--raw-root` を設定した場合は原本を元のパスへ戻し、記録した基準版と一致する wiki に本文・索引・ログ・公開成果物と管理記録を元の ID で復元する。`raw_missing` と `raw_conflicts` で欠落と衝突を確認できる。別ディスクの指紋が違うファイルや、復元先の同じ名前で SHA-256 が異なる原本があれば、確認してから同じコマンドを実行する。中断した場合も同じコマンドで続ける。取り下げは復元前後で有効である。`state: "reconciled"` と終了コード 0 を確認して配信を再開する。

既存の独立記録に `init-restore-ledger --raw-root` で原本の保全を足すと、その時点の原本が別ディスクに同期される。設定した raw の根にある通常のファイルとディレクトリを対象とする。保全済みの原本を改訂するときは別の名前で保存する。ローカルの `wiki_raw_ingest.py save` と UTF-8 派生版の書き込みは、別ディスクへの同期後に成功を返す。他の経路から追加した原本は共通の wiki 保存完了時に保全する。記録済みの原本に欠落や内容の相違があっても、元の記録とコピーを保持して wiki の保存・日誌の復旧を続ける。元の原本との差は `reconcile-restore` で確認できる。設定範囲の外に置く raw は別にバックアップする。復元先には、設定したパスに raw のディレクトリを用意する。本文の記録には配信していないライブ記事も含まれるため、別ディスクにもライブ wiki・raw と同じアクセス制限を設ける。復元先に設定ファイルがない場合は、配信側と復元コマンドに `INFO_HIVE_RESTORE_LEDGER` を渡す。
