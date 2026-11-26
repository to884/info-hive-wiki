---
description: 取り込み済みの会話を読み直し、wiki の修正案を保存する。
argument-hint: "--wiki <path> --raw-root <path> --out <path> [--limit <1-5>]"
argument-description: "対象 wiki、取り込み済みの原本、提案の出力先をそれぞれ絶対パスで指定する。--limit の既定値は 2。"
---

引数 $ARGUMENTS から、`--wiki`、`--raw-root`、`--out` の絶対パスと、指定があれば `--limit`（1〜5 の整数）を取り出す。不足や曖昧な指定があれば、実行せずにどの値が必要か伝える。出力先は wiki と raw の外に置く。定期タスクでは、作業ディレクトリが変わっても同じ絶対パスを使う。

この操作は Python 3.12〜3.14 と認証済みの Claude Code CLI が使えるローカル環境で行う。次のコマンドで、スクリプトの場所と各パスを実際の値に置き換える。引数は個別に渡し、利用者の入力をそのままシェルに展開しない。`--limit` の指定がなければ `--limit 2` を省ける。

```sh
python -X utf8 -B "${CLAUDE_PLUGIN_ROOT}/skills/info-hive/scripts/wiki_dream.py" --scheduled --wiki "<wiki>" --raw-root "<raw>" --out "<out>" --limit 2 --timeout 480
```

Claude Code のシェルでは、`timeout` に 600000 ミリ秒を指定する。その長さを指定できない場合はバックグラウンドで起動し、完了を待つ。Codex でシェルから実行中のセッション ID が返ったら、そのセッションの出力を完了まで待つ。同じ回のコマンドは再起動しない。

スクリプトは出力先の台帳で処理済みの会話を除外する。保存した提案の場所、処理対象がなかったこと、別の実行が進行中だったこと、時間切れや失敗を実際の出力に従って報告する。提案の作成までで止め、元の wiki には書き込まない。
