#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = []
# ///
"""UserPromptSubmit のフックの入口（ADR-0032）。hooks/hooks.json から uv で起動する。

標準入力のフックの入力（JSON）を読み、指示に関係しそうな wiki のページの候補を、
hookSpecificOutput.additionalContext として標準出力に書く。足すものがなければ何も書かない。

何が起きても、何も書かずに終了コード 0 で終わる。0 以外を返すと、利用者の画面に hook error の知らせが出て、
2 なら利用者の指示そのものが止まるためである（起動行の uv の段の 2 は、hooks.json の `; exit 0` で 0 にする）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

DEADLINE_SECONDS = 2.5


def main() -> None:
    started = time.monotonic()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import wiki_initialized
        import wiki_prompt_context as context

        raw = sys.stdin.buffer.read(context.MAX_STDIN_BYTES + 1)
        if len(raw) > context.MAX_STDIN_BYTES:
            return
        hook_input = json.loads(raw.decode("utf-8"))

        def is_wiki(root) -> bool:
            # 初期化された wiki（ルートに SCHEMA.md がある）だけを読む。なければ黙って何もしない。
            return wiki_initialized.check(root) is None

        text = context.build_context(hook_input, env=dict(os.environ), is_wiki=is_wiki,
                                     deadline=started + DEADLINE_SECONDS)
        if text:
            sys.stdout.buffer.write(context.hook_output(text).encode("ascii"))
            sys.stdout.buffer.flush()
    except BaseException:  # noqa: BLE001 - フックは何が起きても黙って 0 で終わる
        return


if __name__ == "__main__":
    main()
    # 標準出力の後始末で失敗しても 0 で終わる。
    os._exit(0)
