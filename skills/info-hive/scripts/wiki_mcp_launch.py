#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""wiki の MCP の中継（wiki_mcp_relay.py）を起動する入口。プラグインの .mcp.json から起動する。

標準ライブラリだけで書き、依存を持たない。.mcp.json から `uv run --script wiki_mcp_relay.py` で中継を
直接起動すると、uv が中継の依存を入れている途中でホストに止められたとき（セッションの終わりなど）、
環境が作りかけのまま残り、それ以降の起動がすべて失敗する。この入口には壊れる依存がない。
中継の環境を確かめ、壊れていれば入れ直してから、その python で中継を動かす。

- 標準入力と標準出力は MCP の通信に使うので、そのまま中継に渡す。uv の出力は標準エラーに書く。
- 引数はそのまま中継に渡す（`--ensure` など）。終了コードは中継のものを返す。
- 中継の環境を用意できなかったときは、理由を標準エラーと `<データの置き場所>/logs/launch.log` に書いて止まる。
  uv が TLS 証明書を検証できなかったとき（TLS を検査する社内ネットワーク）は、OS の証明書ストアを使って
  1 回だけやり直す（wiki_mcp_common._uv_sync）。

CLAUDE_PLUGIN_ROOT を持たないホスト（Codex など）は、このファイルのパスを指定して起動する。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_mcp_common as common  # noqa: E402

RELAY = common.SCRIPTS / "wiki_mcp_relay.py"
# 中継の依存（mcp など、約 30 個）の取得の待ち。サーバーの依存とは別で、こちらは小さい。
PREPARE_TIMEOUT = 600


def log(message: str) -> None:
    print(f"[wiki-launch] {message}", file=sys.stderr, flush=True)


def prepare(force: bool = False) -> str | None:
    try:
        return common.prepare_script(RELAY, output=sys.stderr, timeout=PREPARE_TIMEOUT, log=log, force=force)
    except Exception as exc:  # noqa: BLE001 - 理由をホストのログに残して止まる
        message = f"中継の環境を用意できなかった: {type(exc).__name__}: {exc}"
        log(message)
        if not isinstance(exc, common.UvTlsError):  # UvTlsError は _uv_sync が記録済み
            common.record_failure(message)
        return None


def run_relay(python: str) -> int:
    # 標準入出力は明示して渡す（Windows では、渡さないと子に引き継がれないことがある）。
    return subprocess.run([python, "-X", "utf8", str(RELAY), *sys.argv[1:]], stdin=sys.stdin, stdout=sys.stdout,
                          stderr=sys.stderr, env={**os.environ, "PYTHONUTF8": "1"},
                          creationflags=common.child_creationflags()).returncode


def main() -> int:
    python = prepare()
    if python is None:
        return 1
    code = run_relay(python)
    if code == common.BROKEN_ENV_EXIT:
        # 中継は依存を import できずに、標準入力を読む前に止まった。環境を入れ直して、1 回だけ起動し直す。
        log("中継が依存を import できなかったので、環境を入れ直して起動し直す")
        python = prepare(force=True)
        if python is None:
            return 1
        code = run_relay(python)
    return code


if __name__ == "__main__":
    # 標準エラーはホストが UTF-8 として読む。Windows の既定は cp932 なので、明示する。標準出力は使わない。
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
