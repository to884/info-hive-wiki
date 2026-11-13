#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "mcp==1.30.0",
#   "uvicorn==0.54.0",
#   "numpy==2.5.4",
#   "usearch==2.26.6",
#   "tantivy==0.27.0",
#   "fastembed==0.8.0",
#   "onnxruntime==1.31.1",
#   "pyyaml==6.0.3",
# ]
# ///
"""wiki の MCP サーバー（ADR-0012）。127.0.0.1 で待ち受ける streamable HTTP のサーバーで、応答は JSON にする。

ふつうは直接起動しない。stdio の中継（wiki_mcp_relay.py）が、動いているサーバーがなければ起動する。

- 127.0.0.1 だけで待ち受ける。起動ごとのトークンを Bearer で確かめ、Host ヘッダーを確かめる（ADR-0001）。
- 起動したら、ポート、トークン、pid、コードの版を server.json に一時ファイル経由で書く。
- 要求がない状態が INFO_HIVE_MCP_IDLE_SECONDS 秒（既定 900 秒）続いたら止まる。処理中の要求と、
  裏で動いている埋め込みがある間は止まらない。
- モデルの読み込み、埋め込み、索引の書き込みは、このプロセスだけが行う。wiki の Markdown は書き換えない。

ツール: query、get_page、list_index、lint、stats、graph、index_chunks。lint、stats、graph は、
wiki_lint.py、wiki_graph_lint.py、wiki_stats.py、wiki_graph_query.py の関数を同じプロセスで呼ぶ薄い包みで、
CLI の --json と同じ形を返す。lint は検査だけで、直す操作は持たない。
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
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_mcp_common as common  # noqa: E402

# 埋め込みの待ちが少ないときは、検索の前にその場で埋め込む。多いときは裏で埋め込み、語彙の結果と途中の
# ベクトルで先に答える（初回の再埋め込みは長くかかる。ADR-0011）。
INLINE_EMBED_LIMIT = 64
# 要求の本文の上限。mcp の既定（4 MiB）では、ベクトルつきの index_chunks が数百件で上限に当たる。
# 127.0.0.1 だけで待ち受け、トークンを確かめるので、大きくしてよい。
MAX_REQUEST_BYTES = 64 * 1024 * 1024


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


def wiki_root(wiki: str) -> Path:
    """ツールに渡された wiki のルートを確かめて返す。なければ ValueError（ツールのエラーになる）。

    SCHEMA.md のないディレクトリは、初期化された wiki とみなさない（ADR-0019）。索引のキャッシュ（State.wikis）を
    引く前に毎回確かめるので、あとで SCHEMA.md を消した wiki にも答えない。判定はパスを解決するより前に行い、
    ない、ディレクトリでない、調べられないも、同じ理由の文と案内で返す。
    """
    import unicodedata

    import wiki_initialized
    if not wiki:
        raise ValueError("wiki（wiki のルートのパス）を指定する")
    # 正規化と同じく NFC にそろえてから確かめる（normalize_wiki_root はそのあとで同じパスを解決する）。
    wiki_initialized.require(Path(unicodedata.normalize("NFC", wiki)).expanduser())
    try:
        return common.normalize_wiki_root(wiki)
    except OSError as exc:
        raise ValueError(f"wiki のディレクトリを確かめられない: {wiki}（{type(exc).__name__}）\n"
                         f"{wiki_initialized.INIT_HINT}") from exc


def plain(value):
    """CLI の `--json` と同じく、日付などを文字列にした JSON の値にそろえる。"""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def lint_graph(root: Path) -> dict:
    """`wiki_graph_lint.py --json` と同じ検査。オントロジーの有無と読み方は CLI の 3 つの入口と同じ規則に従う。"""
    import wiki_graph_common
    try:
        ontology_path = wiki_graph_common.require_ontology(root)
    except wiki_graph_common.GraphLayerDisabled as exc:
        return {"state": "disabled", "message": wiki_graph_common.disabled_message(exc)}
    import wiki_graph_extract
    import wiki_graph_lint
    try:
        ontology = wiki_graph_extract.read_ontology(ontology_path)
    except wiki_graph_extract.OntologyError as exc:
        return {"state": "error", "message": str(exc)}
    return {"state": "checked", "findings": plain(wiki_graph_lint.lint(wiki_graph_lint.collect_pages(root), ontology))}


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
        root = wiki_root(wiki)
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
        max_request_body_size=MAX_REQUEST_BYTES,
        instructions="info-hive の wiki を検索し、ページと索引を読み、lint、統計、グラフの問い合わせを行うためのツール。"
                     "wiki の Markdown は書き換えない。wiki には初期化された wiki（ルートに SCHEMA.md がある）のルートを"
                     "渡す。SCHEMA.md のないディレクトリはエラーになるので、/wiki:init を提案する。",
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

        wiki には wiki のルートのパスを渡す。初期化された wiki（ルートに SCHEMA.md がある）でなければエラーになり、
        索引も作らない。結果の形は `wiki_search.py --json` と同じ（results の各行に slug、
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
            return wiki_index.list_index(wiki_root(wiki), name)
        return await offload(run)

    @mcp.tool()
    async def lint(wiki: str, soft_cap: int = 100, hard_cap: int = 200, soft_char_cap: int = 10000,
                   hard_char_cap: int = 25000, required_fm: list[str] | None = None, suggest_pages: bool = False,
                   suggest_min: int = 5, graph: bool = True, allowed_tags: list[str] | None = None) -> dict:
        """wiki に lint をかけ、検出結果を返す。検査するだけで、wiki は直さない。

        結果は `wiki_lint.py --json` と同じ形（orphans、broken_links、oversized_hard などと summary）で、
        引数も同じ意味を持つ（soft_cap と hard_cap は行数、soft_char_cap と hard_char_cap は空白を除いた
        文字数）。SCHEMA.md が別の上限を決めている wiki では、その値を渡す。required_fm を省くと
        type、title、tags、created、updated を必須にする。
        allowed_tags には SCHEMA.md の「タグの体系」に並ぶタグを渡す。渡すと体系にないタグを unknown_tags に
        挙げる。省くか空にするとタグの検査をせず、summary の unknown_tags は null になる。
        graph が真なら、`wiki_graph_lint.py --json` の結果も graph に入れる。graph の state は、
        checked（findings に結果）、disabled（オントロジーがない。message に理由）、error（オントロジーを
        読めない）、skipped（graph が偽）のどれかになる。判定モデルを使う検査（--decide）は含めない。
        """
        if min(soft_cap, hard_cap, soft_char_cap, hard_char_cap, suggest_min) < 1:
            raise ToolError("上限と suggest_min は 1 以上にする")

        def run():
            import wiki_lint
            root = wiki_root(wiki)
            fields = list(required_fm) if required_fm is not None else list(wiki_lint.DEFAULT_REQUIRED_FM)
            findings = wiki_lint.lint(wiki_lint.collect_pages(root), soft_cap, hard_cap, fields, suggest_pages,
                                      suggest_min, soft_char_cap, hard_char_cap,
                                      index_entries=wiki_lint.collect_index_entries(root),
                                      allowed_tags=allowed_tags)
            result = plain(findings)
            result["graph"] = lint_graph(root) if graph else {"state": "skipped"}
            return result
        return await offload(run)

    @mcp.tool()
    async def stats(wiki: str) -> dict:
        """wiki の規模、構成、リンク密度の集計と、このサーバーの索引の状態を返す。

        集計は `wiki_stats.py --json` と同じ形（pages、chars、pages_by_type、largest、most_linked、advice
        など）。index に索引のセクション数、埋め込み済みの数、状態（ready、embedding、pending）、置き場所、
        埋め込みモデルを、server にサーバーのコードの版を足す。索引をまだ開いていなければ開いて wiki の
        変更を取り込むが、埋め込みは始めない（query を呼ぶと始まる）。
        """
        def run():
            import wiki_index
            import wiki_stats
            index = state.wiki(wiki)
            index.refresh()
            result = wiki_stats.compute_stats(index.root)
            counts = index.collection.stats()
            if counts["rows"] == counts["embedded"]:
                index_state = "ready"
            elif index.root in state.embedding:
                index_state = "embedding"
            else:
                index_state = "pending"
            model_ready = wiki_index._EMBEDDER["value"] is not None or (
                common.models_dir() / wiki_index.MODEL["folder"] / "verified.json").is_file()
            result["index"] = {
                "sections": counts["rows"], "embedded": counts["embedded"],
                "pending": counts["rows"] - counts["embedded"], "state": index_state, "dir": str(index.dir),
                "embedder": wiki_index.embedder_id(),
                "fingerprint": wiki_index.fingerprint_digest(wiki_index.wiki_fingerprint()),
                "model": {"name": wiki_index.MODEL["name"], "runtime": wiki_index.MODEL["runtime"],
                          "ready": model_ready, "error": wiki_index._EMBEDDER.get("error")},
            }
            result["server"] = {"version": common.code_version(), "protocol": common.PROTOCOL, "pid": os.getpid()}
            return result
        return await offload(run)

    @mcp.tool()
    async def graph(wiki: str, command: Literal["neighbors", "edges", "path", "facts"], node: str | None = None,
                    subject: str | None = None, predicate: str | None = None, from_node: str | None = None,
                    to_node: str | None = None, max_depth: int = 6, about: str | None = None) -> dict:
        """wiki のグラフ（`<wiki>/graph/graph.sqlite`）に問い合わせる。結果は `wiki_graph_query.py --json` と同じ形。

        command ごとに要る引数: neighbors は node（1 ホップ先のノード）、edges は subject（と任意の
        predicate）、path は from_node と to_node（有向の最短経路。max_depth まで）、facts は about（出る
        エッジと入るエッジ）。ノード ID は `concept:消費税` のような形で、NFC にそろえてから探す。
        グラフはナビゲーション用の索引にすぎない。重要な主張は、エッジの source のページで確かめる。
        オントロジーがない wiki と、graph.sqlite がまだない wiki ではエラーになる（グラフの DB は
        wiki_graph_extract.py が作る。このツールは DB を作らず、読み取り専用で開く）。
        """
        if max_depth < 1:
            raise ToolError("max_depth は 1 以上にする")

        def run():
            import wiki_graph_common
            import wiki_graph_query
            root = wiki_root(wiki)
            try:
                result = wiki_graph_query.run_query(root, command, node=node, subject=subject, predicate=predicate,
                                                    start=from_node, goal=to_node, max_depth=max_depth, about=about)
            except wiki_graph_common.GraphLayerDisabled as exc:
                raise ValueError(wiki_graph_common.disabled_message(exc)) from exc
            return plain(result)
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
        1 回の要求の本文は 64 MiB まで。ベクトルつきなら 1,000〜2,000 件ずつに分けて渡す。
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
    parser.add_argument(common.CHECK_IMPORTS_ARG, action="store_true",
                        help="uv の環境がそろっているかだけを確かめて終わる（中継が環境を確かめるときに使う）")
    args = parser.parse_args()
    # uv の環境が壊れていれば BROKEN_ENV_EXIT で止まる。中継は、それを見て環境を入れ直して起動し直す。
    # 依存はふだん使う時点で読み込むので、確かめないと、待ち受けたあとの要求で初めて失敗してしまう。
    common.require_env(common.SERVER_MODULES)
    if args.check_imports:
        return
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
