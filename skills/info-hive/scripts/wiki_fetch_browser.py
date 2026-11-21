#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["playwright==1.65.2"]
# ///
"""Playwright で描画した DOM を、wiki_fetch.py に UTF-8 の JSON で返す。

uv run --script wiki_fetch_browser.py <URL> [--browser system|chromium|chrome|msedge]
raw や HTML の一時ファイルは書かない。既定では、Windows の既定ブラウザに公式拡張で接続して対象 URL を開く。
ログイン状態はブラウザ内で使い、Cookie やプロファイルは書き出さない。
--browser-session isolated は独立したブラウザで取得し、普段のログイン状態は使わない。
isolated で Chromium を明示したときだけ、必要なら Chromium を導入する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path, PureWindowsPath

import wiki_fetch


BROWSERS = ("system", "chromium", "chrome", "msedge")
EXTENSION_CORE_VERSION = "1.66.0-alpha-2026-11-09"


def capture_existing(url: str, timeout: float, max_bytes: int, block_private: bool,
                     selected: str, max_images: int) -> dict:
    """公式拡張で空の接続タブまたは許可済みの対象タブを使う。"""
    allowed_url(url, block_private, navigation=True)
    options = browser_launch_options(url, selected)
    if options["channel"] not in ("chrome", "msedge"):
        raise wiki_fetch.FetchError("普段のログイン状態を使うには、公式 Playwright 拡張が入った Chrome または Edge を指定する。"
                                   "独立した Chromium で取得するなら --browser-session isolated を付ける")
    npm = shutil.which("npm")
    if npm is None or shutil.which("node") is None:
        raise wiki_fetch.FetchError("普段のブラウザへの接続には Node.js 20 以上と npm が必要である")
    request = {"url": url, "timeout": timeout, "max_bytes": max_bytes, "max_images": max_images,
               "channel": options["channel"], "executable_path": options.get("executable_path"),
               "python_executable": sys.executable, "selector_script": str(Path(__file__).resolve())}
    command = [npm, "exec", "--yes", f"--package=playwright-core@{EXTENSION_CORE_VERSION}", "--", "node",
               str(Path(__file__).with_name("wiki_browser_session.cjs"))]
    done = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout + 180,
                          env={**wiki_fetch.browser_environment(extension=True), "PYTHONIOENCODING": "utf-8"})
    try:
        result = json.loads(done.stdout)
        if not isinstance(result, dict):
            raise ValueError("結果がオブジェクトでない")
    except ValueError:
        raise wiki_fetch.FetchError("普段のブラウザから読み取りの結果を得られなかった。Node.js、npm、Playwright 拡張を確かめる") from None
    if done.returncode != 0 or "error" in result:
        raise wiki_fetch.FetchError(str(result.get("error", "普段のブラウザに接続できなかった")))
    return result


def windows_default_executable(scheme: str) -> str:
    """Windows にプロトコルの既定アプリを尋ねる。関連付けのレジストリは変更しない。"""
    import ctypes
    from ctypes import wintypes

    # ASSOCF_IS_PROTOCOL、ASSOCSTR_EXECUTABLE。システムの DLL だけを読み込む。
    query = ctypes.WinDLL("shlwapi", winmode=0x00000800).AssocQueryStringW
    query.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR, wintypes.LPCWSTR,
                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = ctypes.c_long
    length = wintypes.DWORD(0)
    result = query(0x1000, 2, scheme, None, None, ctypes.byref(length))
    if result not in (0, 1) or not 0 < length.value <= 32768:
        raise wiki_fetch.FetchError(f"Windows の {scheme} の既定ブラウザを調べられない（HRESULT 0x{result & 0xffffffff:08x}）")
    output = ctypes.create_unicode_buffer(length.value)
    result = query(0x1000, 2, scheme, None, output, ctypes.byref(length))
    if result != 0 or not output.value:
        raise wiki_fetch.FetchError(f"Windows の {scheme} の既定ブラウザを調べられない（HRESULT 0x{result & 0xffffffff:08x}）")
    return output.value


def browser_launch_options(url: str, selected: str = "system") -> dict:
    """既定のブラウザの実パスか、明示した Playwright のチャンネルを返す。"""
    if selected not in BROWSERS:
        raise wiki_fetch.FetchError(f"ブラウザの指定が不明である: {selected}")
    if selected != "system":
        return {"channel": selected, "headless": True}
    if sys.platform != "win32":
        raise wiki_fetch.FetchError("OS の既定ブラウザの自動検出は Windows に対応している。"
                                   "この OS では --browser chrome、msedge、chromium のいずれかを指定する")
    executable = windows_default_executable(urllib.parse.urlsplit(url).scheme.lower())
    path = PureWindowsPath(executable)
    channel = {"chrome.exe": "chrome", "msedge.exe": "msedge"}.get(path.name.lower())
    if channel is None:
        raise wiki_fetch.FetchError(f"OS の既定ブラウザ（{path.name}）は Playwright の自動取得に対応していない。"
                                   "保存した HTML を渡すか、--browser で Chrome、Edge、Chromium を指定する。"
                                   "通常の Firefox と Safari は Playwright で操作できない")
    if not path.is_absolute() or not Path(executable).is_file():
        raise wiki_fetch.FetchError("OS の既定ブラウザの実行ファイルが見つからない。Windows の既定のアプリを確かめる")
    # channel の標準の場所でなく、OS が返した実パスを起動する。
    return {"channel": channel, "executable_path": executable, "headless": True}


def allowed_url(url: str, block_private: bool, *, navigation: bool = False) -> None:
    """直接の要求とリダイレクトの先に、通常の取得と同じ制限を当てる。"""
    wiki_fetch.check_url(url)
    if navigation:
        wiki_fetch.refuse_youtube(url, "ブラウザの移動先 ")
    if block_private and wiki_fetch.is_private_host(urllib.parse.urlsplit(url).hostname or ""):
        raise wiki_fetch.FetchError(f"内部のアドレスは取得しない: {url}")


def install_chromium(playwright) -> None:
    """対応する Chromium がなければ、一度導入する。既存のプロファイルは使わない。"""
    if Path(playwright.chromium.executable_path).is_file():
        return
    print("Playwright 用の Chromium を初めて導入している。次回からはキャッシュを使う", file=sys.stderr)
    done = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300, check=False, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if done.returncode != 0:
        raise wiki_fetch.FetchError(f"Chromium を導入できない: {done.stderr.strip()[-1500:]}")


def capture_page(url: str, timeout: float, max_bytes: int, block_private: bool, selected: str = "system",
                 session: str = "existing", max_images: int = 50) -> dict:
    if session == "existing":
        return capture_existing(url, timeout, max_bytes, block_private, selected, max_images)
    if session != "isolated":
        raise wiki_fetch.FetchError(f"ブラウザのセッションの指定が不明である: {session}")
    return capture_isolated(url, timeout, max_bytes, block_private, selected)


def capture_isolated(url: str, timeout: float, max_bytes: int, block_private: bool, selected: str = "system") -> dict:
    """ブラウザを一度起動し、本文の出現を待ち、DOM と最終 URL を返す。"""
    # このモジュール自体の読み込みと単体試験では、Playwright を必要としない。
    from playwright.sync_api import Error, sync_playwright

    allowed_url(url, block_private, navigation=True)
    options = browser_launch_options(url, selected)
    with sync_playwright() as playwright:
        if selected == "chromium":
            install_chromium(playwright)
        try:
            browser = playwright.chromium.launch(**options)
        except Error as exc:
            raise wiki_fetch.FetchError(f"指定したブラウザ（{options['channel']}）を起動できない。"
                                       f"インストールと起動の設定を確かめる: {exc}") from None
        try:
            context = browser.new_context(accept_downloads=False, service_workers="block")
            page = context.new_page()
            context.on("page", lambda popup: popup.close() if popup != page else None)
            page.on("dialog", lambda dialog: dialog.dismiss())
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            deadline = time.monotonic() + timeout
            pending_redirect: list[str] = []
            failures: list[Exception] = []

            def remaining_ms() -> float:
                remaining = (deadline - time.monotonic()) * 1000
                if remaining <= 0:
                    raise wiki_fetch.FetchError(f"Playwright の取得が {timeout:g} 秒を超えた")
                return remaining

            def route_request(route) -> None:
                request = route.request
                main = request.is_navigation_request() and request.frame == page.main_frame
                response = None
                try:
                    allowed_url(request.url, block_private, navigation=main)
                    if request.resource_type in ("image", "media", "font"):
                        route.abort()
                        return
                    # 3xx はブラウザに渡さない。Chromium はチェーンの最初の要求だけを interception するため。
                    response = route.fetch(max_redirects=0, max_retries=0, timeout=remaining_ms())
                    if 300 <= response.status < 400 and response.headers.get("location"):
                        destination = urllib.parse.urljoin(request.url, response.headers["location"])
                        allowed_url(destination, block_private, navigation=main)
                        if main:
                            pending_redirect.append(destination)
                            # abort で Chrome のエラーページが後から現れると、次の goto と競合する。
                            # 転送先を含まない空の 200 で現在の遷移を終え、そのあと新たに goto する。
                            route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>移動中</title>")
                            return
                        # 副リソースの転送は追わない。最終の応答を元 URL の応答にすると相対 import の基準が崩れる。
                        route.abort()
                        return
                    if main and response.status >= 400:
                        raise wiki_fetch.FetchError(f"ブラウザでも HTTP {response.status}: {request.url}")
                    if main and "attachment" in response.headers.get("content-disposition", "").lower():
                        raise wiki_fetch.FetchError("ブラウザの移動先がダウンロードになった。通常のファイルとして取り込む")
                    if main:
                        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if ctype and ctype not in ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml"):
                            raise wiki_fetch.FetchError(f"ブラウザの移動先が HTML ではない（{ctype}）")
                    if len(response.body()) > max_bytes:
                        raise wiki_fetch.FetchError(f"ブラウザの応答が {max_bytes:,} バイトを超えた: {request.url}")
                    route.fulfill(response=response)
                except Exception as exc:
                    if main:
                        failures.append(exc)
                    route.abort()
                finally:
                    if response is not None:
                        response.dispose()

            context.route("**/*", route_request)
            next_url = url
            redirects = 0
            started = time.monotonic()
            previous = None
            stable_since = started
            while True:
                try:
                    page.goto(next_url, wait_until="domcontentloaded", timeout=remaining_ms())
                except Error:
                    if failures:
                        raise failures[0]
                    if not pending_redirect:
                        raise
                if failures:
                    raise failures[0]
                while not pending_redirect:
                    allowed_url(page.url, block_private, navigation=True)
                    html = page.content()
                    data = html.encode("utf-8")
                    if len(data) > max_bytes:
                        raise wiki_fetch.FetchError(f"DOM が {max_bytes:,} バイトを超えた")
                    content = wiki_fetch.prepare_page(data, "text/html; charset=utf-8", page.url, False)
                    body = "\n\n".join(content.blocks)
                    now = time.monotonic()
                    if body != previous:
                        previous, stable_since = body, now
                    # 短い「読み込み中」が静止していても終えず、期限まで本文の出現を待つ。
                    if (not content.is_short() and now - started >= 2 and now - stable_since >= 1) or now >= deadline:
                        return {"html": html, "final_url": page.url, "browser": options["channel"]}
                    page.wait_for_timeout(min(250, max(1, (deadline - now) * 1000)))
                    if failures:
                        raise failures[0]
                redirects += 1
                if redirects > 10:
                    raise wiki_fetch.FetchError("ブラウザのリダイレクトが 10 回を超えた")
                next_url = pending_redirect.pop()
                pending_redirect.clear()
                previous = None
        finally:
            browser.close()


def article_image_urls(page: wiki_fetch.PageContent, url: str, maximum: int) -> list[str]:
    """変換器が選んだ本文の画像から、対象ページと同じ origin の候補を返す。"""
    origin = urllib.parse.urlsplit(url)
    found = []
    for image in page.images:
        if len(found) >= maximum:
            break
        source = image["url"]
        parts = urllib.parse.urlsplit(source)
        if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc) or source in found:
            continue
        width, height = wiki_fetch._attr_px(image["width"]), wiki_fetch._attr_px(image["height"])
        if (width is not None and width <= wiki_fetch.SMALL_IMAGE_PX) or (height is not None and height <= wiki_fetch.SMALL_IMAGE_PX):
            continue
        found.append(source)
    return found


def inspect_dom(html: str, url: str, maximum: int) -> dict:
    """表示中の DOM に、通常の本文の長さの判定と画像選択を適用する。"""
    page = wiki_fetch.prepare_page(html.encode("utf-8"), "text/html; charset=utf-8", url, False)
    body = "\n\n".join(page.blocks).encode("utf-8")
    return {"short": page.is_short(), "body_sha256": hashlib.sha256(body).hexdigest(),
            "images": article_image_urls(page, url, maximum)}


def main(argv: list[str] | None = None) -> int:
    if (sys.argv[1:] if argv is None else argv) == ["--inspect-dom"]:
        request = json.load(sys.stdin)
        print(json.dumps(inspect_dom(request["html"], request["url"], request["max_images"]), ensure_ascii=False))
        return 0
    parser = wiki_fetch.localize_argparse(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("url", help="描画するページの URL")
    parser.add_argument("--timeout", type=wiki_fetch.timeout_seconds, default=30, help="描画を待つ秒数（既定: 30）")
    parser.add_argument("--max-bytes", type=int, default=wiki_fetch.DEFAULT_MAX_BYTES, help="DOM と応答のバイト数の上限")
    parser.add_argument("--block-private", action="store_true", help="内部のアドレスへの要求を拒む")
    parser.add_argument("--browser", choices=BROWSERS, default="system",
                        help="使うブラウザ（既定: system。Windows の URL の既定アプリを使う）")
    parser.add_argument("--browser-session", choices=("existing", "isolated"), default="existing",
                        help="既定は普段のブラウザに公式拡張で接続する。isolated は独立したブラウザで取得する")
    parser.add_argument("--max-images", type=int, default=50, help="既存タブから読む画像数の上限")
    args = parser.parse_args(argv)
    try:
        result = capture_page(args.url, args.timeout, args.max_bytes, args.block_private, args.browser,
                              args.browser_session, args.max_images)
    except Exception as exc:
        result = {"error": str(exc)}
        if isinstance(exc, wiki_fetch.YouTubeRefused):
            result.update({"kind": "youtube", "url": exc.url})
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
