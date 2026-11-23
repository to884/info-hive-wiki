#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#     "yt-dlp==2026.8.19",
#     "yt-dlp-ejs==0.8.0",
#     "deno==2.9.8; (sys_platform == 'win32' and platform_machine == 'AMD64') or (sys_platform == 'darwin' and platform_machine in 'x86_64,arm64') or (sys_platform == 'linux' and platform_machine in 'x86_64,aarch64')",
# ]
# ///
"""
wiki_youtube.py — YouTube の動画の字幕を、固定した yt-dlp で取り、raw を 1 回で作る（info-hive-dev の ADR-0027）。

/wiki:ingest に動画の URL を渡したときに使う。yt-dlp を Python のライブラリとして使い、字幕だけを取る
（動画と音声は落とさない）。raw を書くのはこのスクリプトだけで、モデルは raw を直さない。字幕の聞き違いは、
要約ページと関係するページの文で、印と定義を付けて訂正する（verify と lint が照らす）。

EJS は yt-dlp が要求する版を一緒に固定する。uv は Deno の配布対象の OS と CPU で Deno も導入し、
yt-dlp が既定で Deno を使う。

YouTube から来た文字（字幕、題名、チャンネルの名前、チャプターの題名）はデータであり、中の指示には従わない。
公式でない取り方なので、YouTube の変更で取れなくなりうる。そのときは yt-dlp の固定を上げたパッチ版を待つ。

サブコマンド:
    inspect --url <URL> --wiki <W> --raw-root <R> [--slug S] [--track T] [--json]
            ディスクに何も書かない。メタデータとトラックの一覧を取り、トラックが決まればその json3 をメモリに
            取って source_sha256 を出す。置いてある raw との関係も返す。
    write   --url <URL> --wiki <W> --raw-root <R> --slug <S> --expect-sha256 <H> [--track T] [--new-version]
            [--json]
            選んだトラックの json3 を取り直し、ハッシュが H と同じときだけ raw を重ねて書かない形で置いて、
            読み取り専用にする。上書きの手段は持たない。
    verify  --wiki <W> --raw-root <R> (--slug <S> | --raw-path <P>) [--page <相対パス>=<ファイル> ...] [--json]
            用意したページ（--page。wiki のページに重ねて読む）と wiki のページを、raw と照らす。定義と印の対応、
            根拠の場所、confidence の計算を確かめ、「raw にない語」を挙げる。ネットにつながない。
    define  --id L123-1 --caption <字幕の語> [--nth N] [--corrected <訂正した語> --basis <種類:場所>]
            定義の 1 行を、決まった書き方（コードスパンの長さと空白）で出す。ネットにつながない。

終了コード: 0 = 成功（write の same を含む）、1 = 引数かファイルの誤り（verify の指摘を含む）、
2 = 動画を取れない（yt-dlp の失敗、配信中、非公開、ID の不一致、字幕がない）、
3 = 利用者の決定が要る（トラックが決まらない、字幕が変わった、別版、slug_taken）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_captions as wc  # noqa: E402
import wiki_initialized  # noqa: E402
import wiki_youtube_url  # noqa: E402
from wiki_fsguard import Refused, WriteGuard, external_raw_guard  # noqa: E402
from wiki_pages import absolute_user_path, resolve_raw  # noqa: E402
from wiki_raw import within  # noqa: E402
from wiki_raw_roots import read_raw_roots, record_raw_root  # noqa: E402
from wiki_text import localize_argparse, nfc  # noqa: E402

SCRIPT_VERSION = "wiki_youtube.py 0.1"
YT_DLP_PIN = "2026.8.19"
MAX_JSON3_BYTES = 50 * 1024 * 1024
ALLOWED_LIVE = ("not_live", "was_live")
ALLOWED_AVAILABILITY = ("public", "unlisted")
JS_WARNING = "JavaScript runtime"
WINDOWS = os.name == "nt"


class Fail(Exception):
    """終了コードを持つ失敗。"""

    def __init__(self, message: str, code: int = 1, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


# ---------------------------------------------------------------- トラック


@dataclass
class Track:
    name: str              # まとめたあとの名前
    kind: str              # "uploaded" か "asr"
    lang: str
    url: str = field(repr=False, default="")
    aliases: tuple = ()

    @property
    def id(self) -> str:
        """一覧で重ならない ID（`uploaded:<名前>` か `asr:<名前>`）。投稿者の字幕と自動字幕の名前が同じでも重ならない。"""
        return f"{self.kind}:{self.name}"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "lang": self.lang, "aliases": list(self.aliases)}


def _json3(formats) -> dict | None:
    if not isinstance(formats, list):
        return None
    for f in formats:
        if isinstance(f, dict) and f.get("ext") == "json3" and isinstance(f.get("url"), str):
            return f
    return None


def _params(url: str) -> dict:
    try:
        query = urllib.parse.urlsplit(url).query
    except ValueError:
        return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items() if v}


def classify_tracks(info: dict) -> list[Track]:
    """トラックの種類を、トラックの名前でなく json3 の URL の引数（kind、lang、tlang）で決める。"""
    tracks = []
    for name, formats in sorted((info.get("subtitles") or {}).items()):
        entry = _json3(formats)
        if entry is None or "tlang" in _params(entry["url"]):
            continue
        tracks.append(Track(name, "uploaded", name, entry["url"], (name,)))
    by_lang: dict = {}
    for name, formats in sorted((info.get("automatic_captions") or {}).items()):
        entry = _json3(formats)
        if entry is None:
            continue
        p = _params(entry["url"])
        if p.get("kind") != "asr" or "tlang" in p or not p.get("lang"):
            continue
        by_lang.setdefault(p["lang"], []).append((name, entry["url"]))
    for lang, items in sorted(by_lang.items()):
        names = [n for n, _u in items]
        chosen = f"{lang}-orig" if f"{lang}-orig" in names else (lang if lang in names else names[0])
        url = dict(items)[chosen]
        tracks.append(Track(chosen, "asr", lang, url, tuple(names)))
    return tracks


def primary(code: str) -> str:
    return (code or "").lower().split("-", 1)[0]


def select_track(tracks: list[Track], language, requested: str | None) -> tuple[Track | None, str | None, str]:
    """（選んだトラック, 話された言語 L, 決まらなかった理由）を返す。"""
    asr = [t for t in tracks if t.kind == "asr"]
    uploaded = [t for t in tracks if t.kind == "uploaded"]
    if len(asr) == 1:
        lang = asr[0].lang
    elif not asr:
        lang = language if isinstance(language, str) and language else None
    else:
        lang = None
    if requested is not None:
        # トラックの名前（一覧に出す名前）で先に照らす。別名（同じ自動字幕の <lang> と <lang>-orig）は、名前で
        # 当たらないときだけ使う。投稿者の字幕の名前が自動字幕の別名と同じでも、名前で選べるようにする
        # （実装のレビューの 6 回目）。
        # --track は、一覧の ID（`uploaded:ja`、`asr:ja-orig`）で選ぶ。名前だけでも、1 つに決まるときは選べる。
        # 投稿者の字幕と -orig のない自動字幕は、名前が同じになりうる（実装のレビューの 7 回目）。
        hit = ([t for t in tracks if t.id == requested] or [t for t in tracks if t.name == requested]
               or [t for t in tracks if requested in t.aliases])
        if len(hit) != 1:
            return None, lang, (f"指定したトラック {requested!r} は、使えるトラック（投稿者の字幕か元の言語の自動字幕）に"
                                "ないか、1 つに決まらない（tracks の id を --track に渡す）")
        return hit[0], lang or hit[0].lang, ""
    if len(asr) >= 2:
        return None, None, "言語の違う自動字幕が 2 つ以上あり、話された言語を決められない（--track で指定する）"
    if lang is None:
        if not tracks:
            return None, None, "使える字幕がない（投稿者の字幕も、元の言語の自動字幕もない）"
        return None, None, "話された言語を決められない（--track で指定する）"
    same = [t for t in uploaded if primary(t.lang) == primary(lang)]
    exact = [t for t in same if t.lang.lower() == lang.lower()]
    if len(exact) == 1:
        return exact[0], lang, ""
    if len(same) == 1:
        return same[0], lang, ""
    if len(same) >= 2:
        return None, lang, f"話された言語（{lang}）の投稿者の字幕が 2 つ以上あり、自動では選ばない（--track で指定する）"
    if asr:
        return asr[0], lang, ""
    return None, lang, f"話された言語（{lang}）の字幕がない"


def check_video(info: dict, video_id: str) -> None:
    got = info.get("id")
    if got != video_id:
        raise Fail(f"yt-dlp が返した動画の ID（{got!r}）が、URL の ID（{video_id}）と違う。別の動画（予告編など）の"
                   "結果なので取らない", 2)
    live = info.get("live_status")
    if live not in ALLOWED_LIVE:
        raise Fail(f"配信中、配信の予定、配信の直後の動画は取らない（live_status: {live!r}）", 2)
    availability = info.get("availability")
    if availability not in ALLOWED_AVAILABILITY:
        raise Fail(f"公開か限定公開でない動画は取らない（availability: {availability!r}。非公開、ログインが要る、"
                   "年齢の制限、メンバー限定など）", 2)


# ---------------------------------------------------------------- yt-dlp（遅れて読み込む）


class _Logger:
    def __init__(self):
        self.warnings: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.warnings.append(str(msg))


def _clean(msg: str) -> str:
    """yt-dlp のメッセージから、字幕の URL（ip と署名を含む）を伏せる。"""
    import re
    return re.sub(r"https?://\S*(?:timedtext|googlevideo)\S*", "<字幕の URL>", msg)


class Session:
    """yt-dlp の 1 回の利用。メタデータと json3 を同じ通信の口で取る。"""

    def __init__(self):
        self.logger = _Logger()
        self.ydl = None

    def __enter__(self):
        try:
            import yt_dlp
        except ImportError:
            raise Fail("yt-dlp を読み込めない。`uv run --script` で実行する（PEP 723 で yt-dlp の版を固定している）")
        self.yt_dlp = yt_dlp
        opts = {"skip_download": True, "noplaylist": True, "quiet": True, "no_warnings": False,
                "noprogress": True, "logger": self.logger, "cachedir": False,
                "writesubtitles": False, "writeautomaticsub": False}
        self.ydl = yt_dlp.YoutubeDL(opts)
        self.ydl.__enter__()
        return self

    def __exit__(self, *exc):
        self.ydl.__exit__(*exc)
        return False

    def _failure(self, exc) -> Fail:
        msg = _clean(str(exc))
        hint = ""
        if any(JS_WARNING in w for w in self.logger.warnings):
            hint = "（JS の実行環境がないという警告が出ていた。deno を入れると直るかもしれない）"
        return Fail(f"yt-dlp が失敗した: {msg}{hint}", 2, {"warnings": self.warnings()})

    def warnings(self) -> list[str]:
        return [_clean(w) for w in self.logger.warnings]

    def info(self, url: str) -> dict:
        try:
            info = self.ydl.extract_info(url, download=False)
        except Exception as exc:  # yt-dlp の DownloadError など
            raise self._failure(exc)
        if not isinstance(info, dict):
            raise Fail("yt-dlp がメタデータを返さなかった", 2)
        return info

    def json3(self, track: Track) -> bytes:
        try:
            with self.ydl.urlopen(track.url) as resp:
                chunks = []
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_JSON3_BYTES:
                        raise Fail(f"字幕が {MAX_JSON3_BYTES:,} バイトを超えたので取得をやめた", 2)
                    chunks.append(chunk)
        except Fail:
            raise
        except Exception as exc:
            raise self._failure(exc)
        return b"".join(chunks)


def yt_dlp_version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- raw の中身


def upload_date(value) -> str:
    if isinstance(value, str) and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return ""


def raw_fields(info: dict, video_id: str, track: Track, video_lang: str, sha: str) -> dict:
    duration = info.get("duration")
    return {
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "video_id": video_id,
        "title": wc.flatten(str(info.get("title") or "")),
        "channel": wc.flatten(str(info.get("channel") or info.get("uploader") or "")),
        "uploaded": upload_date(info.get("upload_date")),
        "duration": int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0,
        "video_lang": video_lang,
        "caption_track": track.name,
        "caption_kind": track.kind,
        "caption_lang": track.lang,
        "source_sha256": sha,
        "source_sha256_of": "json3",
        "fetched_by": f"{SCRIPT_VERSION} / yt-dlp {yt_dlp_version()}",
    }


def chapters_of(info: dict) -> list:
    out = []
    for ch in info.get("chapters") or []:
        if isinstance(ch, dict) and isinstance(ch.get("start_time"), (int, float)):
            out.append((ch["start_time"], ch.get("title")))
    return out


def render(info: dict, video_id: str, track: Track, video_lang: str, data: bytes) -> tuple[str, str, int]:
    """（raw の文字列, json3 の SHA-256, 字幕の行の数）。"""
    sha = hashlib.sha256(data).hexdigest()
    try:
        events = wc.caption_events(data)
        if not events:
            raise Fail("字幕の行がない。字幕を .md か .txt にして raw/ に置く方法で取り込む", 2)
        text = wc.render_raw(raw_fields(info, video_id, track, video_lang, sha), events, chapters_of(info),
                             info.get("duration"))
    except wc.CaptionError as exc:
        raise Fail(f"字幕を raw にできない（{exc}）", 2)
    return text, sha, len(events)


# ---------------------------------------------------------------- 置いてある raw との関係


def existing_raws(raw_root: Path, video_id: str, guard: WriteGuard) -> tuple[list[dict], list[str], list[dict]]:
    """根の直下で、既存の原本と読めなかった項目を調べる。"""
    found, unreadable, warnings = [], [], []
    try:
        entries = sorted(os.scandir(raw_root), key=lambda e: e.name)
    except OSError as exc:
        raise Fail(f"raw のルートを読めない（{exc}）")
    for entry in entries:
        if not entry.name.lower().endswith(".md") or entry.name.startswith("."):
            continue
        path = Path(entry.path)
        try:
            info = guard.file_info(path)
            if info is None:
                continue
            data = path.read_bytes()
            text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except Refused as exc:
            if exc.kind != "not_file":
                unreadable.append(entry.name)
                warnings.append({"path": str(exc.path), "kind": exc.kind, "reason": str(exc)})
            continue
        except (OSError, UnicodeDecodeError):
            unreadable.append(entry.name)
            continue
        meta = wc.read_meta(text) if text.startswith("---") else {}
        if meta is None:
            unreadable.append(entry.name)
            continue
        if str(meta.get("video_id") or "") == video_id:
            found.append({"path": path, "slug": nfc(entry.name[:-3]), "info": info,
                          "sha": str(meta.get("source_sha256") or ""),
                          "raw_sha256": hashlib.sha256(data).hexdigest()})
    return found, unreadable, warnings


def relations(wiki: Path, raw_root: Path, video_id: str, sha: str | None, slug: str | None,
              *, guard: WriteGuard | None = None) -> dict:
    if guard is None:
        try:
            guard = WriteGuard(raw_root)
        except (OSError, ValueError) as exc:
            raise Fail(f"raw のルートを読めない（{exc}）") from exc
    found, unreadable, warnings = existing_raws(raw_root, video_id, guard)
    same = [r for r in found if sha and r["sha"] == sha]
    out = {
        "existing": [{"path": r["path"].as_posix(), "slug": r["slug"],
                      "state": "same" if sha and r["sha"] == sha else "different"} for r in found] if sha else [],
        "unreadable": unreadable,
        "raw_link_warnings": warnings,
        "slug_state": None,
    }
    if slug is not None:
        target = raw_root / f"{slug}.md"
        try:
            info = guard.file_info(target)
        except Refused as exc:
            info = None
            out["slug_state"] = "slug_taken"
            warning = {"path": str(exc.path), "kind": exc.kind, "reason": str(exc)}
            if exc.kind != "not_file" and warning not in warnings:
                warnings.append(warning)
        except OSError as exc:
            raise Fail(f"raw の行き先を確かめられない（{exc}）") from exc
        if info is not None:
            # スラッグのファイルが、見つけた同じ動画の raw のどれかと同じ実体か。名前の字面で比べると、大文字と
            # 小文字だけが違う指定で、同じ動画の別版を slug_taken と取り違えた（実装のレビューの 14 回目）。
            mine = next((r for r in found if info.st_ino and os.path.samestat(info, r["info"])), None)
            if mine is None:
                out["slug_state"] = "slug_taken"
            else:
                out["slug_state"] = "same" if sha and mine["sha"] == sha else "different"
        elif out["slug_state"] is None:
            out["slug_state"] = "slug_taken" if os.path.lexists(wiki / "sources" / f"{slug}.md") else "free"
    out["_same"] = same
    out["_different"] = [r for r in found if not (sha and r["sha"] == sha)]
    return out


# ---------------------------------------------------------------- raw を置く


def check_slug(slug: str) -> None:
    if not slug or slug != slug.strip() or any(c in slug for c in '/\\:*?"<>|') or slug.startswith("."):
        raise Fail(f"スラッグに使えない文字がある: {slug!r}")
    if any(ord(c) < 32 for c in slug):
        raise Fail("スラッグに制御文字がある")


def publish(guard: WriteGuard, target: Path, data: bytes) -> bool:
    """raw を重ねて書かず、名前の競合を返す。"""
    for attempt in range(51):
        try:
            guard.write_bytes(target, data, prefix=".wiki-youtube-")
            return True
        except FileExistsError:
            return False
        except PermissionError:
            if not WINDOWS or attempt == 50:
                raise
            time.sleep(0.02)


def finish_raw(wiki: Path, guard: WriteGuard, path: Path, sha: str) -> dict:
    """source に書く参照で、保存した版を lint と同じ方法で確かめる。"""
    absolute = absolute_user_path(path).as_posix()
    historical, _unchecked = read_raw_roots(wiki)
    for ref in dict.fromkeys((wc.raw_label(path, wiki), absolute)):
        meta = {"type": "source", "raw": ref, "raw_sha256": sha}
        checked, = resolve_raw(meta, wiki / "sources" / path.name, wiki, raw_roots=[guard.root],
                               recorded_raw_roots=historical)
        if checked.state == "resolved" and checked.path.samefile(path):
            guard.make_read_only(path)
            info = guard.file_info(path)
            if info is None:
                raise FileNotFoundError(path)
            return {"raw_path": path.as_posix(), "raw_ref": ref, "raw_sha256": sha,
                    "read_only": not (info.st_mode & 0o222)}
    raise Fail(f"要約ページの raw を確かめられない: {checked.state}: {checked.reason} {absolute}")


# ---------------------------------------------------------------- サブコマンド


def video_of(url: str) -> wiki_youtube_url.YouTubeURL:
    found = wiki_youtube_url.classify(url)
    if found.kind == "other":
        raise Fail(f"YouTube の動画の URL でない: {url}")
    if found.kind == "youtube_other":
        raise Fail(f"YouTube の動画でない URL なので取らない（{found.reason}）: {url}")
    return found


def take(url: str, requested: str | None) -> dict:
    """メタデータを取り、トラックを選び、json3 を取って raw を作る（ディスクには書かない）。"""
    found = video_of(url)
    notes = []
    if found.has_list:
        notes.append("URL に list= があるので、再生リストではなくこの動画だけを取る")
    with Session() as s:
        info = s.info(found.canonical)
        check_video(info, found.video_id)
        tracks = classify_tracks(info)
        if not tracks:
            raise Fail("字幕がない（投稿者の字幕も、元の言語の自動字幕もない）。字幕を .md か .txt にして raw/ に"
                       "置く方法で取り込む", 2, {"warnings": s.warnings()})
        track, lang, reason = select_track(tracks, info.get("language"), requested)
        result = {
            "video_id": found.video_id, "url": found.canonical,
            "title": wc.flatten(str(info.get("title") or "")),
            "channel": wc.flatten(str(info.get("channel") or info.get("uploader") or "")),
            "uploaded": upload_date(info.get("upload_date")), "duration": info.get("duration"),
            "live_status": info.get("live_status"), "availability": info.get("availability"),
            "video_lang": lang, "tracks": [t.public() for t in tracks],
            "chapters": len(chapters_of(info)), "track": None, "reason": reason or None,
            "source_sha256": None, "lines": None, "notes": notes, "yt_dlp": yt_dlp_version(),
        }
        if track is None:
            result["warnings"] = s.warnings()
            return result
        data = s.json3(track)
        # 字幕を取った時刻。raw には書かず、要約ページの fetched_at に写す（ADR-0027）。時差つきの ISO 8601。
        result["fetched_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        result["warnings"] = s.warnings()
    text, sha, count = render(info, found.video_id, track, lang or track.lang, data)
    result.update({"track": track.public(), "source_sha256": sha, "lines": count, "_raw": text})
    return result


def out_json(obj) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def emit(args, obj: dict, code: int) -> int:
    obj = {k: v for k, v in obj.items() if not k.startswith("_")}
    if args.json:
        out_json(obj)
    else:
        for key, value in obj.items():
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            sys.stdout.write(f"{key}: {text}\n")
    return code


def cmd_inspect(args) -> int:
    wiki, raw_root = Path(args.wiki), Path(args.raw_root)
    if not raw_root.is_dir():
        raise Fail(f"raw のルートがない: {raw_root}")
    if args.slug is not None:
        check_slug(args.slug)
    result = take(args.url, args.track)
    if result["track"] is None:
        result["status"] = "track_undecided"
        return emit(args, result, 3)
    rel = relations(wiki, raw_root, result["video_id"], result["source_sha256"], args.slug)
    result.update({k: v for k, v in rel.items() if not k.startswith("_")})
    result["status"] = "ready"
    return emit(args, result, 0)


def cmd_write(args) -> int:
    wiki, raw_root = Path(args.wiki), Path(args.raw_root)
    try:
        guard = external_raw_guard(wiki, raw_root)
    except (OSError, ValueError) as exc:
        raise Fail(f"raw の行き先を使えない: {exc}") from exc
    check_slug(args.slug)
    result = take(args.url, args.track)
    if result["track"] is None:
        result["status"] = "track_undecided"
        return emit(args, result, 3)
    if result["source_sha256"] != args.expect_sha256:
        result["status"] = "changed"
        result["message"] = "inspect のあとに字幕が変わったので書かない。inspect からやり直す"
        return emit(args, result, 3)
    rel = relations(wiki, raw_root, result["video_id"], result["source_sha256"], args.slug, guard=guard)
    result.update({k: v for k, v in rel.items() if not k.startswith("_")})
    same = rel["_same"][0] if rel["_same"] else None
    if same is None and rel["slug_state"] == "different":
        result["status"] = "different"
        result["message"] = ("このスラッグには、同じ動画の字幕の違う raw がある。別版のスラッグ（<スラッグ>-<YYYYMMDD>）で"
                             "書くと利用者が決めたら、そのスラッグと --new-version を付けて書く")
        return emit(args, result, 3)
    if same is None and rel["slug_state"] != "free":
        result["status"] = "slug_taken"
        return emit(args, result, 3)
    if same is None and rel["_different"] and not args.new_version:
        result["status"] = "different"
        result["message"] = ("同じ動画の、字幕の違う raw がある。別版のスラッグ（<スラッグ>-<YYYYMMDD>）で書くと利用者が"
                             "決めたら、--new-version を付けて書く")
        return emit(args, result, 3)
    try:
        record_raw_root(wiki, guard.root, writer="wiki_youtube")
    except (OSError, ValueError, RuntimeError) as exc:
        raise Fail(f"raw の根を記録できない: {exc}") from exc
    try:
        if same is not None:
            result.update(finish_raw(wiki, guard, same["path"], same["raw_sha256"]), status="same")
        else:
            data = result["_raw"].encode("utf-8")
            path = raw_root / f"{args.slug}.md"
            if not publish(guard, path, data):
                result["status"] = "slug_taken"
                return emit(args, result, 3)
            result.update(finish_raw(wiki, guard, path, hashlib.sha256(data).hexdigest()), status="created")
    except (OSError, ValueError) as exc:
        raise Fail(f"raw を保存できない（{exc}）") from exc
    return emit(args, result, 0)


def parse_page_args(values: list[str] | None, wiki=None) -> dict:
    overlay = {}
    for value in values or []:
        if "=" not in value:
            raise Fail(f"--page は <wiki からの相対パス>=<ファイル> の形で渡す: {value}")
        rel, file = value.split("=", 1)
        rel = rel.replace("\\", "/").strip("/")
        if not rel.lower().endswith(".md") or rel.startswith("../") or "/../" in rel or os.path.isabs(rel):
            raise Fail(f"--page の相対パスは、wiki の中の .md にする: {rel}")
        # 場所の名前はそろえずに渡す（ファイルシステムに尋ねる前に名前を変えない。実装のレビューの 17 回目）。
        if rel in overlay:
            raise Fail(f"--page に同じ場所を 2 回渡した（片方を黙って捨てない）: {rel}")
        try:
            # wiki の中の記事を渡されたら、記事を読む口を通す（info-hive-dev の ADR-0029。リンクなら読まない）
            import wiki_pages
            overlay[rel] = wiki_pages.read_user_file(Path(file), wiki)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise Fail(f"--page のファイルを読めない（{exc}）: {file}")
    return overlay


def cmd_verify(args) -> int:
    wiki, raw_root = Path(args.wiki), Path(args.raw_root)
    historical, unchecked = read_raw_roots(wiki)
    if args.raw_path is not None:
        raw_path = Path(args.raw_path)
    else:
        check_slug(args.slug)
        raw_path = raw_root / f"{args.slug}.md"
    try:
        absolute = absolute_user_path(raw_path)
        root = absolute_user_path(raw_root)
        if not any(within(absolute, base) and absolute != base for base in (root, root.resolve(strict=True))):
            raise ValueError("対象の raw は --raw-root の中に指定する")
        checked, = resolve_raw({"type": "source", "raw": str(absolute)}, wiki / "sources" / raw_path.name,
                               wiki, raw_roots=[root], recorded_raw_roots=historical)
        if checked.state != "resolved":
            raise ValueError(f"{checked.state}: {checked.reason} {checked.links or checked.unchecked}")
        raw_path = checked.path
        doc = wc.parse_raw(raw_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError, wc.CaptionError) as exc:
        raise Fail(f"動画の raw を読めない（{exc}）: {raw_path}")
    overlay = parse_page_args(args.page, wiki)
    label = wc.raw_label(raw_path, wiki)
    # 指摘は wiki 全体のものを返す（この raw に関わるかで絞らない）。結果と「raw にない語」だけをこの raw に絞る。
    try:
        result = wc.check_wiki(wiki, overlay, only_raw=str(Path(os.path.abspath(raw_path))), unknown_for=set(overlay),
                               raw_roots=[raw_root], recorded_raw_roots=historical, wiki_root_given=wiki)
    except ValueError as exc:
        raise Fail(str(exc))
    raws = [r.as_dict() for r in result.raws]
    findings = [f.as_dict() for f in result.findings]
    obj = {
        "raw": label, "video_id": doc.video_id, "caption_kind": doc.caption_kind, "lines": doc.count,
        "read_only": not (os.stat(raw_path).st_mode & 0o222),
        "findings": findings, "result": raws[0] if raws else None, "unknown_words": result.unknown_words,
        "raw_warnings": result.raw_warnings + [{"state": "unknown_raw_root", **item} for item in unchecked],
    }
    if not raws or not raws[0]["summaries"]:
        if not any(f["code"] == "no_summary" for f in obj["findings"]):
            obj["findings"].append({"path": label, "line": 1, "code": "no_summary",
                                    "message": "この raw を `raw` に持つ要約ページがない（--page で用意したページを渡す）"})
    return emit(args, obj, 1 if obj["findings"] else 0)


def cmd_define(args) -> int:
    ident = args.id
    if not wc.IDENT_RE.fullmatch(ident):
        raise Fail(f"識別子は L<行の番号>-<n> の形にする: {ident}")
    if (args.corrected is None) != (args.basis is None):
        raise Fail("訂正の定義は --corrected と --basis を両方渡す。留保の定義はどちらも渡さない")
    line = wc.format_definition(ident, args.caption, args.nth, args.corrected, args.basis)
    parsed = wc.parse_definition(line)
    if isinstance(parsed, str):
        raise Fail(f"定義を作れない（{parsed}）")
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = localize_argparse(argparse.ArgumentParser(
        description="YouTube の動画の字幕を、固定した yt-dlp で取り、raw を 1 回で作る（ADR-0027）"))
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, slug_required: bool | None):
        p.add_argument("--wiki", required=True)
        p.add_argument("--raw-root", required=True)
        if slug_required is not None:
            p.add_argument("--slug", required=slug_required)
        p.add_argument("--json", action="store_true")

    p = localize_argparse(sub.add_parser("inspect", help="メタデータとトラックを取り、source_sha256 を出す（書かない）"))
    p.add_argument("--url", required=True)
    p.add_argument("--track")
    common(p, False)
    p = localize_argparse(sub.add_parser("write", help="raw を重ねて書かない形で置き、読み取り専用にする"))
    p.add_argument("--url", required=True)
    p.add_argument("--track")
    p.add_argument("--expect-sha256", required=True)
    p.add_argument("--new-version", action="store_true",
                   help="同じ動画の字幕の違う raw があっても、別版のスラッグで書く（利用者が決めたとき）")
    common(p, True)
    p = localize_argparse(sub.add_parser("verify", help="用意したページと wiki のページを raw と照らす"))
    p.add_argument("--page", action="append", default=None,
                   help="<wiki からの相対パス>=<ファイル>。wiki のページに重ねて読む（繰り返し指定できる）")
    common(p, None)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--slug")
    target.add_argument("--raw-path", help="write が返した対象の原本のパス")
    p = localize_argparse(sub.add_parser("define", help="定義の 1 行を決まった書き方で出す"))
    p.add_argument("--id", required=True)
    p.add_argument("--caption", required=True)
    p.add_argument("--nth", type=int)
    p.add_argument("--corrected")
    p.add_argument("--basis")
    return parser


COMMANDS = {"inspect": cmd_inspect, "write": cmd_write, "verify": cmd_verify, "define": cmd_define}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse は引数の誤りを終了コード 2 にするが、2 は「動画を取れない」なので 1 に変える。
        return 1 if exc.code == 2 else (exc.code or 0)
    if getattr(args, "wiki", None) is not None:
        wiki_initialized.require_or_exit(args.wiki)
    try:
        return COMMANDS[args.command](args)
    except Fail as exc:
        if getattr(args, "json", False):
            out_json({"status": "error", "message": str(exc), "exit_code": exc.code, **exc.extra})
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
