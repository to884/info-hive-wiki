"""wiki の MCP サーバーが持つ検索の基盤（ADR-0011）。

- 埋め込み: cl-nagoya/ruri-v3-130m の ONNX fp32 を fastembed（onnxruntime）で動かす
- ベクトル索引: usearch の HNSW（f16、コサイン、connectivity 16、expansion_add 256、expansion_search 1,024）
- 語彙検索: Tantivy の BM25。wiki の索引には wiki_search.tokenize() の bigram のトークンを入れる
- 融合: RRF（k 30、語彙の重み 0.5、候補は片側 200 件）
- wiki の距離の上限: 0.208

索引の集まり（Collection）は、台帳（SQLite）、usearch のファイル、Tantivy のディレクトリの 3 つでできている。
台帳を正とし、usearch と Tantivy は台帳から作り直せる派生物として扱う。更新の途中で止まったときは、
台帳の dirty の印を見て、次に開いたときに 3 つをそろえ直す（reconcile）。

書き手はこのプロセス（wiki の MCP サーバー）だけである。同じプロセスの中の検索とは、書き手を優先する
読み書きロックで分ける。usearch は一時ファイルに保存してから os.replace で置き換える。Tantivy の commit が
失敗したら rollback し、同じ変更を入れ直す。

MCP には依存しない。テストと測定から直接呼べる。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
from argparse import Namespace
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_mcp_common as common  # noqa: E402
import wiki_pages  # noqa: E402
import wiki_search  # noqa: E402

# 埋め込みモデル（ADR-0011）。重みは onnx-community の書き出し、トークナイザーと設定は cl-nagoya の固定の
# revision から取り、models/ の下に組み立てる。sha256 は追加測定 E9（onnx-distribution.json）の値。
MODEL = {
    "name": "cl-nagoya/ruri-v3-130m",
    "dimension": 512,
    "query_prefix": "検索クエリ: ",
    "passage_prefix": "検索文書: ",
    "max_distance": 0.208,
    "files": [
        ("onnx-community/ruri-v3-130m-ONNX", "cbd305d39c17cc58375ceb29586996e7d98d48c0", "onnx/model.onnx",
         "1b9031dd54d448c236f8534e2256fabe73fe7b20dc8770d2490f1700119f98b4"),
        ("cl-nagoya/ruri-v3-130m", "e3114c6ee10dbab8b4b235fbc6dcf9dd4d5ac1a6", "config.json",
         "d1dd6fc1235582f38e4d6820d4ca5b4e8e7a118320a9e346dffec162c2793284"),
        ("cl-nagoya/ruri-v3-130m", "e3114c6ee10dbab8b4b235fbc6dcf9dd4d5ac1a6", "tokenizer.json",
         "0a94ac9a0a02c067bdef25b72ae9f4ee33f48f552e55988d444f6d25eeb1d062"),
        ("cl-nagoya/ruri-v3-130m", "e3114c6ee10dbab8b4b235fbc6dcf9dd4d5ac1a6", "tokenizer_config.json",
         "34004511993472f15c7202074284c57788c161fb6567de164b8e0c0266f155e4"),
        ("cl-nagoya/ruri-v3-130m", "e3114c6ee10dbab8b4b235fbc6dcf9dd4d5ac1a6", "special_tokens_map.json",
         "30bf8256f9a1eb3287af2a9b7940465e29a38aad4a459a3e773bb3a14bd34c0f"),
    ],
    "folder": "ruri-v3-130m-onnx-cbd305d",
    "runtime": "fastembed-0.8.0+onnxruntime-1.31.1/fp32",
}
USEARCH = {"metric": "cos", "dtype": "f16", "connectivity": 16, "expansion_add": 256, "expansion_search": 1024}
FUSION = {"k": 30, "lexical_weight": 0.5, "candidates": 200}
# ベクトルの数がこれ以下なら、HNSW ではなく総当たりで探す。HNSW のグラフは作るたびに少し変わり、
# 小さな wiki（評価セットの 103 件）でも、作り直すと 32 問中 13 問で意味の検索の順位が変わった。
# 総当たりなら結果は決まり、この規模では十分に速い。
EXACT_SEARCH_LIMIT = 50_000
# 古い世代の行が残っている間は、候補をこの倍率で多めに取ってから無効な行を除く（ADR-0011）。
OVERFETCH = 1.25
# 1 回の埋め込みに渡す文字数の上限の目安。長い文書をまとめて渡すと onnxruntime が大きな領域を確保する
# （追加測定 E6）ので、文字数でバッチを区切る。
EMBED_BATCH_ITEMS = 16
EMBED_BATCH_CHARS = 12_000
# usearch の保存の間隔。大きな索引を毎回書き出さないよう、最後の保存からこの秒数が過ぎるまで遅らせる。
SAVE_INTERVAL_SECONDS = 30.0
TANTIVY_RETRIES = 6
# 索引の書き手のロックを待つ秒数。止まりかけのサーバーが索引を閉じ終えるのを待つ。
WRITER_LOCK_TIMEOUT = 60.0
LEDGER_SCHEMA = 1
# 埋め込みに使うスレッドの数（onnxruntime の intra_op）。未設定なら onnxruntime の既定（全コア）。
EMBED_THREADS_ENV = "INFO_HIVE_EMBED_THREADS"
# テスト用の埋め込み。値が "hash" のとき、モデルを読み込まずに文字の bigram から決定的なベクトルを作る。
TEST_EMBEDDER_ENV = "INFO_HIVE_TEST_EMBEDDER"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 埋め込み
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model_files(target: Path | None = None) -> Path:
    """モデルのファイルを models/ に組み立てる。取得したら sha256 を確かめ、確かめた印を残す。"""
    target = target or common.models_dir() / MODEL["folder"]
    marker = target / "verified.json"
    expected = {name: sha for _, _, name, sha in MODEL["files"]}
    try:
        if json.loads(marker.read_text(encoding="utf-8")) == expected and all((target / n).is_file() for n in expected):
            return target
    except (OSError, ValueError):
        pass
    os.environ.setdefault("HF_HUB_DISABLE_SHARED_BLOBS", "1")
    from huggingface_hub import hf_hub_download

    target.mkdir(parents=True, exist_ok=True)
    for repo, revision, name, sha in MODEL["files"]:
        path = target / name
        if not path.is_file() or sha256_file(path) != sha:
            log(f"埋め込みモデルのファイルを取得する: {repo} {name}")
            hf_hub_download(repo, name, revision=revision, local_dir=str(target))
        actual = sha256_file(path)
        if actual != sha:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"モデルのファイル {name} の sha256 が合わない（{actual[:12]}…）")
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(expected), encoding="utf-8")
    os.replace(temporary, marker)
    return target


class Embedder:
    """ruri-v3-130m の ONNX。接頭辞はここで付ける（fastembed は付けない）。"""

    id = f"{MODEL['name']}@{MODEL['files'][0][1][:7]}/{MODEL['runtime']}"
    dimension = MODEL["dimension"]

    def __init__(self, folder: Path | None = None):
        import numpy as np
        os.environ.setdefault("HF_HUB_DISABLE_SHARED_BLOBS", "1")
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        folder = ensure_model_files(folder)
        name = "info-hive/" + MODEL["folder"]
        if name.lower() not in {m["model"].lower() for m in TextEmbedding.list_supported_models()}:
            TextEmbedding.add_custom_model(
                model=name, pooling=PoolingType.MEAN, normalization=True,
                sources=ModelSource(hf=MODEL["files"][0][0]), dim=MODEL["dimension"],
                model_file=MODEL["files"][0][2], license="apache-2.0", size_in_gb=0.53,
            )
        self._np = np
        threads = int(os.environ.get(EMBED_THREADS_ENV, "0") or 0) or None
        self._model = TextEmbedding(model_name=name, cache_dir=str(folder.parent / "fastembed"),
                                    specific_model_path=str(folder), threads=threads)
        self._lock = threading.Lock()

    def _embed(self, texts: list[str]):
        np = self._np
        out = np.zeros((len(texts), self.dimension), dtype=np.float32)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        batch: list[int] = []
        chars = 0

        def run(indices):
            with self._lock:  # onnxruntime の 1 つのセッションを複数のスレッドで同時に使わない
                vectors = list(self._model.embed([texts[i] for i in indices], batch_size=len(indices)))
            for i, vector in zip(indices, vectors):
                out[i] = vector

        for i in order:
            if batch and (len(batch) >= EMBED_BATCH_ITEMS or chars + len(texts[i]) > EMBED_BATCH_CHARS):
                run(batch)
                batch, chars = [], 0
            batch.append(i)
            chars += len(texts[i])
        if batch:
            run(batch)
        return out

    def passages(self, texts: list[str]):
        return self._embed([MODEL["passage_prefix"] + t for t in texts])

    def query(self, text: str):
        return self._embed([MODEL["query_prefix"] + text])[0]


class HashEmbedder:
    """テスト用。文字の bigram を 512 次元に散らした決定的なベクトル。モデルを読み込まない。"""

    id = "test-hash-bigram-v1"
    dimension = MODEL["dimension"]

    def __init__(self):
        import numpy as np
        self._np = np

    def _one(self, text: str):
        np = self._np
        vector = np.zeros(self.dimension, dtype=np.float32)
        tokens = wiki_search.tokenize(text) or ["\0"]
        for token in tokens:
            h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")
            vector[h % self.dimension] += 1.0 if (h >> 32) & 1 else -1.0
        norm = float(np.linalg.norm(vector)) or 1.0
        return vector / norm

    def passages(self, texts: list[str]):
        return self._np.stack([self._one(t) for t in texts]) if texts else self._np.zeros((0, self.dimension), "float32")

    def query(self, text: str):
        return self._one(text)


_EMBEDDER = {"value": None, "error": None}
_EMBEDDER_LOCK = threading.Lock()


def get_embedder():
    """プロセスに 1 つの埋め込みを返す。読み込めなければ RuntimeError（語彙検索だけで続けるため）。"""
    with _EMBEDDER_LOCK:
        if _EMBEDDER["value"] is None:
            try:
                if os.environ.get(TEST_EMBEDDER_ENV) == "hash":
                    _EMBEDDER["value"] = HashEmbedder()
                else:
                    started = time.perf_counter()
                    _EMBEDDER["value"] = Embedder()
                    log(f"埋め込みモデルを読み込んだ（{time.perf_counter() - started:.1f} 秒）")
                _EMBEDDER["error"] = None
            except Exception as exc:  # noqa: BLE001 - 理由を記録して語彙検索に戻る
                _EMBEDDER["error"] = f"{type(exc).__name__}: {exc}"
                raise RuntimeError(_EMBEDDER["error"]) from exc
        return _EMBEDDER["value"]


def embedder_id() -> str:
    return HashEmbedder.id if os.environ.get(TEST_EMBEDDER_ENV) == "hash" else Embedder.id


# ---------------------------------------------------------------------------
# 読み書きロック（書き手を優先する）
# ---------------------------------------------------------------------------

class RWLock:
    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def read(self):
        return _Guard(self._acquire_read, self._release_read)

    def write(self):
        return _Guard(self._acquire_write, self._release_write)

    def _acquire_read(self):
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1

    def _release_read(self):
        with self._cond:
            self._readers -= 1
            if not self._readers:
                self._cond.notify_all()

    def _acquire_write(self):
        with self._cond:
            self._waiting_writers += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True

    def _release_write(self):
        with self._cond:
            self._writer = False
            self._cond.notify_all()


class _Guard:
    def __init__(self, acquire, release):
        self._acquire, self._release = acquire, release

    def __enter__(self):
        self._acquire()

    def __exit__(self, *exc):
        self._release()


class WriterLock:
    """索引のディレクトリの書き手を 1 つのプロセスに限る OS のファイルロック。

    止まりかけのサーバーが索引を閉じ終える前に、次のサーバーが同じ索引を開くことを防ぐ。
    """

    NAME = "writer.lock"

    def __init__(self, folder: Path, timeout: float | None = None):
        timeout = WRITER_LOCK_TIMEOUT if timeout is None else timeout
        folder.mkdir(parents=True, exist_ok=True)
        self._handle = open(folder / self.NAME, "a+b")  # noqa: SIM115 - close() で閉じる
        ends = time.monotonic() + timeout
        while True:
            try:
                self._lock()
                return
            except OSError:
                if time.monotonic() > ends:
                    self._handle.close()
                    raise RuntimeError(f"索引 {folder.name} を別のプロセスが使っている") from None
                time.sleep(0.1)

    def _lock(self):
        if os.name == "nt":
            import msvcrt
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        if self._handle.closed:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()


# ---------------------------------------------------------------------------
# 索引の集まり（台帳 + usearch + Tantivy）
# ---------------------------------------------------------------------------

@dataclass
class Item:
    locator: str
    content_hash: str
    terms: list[str]
    text: str
    doc: str = ""
    generation: int = 0
    payload: dict = field(default_factory=dict)
    vector: object = None  # numpy の配列。None なら後で埋め込む


# 索引のディレクトリの中で、fingerprint が変わって作り直すときも残すファイル。
METADATA_FILES = ("writer.lock", "collection.json", "wiki.json")


def wipe(folder: Path, keep: tuple[str, ...] = ("writer.lock",)) -> None:
    """索引のディレクトリの中身を消す。持っているロックのファイルは残す。"""
    for child in folder.iterdir():
        if child.name in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def fingerprint_digest(fingerprint: dict) -> str:
    return hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Collection:
    """1 つの索引の集まり。wiki のセクションと、index_chunks のチャンクの両方に使う。"""

    def __init__(self, path: Path, fingerprint: dict):
        self.path = path
        self.fingerprint = fingerprint
        self.digest = fingerprint_digest(fingerprint)
        self.lock = RWLock()
        self._keys: dict[int, str] = {}       # 有効な行の key → locator
        self._locators: dict[str, int] = {}
        self._hashes: dict[str, str] = {}     # locator → content_hash
        self._vector_keys: set[int] = set()
        self._dirty_vectors = False
        self._last_save = 0.0
        # 書き込みが途中で失敗し、台帳と索引がずれているかもしれない。そろえ直すまで dirty の印を消さない。
        self._needs_reconcile = False
        self._writer_lock = WriterLock(path)
        try:
            self._open()
        except BaseException:
            self._writer_lock.release()
            raise

    # --- 開く、そろえる ---

    def _open(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self.db =sqlite3.connect(self.path / "ledger.sqlite", check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        if meta and (meta.get("fingerprint") != self.digest or meta.get("schema") != str(LEDGER_SCHEMA)):
            log(f"索引の fingerprint が変わったので作り直す: {self.path.name}")
            self.db.close()
            # 索引を開くための情報（collection.json、wiki.json）は残す。消すと、再起動のあとで見つからなくなる。
            wipe(self.path, keep=METADATA_FILES)
            return self._open()
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS rows (key INTEGER PRIMARY KEY, locator TEXT NOT NULL UNIQUE, "
                "content_hash TEXT NOT NULL, terms TEXT NOT NULL, text TEXT NOT NULL, has_vector INTEGER NOT NULL, "
                "doc TEXT NOT NULL, generation INTEGER NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS rows_doc ON rows(doc, generation)")
            if not meta:
                self.db.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [
                    ("fingerprint", self.digest), ("schema", str(LEDGER_SCHEMA)), ("next_key", "1"), ("dirty", "0"),
                    ("fingerprint_json", json.dumps(self.fingerprint, ensure_ascii=False, sort_keys=True))])
        self._open_usearch()
        self._open_tantivy()
        for key, locator, content_hash, has_vector in self.db.execute(
                "SELECT key, locator, content_hash, has_vector FROM rows"):
            self._keys[key] = locator
            self._locators[locator] = key
            self._hashes[locator] = content_hash
            if has_vector:
                self._vector_keys.add(key)
        if self._meta("dirty") != "0":
            self.reconcile()

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def _open_usearch(self):
        from usearch.index import Index
        self._usearch_path = self.path / "vectors.usearch"
        self.vectors = Index(ndim=self.fingerprint.get("dimension", MODEL["dimension"]), metric=USEARCH["metric"],
                             dtype=USEARCH["dtype"], connectivity=USEARCH["connectivity"],
                             expansion_add=USEARCH["expansion_add"], expansion_search=USEARCH["expansion_search"])
        if self._usearch_path.exists():
            try:
                # view ではなく読み込む。メモリに写像したままにすると、Windows で置き換えられなくなる。
                self.vectors.load(str(self._usearch_path))
                self.vectors.expansion_search = USEARCH["expansion_search"]
            except Exception as exc:  # noqa: BLE001 - 壊れていたら台帳から作り直す
                log(f"usearch の索引を読めなかったので作り直す（{type(exc).__name__}）")
                self.vectors.reset()
                with self.db:
                    self.db.execute("UPDATE rows SET has_vector = 0")
                    self._set_meta("dirty", "1")

    def _open_tantivy(self):
        import tantivy
        self._tantivy = tantivy
        builder = tantivy.SchemaBuilder()
        builder.add_integer_field("key", stored=True, indexed=True, fast=True)
        builder.add_text_field("terms", stored=False, tokenizer_name="ws", index_option="freq")
        self._schema = builder.build()
        folder = self.path / "lexical"
        folder.mkdir(exist_ok=True)
        self.lexical = tantivy.Index(self._schema, path=str(folder), reuse=True)
        self.lexical.register_tokenizer("ws", tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.whitespace()).build())
        self.lexical.reload()

    def reconcile(self) -> dict:
        """台帳に合わせて、Tantivy と usearch をそろえ直す。"""
        with self.lock.write():
            return self._reconcile_locked()

    def _reconcile_locked(self) -> dict:
        """reconcile の本体。書き込みのロックを持って呼ぶ。"""
        ledger = {key: terms for key, terms in self.db.execute("SELECT key, terms FROM rows")}
        searcher = self.lexical.searcher()
        in_lexical = set()
        if searcher.num_docs:
            hits = searcher.search(self._tantivy.Query.all_query(), limit=searcher.num_docs).hits
            in_lexical = {int(k) for k in searcher.fast_field_values("key", [a for _, a in hits])}
        extra = sorted(in_lexical - set(ledger))
        missing = sorted(set(ledger) - in_lexical)
        self._commit_lexical(extra, [(key, ledger[key]) for key in missing])
        stored = set(int(k) for k in self.vectors.keys) if len(self.vectors) else set()
        with_vector = {key for key, in self.db.execute("SELECT key FROM rows WHERE has_vector = 1")}
        orphan = sorted(stored - set(ledger))
        lost = sorted(with_vector - stored)
        for key in orphan:
            self.vectors.remove(key)
        with self.db:
            self.db.executemany("UPDATE rows SET has_vector = 0 WHERE key = ?", [(k,) for k in lost])
        self._vector_keys = with_vector - set(lost)
        self._needs_reconcile = False
        self._save_vectors(force=True)
        with self.db:
            self._set_meta("dirty", "0")
        summary = {"lexical_removed": len(extra), "lexical_added": len(missing),
                   "vectors_removed": len(orphan), "vectors_lost": len(lost)}
        if any(summary.values()):
            log(f"索引をそろえ直した: {self.path.name} {summary}")
        return summary

    # --- 書き込み ---

    def _commit_lexical(self, delete_keys, add_docs) -> None:
        """Tantivy に削除と追加を入れて commit する。失敗したら rollback して入れ直す（ADR-0011）。"""
        if not delete_keys and not add_docs:
            return
        tantivy = self._tantivy
        last = None
        for attempt in range(TANTIVY_RETRIES):
            writer = None
            failure = None
            try:
                writer = self.lexical.writer(heap_size=50_000_000, num_threads=1)
                for key in delete_keys:
                    writer.delete_documents_by_term("key", int(key))
                for key, terms in add_docs:
                    writer.add_document(tantivy.Document(key=int(key), terms=terms))
                writer.commit()
                writer.wait_merging_threads()
            except Exception as exc:  # noqa: BLE001 - Windows で約 2% の commit が拒否される
                # 例外の traceback は writer を参照し続け、Tantivy の書き手のロックが外れない。文字列だけを残す。
                failure = f"{type(exc).__name__}: {exc}"
                if writer is not None:
                    try:
                        writer.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            writer = None  # 書き手を手放し、Tantivy のロックを外す
            if failure is None:
                self.lexical.reload()
                return
            last = failure
            time.sleep(0.05 * (attempt + 1))
        raise RuntimeError(f"Tantivy の commit が {TANTIVY_RETRIES} 回失敗した: {last}")

    def _save_vectors(self, force: bool = False) -> None:
        if not (self._dirty_vectors or force):
            return
        if not force and time.monotonic() - self._last_save < SAVE_INTERVAL_SECONDS:
            return
        temporary = self._usearch_path.with_suffix(".usearch.tmp")
        self.vectors.save(str(temporary))
        os.replace(temporary, self._usearch_path)
        self._dirty_vectors = False
        self._last_save = time.monotonic()
        if not self._needs_reconcile:
            with self.db:
                self._set_meta("dirty", "0")

    def flush(self) -> None:
        with self.lock.write():
            self._save_vectors(force=self._dirty_vectors)

    def hashes(self) -> dict[str, str]:
        with self.lock.read():
            return dict(self._hashes)

    def apply(self, upserts: list[Item], deletes: list[str] = ()) -> dict:
        """行を加え、置き換え、消す。置き換えた行は新しい key を持つ（古い key は古い世代として消える）。"""
        with self.lock.write():
            self._before_write()
            try:
                return self._apply_locked(upserts, deletes)
            except BaseException:
                self._needs_reconcile = True
                raise

    @property
    def needs_reconcile(self) -> bool:
        return self._needs_reconcile

    def _before_write(self) -> None:
        """書き込みの前に、前に失敗した書き込みの分をそろえ直し、dirty の印を立てる。"""
        if self._needs_reconcile:
            self._reconcile_locked()
        with self.db:
            self._set_meta("dirty", "1")

    def _apply_locked(self, upserts: list[Item], deletes) -> dict:
        import numpy as np
        old_keys = []
        for locator in list(deletes) + [item.locator for item in upserts]:
            key = self._locators.get(locator)
            if key is not None:
                old_keys.append(key)
        old_keys = sorted(set(old_keys))
        next_key = int(self._meta("next_key") or 1)
        new_rows = []
        for item in upserts:
            new_rows.append((next_key, item))
            next_key += 1
        with self.db:
            self.db.executemany("DELETE FROM rows WHERE key = ?", [(k,) for k in old_keys])
            self.db.executemany(
                "INSERT INTO rows(key, locator, content_hash, terms, text, has_vector, doc, generation, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(key, item.locator, item.content_hash, " ".join(item.terms), item.text,
                  int(item.vector is not None), item.doc, int(item.generation),
                  json.dumps(item.payload, ensure_ascii=False)) for key, item in new_rows])
            self._set_meta("next_key", str(next_key))
        for key in old_keys:
            locator = self._keys.pop(key, None)
            if locator is not None:
                self._locators.pop(locator, None)
                self._hashes.pop(locator, None)
        for key, item in new_rows:
            self._keys[key] = item.locator
            self._locators[item.locator] = key
            self._hashes[item.locator] = item.content_hash
        self._commit_lexical(old_keys, [(key, " ".join(item.terms)) for key, item in new_rows])
        removed = [k for k in old_keys if k in self._vector_keys]
        for key in removed:
            self.vectors.remove(key)
            self._vector_keys.discard(key)
        given = [(key, item.vector) for key, item in new_rows if item.vector is not None]
        if given:
            self.vectors.add(np.array([k for k, _ in given], dtype=np.uint64),
                             np.stack([np.asarray(v, dtype=np.float32) for _, v in given]))
            self._vector_keys.update(k for k, _ in given)
        if removed or given:
            self._dirty_vectors = True
        self._save_vectors(force=True)
        return {"added": len(new_rows), "removed": len(old_keys)}

    def pending(self, limit: int | None = None) -> list[tuple[int, str]]:
        """ベクトルのまだない行の (key, 埋め込む文字列)。"""
        sql = "SELECT key, text FROM rows WHERE has_vector = 0 ORDER BY key"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.lock.read():
            return list(self.db.execute(sql))

    def pending_count(self) -> int:
        with self.lock.read():
            return len(self._keys) - len(self._vector_keys)

    def add_vectors(self, keys: list[int], vectors) -> int:
        """埋め込んだベクトルを加える。その間に消えた行の分は捨てる。"""
        import numpy as np
        with self.lock.write():
            pairs = [(k, v) for k, v in zip(keys, vectors) if k in self._keys and k not in self._vector_keys]
            if not pairs:
                return 0
            self._before_write()
            try:
                self.vectors.add(np.array([k for k, _ in pairs], dtype=np.uint64),
                                 np.stack([np.asarray(v, dtype=np.float32) for _, v in pairs]))
                self._vector_keys.update(k for k, _ in pairs)
                with self.db:
                    self.db.executemany("UPDATE rows SET has_vector = 1 WHERE key = ?", [(k,) for k, _ in pairs])
                self._dirty_vectors = True
                self._save_vectors()
            except BaseException:
                self._needs_reconcile = True
                raise
            return len(pairs)

    def delete_where(self, sql: str, params: tuple) -> list[str]:
        with self.lock.read():
            locators = [row[0] for row in self.db.execute(f"SELECT locator FROM rows WHERE {sql}", params)]
        if locators:
            self.apply([], locators)
        return locators

    # --- 検索 ---

    def key_of(self, locator: str) -> int | None:
        return self._locators.get(locator)

    def search_lexical(self, tokens: list[str], allowed: set[int] | None, limit: int) -> list[int]:
        """BM25 の順に key を返す。重なったトークンは重ねて数える（wiki の BM25 と同じ）。"""
        if not tokens:
            return []
        tantivy = self._tantivy
        with self.lock.read():
            searcher = self.lexical.searcher()
            if not searcher.num_docs:
                return []
            query = tantivy.Query.boolean_query(
                [(tantivy.Occur.Should, tantivy.Query.term_query(self._schema, "terms", t)) for t in tokens])
            fetch = searcher.num_docs if allowed is not None else min(searcher.num_docs, math.ceil(limit * OVERFETCH) + 1)
            ranked = []
            hits = [(score, address) for score, address in searcher.search(query, limit=fetch).hits if score > 0]
            for key in searcher.fast_field_values("key", [address for _, address in hits]):
                key = int(key)
                if key not in self._keys or (allowed is not None and key not in allowed):
                    continue
                ranked.append(key)
                if len(ranked) >= limit:
                    break
            return ranked

    def search_dense(self, vector, allowed: set[int] | None, limit: int,
                     max_distance: float | None) -> list[tuple[int, float]]:
        import numpy as np
        with self.lock.read():
            if not self._vector_keys:
                return []
            query = np.asarray(vector, dtype=np.float32)
            if allowed is None:
                count = min(len(self.vectors), math.ceil(limit * OVERFETCH) + 1)
                matches = self.vectors.search(query, count, exact=len(self.vectors) <= EXACT_SEARCH_LIMIT)
                pairs = [(int(k), float(d)) for k, d in zip(matches.keys, matches.distances)]
            else:
                keys = [k for k in allowed if k in self._vector_keys]
                if not keys:
                    return []
                stored = np.asarray(self.vectors.get(np.array(keys, dtype=np.uint64)), dtype=np.float32)
                norms = np.linalg.norm(stored, axis=1) * (float(np.linalg.norm(query)) or 1.0)
                distances = 1.0 - (stored @ query) / np.where(norms == 0, 1.0, norms)
                order = np.argsort(distances, kind="stable")
                pairs = [(keys[i], float(distances[i])) for i in order]
            out = []
            for key, distance in pairs:
                if key not in self._keys or key not in self._vector_keys:
                    continue
                if max_distance is not None and distance > max_distance:
                    continue
                out.append((key, distance))
                if len(out) >= limit:
                    break
            return out

    def rows(self, keys: list[int]) -> dict[int, dict]:
        if not keys:
            return {}
        with self.lock.read():
            marks = ",".join("?" * len(keys))
            return {
                key: {"locator": locator, "text": text, "doc": doc, "generation": generation,
                      "payload": json.loads(payload)}
                for key, locator, text, doc, generation, payload in self.db.execute(
                    f"SELECT key, locator, text, doc, generation, payload FROM rows WHERE key IN ({marks})", keys)
            }

    def stats(self) -> dict:
        with self.lock.read():
            return {"rows": len(self._keys), "embedded": len(self._vector_keys)}

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.db.close()
            self._writer_lock.release()


def embed_pending(collection: Collection, embedder, batch: int = 64, should_stop=None, progress=None) -> int:
    """ベクトルのない行を埋め込む。should_stop() が真になったら途中で止める（続きは次の呼び出し）。"""
    done = 0
    while True:
        if should_stop and should_stop():
            break
        rows = collection.pending(batch)
        if not rows:
            break
        vectors = embedder.passages([text for _, text in rows])
        done += collection.add_vectors([key for key, _ in rows], vectors)
        if progress:
            progress(done)
    collection.flush()
    return done


def fuse(dense: list[int], lexical: list[int]) -> list[tuple[int, float, list[str]]]:
    """RRF（ADR-0011）。語彙の順位は重みを掛ける。同点は語彙、意味の順に先に現れたものを前にする。"""
    k, weight, n = FUSION["k"], FUSION["lexical_weight"], FUSION["candidates"]
    scores: dict[int, float] = {}
    retrievers: dict[int, list[str]] = {}
    order: list[int] = []
    for rank, key in enumerate(lexical[:n], 1):
        scores[key] = scores.get(key, 0.0) + weight / (k + rank)
        retrievers.setdefault(key, []).append("bm25")
        order.append(key)
    for rank, key in enumerate(dense[:n], 1):
        if key not in scores:
            order.append(key)
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        retrievers.setdefault(key, []).append("embedding")
    position = {key: i for i, key in enumerate(order)}
    return sorted(((key, scores[key], retrievers[key]) for key in order), key=lambda r: (-r[1], position[r[0]]))


# ---------------------------------------------------------------------------
# wiki のセクションの索引
# ---------------------------------------------------------------------------

WIKI_CHUNKING = "wiki-section-v1"     # wiki_search.collect_sections の searchable_text
WIKI_TOKENIZER = "wiki-bigram-v1"     # wiki_search.tokenize


def wiki_fingerprint() -> dict:
    return {"kind": "wiki-sections", "embedder": embedder_id(), "dimension": MODEL["dimension"],
            "prefix": [MODEL["query_prefix"], MODEL["passage_prefix"]], "chunking": WIKI_CHUNKING,
            "tokenizer": WIKI_TOKENIZER, "usearch": USEARCH, "lexical": "tantivy-0.27-bm25-ws"}


def section_locator(section: dict) -> str:
    return wiki_search.section_locator(section)


def section_hash(section: dict) -> str:
    return hashlib.sha256(section["searchable_text"].encode("utf-8")).hexdigest()


def stat_signature(root: Path) -> str:
    """wiki の Markdown のパス、大きさ、更新時刻から作る印。変わっていなければ読み直さない。

    ページと同じたどり方（wiki_pages.walk_markdown）で、最上位の除くディレクトリ（raw/、indexes/ など）には降りない。
    その下のファイルはページではないので、印に入れなくてよい。最上位の index.md などのページでない Markdown は入る。
    たどる途中の権限と消失のほかの OSError は送出し、refresh() は索引の行を消さずに失敗する。
    """
    digest = hashlib.sha256()
    for f in wiki_pages.walk_markdown(root, skip_top=True).files:
        digest.update(f"{f.rel}\0{f.size}\0{f.mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


class WikiIndex:
    """1 つの wiki のセクションの索引と、検索に使うページの写し。"""

    def __init__(self, root: Path):
        self.root = root
        self.dir = common.wiki_index_dir(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "wiki.json").write_text(json.dumps({"root": root.as_posix()}, ensure_ascii=False), encoding="utf-8")
        self.collection = Collection(self.dir, wiki_fingerprint())
        self._refresh_lock = threading.Lock()
        self._signature = None
        self.pages: list[dict] = []
        self.sections: list[dict] = []
        self.by_locator: dict[str, dict] = {}
        self._page_bm25 = None

    def refresh(self) -> dict:
        """wiki の変更を台帳と語彙の索引に入れる。埋め込みは embed_pending で別に行う。"""
        with self._refresh_lock:
            if self.collection.needs_reconcile:
                # 前の書き込みが途中で失敗した。台帳の行は残っているので、索引を台帳に合わせる。
                self.collection.reconcile()
            signature = stat_signature(self.root)
            if signature == self._signature:
                return {"changed": False}
            pages = wiki_search.collect_pages(self.root, self.dir / "parse-cache.json")
            sections = wiki_search.collect_sections(pages)
            current = {section_locator(s): s for s in sections}
            known = self.collection.hashes()
            upserts = []
            for locator, section in current.items():
                content_hash = section_hash(section)
                if known.get(locator) != content_hash:
                    upserts.append(Item(locator=locator, content_hash=content_hash, terms=section["tokens"],
                                        text=section["searchable_text"], doc=section["page"]["rel_path"]))
            deletes = [locator for locator in known if locator not in current]
            result = self.collection.apply(upserts, deletes) if upserts or deletes else {"added": 0, "removed": 0}
            self.pages, self.sections, self.by_locator = pages, sections, current
            self._page_bm25 = None
            self._signature = signature
            return {"changed": True, **result}

    def _covered(self, tokens: list[str], filtered: list[dict]) -> set[str] | None:
        """日本語のトークンを含むクエリで、被覆率の低いページを語彙の結果から外す（wiki_search と同じ）。"""
        if all(token.isascii() for token in tokens):
            return None
        if len(filtered) == len(self.pages):
            if self._page_bm25 is None:
                self._page_bm25 = wiki_search.build_bm25(self.pages)
            idx = self._page_bm25
        else:
            idx = wiki_search.build_bm25(filtered)
        return {page["rel_path"] for i, page in enumerate(filtered)
                if wiki_search.query_coverage(tokens, i, idx) >= wiki_search.MIN_CJK_QUERY_COVERAGE}

    def search(self, query: str, top: int = 10, type: str | None = None, tags: list[str] | None = None,
               since: str | None = None, per_page: int = 2, embedder=None) -> dict:
        args = Namespace(type=type, tag=list(tags or []), since=since)
        filtered = [page for page in self.pages if wiki_search.passes_filters(page, args)]
        filtered_paths = {page["rel_path"] for page in filtered}
        tokens = wiki_search.tokenize(query)
        n = FUSION["candidates"]
        everything = len(filtered) == len(self.pages)
        dense_allowed = None if everything else {
            key for s in self.sections if s["page"]["rel_path"] in filtered_paths
            for key in [self.collection.key_of(section_locator(s))] if key is not None}
        covered = self._covered(tokens, filtered) if tokens else None
        if covered is None and everything:
            lexical_allowed = None
        else:
            keep = filtered_paths if covered is None else filtered_paths & covered
            lexical_allowed = {key for s in self.sections if s["page"]["rel_path"] in keep
                               for key in [self.collection.key_of(section_locator(s))] if key is not None}
        lexical = self.collection.search_lexical(tokens, lexical_allowed, n) if filtered else []
        mode, warning, dense = "lexical", None, []
        if embedder is not None and filtered:
            dense = [key for key, _ in self.collection.search_dense(
                embedder.query(query), dense_allowed, n, MODEL["max_distance"])]
            mode = "hybrid"
        ranked = fuse(dense, lexical) if mode == "hybrid" else [
            (key, 1.0 / (FUSION["k"] + rank), ["bm25"]) for rank, key in enumerate(lexical, 1)]
        sections_by_page = {page["rel_path"]: page["sections"] for page in self.pages}
        page_counts = Counter()
        results = []
        key_to_locator = self.collection._keys
        for key, score, retrievers in ranked:
            section = self.by_locator.get(key_to_locator.get(key, ""))
            if section is None:
                continue
            rel_path = section["page"]["rel_path"]
            if page_counts[rel_path] >= per_page:
                continue
            page_counts[rel_path] += 1
            results.append(wiki_search.json_result(score, section["page"], section, sections_by_page[rel_path],
                                                   retrievers))
            if len(results) >= top:
                break
        stats = self.collection.stats()
        return {"query": query, "wiki": self.root.as_posix(), "granularity": "section", "mode": mode,
                "results": results, "warning": warning,
                "index": {"sections": stats["rows"], "embedded": stats["embedded"],
                          "state": "ready" if stats["rows"] == stats["embedded"] else "embedding"}}

    def get_page(self, page: str) -> dict | None:
        """スラッグか wiki の中の相対パスでページを返す。収集したページ（raw や graph などを除く）だけを返す。"""
        wanted = wiki_search.clean_link_target(page.strip().removeprefix("[[").removesuffix("]]"))
        match = next((p for p in self.pages if p["rel_path"][:-3] == wanted or p["rel_path"] == page), None)
        if match is None:
            candidates = [p for p in self.pages if p["slug"] == wanted]
            if len(candidates) > 1:
                raise ValueError(f"スラッグ {wanted} のページが複数ある。パスで指定する: "
                                 + "、".join(p["rel_path"] for p in candidates))
            match = candidates[0] if candidates else None
        if match is None:
            return None
        text = (self.root / match["rel_path"]).read_text(encoding="utf-8-sig")
        _meta, body = wiki_search.parse_frontmatter(text)
        return {"slug": match["slug"], "rel_path": match["rel_path"], "meta": match["meta"], "body": body,
                "links": match["links"],
                "sections": [{"section_index": s["section_index"], "heading_path": s["heading_path"],
                              "level": s["level"]} for s in match["sections"]]}

    def close(self):
        self.collection.close()


def list_index(root: Path, name: str | None = None) -> dict:
    """index.md と indexes/ の一覧を返す。name を渡すと indexes/ の中のそのファイルを返す。"""
    if name:
        target = (root / "indexes" / name).resolve()
        # 拡張子の大文字と小文字は問わない（一覧は Upper.MD も返す）。拡張子が .md でなければ、.md を付けたものとみなす。
        if target.suffix.lower() != ".md":
            target = target.with_suffix(".md")
        if not target.is_relative_to((root / "indexes").resolve()) or not target.is_file():
            raise FileNotFoundError(f"indexes/ に {name} がない")
        return {"wiki": root.as_posix(), "name": target.relative_to(root).as_posix(),
                "content": target.read_text(encoding="utf-8-sig")}
    index_md = root / "index.md"
    files = []
    folder = root / "indexes"
    # 索引のファイルは lint と同じ集め方（wiki_pages.walk_index_files）にする。ふつうのファイルだけで、.md で終わる
    # 名前のディレクトリで失敗しない。並びは今までどおり Path の順である。
    found = [f for f in wiki_pages.walk_index_files(root).files if f.rel != "index.md"]
    for f in sorted(found, key=lambda f: root / f.rel):
        path = root / f.rel
        text = path.read_text(encoding="utf-8-sig")
        meta, body = wiki_search.parse_frontmatter(text)
        heading = next((line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")), None)
        files.append({"name": path.relative_to(folder).as_posix(), "title": meta.get("title") or heading,
                      "bytes": f.size})
    return {"wiki": root.as_posix(),
            "index": index_md.read_text(encoding="utf-8-sig") if index_md.is_file() else None,
            "indexes": files}


# ---------------------------------------------------------------------------
# index_chunks のチャンクの索引
# ---------------------------------------------------------------------------

def chunk_fingerprint(tokenizer: str) -> dict:
    return {"kind": "chunks", "embedder": embedder_id(), "dimension": MODEL["dimension"],
            "prefix": [MODEL["query_prefix"], MODEL["passage_prefix"]], "tokenizer": tokenizer,
            "usearch": USEARCH, "lexical": "tantivy-0.27-bm25-ws"}


def decode_vector(chunk: dict):
    import base64
    import numpy as np
    if chunk.get("vector_b64"):
        vector = np.frombuffer(base64.b64decode(chunk["vector_b64"]), dtype="<f4")
    elif chunk.get("vector") is not None:
        vector = np.asarray(chunk["vector"], dtype=np.float32)
    else:
        return None
    if vector.shape != (MODEL["dimension"],):
        raise ValueError(f"チャンク {chunk.get('id')} のベクトルの次元が {MODEL['dimension']} ではない")
    norm = float(np.linalg.norm(vector))
    if not norm or not math.isfinite(norm):
        raise ValueError(f"チャンク {chunk.get('id')} のベクトルが 0 か有限でない")
    return vector / norm


class ChunkStore:
    """index_chunks で受け取ったチャンクの索引。collection ごとに 1 つ。"""

    def __init__(self, name: str):
        self.name = name
        self.dir = common.collection_dir(name)
        self.dir.mkdir(parents=True, exist_ok=True)
        info_path = self.dir / "collection.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = None
        self.tokenizer = info.get("tokenizer") if info else None
        # collection を開く・作り直すときのロック。同時に初めて呼ばれても、索引は 1 回だけ開く。
        self._init_lock = threading.RLock()
        # 書き込みを直列にするロック。検索はこのロックを待たない。
        self._write_lock = threading.Lock()
        self.collection = Collection(self.dir, chunk_fingerprint(self.tokenizer)) if self.tokenizer else None

    def ensure(self, tokenizer: str, reset: bool = False) -> bool:
        """tokenizer を確かめる。違う tokenizer の索引があれば、reset が真のときだけ作り直す。"""
        with self._init_lock:
            return self._ensure_locked(tokenizer, reset)

    def _ensure_locked(self, tokenizer: str, reset: bool) -> bool:
        if self.collection is not None and self.tokenizer == tokenizer and not reset:
            return False
        if self.collection is not None and self.tokenizer != tokenizer and not reset:
            raise ValueError(f"collection {self.name} の tokenizer は {self.tokenizer} で、{tokenizer} ではない。"
                             "作り直すときは reset を真にする")
        if self.collection is not None:
            self.collection.close()
            self.collection = None
        if reset:
            wipe(self.dir)
        (self.dir / "collection.json").write_text(json.dumps({"name": self.name, "tokenizer": tokenizer}),
                                                  encoding="utf-8")
        self.tokenizer = tokenizer
        self.collection = Collection(self.dir, chunk_fingerprint(tokenizer))
        return True

    def index(self, chunks: list[dict], generation: int, tokenizer: str, delete_docs: list[str] = (),
              reset: bool = False, embedder=None) -> dict:
        with self._write_lock:
            return self._index_locked(chunks, generation, tokenizer, delete_docs, reset, embedder)

    def _index_locked(self, chunks, generation, tokenizer, delete_docs, reset, embedder) -> dict:
        was_reset = self.ensure(tokenizer, reset)
        collection = self.collection
        items, seen = [], set()
        for chunk in chunks:
            chunk_id = str(chunk.get("id") or "")
            text = chunk.get("text")
            tokens = chunk.get("tokens")
            if not chunk_id or chunk_id in seen:
                raise ValueError("チャンクの id は空にできず、1 回の呼び出しの中で重ねられない")
            if not isinstance(text, str) or not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
                raise ValueError(f"チャンク {chunk_id} には text（文字列）と tokens（文字列の配列）が要る")
            seen.add(chunk_id)
            # 語彙の索引は空白で区切るので、トークンの中の空白は取り除く。
            terms = [t.replace(" ", "").replace("\t", "").replace("\n", "") for t in tokens]
            terms = [t for t in terms if t]
            vector = decode_vector(chunk)
            payload = {"metadata": chunk.get("metadata") or {}}
            content = json.dumps([text, terms, payload], ensure_ascii=False)
            items.append(Item(locator=chunk_id, content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                              terms=terms, text=text, doc=str(chunk.get("doc") or ""), generation=int(generation),
                              payload=payload, vector=vector))
        if items and any(item.vector is None for item in items):
            if embedder is None:
                raise RuntimeError("ベクトルのないチャンクがあるが、埋め込みモデルを使えない")
            missing = [item for item in items if item.vector is None]
            vectors = embedder.passages([item.text for item in missing])
            for item, vector in zip(missing, vectors):
                item.vector = vector
        result = collection.apply(items, []) if items else {"added": 0, "removed": 0}
        retired = []
        docs = sorted({item.doc for item in items if item.doc})
        for doc in docs:
            retired += collection.delete_where("doc = ? AND generation < ?", (doc, int(generation)))
        for doc in delete_docs:
            retired += collection.delete_where("doc = ?", (str(doc),))
        stats = collection.stats()
        return {"collection": self.name, "tokenizer": tokenizer, "generation": int(generation),
                "accepted": len(items), "replaced": result["removed"], "retired": len(retired),
                "reset": was_reset and reset, "rows": stats["rows"], "embedded": stats["embedded"]}

    def search(self, query: str, query_tokens: list[str], top: int = 10, docs: list[str] | None = None,
               max_distance: float | None = None, embedder=None) -> dict:
        with self._init_lock:
            collection = self.collection
        if collection is None:
            raise FileNotFoundError(f"collection {self.name} はまだない")
        allowed = None
        if docs:
            marks = ",".join("?" * len(docs))
            with collection.lock.read():
                allowed = {k for k, in collection.db.execute(f"SELECT key FROM rows WHERE doc IN ({marks})",
                                                            [str(d) for d in docs])}
        n = FUSION["candidates"]
        lexical = collection.search_lexical([t for t in query_tokens if t and " " not in t], allowed, n)
        mode = "lexical"
        dense = []
        if embedder is not None:
            dense = [k for k, _ in collection.search_dense(embedder.query(query), allowed, n, max_distance)]
            mode = "hybrid"
        ranked = fuse(dense, lexical) if mode == "hybrid" else [
            (key, 1.0 / (FUSION["k"] + rank), ["bm25"]) for rank, key in enumerate(lexical, 1)]
        ranked = ranked[:top]
        rows = collection.rows([key for key, _, _ in ranked])
        results = [{"id": rows[key]["locator"], "doc": rows[key]["doc"], "generation": rows[key]["generation"],
                    "score": round(score, 6), "retrievers": retrievers, "text": rows[key]["text"],
                    "metadata": rows[key]["payload"].get("metadata", {})}
                   for key, score, retrievers in ranked if key in rows]
        stats = collection.stats()
        return {"query": query, "collection": self.name, "mode": mode, "results": results,
                "index": {"rows": stats["rows"], "embedded": stats["embedded"]}}

    def close(self):
        if self.collection is not None:
            self.collection.close()
