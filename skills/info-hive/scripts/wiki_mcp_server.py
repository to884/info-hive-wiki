#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp==1.30.0",
#   "uvicorn==0.54.0",
#   "numpy==2.5.4",
#   "usearch==2.26.6",
#   "tantivy==0.27.0",
#   "fastembed==0.8.0",
#   "onnxruntime==1.31.1",
# ]
# ///
"""wiki の MCP サーバー（ADR-0012）。127.0.0.1 で待ち受ける streamable HTTP のサーバーで、応答は JSON にする。

ふつうは直接起動しない。stdio の中継（wiki_mcp_relay.py）が、動いているサーバーがなければ起動する。

- 127.0.0.1 だけで待ち受ける。起動ごとのトークンを Bearer で確かめ、Host ヘッダーを確かめる（ADR-0001）。
- 起動したら、ポート、トークン、pid、コードの版を server.json に一時ファイル経由で書く。
- 要求がない状態が INFO_HIVE_MCP_IDLE_SECONDS 秒（既定 900 秒）続いたら止まる。処理中の要求と、
  裏で動いている埋め込みがある間は止まらない。
- モデルの読み込み、埋め込み、索引の書き込みは、このプロセスだけが行う。wiki の Markdown は書き換えない。

ツール: query、get_page、list_index、index_chunks。
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_mcp_common as common  # noqa: E402

# 埋め込みの待ちが少ないときは、検索の前にその場で埋め込む。多いときは裏で埋め込み、語彙の結果と途中の
# ベクトルで先に答える（初回の再埋め込みは長くかかる。ADR-0011）。
INLINE_EMBED_LIMIT = 64


class Activity:
    """処理中の要求の数と、最後に要求が終わった時刻。裏の作業も busy として数える。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.in_flight = 0
        self.background = 0
        self.last = time.monotonic()

    def enter(self):
        with self._lock:
            self.in_flight += 1
            self.last = time.monotonic()

    def exit(self):
        with self._lock:
            self.in_flight -= 1
            self.last = time.monotonic()

    def touch(self):
        with self._lock:
            self.last = time.monotonic()

    def bg(self, delta: int):
        with self._lock:
            self.background += delta
            self.last = time.monotonic()

    def idle_for(self) -> float:
        with self._lock:
            if self.in_flight or self.background:
                return 0.0
            return time.monotonic() - self.last


def json_response(status: int, payload: dict, extra_headers=()):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())]
    headers += list(extra_headers)

    async def respond(send):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
    return respond


class Guard:
    """ASGI のミドルウェア。Host ヘッダーと Bearer のトークンを確かめ、要求の出入りを数える。

    DNS rebinding を防ぐため、Host は 127.0.0.1:<ポート> だけを受け付ける。トークンは定数時間で比べる。
    数えるのは POST だけにする。MCP のクライアントが開いたままにする GET（サーバーからの通知の流れ）を
    数えると、いつまでも止まらなくなるためである。
    """

    def __init__(self, app, token: str, port: int, activity: Activity):
        self.app = app
        self.expected = f"Bearer {token}".encode("latin-1")
        self.hosts = {f"127.0.0.1:{port}".encode()}
        self.activity = activity

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if headers.get(b"host") not in self.hosts:
            return await json_response(421, {"error": "misdirected request"})(send)
        if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
            return await json_response(401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])(send)
        counted = scope.get("method") == "POST"
        if counted:
            self.activity.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            if counted:
                self.activity.exit()


class State:
    """wiki とチャンクの索引、埋め込みモデル、裏の埋め込みを持つ。"""

    def __init__(self, activity: Activity):
        self.activity = activity
        self.lock = threading.Lock()
        self.wikis = {}
        self.chunks = {}
        self.embedding = set()      # 裏で埋め込み中の wiki
        self.stopping = threading.Event()
        self.model_loading = False
        self.threads = []

    def wiki(self, wiki: str):
        import wiki_index
        if not wiki:
            raise ValueError("wiki（wiki のルートのパス）を指定する")
        try:
            root = common.normalize_wiki_root(wiki)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ValueError(f"wiki のルートが見つからない: {wiki}") from exc
        with self.lock:
            index = self.wikis.get(root)
            if index is None:
                index = self.wikis[root] = wiki_index.WikiIndex(root)
        return index

    def chunk_store(self, name: str):
        import wiki_index
        with self.lock:
            store = self.chunks.get(name)
            if store is None:
                store = self.chunks[name] = wiki_index.ChunkStore(name)
        return store

    def embedder(self, wait: bool):
        """埋め込みモデルを返す。モデルのファイルがまだなく wait が偽なら、裏で取得を始めて None を返す。"""
        import wiki_index
        if wiki_index._EMBEDDER["value"] is not None:
            return wiki_index._EMBEDDER["value"]
        ready = os.environ.get(wiki_index.TEST_EMBEDDER_ENV) == "hash" or (
            common.models_dir() / wiki_index.MODEL["folder"] / "verified.json").is_file()
        if wait or ready:
            return wiki_index.get_embedder()
        with self.lock:
            if self.model_loading:
                return None
            self.model_loading = True

        def load():
            self.activity.bg(+1)
            try:
                wiki_index.get_embedder()
            except Exception as exc:  # noqa: BLE001
                wiki_index.log(f"埋め込みモデルを用意できなかった: {exc}")
            finally:
                with self.lock:
                    self.model_loading = False
                self.activity.bg(-1)
        threading.Thread(target=load, name="model-load", daemon=True).start()
        return None

    def start_background_embedding(self, index, embedder):
        import wiki_index
        with self.lock:
            if index.root in self.embedding:
                return
            self.embedding.add(index.root)

        def run():
            self.activity.bg(+1)
            started = time.perf_counter()
            try:
                done = wiki_index.embed_pending(index.collection, embedder, should_stop=self.stopping.is_set)
                wiki_index.log(f"{index.root} の {done} 件を埋め込んだ（{time.perf_counter() - started:.1f} 秒）")
            except Exception as exc:  # noqa: BLE001
                wiki_index.log(f"{index.root} の埋め込みに失敗した: {type(exc).__name__}: {exc}")
            finally:
                with self.lock:
                    self.embedding.discard(index.root)
                self.activity.bg(-1)
        thread = threading.Thread(target=run, name="embed", daemon=True)
        self.threads.append(thread)
        thread.start()

    def query_wiki(self, wiki, query, top, type, tags, since, per_page) -> dict:
        import wiki_index
        index = self.wiki(wiki)
        index.refresh()
        warning = None
        try:
            embedder = self.embedder(wait=False)
            if embedder is None:
                warning = "埋め込みモデルを用意している。終わるまでは語彙検索だけで答える"
        except RuntimeError as exc:
            embedder, warning = None, f"埋め込みモデルを使えない（{exc}）。語彙検索だけで答える"
        if embedder is not None:
            pending = index.collection.pending_count()
            if pending and index.root not in self.embedding and pending <= INLINE_EMBED_LIMIT:
                wiki_index.embed_pending(index.collection, embedder)
            elif pending:
                self.start_background_embedding(index, embedder)
        result = index.search(query, top=top, type=type, tags=tags, since=since, per_page=per_page,
                              embedder=embedder)
        if result["index"]["state"] == "embedding" and embedder is not None:
            warning = warning or "索引を埋め込んでいる途中なので、意味の検索は一部のセクションだけが対象"
        result["warning"] = warning
        return result

    def close(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=120)
        with self.lock:
            indexes = list(self.wikis.values()) + list(self.chunks.values())
            self.wikis.clear()
            self.chunks.clear()
            for index in indexes:
                try:
                    index.close()
                except Exception as exc:  # noqa: BLE001
                    print(f"索引を閉じられなかった: {exc}", file=sys.stderr)


def build_app(state: State, token: str, port: int):
    import anyio
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        "info-hive-wiki", host="127.0.0.1", port=port, json_response=True, log_level="WARNING",
        instructions="info-hive の wiki を検索し、ページと索引を読むためのツール。wiki の Markdown は書き換えない。",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=[f"127.0.0.1:{port}"], allowed_origins=[]),
    )

    async def offload(fn, *args):
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except (ValueError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def query(query: str, wiki: str | None = None, top: int = 10, type: str | None = None,
                    tags: list[str] | None = None, since: str | None = None, per_page: int = 2,
                    collection: str | None = None, query_tokens: list[str] | None = None,
                    docs: list[str] | None = None, max_distance: float | None = None) -> dict:
        """wiki をハイブリッド検索（語彙の BM25 と埋め込み、RRF）し、セクション単位の証拠の行を返す。

        wiki には wiki のルートのパスを渡す。結果の形は `wiki_search.py --json` と同じ（results の各行に slug、
        rel_path、heading_path、snippet、score、retrievers など）。type、tags、since で絞り込める。
        collection を渡すと、wiki ではなく index_chunks で入れたチャンクを検索する。そのときは query_tokens
        （index_chunks と同じ作り方のトークン）を渡し、docs で文書を絞り込める。
        """
        if top < 1 or per_page < 1:
            raise ToolError("top と per_page は 1 以上にする")
        if collection:
            def run():
                store = state.chunk_store(collection)
                embedder = state.embedder(wait=False)
                return store.search(query, list(query_tokens or []), top=top, docs=docs, max_distance=max_distance,
                                    embedder=embedder)
            return await offload(run)
        return await offload(state.query_wiki, wiki, query, top, type, tags, since, per_page)

    @mcp.tool()
    async def get_page(wiki: str, page: str) -> dict:
        """wiki の 1 ページを返す。page はスラッグ（例: 熱中症）か wiki の中の相対パス（例: concepts/熱中症.md）。

        frontmatter は日本語の別名のキーを英語にそろえた meta として、本文は body として返す。
        """
        def run():
            index = state.wiki(wiki)
            index.refresh()
            found = index.get_page(page)
            if found is None:
                raise FileNotFoundError(f"ページが見つからない: {page}")
            return found
        return await offload(run)

    @mcp.tool()
    async def list_index(wiki: str, name: str | None = None) -> dict:
        """wiki の index.md の本文と、indexes/ のファイルの一覧を返す。name を渡すと indexes/ のそのファイルを返す。"""
        import wiki_index

        def run():
            root = common.normalize_wiki_root(wiki)
            return wiki_index.list_index(root, name)
        return await offload(run)

    @mcp.tool()
    async def index_chunks(collection: str, tokenizer: str, generation: int, chunks: list[dict],
                           delete_docs: list[str] | None = None, reset: bool = False) -> dict:
        """rag などからチャンクを受け取り、wiki の外の索引（collection ごと）に加える。wiki の Markdown は変えない。

        chunks の各要素: id（collection の中で一意）、text、tokens（語彙検索のトークンの配列。呼び出し側が
        tokenizer の方式で作る）、doc（文書の識別子）、metadata（任意）、vector か vector_b64（任意。
        ruri-v3-130m の 512 次元。なければサーバーが埋め込む）。
        同じ doc の、generation より古い世代の行は消す。delete_docs の文書は丸ごと消す。
        tokenizer が既存の collection と違うときは失敗する。reset を真にすると作り直す。
        """
        def run():
            store = state.chunk_store(collection)
            needs_embedding = any(c.get("vector") is None and not c.get("vector_b64") for c in chunks)
            embedder = state.embedder(wait=True) if needs_embedding else None
            return store.index(chunks, generation, tokenizer, list(delete_docs or []), reset, embedder)
        return await offload(run)

    return Guard(mcp.streamable_http_app(), token, port, state.activity)


def main():
    parser = argparse.ArgumentParser(description="wiki の MCP サーバー（ふつうは中継が起動する）")
    parser.add_argument("--idle", type=float, default=None, help="要求がなくなってから止まるまでの秒数")
    args = parser.parse_args()
    idle = args.idle if args.idle else common.idle_seconds()
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    activity = Activity()
    state = State(activity)
    app = build_app(state, token, port)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on", server_header=False,
                                           timeout_graceful_shutdown=5))

    async def run():
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if serving.done():
                await serving
                return
            await asyncio.sleep(0.02)
        # 待機の時間は、待ち受けを始めたときから数える（起動にかかった時間を含めない）。
        activity.touch()
        common.write_server_info({"port": port, "token": token, "pid": os.getpid(), "version": common.code_version(),
                                  "protocol": common.PROTOCOL, "started": time.time()})
        print(f"wiki の MCP サーバーを 127.0.0.1:{port} で起動した（pid {os.getpid()}、待機 {idle:.0f} 秒で停止）",
              file=sys.stderr, flush=True)
        try:
            while not serving.done():
                await asyncio.sleep(0.5)
                if activity.idle_for() > idle:
                    print("要求がないので止まる", file=sys.stderr, flush=True)
                    server.should_exit = True
                    break
            await serving
        finally:
            # 索引を閉じ終えてから server.json を消す。次のサーバーは索引のロックを待つ。
            state.close()
            common.remove_server_info(os.getpid())

    try:
        asyncio.run(run())
    finally:
        state.close()
        common.remove_server_info(os.getpid())


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
