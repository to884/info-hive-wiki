#!/usr/bin/env python3
"""
wiki_decide.py: LLM Wiki 用の任意の型付き判定チェック（標準ライブラリのみ）。

判定モデルは状態を読み、型付きの質問に選択肢と確率で答える。テキストは書かない。
このツールは既定では無効である。wiki のテキストをバックエンドに送るのは、
--backend か環境変数 INFO_HIVE_DECISIONS でそのバックエンドを選んだときだけである。

バックエンド:
    local       LiquidAI/d1-2B-GGUF を載せた llama-server（既定は http://127.0.0.1:8080）。キーは不要。
    typesafe    TypeSafe Jev（https://api.typesafe.ai）。TYPESAFE_API_KEY が必要。
    openrouter  OpenRouter 経由の Jev（https://openrouter.ai/api）。OPENROUTER_API_KEY が必要。
    liquid      Liquid の判定 API（https://api.liquid.ai/decisions）。LIQUID_API_KEY が必要。

環境変数:
    INFO_HIVE_DECISIONS          バックエンド名（既定: off）。
    INFO_HIVE_DECISIONS_URL      バックエンドのベース URL。
    INFO_HIVE_DECISIONS_MODEL    モデル名。

コマンド:
    ask [FILE]                     生の {state, questions} の JSON リクエストを送る（FILE か標準入力）。
    screen FILE                    テキストが AI システムに指示を出しているかを調べる（D5）。
    shortlist SOURCE [--wiki DIR]  新しいソースが変更する wiki のページを順位付けする（D4）。
    contradictions [--wiki DIR]    矛盾するセクションの組を見つける（D3）。
    check                          バックエンドへの接続を確かめる。

バックエンドでエラーが起きると、チェックは処理を通す（fail-open）。出力にはチェックしていない
結果をそのまま残し、"failed_open": true を設定し、stderr に注記を出す。

例:
    INFO_HIVE_DECISIONS=local python wiki_decide.py check
    python wiki_decide.py --backend openrouter screen raw/article.md
    python wiki_decide.py --backend local shortlist raw/paper.md --wiki wiki
"""

from __future__ import annotations

import argparse
import http.client
import math
import json
import os
import re
import subprocess
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
# 同じディレクトリの wiki_text を import する。スクリプトとして実行したときも、テストから
# import したときも見つかるようにする。
sys.path.insert(0, str(SCRIPT_DIR))
from wiki_text import localize_argparse, quote_in_text  # noqa: E402
import wiki_initialized  # noqa: E402
import wiki_workspace  # noqa: E402
QUESTIONS_PATH = SCRIPT_DIR.parent / "assets" / "decision-questions.json"

BACKENDS = {
    "local": {"url": "http://127.0.0.1:8080", "model": "d1-2B", "key": None},
    "typesafe": {"url": "https://api.typesafe.ai", "model": "jev-1.14.0", "key": "TYPESAFE_API_KEY"},
    "openrouter": {"url": "https://openrouter.ai/api", "model": "typesafe/jev-1.14", "key": "OPENROUTER_API_KEY"},
    "liquid": {"url": "https://api.liquid.ai/decisions", "model": "d1.1", "key": "LIQUID_API_KEY"},
}
BACKEND_CHOICES = ("off", *BACKENDS)
DEFAULT_TIMEOUT = 8.0
MAX_WORKERS = 8


def load_questions() -> dict:
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["checks"]


QUESTIONS = load_questions()


class DecisionError(Exception):
    """バックエンドの呼び出しが失敗した。`code` は unavailable、insufficient_credits、
    timeout、http、invalid_answer、bad_request のいずれかである。"""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


Transport = Callable[[dict], dict]


class DecisionClient:
    """System One のリクエストを1つのバックエンドに POST する。テストでは `transport` が HTTP の代わりになる。"""

    def __init__(
        self,
        backend: str,
        url: str,
        model: str,
        key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self.backend = backend
        self.url = url
        self.model = model
        self.key = key
        self.timeout = timeout
        self.transport = transport

    @property
    def endpoint(self) -> str:
        base = self.url.rstrip("/")
        return base if base.endswith("/v1/systemone") else base + "/v1/systemone"

    def ask(self, request: dict) -> dict:
        """{"answers", "model", "latency_ms"} を返す。DecisionError を送出する。"""
        if not isinstance(request, dict) or "state" not in request or not isinstance(request.get("questions"), dict):
            raise DecisionError("bad_request", "リクエストには state と questions が必要である")
        started = time.monotonic()
        if self.transport is not None:
            raw = self.transport({"model": self.model, **request})
        else:
            raw = self._post(request)
        payload = raw.get("result") if isinstance(raw, dict) and not isinstance(raw.get("answers"), dict) and isinstance(raw.get("result"), dict) else raw
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise DecisionError("invalid_answer", "バックエンドの応答に answers のオブジェクトがない")
        return {
            "answers": payload["answers"],
            "model": payload.get("model") if isinstance(payload.get("model"), str) else self.model,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    def _post(self, request: dict) -> dict:
        body = {"model": self.model, "state": request["state"], "questions": request["questions"]}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except (OSError, http.client.HTTPException):
                detail = ""
            code = "insufficient_credits" if exc.code == 402 or "insufficient" in detail.lower() and "credit" in detail.lower() else "http"
            raise DecisionError(code, f"HTTP {exc.code}: {detail}", exc.code) from None
        except http.client.HTTPException as exc:
            # IncompleteRead、BadStatusLine、RemoteDisconnected などのプロトコルのエラー。
            raise DecisionError("unavailable", f"{self.endpoint} の応答が壊れている: {exc!r}") from None
        except (TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise DecisionError("timeout", f"{self.timeout} 秒待っても応答がない") from None
            raise DecisionError("unavailable", f"{self.endpoint} に接続できない: {reason}") from None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise DecisionError("invalid_answer", "バックエンドの応答が JSON でない") from None


def note(message: str) -> None:
    print(f"判定レイヤー: {message}", file=sys.stderr)


def client_from_config(
    backend: str | None = None,
    url: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> DecisionClient | None:
    """フラグ、次に環境変数からクライアントを作る。判定が無効なら None を返す。
    選んだバックエンドのキーか URL がなければ DecisionError("unavailable") を送出する。"""
    name = (backend or os.environ.get("INFO_HIVE_DECISIONS") or "off").strip().lower()
    if name in ("", "off", "none", "0", "false"):
        return None
    if name not in BACKENDS:
        raise DecisionError("bad_request", f"未知のバックエンド {name!r}。{', '.join(BACKEND_CHOICES)} のいずれかを使う")
    spec = BACKENDS[name]
    url = url or os.environ.get("INFO_HIVE_DECISIONS_URL") or spec["url"]
    model = model or os.environ.get("INFO_HIVE_DECISIONS_MODEL") or spec["model"]
    key = os.environ.get(spec["key"]) if spec["key"] else None
    if spec["key"] and not key:
        raise DecisionError("unavailable", f"{name} のバックエンドを使うには {spec['key']} を設定する")
    return DecisionClient(name, url, model, key, timeout or DEFAULT_TIMEOUT)


# ---------------------------------------------------------------------------
# 回答の読み取り
# ---------------------------------------------------------------------------

def read_choice(answers: dict, name: str, keys: list[str]) -> dict:
    """選択肢の回答を、送ったキーと照らし合わせて検証する。確率は `keys` の範囲で
    正規化し直す。DecisionError("invalid_answer") を送出する。"""
    answer = answers.get(name) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise DecisionError("invalid_answer", f"{name} の回答がない")
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in keys:
        raise DecisionError("invalid_answer", f"{name} の回答の選択肢 {choice!r} は未知である")

    def number(value) -> float | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return float(value)

    probs = {}
    raw_probs = answer.get("probabilities")
    if isinstance(raw_probs, dict):
        for k in keys:
            if raw_probs.get(k) is None:
                continue
            p = number(raw_probs[k])
            if p is None or not math.isfinite(p) or p < 0:
                raise DecisionError("invalid_answer", f"{name} の回答の {k} の確率が無効である: {raw_probs[k]!r}")
            probs[k] = p
        if probs:
            total = sum(probs.values())
            if not math.isfinite(total) or total <= 0:
                raise DecisionError("invalid_answer", f"{name} の回答の確率の合計が {total} である")
            probs = {k: v / total for k, v in probs.items()}
    confidence = answer.get("confidence")
    if confidence is not None:
        confidence = number(confidence)
        if confidence is None or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise DecisionError("invalid_answer", f"{name} の回答の確信度が 0 から 1 の範囲にない: {answer.get('confidence')!r}")
    if not probs:
        if confidence is None:
            raise DecisionError("invalid_answer", f"{name} の回答に確率がない")
        probs[choice] = confidence
        if len(keys) == 2:
            probs[next(k for k in keys if k != choice)] = 1 - confidence
    if confidence is None:
        confidence = probs.get(choice, 0.0)
    return {"choice": choice, "confidence": confidence, "probabilities": probs}


def choice_question(check: str) -> dict:
    t = QUESTIONS[check]
    return {t["question"]: {"type": t["type"], "instructions": t["instructions"], "criteria": t["criteria"]}}


def positive_probability(answers: dict, check: str) -> float:
    t = QUESTIONS[check]
    ans = read_choice(answers, t["question"], list(t["criteria"]))
    return ans["probabilities"].get(t["positive"], 0.0)


def ask_many(client: DecisionClient, requests: list[dict]) -> list[dict]:
    """リクエストを並列のスレッドで実行する。最初に起きた DecisionError を送出する。"""
    if not requests:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(requests))) as pool:
        return list(pool.map(client.ask, requests))


def block(client: DecisionClient | None, threshold: float, started: float, **extra) -> dict:
    return {
        "backend": client.backend if client else "off",
        "model": client.model if client else None,
        "threshold": threshold,
        "latency_ms": int((time.monotonic() - started) * 1000),
        **extra,
    }


# ---------------------------------------------------------------------------
# D1 関連度のゲート
# ---------------------------------------------------------------------------

def relevance_request(query: str, title: str, heading: str, text: str) -> dict:
    t = QUESTIONS["relevance"]
    return {
        "state": {"query": query, "title": title, "heading": heading, "text": text[: t["max_text_chars"]]},
        "questions": choice_question("relevance"),
    }


def score_relevance(client: DecisionClient, query: str, rows: list[dict], texts: list[str]) -> list[float]:
    """各行の P(relevant) を返す。`rows` の各要素は `title` と `heading_path` を持つ。DecisionError を送出する。"""
    requests = [
        relevance_request(query, row.get("title") or "", " > ".join(row.get("heading_path") or []), text)
        for row, text in zip(rows, texts)
    ]
    return [positive_probability(r["answers"], "relevance") for r in ask_many(client, requests)]


def gate_pool(
    client: DecisionClient | None,
    query: str,
    rows: list[dict],
    texts: list[str],
    threshold: float,
    top: int,
) -> tuple[list[int], dict[int, dict], dict]:
    """(残したプール内のインデックスを順に並べたもので最大 `top` 件, インデックスごとの行の判定, 判定のブロック) を返す。
    P(relevant) >= threshold の行を残す。失敗したときは処理を通し、先頭の `top` 件のインデックスと failed_open true を返す。"""
    started = time.monotonic()
    try:
        if client is None:
            raise DecisionError("unavailable", "バックエンドが設定されていない。INFO_HIVE_DECISIONS で選ぶ")
        scores = score_relevance(client, query, rows, texts)
    except DecisionError as exc:
        note(f"関連度のゲートが失敗したので、処理を通す（fail-open）。{exc.code}: {exc}")
        kept = list(range(min(top, len(rows))))
        return kept, {}, block(client, threshold, started, pool=len(rows), kept=len(kept), dropped=0,
                               failed_open=True, error=exc.code)
    passed = [i for i, score in enumerate(scores) if score >= threshold]
    row_decisions = {
        i: {"relevant": round(scores[i], 4), "backend": client.backend, "model": client.model} for i in passed
    }
    return passed[:top], row_decisions, block(client, threshold, started, pool=len(rows), kept=len(passed),
                                              dropped=len(rows) - len(passed), failed_open=False)


# ---------------------------------------------------------------------------
# D2 証拠のチェック
# ---------------------------------------------------------------------------

FENCE_RE = re.compile(r"^\s*(```|~~~)")
BLOCK_PREFIX_RE = re.compile(r"^(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)")
# 文の終わり。英語の文は「.!?」のあとの空白で分ける。日本語の文は「。！？」のあとで分け、
# 空白がなくても分ける（日本語は文のあいだに空白を入れない）。句点のあとに閉じ括弧が続くとき
# （「……である。」）は閉じ括弧のあとで分け、「？！」のように句点が続くときは最後の句点のあとで分ける。
JA_CLOSERS = "」』）〉》】〕"
SENTENCE_END_RE = re.compile(
    r"(?<=[.!?])\s+"
    rf"|(?<=[。！？])(?![。！？{JA_CLOSERS}])\s*"
    rf"|(?<=[。！？][{JA_CLOSERS}])(?![{JA_CLOSERS}])\s*"
)


def evidence_in_text(evidence: str, body: str) -> bool:
    """`evidence` が `body` に含まれていれば True を返す。

    大文字と小文字を区別せず、空白の並びを 1 つにまとめ、日本語の文字どうしの間の改行と空白は
    取り除いて比べる（計画の方針 H、wiki_text.normalize_quote）。英数字の間の空白は残す。
    """
    return quote_in_text(evidence, body)


def split_sentences(text: str, max_sentences: int = 60, max_chars: int = 300) -> list[str]:
    """Markdown を文に分割する。"""
    blocks: list[str] = []
    para: list[str] = []
    in_fence = False

    def flush() -> None:
        if para:
            blocks.append(" ".join(para))
        para.clear()

    for raw_line in text.split("\n"):
        if FENCE_RE.match(raw_line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = raw_line.strip()
        if not line or line == "---":
            flush()
            continue
        if BLOCK_PREFIX_RE.match(line):
            flush()
            blocks.append(BLOCK_PREFIX_RE.sub("", line, count=1))
            continue
        para.append(line)
    flush()
    out: list[str] = []
    for blk in blocks:
        for piece in SENTENCE_END_RE.split(blk):
            s = re.sub(r"\s+", " ", piece).strip()
            if not s:
                continue
            out.append(s[:max_chars])
            if len(out) >= max_sentences:
                return out
    return out


def evidence_request(claim: str, evidence: str, sentences: list[str]) -> dict:
    t = QUESTIONS["evidence"]
    criteria = {f"{t['sentence_key_prefix']}{i + 1}": s for i, s in enumerate(sentences)}
    criteria[t["none_key"]] = t["none_criterion"]
    return {
        "state": {"claim": claim, "evidence": evidence},
        "questions": {t["question"]: {"type": t["type"], "instructions": t["instructions"], "criteria": criteria}},
    }


def check_evidence(client: DecisionClient, claim: str, evidence: str, body: str, threshold: float) -> dict:
    """1つのエッジについての D2 の判定。バックエンドが失敗したら DecisionError を送出する。"""
    t = QUESTIONS["evidence"]
    if evidence_in_text(evidence, body):
        return {"supported": True, "choice": "quote", "confidence": 1.0, "sentence": None}
    sentences = split_sentences(body, t["max_sentences"], t["max_sentence_chars"])
    if not sentences:
        return {"supported": False, "choice": t["none_key"], "confidence": 1.0, "sentence": None}
    res = client.ask(evidence_request(claim, evidence, sentences))
    keys = [f"{t['sentence_key_prefix']}{i + 1}" for i in range(len(sentences))] + [t["none_key"]]
    ans = read_choice(res["answers"], t["question"], keys)
    idx = -1 if ans["choice"] == t["none_key"] else int(ans["choice"][len(t["sentence_key_prefix"]):]) - 1
    return {
        "supported": ans["choice"] != t["none_key"] and ans["confidence"] >= threshold,
        "choice": ans["choice"],
        "confidence": round(ans["confidence"], 4),
        "sentence": sentences[idx] if idx >= 0 else None,
    }


# ---------------------------------------------------------------------------
# D3 関係、D4 変更、D5 スクリーニング
# ---------------------------------------------------------------------------

def relation_request(entity: str, a: dict, b: dict) -> dict:
    limit = QUESTIONS["relation"]["max_text_chars"]
    return {
        "state": {
            "entity": entity,
            "passage_a": {"page": a["title"], "text": a["text"][:limit]},
            "passage_b": {"page": b["title"], "text": b["text"][:limit]},
        },
        "questions": choice_question("relation"),
    }


def changes_request(source_title: str, source_text: str, page_title: str, page_text: str) -> dict:
    t = QUESTIONS["changes"]
    return {
        "state": {
            "source": {"title": source_title, "text": source_text[: t["max_source_chars"]]},
            "page": {"title": page_title, "text": page_text[: t["max_text_chars"]]},
        },
        "questions": choice_question("changes"),
    }


def screen_request(path: str, text: str) -> dict:
    return {
        "state": {"path": path, "text": text[: QUESTIONS["orders_agent"]["max_text_chars"]]},
        "questions": choice_question("orders_agent"),
    }


def screen_text(client: DecisionClient | None, path: str, text: str, threshold: float) -> dict:
    started = time.monotonic()
    try:
        if client is None:
            raise DecisionError("unavailable", "バックエンドが設定されていない。INFO_HIVE_DECISIONS で選ぶ")
        res = client.ask(screen_request(path, text))
        score = positive_probability(res["answers"], "orders_agent")
    except DecisionError as exc:
        note(f"スクリーニングが失敗したので、処理を通す（fail-open）。{exc.code}: {exc}")
        return {"path": path, "score": None, "flagged": False,
                **block(client, threshold, started, failed_open=True, error=exc.code)}
    return {"path": path, "score": round(score, 4), "flagged": score >= threshold,
            **block(client, threshold, started, failed_open=False)}


# ---------------------------------------------------------------------------
# wiki 用のヘルパー（検索のパーサーを再利用する）
# ---------------------------------------------------------------------------

def load_search_module():
    sys.path.insert(0, str(SCRIPT_DIR))
    import wiki_search  # noqa: E402

    return wiki_search


def strip_frontmatter(text: str) -> tuple[str, str]:
    """Markdown ファイルの (title, body) を返す。"""
    ws = load_search_module()
    meta, body = ws.parse_frontmatter(text)
    title = ws.text_value(meta.get("title")) if isinstance(meta, dict) else ""
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = m.group(1).strip() if m else ""
    return title or "", body


def search_candidates(source_query: str, wiki: Path, top: int, no_embed: bool) -> list[dict]:
    script = SCRIPT_DIR / "wiki_search.py"
    argv = [source_query, "--wiki", str(wiki), "--json", "--top", str(top), "--per-page", "1"]
    if no_embed:
        argv.append("--no-embed")
    uv = shutil.which("uv")
    cmd = [uv, "run", "--quiet", "--script", str(script), *argv] if uv and not no_embed else [sys.executable, str(script), *argv]
    # 子の Python も UTF-8 モードで動かし、cp932 の既定に左右されないようにする。
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return json.loads(proc.stdout).get("results", [])


def cmd_shortlist(args, client: DecisionClient | None) -> int:
    source_path = Path(args.source)
    text = source_path.read_text(encoding="utf-8-sig")
    title, body = strip_frontmatter(text)
    title = title or source_path.stem
    t = QUESTIONS["changes"]
    query = f"{title} {body[: t['max_source_chars']]}"
    rows = search_candidates(query, args.wiki, args.top, args.no_embed)
    threshold = args.threshold if args.threshold is not None else t["default_threshold"]
    started = time.monotonic()
    candidates = []
    for rank, row in enumerate(rows, 1):
        page_file = args.wiki / row["rel_path"]
        try:
            _, page_body = strip_frontmatter(page_file.read_text(encoding="utf-8-sig"))
        except OSError:
            page_body = row.get("snippet", "")
        candidates.append({"rel_path": row["rel_path"], "title": row["title"], "search_rank": rank, "text": page_body})
    try:
        if client is None:
            raise DecisionError("unavailable", "バックエンドが設定されていない。INFO_HIVE_DECISIONS で選ぶ")
        results = ask_many(client, [changes_request(title, body, c["title"], c["text"]) for c in candidates])
        scores = [positive_probability(r["answers"], "changes") for r in results]
    except DecisionError as exc:
        note(f"変更されるページの順位付けが失敗したので、処理を通す（fail-open）。ページは検索の順のまま。{exc.code}: {exc}")
        out = [{k: v for k, v in c.items() if k != "text"} | {"changes": None} for c in candidates]
        print(json.dumps({"source": str(source_path), "candidates": out,
                          "decision": block(client, threshold, started, failed_open=True, error=exc.code)}, indent=2, ensure_ascii=False))
        return 0
    ranked = sorted(
        ({k: v for k, v in c.items() if k != "text"} | {"changes": round(s, 4), "shortlisted": s >= threshold}
         for c, s in zip(candidates, scores)),
        key=lambda c: (-c["changes"], c["search_rank"]),
    )
    print(json.dumps({"source": str(source_path), "candidates": ranked,
                      "decision": block(client, threshold, started, failed_open=False,
                                        shortlisted=sum(1 for c in ranked if c["shortlisted"]))}, indent=2, ensure_ascii=False))
    return 0


def contradiction_pairs(wiki: Path, max_pairs: int) -> list[tuple[str, dict, dict]]:
    """同じエンティティのページにリンクしている、異なるページのセクションの組。"""
    ws = load_search_module()
    pages = ws.collect_pages(wiki)
    by_path, by_slug = ws.build_link_index(pages)
    entity_titles = {p["slug"]: ws.text_value(p["meta"].get("title")) or p["slug"]
                     for p in pages if p["meta"].get("type") == "entity"}
    mentions: dict[str, list[dict]] = {}
    for page in pages:
        title = ws.text_value(page["meta"].get("title")) or page["slug"]
        for section in page["sections"]:
            linked = set()
            # セクションの links は、本文全体の解析から集めたもの（コードの中と参照リンクの定義の中は数えない）。
            # セクションの本文だけを読み直すと、別のセクションの参照リンクの定義が見えず、ないリンクが生まれる。
            for link in section["links"]:
                slug = ws.resolve_link(link, by_path, by_slug)
                if slug in entity_titles and slug != page["slug"]:
                    linked.add(slug)
            text = section["text"].strip()
            if not text:
                continue
            for slug in sorted(linked):
                mentions.setdefault(slug, []).append({
                    "rel_path": page["rel_path"],
                    "title": title,
                    "heading": " > ".join(section["heading_path"]),
                    "text": text,
                })
    pairs = []
    for slug in sorted(mentions):
        items = mentions[slug]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[i]["rel_path"] == items[j]["rel_path"]:
                    continue
                pairs.append((entity_titles[slug], items[i], items[j]))
                if len(pairs) >= max_pairs:
                    return pairs
    return pairs


def cmd_contradictions(args, client: DecisionClient | None) -> int:
    t = QUESTIONS["relation"]
    threshold = args.threshold if args.threshold is not None else t["default_threshold"]
    pairs = contradiction_pairs(args.wiki, args.max_pairs)
    started = time.monotonic()
    try:
        if client is None:
            raise DecisionError("unavailable", "バックエンドが設定されていない。INFO_HIVE_DECISIONS で選ぶ")
        results = ask_many(client, [relation_request(e, a, b) for e, a, b in pairs])
        answers = [read_choice(r["answers"], t["question"], list(t["criteria"])) for r in results]
    except DecisionError as exc:
        note(f"矛盾のチェックが失敗したので、処理を通す（fail-open）。どの組もチェックしていない。{exc.code}: {exc}")
        print(json.dumps({"pairs": len(pairs), "flagged": [],
                          "decision": block(client, threshold, started, failed_open=True, error=exc.code)}, indent=2, ensure_ascii=False))
        return 0
    flagged = []
    for (entity, a, b), ans in zip(pairs, answers):
        p = ans["probabilities"].get(t["positive"], 0.0)
        if p >= threshold:
            flagged.append({
                "entity": entity,
                "a": {"rel_path": a["rel_path"], "heading": a["heading"]},
                "b": {"rel_path": b["rel_path"], "heading": b["heading"]},
                "contradicts": round(p, 4),
                "choice": ans["choice"],
            })
    flagged.sort(key=lambda f: -f["contradicts"])
    print(json.dumps({"pairs": len(pairs), "flagged": flagged,
                      "decision": block(client, threshold, started, failed_open=False)}, indent=2, ensure_ascii=False))
    return 0


def cmd_screen(args, client: DecisionClient | None) -> int:
    text = Path(args.file).read_text(encoding="utf-8-sig", errors="replace")
    threshold = args.threshold if args.threshold is not None else QUESTIONS["orders_agent"]["default_threshold"]
    print(json.dumps(screen_text(client, str(args.file), text, threshold), indent=2, ensure_ascii=False))
    return 0


def cmd_ask(args, client: DecisionClient | None) -> int:
    raw = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text(encoding="utf-8-sig")
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        note(f"リクエストが JSON でない（{exc}）")
        return 2
    if client is None:
        note("バックエンドが設定されていない。INFO_HIVE_DECISIONS か --backend で選ぶ")
        return 1
    try:
        res = client.ask(request)
    except DecisionError as exc:
        note(f"{client.backend} の呼び出しが失敗した。{exc.code}: {exc}")
        return 1
    print(json.dumps({**res, "backend": client.backend}, indent=2, ensure_ascii=False))
    return 0


def cmd_check(args, client: DecisionClient | None) -> int:
    if client is None:
        print(json.dumps({"ok": False, "backend": "off", "error": "no backend configured"}, ensure_ascii=False))
        return 1
    started = time.monotonic()
    try:
        res = client.ask(relevance_request("what is a wiki", "Wiki", "Overview", "A wiki is a set of linked pages that people edit."))
        positive_probability(res["answers"], "relevance")
    except DecisionError as exc:
        print(json.dumps({"ok": False, "backend": client.backend, "model": client.model,
                          "url": client.endpoint, "error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "backend": client.backend, "model": client.model, "url": client.endpoint,
                      "latency_ms": int((time.monotonic() - started) * 1000)}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=BACKEND_CHOICES, help="判定のバックエンド（既定: INFO_HIVE_DECISIONS の値。未設定なら off）")
    parser.add_argument("--model", help="モデル名（既定: INFO_HIVE_DECISIONS_MODEL の値か、バックエンドの既定）")
    parser.add_argument("--url", help="バックエンドのベース URL（既定: INFO_HIVE_DECISIONS_URL の値か、バックエンドの既定）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="リクエストのタイムアウトの秒数（既定: 8）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="生の {state, questions} のリクエストを送る")
    p.add_argument("file", nargs="?", help="リクエストの JSON ファイル（既定: 標準入力）")

    p = sub.add_parser("screen", help="テキストが AI システムに指示を出しているかを調べる（D5）")
    p.add_argument("file", help="調べるファイル")
    p.add_argument("--threshold", type=float, help="この確率以上ならフラグを立てる（既定: 0.7）")

    p = sub.add_parser("shortlist", help="新しいソースが変更する wiki のページを順位付けする（D4）")
    p.add_argument("source", help="新しいソースのファイル")
    p.add_argument("--wiki", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    p.add_argument("--top", type=int, default=QUESTIONS["changes"]["pool"], help="検索で集める候補の数（既定: 30）")
    p.add_argument("--threshold", type=float, help="この確率以上のページを候補に残す（既定: 0.5）")
    p.add_argument("--no-embed", action="store_true", help="候補を語彙の検索だけで集める")

    p = sub.add_parser("contradictions", help="矛盾するセクションの組を見つける（D3）")
    p.add_argument("--wiki", type=Path, default=None, help=wiki_workspace.WIKI_DEFAULT_HELP)
    p.add_argument("--max-pairs", type=int, default=40, help="チェックする組の最大数（既定: 40）")
    p.add_argument("--threshold", type=float, help="この確率以上の組を報告する（既定: 0.5）")

    sub.add_parser("check", help="バックエンドへの接続を確かめる")
    return localize_argparse(parser)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = client_from_config(args.backend, args.url, args.model, args.timeout)
    except DecisionError as exc:
        note(str(exc))
        if args.command in ("ask", "check"):
            return 1
        client = None
    # --wiki を持つサブコマンド（shortlist、contradictions）は、振り分けの前に wiki の判定を通る。新しいサブコマンドも、
    # --wiki を足せば既定で通る。
    if hasattr(args, "wiki"):
        if args.wiki is None:
            args.wiki = wiki_workspace.default_wiki_or_exit()
        # SCHEMA.md のないディレクトリは、初期化された wiki とみなさない（ADR-0019）。
        try:
            wiki_initialized.require(args.wiki)
        except wiki_initialized.NotInitialized as exc:
            print(str(exc), file=sys.stderr)
            return 1
    handlers = {
        "ask": cmd_ask,
        "screen": cmd_screen,
        "shortlist": cmd_shortlist,
        "contradictions": cmd_contradictions,
        "check": cmd_check,
    }
    return handlers[args.command](args, client)


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
