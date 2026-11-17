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

import os
import sys


def _foreground_priority() -> bool:
    """Windowsの短時間の前景処理を、並列のCPU処理に待たせない。失敗時は通常の優先度で続ける。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        change = kernel32.SetPriorityClass
        change.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        change.restype = ctypes.c_int
        return bool(change(ctypes.c_void_p(-1), 0x8000))  # ABOVE_NORMAL_PRIORITY_CLASS
    except Exception:  # noqa: BLE001 - 優先度を変えられなくてもフックは動かす
        return False


# 親の入口だけで適用し、子はcreationflagsを使う。importする側と子自身では変更しない。
_FOREGROUND = _foreground_priority() if __name__ == "__main__" and sys.argv[1:2] != ["--worker"] else False

import json
import time
from pathlib import Path

DEADLINE_SECONDS = 2.5


def worker(deadline: float) -> None:
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

        picked = context.select(hook_input, env=dict(os.environ), is_wiki=is_wiki, deadline=deadline)
        if picked is not None:
            sys.stdout.buffer.write(json.dumps(context.selection_payload(picked), ensure_ascii=True).encode("ascii"))
            sys.stdout.buffer.flush()
    except BaseException:  # noqa: BLE001 - フックは何が起きても黙って 0 で終わる
        return


def main() -> None:
    """解析を別の Python プロセスに閉じ込め、OS の待ちと GIL を保持する処理にも期限を掛ける。"""
    started = time.monotonic()
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import wiki_prompt_context as context

        hook_input = json.loads(raw.decode("utf-8"))
        env = dict(os.environ)
        if not isinstance(hook_input, dict) or not context.setting(env, "enabled"):
            return
        if hook_input.get("hook_event_name") not in (None, "UserPromptSubmit"):
            return
        if context.query_text(hook_input.get("prompt")) is None:
            # 飛ばす指示では解析の子を起こさない。既存セッションのターンだけを進める。
            # この分岐は正規表現による解析や wiki の走査をしない。状態の OS の待ちはタイマーで止める。
            import threading
            watchdog = threading.Timer(max(0, DEADLINE_SECONDS - (time.monotonic() - started)),
                                       os._exit, args=(0,))
            watchdog.daemon = True
            watchdog.start()
            try:
                context.tick_existing_state(hook_input, env, time.time())
            finally:
                watchdog.cancel()
            return
        import subprocess

        deadline = started + DEADLINE_SECONDS
        worker_deadline = deadline - 0.2  # 親の受け渡しに残す時間。子にも同じ時計の絶対時刻を渡す。
        remaining = worker_deadline - time.monotonic()
        if remaining <= 0:
            return
        result = subprocess.run(
            # CPython の仮想環境から直接呼ばれたときも、子は元の Python で動かす。
            # 標準ライブラリだけを使い、site と利用者の起動時の処理は読み込まない。
            [getattr(sys, "_base_executable", sys.executable), "-I", "-S", str(Path(__file__).resolve()), "--worker", str(worker_deadline)],
            input=raw, capture_output=True, timeout=remaining,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           | (subprocess.ABOVE_NORMAL_PRIORITY_CLASS if _FOREGROUND else 0)) if os.name == "nt" else 0,
        )
        if result.returncode == 0 and result.stdout and time.monotonic() < deadline:
            import threading
            watchdog = threading.Timer(max(0, deadline - time.monotonic()), os._exit, args=(0,))
            watchdog.daemon = True
            watchdog.start()

            def emit(text):
                sys.stdout.buffer.write(context.hook_output(text).encode("ascii"))
                sys.stdout.buffer.flush()

            try:
                context.publish_selection(hook_input, context.selection_from_payload(json.loads(result.stdout)),
                                          env=env, emit=emit, deadline=deadline)
            finally:
                watchdog.cancel()
    except BaseException:  # noqa: BLE001 - 起動失敗とタイムアウトも利用者の指示を止めない
        return


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(float(sys.argv[2]))
    else:
        main()
    # 標準出力の後始末で失敗しても 0 で終わる。
    os._exit(0)
