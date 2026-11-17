#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = []
# ///
"""Claude Code と共用する操作手順を、Codex で読む形にして標準出力に出す。

    python wiki_codex.py query
    python wiki_codex.py help [query]

プラグインに含まれる commands/*.md を正本とする。ファイルの変更、子プロセスの起動、
利用者の引数の埋め込みは行わない。表示した手順を Codex が読み、利用者の依頼に従って実行する。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_help  # noqa: E402
from wiki_text import localize_argparse  # noqa: E402


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HEADER = """# Codex での操作

以下は、プラグインに同梱された共通の操作手順である。

- `<skill-root>` は、このスクリプトの一つ上にある info-hive スキルのディレクトリ、
  `<plugin-root>` は、このスクリプトから求めたプラグインのルートである。実行時は実際の絶対パスに置き換える。
- `<利用者の引数>` は依頼から読む。文字列をそのままシェルに埋め込まない。
- `/wiki:*` に対応する呼び出しは `$wiki:info-hive <操作>` である。
- MCP のツールは、このセッションで表示された名前で呼ぶ。Claude Code のツール名を推測しない。
- Read / Write / Edit / str_replace は、Codex のファイル読み取りと差分編集に読み替える。
- Codex には `wiki:web-raw` がない。Web の raw は `references/web-raw.md` の W1〜W8 を自分で実行する。
  Claude CLI、Agent ツール、Sonnet の起動は行わない。モデルの表示名は実際に使っている自分の名前にする。
- `ingest session` は実行中の Codex の会話を取り込む。S1 の Codex の手順で
  `locate --host codex --current` の `transcript` と `before` を使い、依頼を含むターン全体より前の範囲に固定する。
  `session:<ID>` は Codex の ID として読む。明示した Claude Code の JSONL は従来どおり扱う。
- エージェントのメモリとの連携には `AGENTS.md` を使う。既存の内容を保ち、手順どおり利用者に内容を示す。
- PDF では、このセッションに rag のツールがなければ、共通の PDF の取り込みの手順に従う。
  rag プラグインの Codex 対応や自動導入を前提にしない。

---

"""


def adapt(text: str) -> str:
    """ホストの呼び出し名とパスの表記を変える。コマンドの引数の値は扱わない。"""
    text = text.replace("${CLAUDE_PLUGIN_ROOT}/skills/info-hive", "<skill-root>")
    text = text.replace("${CLAUDE_PLUGIN_ROOT}", "<plugin-root>")
    text = text.replace("skills/info-hive/references/", "<skill-root>/references/")
    text = text.replace("$ARGUMENTS", "<利用者の引数>")
    text = re.sub(r"/wiki:([a-z]*)", lambda m: "$wiki:info-hive" + (" " + m[1] if m[1] else ""), text)
    return text.replace("Claude Code のデスクトップアプリ", "Codex のデスクトップアプリ")


def workflow(root: Path, operation: str) -> str:
    """正本から本文を読む。操作名は固定の一覧に限定してパスの脱出を防ぐ。"""
    if operation not in wiki_help.PREFERRED_ORDER or operation == "help":
        raise ValueError(f"使えない操作: {operation}")
    text = (root / "commands" / f"{operation}.md").read_text(encoding="utf-8")
    match = wiki_help.FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"操作手順の frontmatter がない: {operation}")
    return HEADER + adapt(text[match.end():].lstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex 向けに wiki の操作手順かヘルプを表示する。")
    parser.add_argument("operation", choices=wiki_help.PREFERRED_ORDER, help="表示する操作")
    parser.add_argument("command", nargs="?", help="help で詳しく知りたい操作名")
    localize_argparse(parser)
    args = parser.parse_args(argv)
    if args.command is not None and args.operation != "help":
        parser.error("操作の引数はこのスクリプトに渡さず、表示した手順を実行するときに使う")
    try:
        if args.operation == "help":
            output = io.StringIO()
            help_args = [str(PLUGIN_ROOT)]
            if args.command is not None:
                help_args += ["--command", args.command]
            with contextlib.redirect_stdout(output):
                result = wiki_help.main(help_args)
            sys.stdout.write(adapt(output.getvalue()))
            return result
        sys.stdout.write(workflow(PLUGIN_ROOT, args.operation))
        return 0
    except (OSError, ValueError) as exc:
        print(f"エラー: 操作手順を読めない: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # wiki_help を import しても、その __main__ の UTF-8 設定は実行されない。
    # Windows の cp932 と、ホストの環境変数に依存せずに手順を出す。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
