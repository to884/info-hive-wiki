#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "mcp==1.30.0",
#   "psutil==7.2.2",
# ]
# ///
"""wiki の MCP サーバーへの stdio の中継（ADR-0012）。プラグインの .mcp.json から起動する。

ホストは中継を stdio の MCP サーバーとして起動する。中継は、127.0.0.1 の wiki の MCP サーバーにツールの一覧と
呼び出しをそのまま渡す。ツールの定義はサーバーから受け取るので、中継には書かない。

- server.lock を取り、server.json の指すサーバーが生きていて版が同じならそれを使う。なければ起動する。
  同時に起動された中継があっても、サーバーは 1 つしか起動しない。
- サーバーは要求がない時間が続くと止まる。中継はセッションの間ずっと動くので、呼び出しが失敗したら
  サーバーを確かめ直し（必要なら起動し）、接続し直して 1 回だけやり直す。
- サーバーは中継の子として、Windows では窓のない自分のコンソール（CREATE_NO_WINDOW）と新しいプロセスグループで、
  POSIX では新しいセッションで起動する。中継やホストが終わってもサーバーは残り、次のセッションの中継が使う。
- サーバーの Python の環境は `uv sync --script` で用意し、`uv python find --script` で見つけた python で起動する。
  環境が作りかけで壊れていれば `--reinstall` で入れ直す。サーバーが依存を import できずに終了コード 75 で
  止まったときも、入れ直して 1 回だけ起動し直す。
- .mcp.json は、この中継を直接ではなく、依存のない入口の wiki_mcp_launch.py から起動する。

CLAUDE_PLUGIN_ROOT を持たないホスト（Codex など）は、wiki_mcp_launch.py のパスを指定して起動する。
"""
from __future__ import annotations

import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_mcp_common as common  # noqa: E402

SERVER_SCRIPT = common.SCRIPTS / "wiki_mcp_server.py"
# サーバーの依存を初めて取得するときは時間がかかるので、環境の用意と起動の待ちは長めにする。
PREPARE_TIMEOUT = 1800
# ディスクのキャッシュが冷えていると、サーバーの依存の import だけで 1 分を超えることがある（WSL で測った）。
# 待ちきれずに次の呼び出しがもう 1 つサーバーを起動しないよう、長めに待つ。
START_TIMEOUT = 300
# 環境の用意は、ロックの外の uv sync と、ロックの中の uv sync、確かめ、入れ直し、確かめ直しの 5 回まで
# PREPARE_TIMEOUT ずつかかりうる。サーバーの起動し直しの分も含めて待つ。
LOCK_TIMEOUT = 5 * PREPARE_TIMEOUT + 2 * START_TIMEOUT + 60
PYTHON_EXECUTABLE = re.compile(r"python(?:3(?:\.\d+t?)?)?(?:\.exe)?", re.IGNORECASE)
PYTHON_OPTION = re.compile(r"-(?:[bBdEiIOPqRsSuvx]+|[WX].+)|--check-hash-based-pycs=.+")
SERVER_SCRIPT_SUFFIX = Path("skills/info-hive/scripts/wiki_mcp_server.py")


def log(message: str) -> None:
    print(f"[wiki-relay] {message}", file=sys.stderr, flush=True)


def alive(info: dict | None) -> bool:
    import psutil
    if not info:
        return False
    try:
        if not psutil.pid_exists(int(info["pid"])):
            return False
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=0.5):
            return True
    except (OSError, ValueError, TypeError):
        return False


def server_python(log_file, force: bool = False) -> str:
    """サーバーのスクリプトの依存を入れた環境を用意し、その python のパスを返す。

    環境が作りかけで壊れていれば、入れ直す（wiki_mcp_common.prepare_script）。force が真なら、
    `uv sync` が成功しても依存を import できるかを確かめ、できなければ入れ直す。
    """
    return common.prepare_script(SERVER_SCRIPT, output=log_file, timeout=PREPARE_TIMEOUT, log=log, force=force)


def spawn_server(python: str, log_file, *, start_id: str) -> subprocess.Popen:
    args = [python, "-X", "utf8", str(SERVER_SCRIPT)]
    data = common.data_dir().absolute()
    env = {**os.environ, "PYTHONUTF8": "1", common.DATA_DIR_ENV: str(data), common.START_ID_ENV: start_id}
    common_kwargs = {"stdin": subprocess.DEVNULL, "stdout": log_file, "stderr": log_file, "env": env,
                     "cwd": str(data), "close_fds": True}
    # Windows では窓のない自分のコンソール（server_creationflags、info-hive-wiki#36）、POSIX では新しいセッションにする。
    process = subprocess.Popen(args, creationflags=common.server_creationflags(), start_new_session=os.name != "nt",
                               **common_kwargs)
    # POSIX では、止まったサーバーを親の中継が回収しないとゾンビとして pid が残り、pid が生きているように見える。
    # 中継はセッションのあいだ動き続けるので、サーバーの終了を待って回収するスレッドを置く。
    threading.Thread(target=process.wait, name="reap-server", daemon=True).start()
    return process


def running_script(process) -> Path | None:
    """Python が実行するファイルを確認する。ファイル以外を実行する -c、-m、- は対象外。"""
    import psutil
    try:
        executable = Path(process.exe()).name
    except psutil.AccessDenied:
        # システムサービスなどは実行ファイルのパスを読めなくても名前を取得できる。
        name = process.name()
        if name and not PYTHON_EXECUTABLE.fullmatch(name):
            return None
        raise
    if not PYTHON_EXECUTABLE.fullmatch(executable):
        return None
    args = iter(process.cmdline()[1:])
    for arg in args:
        if arg == "--":
            arg = next(args, "")
            break
        if not arg.startswith("-"):
            break
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            next(args, None)
        elif not PYTHON_OPTION.fullmatch(arg):
            return None
    else:
        return None
    if not arg or arg == "-":
        return None
    script = Path(arg)
    if not script.is_absolute():
        script = Path(process.cwd()) / script
    return script.resolve()


def refused_connection(port: int) -> bool:
    """接続拒否を確認する。時間切れとほかの通信エラーでは待ち受けの有無を判定しない。"""
    try:
        # Windows ではループバックでも、接続拒否を返すまで約 2 秒かかる場合がある。
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def recorded_process(info: dict, root: Path):
    """本人のプロセスを返す。終了済みの記録は None、帰属が不明な記録はエラーにする。"""
    import psutil
    pid = info["pid"]
    try:
        process = psutil.Process(pid)
        script = running_script(process)
        if script is None or Path(*script.parts[-4:]) != SERVER_SCRIPT_SUFFIX:
            if refused_connection(info["port"]):
                return None  # PID は別のプログラムが使っており、記録の待ち受けも閉じている。
            raise RuntimeError(f"pid {pid} が記録のサーバーと確認できない: {root}")
        environ = process.environ()
        start_id = info.get("start_id")
        if "start_id" in info or common.START_ID_ENV in environ:
            if not isinstance(start_id, str) or not start_id:
                raise RuntimeError(f"pid {pid} の起動識別子を確認できない: {root}")
            if environ.get(common.START_ID_ENV) != start_id:
                raise RuntimeError(f"pid {pid} の起動識別子が記録と一致しない: {root}")
        else:
            started = info.get("started")
            if type(started) not in (int, float) or isinstance(started, float) and not math.isfinite(started):
                raise RuntimeError(f"pid {pid} の起動時刻が記録にない: {root}")
            if process.create_time() > started:
                raise RuntimeError(f"pid {pid} の生成時刻が公開時刻より後で、記録のサーバーと確認できない: {root}")
        data = common.data_dir(environ)
        if not data.is_absolute():
            data = Path(process.cwd()) / data
        if not data.samefile(root):
            raise RuntimeError(f"pid {pid} は別のデータの場所で動いている: {root}")
        # 異なるインストール先の旧版には起動識別子がない。記録の接続先を所有することまで確かめる。
        if start_id is None and script != SERVER_SCRIPT:
            address = ("127.0.0.1", info["port"])
            if not any(c.status == psutil.CONN_LISTEN and c.laddr == address for c in process.net_connections("tcp")):
                raise RuntimeError(f"pid {pid} が記録の接続先を所有していると確認できない: {root}")
        return process
    except psutil.NoSuchProcess:
        return None
    except (psutil.Error, OSError, ValueError) as exc:
        raise RuntimeError(f"pid {pid} が記録のサーバーと確認できない。{exc}") from exc


def stop_server(process) -> None:
    """旧サーバーの終了が確認できるまで待つ。"""
    import psutil
    try:
        process.terminate()
        process.wait(timeout=30)
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        raise RuntimeError(f"pid {process.pid} の停止を確認できなかった。{exc}") from exc


def ensure_server() -> tuple[dict, bool]:
    """生きているサーバーの server.json を返す。なければ起動する。2 つ目は今起動したかどうか。"""
    try:
        return _ensure_server()
    except (OSError, ValueError) as exc:
        message = f"wiki の MCP サーバーの起動ファイルを使えない。{exc}"
        log(message)
        raise RuntimeError(message) from exc
    except RuntimeError as exc:
        log(str(exc))
        raise


def _ensure_server() -> tuple[dict, bool]:
    files = common.ServerFiles()
    version = common.code_version()
    with files.startup_lock(timeout=LOCK_TIMEOUT):
        info = files.read_info()
        if info is not None:
            try:
                process = recorded_process(info, files.root)
            except RuntimeError as exc:
                raise RuntimeError(f"{exc}。記録の場所: {files.record}。"
                                   "以前のサーバーが停止済みと確認できた場合は、記録を退避してからやり直す。") from exc
            if process is not None:
                if alive(info) and info.get("version") == version:
                    return info, False
                log(f"古いサーバー（pid {info['pid']}）を止めて起動し直す")
                stop_server(process)
        with files.open_log("server.log") as log_file:
            start_id = files.reserve_start(info)
            try:
                return _start_server(files, start_id, log_file), True
            finally:
                files.release_start(start_id)


def _start_server(files: common.ServerFiles, start_id: str, log_file) -> dict:
    log_path = files.root / "logs" / "server.log"
    python = server_python(log_file)
    process = spawn_server(python, log_file, start_id=start_id)
    repaired = False
    ends = time.monotonic() + START_TIMEOUT
    while time.monotonic() < ends:
        info = files.read_info()
        if info and info.get("start_id") == start_id and alive(info):
            return info
        code = process.poll()
        if code == common.RUNTIME_LIB_EXIT:
            # Visual C++ 再頒布可能パッケージがない。入れ直しても直らないので、対処を伝えて止まる。
            common.raise_vc_runtime_error("wiki の MCP サーバーが依存の DLL を読み込めない。")
        if code == common.BROKEN_ENV_EXIT and not repaired:
            # サーバーが依存を import できなかった。環境を確かめて入れ直し、1 回だけ起動し直す。
            log("サーバーが依存を import できなかったので、環境を入れ直して起動し直す")
            repaired = True
            python = server_python(log_file, force=True)
            process = spawn_server(python, log_file, start_id=start_id)
            ends = time.monotonic() + START_TIMEOUT
            continue
        if code is not None:
            raise RuntimeError(f"wiki の MCP サーバーが終了コード {code} で止まった。"
                               f"{log_path} を見る")
        time.sleep(0.05)
    raise RuntimeError(f"wiki の MCP サーバーが {START_TIMEOUT} 秒以内に起動しなかった。{log_path} を見る")


def flatten(exc: BaseException) -> list[BaseException]:
    inner = getattr(exc, "exceptions", None)
    if not inner:
        return [exc]
    return [leaf for e in inner for leaf in flatten(e)]


def cancel(scopes: set) -> None:
    for scope in list(scopes):
        scope.cancel()


class Upstream:
    """サーバーとの MCP のセッション。接続は 1 つのタスクが持ち、切れたら作り直す。

    mcp 1.30 のクライアントは、サーバーが止まって送信に失敗しても、待っている要求に応答を返さない。
    接続を閉じても、待っている要求は返らない。そのため、接続が切れたら待っている呼び出しを取り消す。
    接続ごとに 1 つの見張りが 1 秒ごとにサーバーを確かめ、止まっていたら接続を閉じる。取り消された
    呼び出しは、call() がつなぎ直してやり直す。前の呼び出しから CHECK_AFTER_SECONDS 秒以上
    空いたときは、呼び出しの前にも確かめる（要求がない時間が続くとサーバーは止まる）。
    呼び出しのたびに確かめたり見張りを作ったりすると、p95 が 20 ms ほど増えた。
    """

    WATCH_SECONDS = 1.0
    CHECK_AFTER_SECONDS = 1.0

    def __init__(self, task_group):
        import anyio
        self._anyio = anyio
        self._tg = task_group
        self._session = None
        self._closed = None
        self._info = None
        self._lock = anyio.Lock()
        self.started_server = None
        self._last_ok = 0.0
        self._pending: set = set()  # 今の接続で待っている呼び出しの取り消しの範囲

    async def _connect(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        anyio = self._anyio
        info, started = await anyio.to_thread.run_sync(ensure_server)
        self.started_server = started
        ready = anyio.Event()
        closed = anyio.Event()
        pending: set = set()  # この接続で待っている呼び出し。接続が切れたら取り消す
        box = {}

        async def hold():
            try:
                client = httpx.AsyncClient(headers={"Authorization": f"Bearer {info['token']}"},
                                           timeout=httpx.Timeout(30.0, read=None))
                async with client:
                    async with streamable_http_client(f"http://127.0.0.1:{info['port']}/mcp",
                                                      http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            box["session"] = session
                            ready.set()
                            await closed.wait()
            except BaseException as exc:  # noqa: BLE001 - 呼び出し側に渡してやり直させる
                box["error"] = exc
                if not isinstance(exc, Exception):
                    raise
            finally:
                # 接続が切れたら（サーバーが止まった、セッションが切れたなど）、待っている呼び出しを取り消す。
                closed.set()
                cancel(pending)
                ready.set()

        async def watch():
            while not closed.is_set():
                await anyio.sleep(self.WATCH_SECONDS)
                if not closed.is_set() and not await anyio.to_thread.run_sync(alive, info):
                    log("サーバーが止まったので接続を閉じ、待っている呼び出しを取り消す")
                    closed.set()
                    cancel(pending)

        self._tg.start_soon(hold)
        await ready.wait()
        if "session" not in box:
            raise RuntimeError(f"wiki の MCP サーバーにつなげなかった: {box.get('error')!r}")
        self._tg.start_soon(watch)
        self._session, self._closed, self._info, self._pending = box["session"], closed, info, pending

    async def session(self):
        async with self._lock:
            usable = self._session is not None and not self._closed.is_set()
            stale = time.monotonic() - self._last_ok > self.CHECK_AFTER_SECONDS
            if usable and stale and not await self._anyio.to_thread.run_sync(alive, self._info):
                self._closed.set()
                usable = False
            if not usable:
                self._session = None
                await self._connect()
            return self._session, self._pending

    async def reset(self):
        async with self._lock:
            if self._closed is not None:
                self._closed.set()
            self._session = None

    async def call(self, fn):
        for attempt in range(2):
            session, pending = await self.session()
            scope = self._anyio.CancelScope()
            pending.add(scope)
            try:
                try:
                    with scope:
                        result = await fn(session)
                finally:
                    pending.discard(scope)
                if scope.cancelled_caught:
                    raise RuntimeError("wiki の MCP サーバーが呼び出しの途中で止まった")
                self._last_ok = time.monotonic()
                return result
            except Exception as exc:  # noqa: BLE001 - サーバーが止まった、セッションが切れたなど
                await self.reset()
                leaves = flatten(exc)
                if attempt:
                    raise leaves[0] from exc
                log(f"サーバーとの接続が切れたのでやり直す（{', '.join(type(e).__name__ for e in leaves)}）")

    async def close(self):
        await self.reset()


async def serve() -> None:
    import anyio
    from mcp import types
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.stdio import stdio_server

    server = Server("wiki", instructions="info-hive の wiki を検索し、ページと索引を読むためのツール。"
                                         "wiki には初期化された wiki（ルートに SCHEMA.md がある）のルートを渡す。"
                                         "SCHEMA.md のないディレクトリはエラーになるので、/wiki:init を提案する。")

    async with anyio.create_task_group() as tg:
        upstream = Upstream(tg)

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            result = await upstream.call(lambda s: s.list_tools())
            return result.tools

        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
            return await upstream.call(lambda s: s.call_tool(name, arguments))

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options(NotificationOptions()))
        await upstream.close()
        tg.cancel_scope.cancel()


def main() -> None:
    # uv の環境が壊れていれば BROKEN_ENV_EXIT で止まる。標準入力を読む前なので、入口が環境を入れ直して
    # 起動し直しても、ホストとの通信は崩れない。
    common.require_env(common.RELAY_MODULES)
    if len(sys.argv) > 1 and sys.argv[1] == common.CHECK_IMPORTS_ARG:
        return
    import anyio
    if len(sys.argv) > 1 and sys.argv[1] == "--ensure":
        # 診断用: サーバーを確かめ（なければ起動し）、server.json の中身からトークンを除いて表示する。
        info, started = ensure_server()
        shown = {k: v for k, v in info.items() if k not in ("token", "start_id")}
        print({"started_now": started, **shown})
        return
    anyio.run(serve)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
