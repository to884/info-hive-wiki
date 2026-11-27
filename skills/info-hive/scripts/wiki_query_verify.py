"""Claude Code のツール結果から wiki の版と全引用本文を照合する。"""

from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from wiki_apply import view_info
from wiki_mcp_common import CHECKED_QUERY_TOOLS as READERS
from wiki_pages import clean_link_target, iter_page_files, new_parser, read_user_file
from wiki_search import parse_frontmatter

PREFIX = "mcp__plugin_wiki_wiki__"
ARTICLES = {"sources", "concepts", "entities", "synthesis"}
NUMBERED = re.compile(r"^[ \t]*(\d+)[→\t](.*)$")
LINK = re.compile(r"\[\[([^\[\]\n]+)\]\]")
RELEASE_ERROR = ("取り下げ", "版が一致しません", "公開済みの版ではありません", "公開版がありません",
                 "指紋が一致しません", "識別子が不正")


@dataclass
class Call:
    name: str
    args: dict
    start: int
    end: int | None = None
    ok: bool = False
    body: str = ""


@dataclass
class Verdict:
    passed: bool = False
    missing: list[str] = field(default_factory=list)
    reason: str = "版・規約・索引の読み取り順を確認できませんでした。"


def same(value, path):
    return isinstance(value, str) and Path(value).is_absolute() and Path(value).resolve() == path


def receipt(body):
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value if isinstance(value, dict) else {}


def inside(root, value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    path = Path(value).resolve()
    return path if path.is_relative_to(root) else None


def article(root, value, *, relative):
    if relative and isinstance(value, str):
        value = str(root / value)
    path = inside(root, value)
    if path is None:
        return None
    parts = path.relative_to(root).parts
    if len(parts) > 1 and parts[0] in ARTICLES and path.suffix.lower() == ".md" and path.is_file():
        return path
    return None


def cited_targets(answer, root):
    """Wikilink と Markdown のローカル記事リンクの参照先を集める。"""
    targets = {link: clean_link_target(link) for link in LINK.findall(answer)}
    parser = new_parser()
    validate_link = parser.validateLink
    # リンクは表示せず照合だけに使う。表示時に無効な file: も、記事の参照先として調べる。
    parser.validateLink = lambda url: validate_link(url) or url.lower().startswith("file:")
    for token in parser.parse(answer):
        for child in token.children or ():
            if child.type not in {"link_open", "image"}:
                continue
            href = child.attrGet("href" if child.type == "link_open" else "src")
            uri = urlsplit(href)
            local = unquote(uri.path).replace("\\", "/")
            remote_file = uri.scheme == "file" and uri.netloc.lower() not in {"", "localhost"}
            if uri.scheme == "file":
                local = url2pathname(uri.path)
            elif len(uri.scheme) == 1 and local.startswith("/") and not uri.netloc:
                local = uri.scheme + ":" + local
            elif uri.scheme or uri.netloc or not local:
                continue
            path = Path(local)
            if path.suffix.lower() != ".md" and (path.suffix or not ARTICLES.intersection(path.parts)):
                continue
            if remote_file or (PureWindowsPath(local).is_absolute() and not path.is_absolute()):
                targets[href] = None
                continue
            resolved = (root / path).resolve()
            if resolved.is_relative_to(root / "raw") or resolved.is_relative_to(root.parent / "raw"):
                continue
            targets[href] = (clean_link_target(resolved.relative_to(root).as_posix())
                             if resolved.is_relative_to(root) else None)
    return targets


def texts(root, calls, *, get_page, release):
    """本文と規約・索引の全行が、どの応答で初めてそろったかを返す。"""
    complete = {}
    received = defaultdict(set)
    schema = root / "SCHEMA.md"
    index = root / "index.md"
    for call in sorted(calls, key=lambda item: item.end):
        if not call.ok:
            continue
        response = receipt(call.body)
        if call.name == "Read":
            path = inside(root, call.args.get("file_path"))
            if path not in (schema, index):
                if release is not None and get_page:
                    continue
                path = article(root, call.args.get("file_path"), relative=False)
            if path is None:
                continue
            lines = read_user_file(call.args["file_path"], root).splitlines()
            for line in call.body.splitlines():
                match = NUMBERED.match(line)
                if match:
                    number = int(match[1])
                    if 1 <= number <= len(lines) and match[2] == lines[number - 1]:
                        received[path].add(number)
            if lines and len(received[path]) == len(lines):
                complete.setdefault(path, call)
        elif call.name == PREFIX + "list_index" and not call.args.get("name"):
            if response.get("index") == read_user_file(index, root):
                complete.setdefault(index, call)
        elif call.name == PREFIX + "get_page":
            path = article(root, response.get("rel_path"), relative=True)
            body = response.get("body")
            if path is None or not isinstance(body, str) or not body.strip():
                continue
            _, expected = parse_frontmatter(read_user_file(root / response["rel_path"], root))
            if body.strip() == expected.strip():
                complete.setdefault(path, call)
    return complete


def verify(wiki, calls, answer, tools):
    """照合に失敗した候補は返さず、本文だけが不足する場合は修正対象を返す。"""
    calls = sorted(calls, key=lambda call: call.start)
    views = []
    data_calls = []
    for call in calls:
        if call.end is None:
            return Verdict(reason="読み取りの応答を確認できませんでした。")
        if not call.name.startswith(PREFIX):
            if call.name == "Read" and inside(wiki, call.args.get("file_path")) is not None:
                data_calls.append(call)
            continue
        tool = call.name.removeprefix(PREFIX)
        if tool not in READERS or not same(call.args.get("wiki"), wiki):
            return Verdict(reason="読み取りの対象を確認できませんでした。")
        if tool == "view":
            if not call.ok:
                return Verdict(reason="wiki の版を確認できませんでした。")
            views.append(call)
        else:
            data_calls.append(call)

    if not views or any(c.start < views[0].end for c in data_calls):
        return Verdict()
    if views[0].args.get("release_id") is not None:
        return Verdict(reason="最初の版確認で公開版 ID が指定されたため回答を保留しました。")
    first = receipt(views[0].body)
    if "release_id" not in first or not isinstance(first.get("read_root"), str):
        return Verdict()
    release = first["release_id"]
    if release is not None and not isinstance(release, str):
        return Verdict()
    root_value = first["read_root"]
    root = inside(wiki, root_value)
    if root is None or any(
        receipt(call.body) != first or call.args.get("release_id") not in (None, release) for call in views
    ):
        return Verdict(reason="確認した版が一致しないため回答を保留しました。")

    for call in data_calls:
        if call.name == "Read":
            if inside(root, call.args.get("file_path")) is None:
                return Verdict(reason="固定した wiki の版と読み取り先が一致しませんでした。")
            continue
        reply = receipt(call.body)
        if call.args.get("release_id") != release or (call.ok and reply.get("release_id") != release):
            return Verdict(reason="検索・本文取得の版が一致しないため回答を保留しました。")
        if not call.ok and (release is not None and any(text in call.body for text in RELEASE_ERROR)):
            return Verdict(reason="指定した公開版を確認できないため回答を保留しました。")

    get_page = "get_page" in tools
    if not get_page and any(c.name == PREFIX + "get_page" and c.ok for c in data_calls):
        return Verdict(reason="get_page の登録状況を確認できませんでした。")
    complete = texts(root, data_calls, get_page=get_page, release=release)
    schema = complete.get(root / "SCHEMA.md")
    index = complete.get(root / "index.md")
    if not schema or not index or index.start <= schema.end:
        return Verdict()
    for call in data_calls:
        file_path = inside(root, call.args.get("file_path")) if call.name == "Read" else None
        uses_index_tool = call.name == PREFIX + "list_index"
        reads_index = file_path == root / "index.md" or (uses_index_tool and not call.args.get("name"))
        reads_split = ((file_path is not None and file_path.is_relative_to(root / "indexes"))
                       or (uses_index_tool and call.args.get("name")))
        reads_articles = (call.name in {PREFIX + "query", PREFIX + "get_page", PREFIX + "graph"}
                          or article(root, call.args.get("file_path"), relative=False) is not None)
        if (reads_index and call.start <= schema.end) or ((reads_split or reads_articles) and call.start <= index.end):
            return Verdict()

    bodies = {path: call for path, call in complete.items()
              if path not in {root / "SCHEMA.md", root / "index.md"} and call.start > index.end}
    citations = cited_targets(answer, root)
    paths = iter_page_files(root)
    by_path = {clean_link_target(path.relative_to(root).as_posix()): path.resolve() for path in paths}
    slugs = defaultdict(list)
    for path in paths:
        slugs[clean_link_target(path.name)].append(path.resolve())
    missing = []
    used = []
    for cite, key in sorted(citations.items()):
        if key in {"SCHEMA", "index"}:
            # 規約とトップ索引は、先に全文を確認している。
            continue
        if key in by_path:
            matches = [by_path[key]]
        else:
            matches = slugs.get(key, ())
        if len(matches) == 1 and matches[0] in bodies:
            used.append(bodies[matches[0]])
        else:
            missing.append(cite)
    if missing:
        return Verdict(missing=missing, reason="全引用の本文を確認できなかったため回答を保留しました。")
    if not used:
        return Verdict(reason="回答に本文を確認できる記事の引用がありませんでした。")
    if release is not None and not get_page:
        if not any(call.args.get("release_id") == release and call.start > max(c.end for c in used)
                   for call in views[1:]):
            return Verdict(reason="本文取得後に同じ公開版を再確認できませんでした。")
    current = view_info(wiki, release)
    if current.get("release_id") != release or not same(current.get("read_root"), root):
        return Verdict(reason="回答時点で同じ版を確認できませんでした。")
    return Verdict(passed=True, reason="")
