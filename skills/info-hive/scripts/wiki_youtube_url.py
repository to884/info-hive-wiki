#!/usr/bin/env python3
"""
wiki_youtube_url.py — URL が YouTube の動画か、動画でない YouTube の URL か、それ以外かを決める（ADR-0027）。

wiki_fetch.py（Web ページの経路）と wiki_youtube.py（動画の経路）の両方が使う。判定をここだけに置き、
2 つの経路の判定が食い違わないようにする。標準ライブラリだけで書いてある。Python 3.12 以上。

動画と認める URL:
    youtube.com、www.youtube.com、m.youtube.com の /watch?v=<ID>、/shorts/<ID>、/live/<ID>、/embed/<ID>
    youtu.be/<ID>
    <ID> は 11 文字の [A-Za-z0-9_-]。

そのほかの youtube.com（サブドメインを含む）と youtu.be の URL は、動画でない YouTube の URL として扱う
（再生リスト、チャンネル、検索など）。どちらの経路も、これを取らずに止まる。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")  # fullmatch で使う（$ は末尾の改行の前にも当たる）
VIDEO_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com")
PATH_KINDS = ("shorts", "live", "embed")


@dataclass(frozen=True)
class YouTubeURL:
    """URL の判定の結果。kind は "video"、"youtube_other"、"other" のどれか。"""

    kind: str
    video_id: str | None = None
    reason: str = ""
    has_list: bool = False

    @property
    def canonical(self) -> str | None:
        """動画なら、raw の frontmatter に書く形（https://www.youtube.com/watch?v=<ID>）。"""
        return f"https://www.youtube.com/watch?v={self.video_id}" if self.video_id else None


def effective_host(url: str) -> str:
    """URL を開くときに、通信の処理が実際につなぐホストを返す（小文字、末尾の . を除く）。

    urllib はホストのパーセントエンコード（www%2eyoutube%2ecom）を復号してつなぎ、日本語などのホストは
    IDNA に変えてつなぐ（全角の文字は NFKC で半角になる）。判定を URL の字面で行うと、判定のホストと、
    つなぐホストが食い違う（info-hive-dev の ADR-0027 の実装のレビューの 1 回目）。そこで、通信の処理と同じ
    手順でホストを決める。決められなければ "" を返す。
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return ""
    host = urllib.parse.unquote(host)
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
    return host.lower().rstrip(".")


def is_youtube_host(host: str) -> bool:
    """youtube.com（サブドメインを含む）か youtu.be（サブドメインを含む）なら真。"""
    h = (host or "").lower().rstrip(".")
    return h in ("youtube.com", "youtu.be") or h.endswith((".youtube.com", ".youtu.be"))


def classify(url: str) -> YouTubeURL:
    """URL を判定する。http と https のほかの URL と、YouTube でない URL は "other" にする。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return YouTubeURL("other")
    if parts.scheme.lower() not in ("http", "https"):
        return YouTubeURL("other")
    host = effective_host(url)
    if not is_youtube_host(host):
        return YouTubeURL("other")
    if parts.username is not None or parts.password is not None:
        return YouTubeURL("youtube_other", reason="URL に利用者の情報（@ の前）が付いている")
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    has_list = bool(query.get("list"))
    path = parts.path
    segments = [s for s in path.split("/") if s]
    if host == "youtu.be" or host.endswith(".youtu.be"):
        if host == "youtu.be" and len(segments) == 1 and VIDEO_ID_RE.fullmatch(segments[0]):
            return YouTubeURL("video", segments[0], has_list=has_list)
        return YouTubeURL("youtube_other", reason="youtu.be の URL に 11 文字の動画の ID がない")
    if host not in VIDEO_HOSTS:
        return YouTubeURL("youtube_other", reason=f"動画の URL として扱わないホスト（{host}）")
    if path in ("/watch", "/watch/"):
        values = query.get("v") or []
        ids = {v for v in values}
        if len(ids) == 1:
            vid = next(iter(ids))
            if VIDEO_ID_RE.fullmatch(vid):
                return YouTubeURL("video", vid, has_list=has_list)
        if len(ids) > 1:
            return YouTubeURL("youtube_other", reason="v= が 2 つ以上あり、どの動画か決められない")
        return YouTubeURL("youtube_other", reason="/watch に 11 文字の v= がない")
    if len(segments) == 2 and segments[0] in PATH_KINDS:
        if VIDEO_ID_RE.fullmatch(segments[1]):
            return YouTubeURL("video", segments[1], has_list=has_list)
        return YouTubeURL("youtube_other", reason=f"/{segments[0]}/ のあとが 11 文字の動画の ID でない")
    if segments[:1] == ["playlist"]:
        return YouTubeURL("youtube_other", reason="再生リストの URL。1 回の取り込みで扱うのは 1 本の動画である")
    if segments and (segments[0].startswith("@") or segments[0] in ("channel", "c", "user")):
        return YouTubeURL("youtube_other", reason="チャンネルの URL。1 回の取り込みで扱うのは 1 本の動画である")
    if segments[:1] == ["results"]:
        return YouTubeURL("youtube_other", reason="検索の URL")
    return YouTubeURL("youtube_other", reason="動画の URL の形でない")
