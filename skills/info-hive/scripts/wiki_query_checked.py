# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""照合を通った Claude Code の wiki 回答だけを標準出力に書く。"""

import argparse
import json
import math
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

from wiki_mcp_common import CHECKED_QUERY_ROOT as SCOPE, checked_query_call
from wiki_query_verify import Call, PREFIX, READERS, Verdict, inside, verify

SCRIPT = Path(__file__).resolve()
PLUGIN = SCRIPT.parents[3]
SERVER = "plugin:wiki:wiki"
MODEL = "claude-sonnet-5-5"
BUILTINS = {"Read", "ToolSearch"}


class QueryError(RuntimeError):
    """端末に示せる固定文だけを持つ問い合わせエラー。"""


def permitted(wiki, name, args):
    if not isinstance(name, str) or not isinstance(args, dict):
        return False
    if name == "Read":
        value = args.get("file_path")
        within_wiki = inside(wiki, value)
        within_plugin = inside(PLUGIN / "skills" / "info-hive" / "references", value)
        path = within_wiki or within_plugin
        return path is not None and path.suffix.lower() == ".md"
    if name == "ToolSearch":
        return True
    return name.startswith(PREFIX) and checked_query_call(wiki, name.removeprefix(PREFIX), args)


def permission_hook():
    try:
        data = json.load(sys.stdin)
        allowed = permitted(Path(os.environ[SCOPE]), data["tool_name"], data["tool_input"])
    except (OSError, KeyError, TypeError, ValueError):
        allowed = False
    if not allowed:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "対象 wiki とプラグインの問い合わせ資料の読み取りだけを許可しています。",
        }}, ensure_ascii=False))
    return 0


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(item.get("text", "") for item in value
                         if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


class Session:
    def __init__(self, process, wiki, deadline):
        self.process, self.wiki, self.deadline = process, wiki, deadline
        self.messages = queue.Queue()
        self.calls = {}
        self.sequence = 0
        self.request_id = 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.messages.put(event)
        finally:
            self.messages.put(None)

    def receive(self):
        try:
            event = self.messages.get(timeout=max(0, self.deadline - time.monotonic()))
        except queue.Empty as error:
            raise TimeoutError from error
        if event is None:
            raise QueryError("Claude Code の応答が途中で終了しました。")
        return event

    def send(self, event):
        self.process.stdin.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def control(self, subtype):
        self.request_id += 1
        request_id = str(self.request_id)
        self.send({"type": "control_request", "request_id": request_id, "request": {"subtype": subtype}})
        while True:
            event = self.receive()
            response = event.get("response")
            if event.get("type") == "control_response" and isinstance(response, dict):
                if response.get("request_id") == request_id:
                    if response.get("subtype") != "success":
                        raise QueryError("Claude Code の読み取り環境を確認できませんでした。")
                    return response.get("response", {})

    def tools(self):
        while time.monotonic() < self.deadline:
            servers = self.control("mcp_status").get("mcpServers", ())
            for server in servers:
                if server.get("name") != SERVER or server.get("status") != "connected":
                    continue
                names = {tool["name"] for tool in server.get("tools", ())}
                if {"view", "query", "list_index"} <= names:
                    return names
            time.sleep(0.25)
        raise TimeoutError

    def turn(self, text):
        self.send({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None})
        while True:
            event = self.receive()
            if event.get("parent_tool_use_id") is not None:
                raise QueryError("サブエージェントの読み取りは照合できませんでした。")
            if event.get("type") == "result":
                if event.get("is_error") or event.get("subtype") != "success":
                    raise QueryError("Claude Code が問い合わせを完了できませんでした。")
                return str(event.get("result") or "")
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                continue
            for part in message["content"]:
                if not isinstance(part, dict):
                    continue
                self.sequence += 1
                if event.get("type") == "assistant" and part.get("type") == "tool_use":
                    args = part.get("input", {})
                    name = part["name"]
                    if not isinstance(args, dict) or not isinstance(name, str):
                        raise QueryError("Claude Code の読み取り要求の形式を確認できませんでした。")
                    self.calls.setdefault(part["id"], Call(name, args, self.sequence))
                elif event.get("type") == "user" and part.get("type") == "tool_result":
                    call = self.calls.get(part.get("tool_use_id"))
                    if call is not None and call.end is None:
                        call.end = self.sequence
                        call.ok = not part.get("is_error")
                        if not permitted(self.wiki, call.name, call.args):
                            if call.ok:
                                raise QueryError("対象外の読み取り結果を検出しました。")
                            del self.calls[part["tool_use_id"]]
                            continue
                        call.body = content_text(part.get("content"))


def query(wiki, question, effort, executable, timeout):
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT", "CLAUDE_CODE_EFFORT_LEVEL"}}
    env.update({SCOPE: str(wiki), "PYTHONUTF8": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "INFO_HIVE_PROMPT_CONTEXT": "0"})
    hook = shlex.join([Path(sys.executable).as_posix(), "-X", "utf8", SCRIPT.as_posix(), "--permission-hook"])
    settings = {
        "outputStyle": "Default",
        "permissions": {"blockReadsOutsideWorkingDirectories": True,
                        "additionalDirectories": [str(PLUGIN / "skills/info-hive/references")]},
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": hook}]}]},
    }
    command = [
        executable, "-p", "--model", MODEL, "--effort", effort,
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--plugin-dir", str(PLUGIN), "--setting-sources", "",
        "--settings", json.dumps(settings), "--tools", "Read,ToolSearch",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--allowedTools", *sorted(BUILTINS), *(PREFIX + name for name in sorted(READERS)),
    ]
    deadline = time.monotonic() + timeout
    location = json.dumps(wiki.as_posix(), ensure_ascii=False)
    prompt = f"/wiki:query 対象 wiki の絶対パスは {location} です。wiki の更新は不要です。\n質問: {question}"
    with subprocess.Popen(command, cwd=wiki, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8") as process:
        session = Session(process, wiki, deadline)
        try:
            session.control("initialize")
            tools = session.tools()
            answer = session.turn(prompt)
            for attempt in range(2):
                if "get_page" not in tools and "get_page" in session.tools():
                    return "", Verdict(reason="照合中に get_page が登録されたため回答を保留しました。")
                verdict = verify(wiki, session.calls.values(), answer, tools)
                if verdict.passed:
                    return answer, verdict
                if attempt or not verdict.missing:
                    return "", verdict
                missing = json.dumps(verdict.missing, ensure_ascii=False)
                if "get_page" in tools:
                    method = ("引用を残す場合は wiki MCP の get_page で記事の本文を取得してください。"
                              "元の wiki パスを指定し、公開版では view と同じ release_id を渡してください。")
                else:
                    method = ("固定した read_root から本文を取得してください。"
                              "公開版では本文取得の後、同じ release_id で view をもう一度呼んでください。")
                answer = session.turn(
                    f"本文を取得できた記録がない引用は {missing} です。{method}"
                    "索引に言及するときは `index.md` のようにファイル名だけを書いてください。"
                    "本文を確認できない引用と裏付けのない記述を外し、元の質問への修正済み回答だけを返してください。"
                )
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["--permission-hook"]:
        return permission_hook()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, required=True, help="問い合わせる wiki のルート")
    parser.add_argument("--effort", choices=("medium", "high"), default="medium", help="既定値は medium")
    parser.add_argument("--timeout", type=float, default=240, help="準備と修正を含む上限秒数")
    parser.add_argument("question")
    args = parser.parse_args()
    executable = shutil.which("claude")
    wiki = args.wiki.expanduser()
    if not executable or not wiki.is_dir() or not math.isfinite(args.timeout) or args.timeout <= 0 or not args.question.strip():
        parser.error("Claude Code、wiki のディレクトリ、空でない質問を指定してください。")
    try:
        version = subprocess.check_output([executable, "--version"], encoding="utf-8", timeout=10)
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 1, 314):
            raise QueryError("対応している Claude Code の版を確認できませんでした。")
        answer, verdict = query(wiki.resolve(), args.question, args.effort, executable, args.timeout)
    except QueryError as error:
        answer, verdict = "", Verdict(reason=str(error))
    except (ValueError, FileNotFoundError):
        answer, verdict = "", Verdict(reason="wiki の版や本文を再照合できなかったため回答を保留しました。")
    except (OSError, RuntimeError, TypeError, AttributeError, KeyError, OverflowError, subprocess.SubprocessError):
        answer, verdict = "", Verdict(reason="Claude Code の照合付き問い合わせを完了できませんでした。")
    print(answer if verdict.passed else verdict.reason, file=sys.stdout if verdict.passed else sys.stderr)
    return 0 if verdict.passed else 2


if __name__ == "__main__":
    sys.exit(main())
