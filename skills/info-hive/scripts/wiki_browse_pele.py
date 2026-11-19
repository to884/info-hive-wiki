"""静的の書き出しで、Pele の子プロセスから明暗の SVG を得る。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import deque

from wiki_browse_core import ASSET_DIR, ASSET_MAX_BYTES, svg_size

MAX_SOURCE_CHARS = 50_000
BATCH_SIZE = 16
BATCH_SECONDS = 12
BUILD_SECONDS = 60
SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}"
XML_TEXT_ATTRS = {"{http://www.w3.org/XML/1998/namespace}lang", "{http://www.w3.org/XML/1998/namespace}space"}
PASSIVE_TAGS = frozenset((
    "svg", "g", "defs", "title", "desc", "rect", "circle", "ellipse", "line", "path", "polyline", "polygon",
    "text", "tspan", "marker", "clipPath", "mask", "use", "symbol", "linearGradient", "radialGradient", "stop", "pattern",
))
PASSIVE_ATTRS = frozenset((
    "id", "class", "role", "lang", "viewBox", "preserveAspectRatio", "width", "height", "x", "y", "x1", "y1", "x2",
    "y2", "dx", "dy", "cx", "cy", "r", "rx", "ry", "d", "pathLength", "points", "transform", "opacity", "fill-opacity",
    "stroke-opacity", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset",
    "stroke-miterlimit", "fill-rule", "clip-rule", "vector-effect", "font-size", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "alignment-baseline", "baseline-shift", "direction", "letter-spacing", "text-decoration",
    "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "orient", "clipPathUnits", "maskUnits", "maskContentUnits",
    "gradientUnits", "gradientTransform", "spreadMethod", "offset", "stop-opacity", "fx", "fy", "patternUnits",
    "patternContentUnits", "patternTransform", "shape-rendering", "display", "visibility", "overflow", "focusable",
))
CSS_ATTRS = frozenset(("style", "fill", "stroke", "filter", "mask", "clip-path", "marker", "marker-start",
                       "marker-mid", "marker-end", "cursor", "color", "stop-color", "font-family"))
CSS_FUNCTIONS = frozenset(("var", "calc", "rgb", "rgba", "hsl", "hsla", "oklch", "oklab", "color", "min", "max",
                           "clamp", "matrix", "translate", "translatex", "translatey", "scale", "rotate", "skewx", "skewy"))
LOCAL_ID = r"#[a-zA-Z_][a-zA-Z0-9_.:-]*"
CSS_LOCAL_URL = re.compile(rf"\burl\(\s*(['\"]?)({LOCAL_ID})\1\s*\)", re.IGNORECASE)
CSS_FUNCTION = re.compile(r"([\w-]+)\s*\(")


def _passive_css(value: str) -> bool:
    if any(mark in value for mark in ("\\", "@", "/*", "\r", "\n")):
        return False
    value = CSS_LOCAL_URL.sub("", value)
    return not re.search(r"\b(?:-moz-binding|behavior)\s*:", value, re.IGNORECASE) and all(
        name.lower() in CSS_FUNCTIONS for name in CSS_FUNCTION.findall(value)
    )


def _image(value: str) -> tuple[bytes, str] | None:
    """配布先で直接開いても使える、受動的な SVG と図の題を返す。"""
    try:
        data = value.encode("utf-8")
        if len(data) > ASSET_MAX_BYTES or "<!" in value or "<?" in value:
            return None
        root = ET.fromstring(data)
    except (UnicodeError, ET.ParseError):
        return None
    if root.tag != SVG + "svg" or svg_size(data) is None:
        return None
    for element in root.iter():
        if not element.tag.startswith(SVG) or element.tag[len(SVG):] not in PASSIVE_TAGS:
            return None
        for name, content in element.attrib.items():
            if name in ("href", XLINK + "href"):
                if not re.fullmatch(LOCAL_ID, content):
                    return None
            elif name in CSS_ATTRS:
                if not _passive_css(content):
                    return None
            elif name not in PASSIVE_ATTRS and name not in XML_TEXT_ATTRS and not name.startswith(("aria-", "data-")):
                return None
    title = root.findtext(SVG + "title", "").strip()
    return data, title


def _node_executable() -> str | None:
    name = "node.exe" if os.name == "nt" else "node"
    for folder in os.get_exec_path():
        if os.name == "nt":
            folder = folder.strip('"')
        if os.path.isabs(folder):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def render_sources(sources: list[str]) -> tuple[dict[str, tuple[bytes, bytes, str]], list[str]]:
    eligible = [source for source in sources if 0 < len(source) <= MAX_SOURCE_CHARS]
    if not eligible:
        return {}, []
    node = _node_executable()
    if node is None:
        return {}, ["Node.js 20 以上が見つからない。Mermaid の図はコードで表示する"]
    script = os.path.join(ASSET_DIR, "render-pele.mjs")
    images = {}
    warnings = []
    deadline = time.monotonic() + BUILD_SECONDS
    pending = deque(eligible[offset:offset + BATCH_SIZE] for offset in range(0, len(eligible), BATCH_SIZE))
    failed = False
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            warnings.append("Mermaid の描画が時間の上限に達した。残りの図はコードで表示する")
            break
        batch = pending.popleft()
        try:
            process = subprocess.run([node, script], input=json.dumps(batch, ensure_ascii=False),
                                     capture_output=True, encoding="utf-8", timeout=min(remaining, BATCH_SECONDS))
            process.check_returncode()
            rows = json.loads(process.stdout)
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise ValueError("Pele の応答の件数が図の原稿の件数と合わない")
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
            if len(batch) == 1:
                failed = True
            else:
                middle = len(batch) // 2
                pending.extend((batch[:middle], batch[middle:]))
            continue
        except OSError:
            warnings.append("Pele を実行できない。Node.js 20 以上を確認する。残りの図はコードで表示する")
            break
        for source, row in zip(batch, rows):
            if not isinstance(row, list) or len(row) != 2 or not all(isinstance(svg, str) for svg in row):
                continue
            light, dark = map(_image, row)
            if light and dark:
                images[source] = (light[0], dark[0], light[1] or dark[1])
    if failed:
        warnings.append("Pele で描画が完了しなかった図がある。その図はコードで表示する")
    return images, warnings
