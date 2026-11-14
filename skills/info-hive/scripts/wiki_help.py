#!/usr/bin/env python3
"""
wiki_help.py — wiki プラグインのコマンドの一覧か、1 つのコマンドの詳しいヘルプを表示する。

一覧: `commands/*.md` の frontmatter から `description`、`argument-hint`、`argument-description` を読み、
コマンドごとに 3 行の Markdown の箇条書きにする。`/wiki:help` から呼び、書式付きで表示する。

    - **`/wiki:init`** `[--user] [--wiki-dir <name>] [--raw-dir <name>]`
      - ユーザースコープの LLM Wiki、またはこのプロジェクトの LLM Wiki を初期化する。
      - `--user` を付けると ...（argument-description の文）
    - **`/wiki:stats`**
      - wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。
      - 引数はない。

個別のヘルプ（`--command <名前>`）: 同じ frontmatter の値に、`docs/commands.md` の
`` ## `/wiki:<名前> ...` `` の節の本文を足す。`/wiki:help <名前>` から呼ぶ。
節の相対リンクは、`plugin.json` の `repository` と `version` から、GitHub の、導入した版のタグ
（`v<version>`）の文書への URL に書き換える。書き換えのあとで、同梱の Markdown のパーサーで前後を読み、
リンクの宛先のほかが変わっていないことを確かめる。変わっていれば、書き換えずに節をそのまま出して警告する。

使い方:
    python wiki_help.py [<plugin-root>] [--json] [--command <名前>]

<plugin-root> を省くと、このスクリプトの置き場所からプラグインのルートを求める。
<名前> は `query`、`/wiki:query`、`wiki:query` のどれでもよい。空なら一覧を出す。
"""

import argparse
import difflib
import html
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, unquote

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import _fm_unquote, localize_argparse  # noqa: E402


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
# コマンドの frontmatter のキーには `argument-hint` のようにハイフンが入るので、
# ページ用の wiki_text.parse_frontmatter_text（`\w+` のキーだけ）は使わず、ここで読む。
KEY_RE = re.compile(r"^([\w-]+):\s*(.*)$")

# README の表と同じワークフローの順。ここにないコマンドは、名前順で後ろに並べる。
PREFERRED_ORDER = ["init", "ingest", "query", "lint", "stats", "graph", "browse", "learn", "evolve", "upgrade", "help"]

DEFAULT_PLUGIN_NAME = "wiki"
NO_ARGUMENTS = "引数はない。"
MISSING_ARGUMENT_DESCRIPTION = "（引数の説明は未記入）"

# 個別のヘルプの詳細の出どころ（プラグインのルートからの相対パス）。
COMMANDS_DOC = Path("docs") / "commands.md"

# CommonMark のコードフェンス。行頭の空白は 3 つまで。バッククォートのフェンスの情報文字列に ` は入らない。
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
# インラインのリンクと画像。文字の中のエスケープと、1 段の入れ子の [] を読む。
# 宛先は `<`、空白、丸かっこを含まないものだけを書き換える。読めないリンクは、書き換えのあとの照らし合わせで見つかる。
LINK_RE = re.compile(
    r"(!?\[(?:[^\[\]\\\n]|\\.|\[(?:[^\[\]\\\n]|\\.)*\])*\]\()([^()\s<][^()\s]*)(\))"
)
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# CommonMark のバックスラッシュのエスケープ（ASCII の記号）。
BACKSLASH_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
# plugin.json の repository と version。
GITHUB_REPOSITORY_RE = re.compile(r"^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")


class HelpError(Exception):
    """利用者の入力の誤り。メッセージをそのまま標準エラーに出す。"""


class Section(NamedTuple):
    """`docs/commands.md` の 1 つの節。start と end は、本文の行の範囲（0 から数える、end は含まない）。"""
    body: str
    start: int
    end: int


def parse_command_frontmatter(text: str) -> dict | None:
    """コマンドの Markdown の先頭にある frontmatter を、キーと文字列の dict で返す。なければ None。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    meta: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        kv = KEY_RE.match(line)
        if kv:
            meta[kv.group(1)] = _fm_unquote(kv.group(2).strip())
    return meta


def read_manifest(root: Path) -> dict:
    """`.claude-plugin/plugin.json` を dict で返す。読めなければ空の dict。"""
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_name(root: Path) -> str:
    """`.claude-plugin/plugin.json` の name を返す。読めなければ既定の名前を返す。"""
    name = read_manifest(root).get("name")
    return name if isinstance(name, str) and name else DEFAULT_PLUGIN_NAME


def sort_key(name: str) -> tuple[int, str]:
    if name in PREFERRED_ORDER:
        return (PREFERRED_ORDER.index(name), name)
    return (len(PREFERRED_ORDER), name)


def collect_commands(root: Path, plugin: str) -> tuple[list[dict], list[str]]:
    """`<root>/commands/*.md` を読み、(コマンドの一覧, 警告の一覧) を返す。

    一覧の各項目は name、command、description、argument_hint、argument_description を持つ。
    argument_hint と argument_description は、frontmatter にないときは None である。
    """
    commands: list[dict] = []
    warnings: list[str] = []
    commands_dir = root / "commands"
    if not commands_dir.is_dir():
        warnings.append(f"コマンドのディレクトリがない: {commands_dir}")
        return commands, warnings
    for path in sorted(commands_dir.glob("*.md")):
        name = path.stem
        meta = parse_command_frontmatter(path.read_text(encoding="utf-8"))
        if meta is None:
            warnings.append(f"{path.name}: frontmatter がないので飛ばした")
            continue
        description = meta.get("description", "").strip()
        if not description:
            warnings.append(f"{path.name}: description がないので飛ばした")
            continue
        argument_hint = meta.get("argument-hint", "").strip() or None
        argument_description = meta.get("argument-description", "").strip() or None
        if argument_hint and not argument_description:
            warnings.append(f"{path.name}: argument-hint はあるが argument-description がない")
        commands.append({
            "name": name,
            "command": f"/{plugin}:{name}",
            "description": description,
            "argument_hint": argument_hint,
            "argument_description": argument_description,
        })
    commands.sort(key=lambda c: sort_key(c["name"]))
    return commands, warnings


def code_span(text: str) -> str:
    """Markdown のインラインコードにする。中のバッククォートより長い区切りを使う。"""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # 区切りと同じ文字で始まる・終わるときは、間に空白を入れる（CommonMark の規則）。
    if text.startswith("`") or text.endswith("`"):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def argument_text(command: dict) -> str:
    """一覧の 3 行目と、個別のヘルプの「引数」に出す文。"""
    if not command["argument_hint"]:
        return NO_ARGUMENTS
    return command["argument_description"] or MISSING_ARGUMENT_DESCRIPTION


def render(commands: list[dict]) -> str:
    """コマンドごとに 3 行の Markdown の箇条書きにする。末尾は改行で終わる。

    コマンド名は太字のインラインコード、引数の書式はインラインコードにする。
    `<質問>` のような山かっこや `|` が、HTML のタグや表の区切りとして解釈されないようにするためである。
    """
    lines: list[str] = []
    for c in commands:
        head = f"- **{code_span(c['command'])}**"
        if c["argument_hint"]:
            head = f"{head} {code_span(c['argument_hint'])}"
        lines.append(head)
        lines.append(f"  - {c['description']}")
        lines.append(f"  - {argument_text(c)}")
    return "\n".join(lines) + ("\n" if lines else "")


# ---- 個別のヘルプ: 名前 ----

def normalize_command_name(raw: str, plugin: str) -> str:
    """利用者が書いたコマンド名を、`commands/` のファイル名の形（`query`）にする。

    前後の空白を除き、小文字にし、先頭の `/` を 1 つと `<plugin>:` を除く。
    名前を 1 つにできないとき（空白を含む、空になる、ほかのプラグインの名前）は HelpError を上げる。
    名前からパスは作らない。照合は collect_commands() の一覧の中だけで行う。
    """
    name = raw.strip()
    if any(ch.isspace() for ch in name):
        raise HelpError(f"コマンド名は 1 つだけ指定する: {name}")
    name = name.lower()
    if name.startswith("/"):
        name = name[1:]
    prefix = f"{plugin.lower()}:"
    if name.startswith(prefix):
        name = name[len(prefix):]
    if not name:
        raise HelpError(f"コマンド名がない: {raw.strip()}")
    if ":" in name:
        raise HelpError(f"/{plugin}: のコマンドだけを扱う: {raw.strip()}")
    return name


def find_command(commands: list[dict], name: str) -> dict | None:
    """正規化した名前に当たるコマンドを返す。大文字と小文字は区別しない。"""
    for c in commands:
        if c["name"].lower() == name:
            return c
    return None


# ---- 個別のヘルプ: 文書の節 ----

def fenced_lines(lines: list[str]) -> list[bool]:
    """各行が最上位のコードフェンスの中（開きと閉じの行を含む）かどうかを返す。"""
    flags: list[bool] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        if fence is None:
            m = FENCE_OPEN_RE.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
                flags.append(True)
            else:
                flags.append(False)
            continue
        flags.append(True)
        m = FENCE_CLOSE_RE.match(line)
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
            fence = None
    return flags


def new_parser():
    """同梱の Markdown のパーサー（lint と閲覧と同じ設定）。一覧のときに読み込まないよう、使うときに読み込む。"""
    import wiki_pages
    return wiki_pages.new_parser()


def find_doc_section(text: str, plugin: str, name: str) -> tuple[Section | None, list[str]]:
    """`docs/commands.md` の本文から、`` ## `/<plugin>:<name> ...` `` の節を返す。

    見出しは同梱の Markdown のパーサーで読む（コードフェンスの中の `##`、閉じの `#` の並び、Setext の見出しも
    パーサーのとおり）。節は見出しの次の行から、次の `#` か `##` の見出しの手前まで。見出しの行は含めず、
    前後の空行を除く。行は LF で分ける（CRLF は LF にしてから渡す）。節がなければ None。戻り値は (節, 警告の一覧)。
    """
    lines = text.split("\n")
    title_re = re.compile(r"^`/" + re.escape(plugin) + ":" + re.escape(name) + r"(?:[ \t][^`]*)?`$")
    tokens = new_parser().parse(text, {})
    # (見出しの開始の行, 次の行, 見出しの文字) を、最上位の h1 と h2 について並べる。
    headings = [(t.map[0], t.map[1], tokens[i + 1].content.strip())
                for i, t in enumerate(tokens)
                if t.type == "heading_open" and t.level == 0 and t.tag in ("h1", "h2") and t.map]
    matches = [k for k, (_, _, title) in enumerate(headings) if title_re.match(title)]
    if not matches:
        return None, []
    warnings: list[str] = []
    if len(matches) > 1:
        warnings.append(f"{COMMANDS_DOC.as_posix()}: /{plugin}:{name} の節が {len(matches)} つある。最初の節を使う")
    k = matches[0]
    start = headings[k][1]
    end = headings[k + 1][0] if k + 1 < len(headings) else len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return Section("\n".join(lines[start:end]), start, end), warnings


# ---- 個別のヘルプ: リンクの書き換え ----

def docs_base_url(root: Path) -> tuple[str | None, list[str]]:
    """GitHub の、導入した版のタグの木の URL（`https://github.com/<owner>/<repo>/blob/v<version>`）を返す。

    `plugin.json` の repository が GitHub の URL でないか、version が版の番号でなければ None と警告を返す。
    """
    manifest = read_manifest(root)
    repository, version = manifest.get("repository"), manifest.get("version")
    m = GITHUB_REPOSITORY_RE.match(repository) if isinstance(repository, str) else None
    if not m:
        return None, [f"plugin.json の repository が GitHub の URL でないので、リンクを書き換えない: {repository!r}"]
    if not (isinstance(version, str) and VERSION_RE.match(version)):
        return None, [f"plugin.json の version が版の番号でないので、リンクを書き換えない: {version!r}"]
    return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/v{version}", []


def link_url(dest: str, root: Path, docs_dir: Path, base: str, image: bool) -> tuple[str, Path] | None:
    """リンクの宛先（エスケープと文字参照を戻したもの。百分率符号化は残っていてよい）を、GitHub の URL にする。

    戻り値は (URL, 指すファイル)。空の宛先、スキームのある宛先、`/` で始まる宛先は None（変えない）。
    `#frag` だけの宛先は `docs/commands.md` を指す。プラグインのルートの外を指す宛先は ValueError。
    リンクのフラグメントは残す（GitHub の見出しのアンカー）。画像は `?raw=true` を付けて、画像そのものを指す。
    `#` で分けてから、パスとフラグメントのそれぞれの百分率符号化を戻す（`%23` を区切りと読まない）。
    """
    if not dest or SCHEME_RE.match(dest) or dest.startswith("/"):
        return None
    path_part, sep, frag = dest.partition("#")
    path_part, frag = unquote(path_part), unquote(frag)
    file = (docs_dir / path_part).resolve() if path_part else (docs_dir / COMMANDS_DOC.name).resolve()
    rel = file.relative_to(root)  # ルートの外なら ValueError
    url = f"{base}/{quote(rel.as_posix(), safe='/')}"
    if image:
        url += "?raw=true"
    elif sep and frag:
        url += "#" + quote(frag, safe="")
    return url, file


def raw_destination(raw: str) -> str:
    """Markdown に書いた宛先から、バックスラッシュのエスケープと文字参照を戻す（百分率符号化は残す）。"""
    return html.unescape(BACKSLASH_ESCAPE_RE.sub(r"\1", raw))


def mask_code_spans(line: str) -> str:
    """1 行の中のインラインコードの中身を、リンクの記号にならない文字で伏せた、同じ長さの写しを返す。

    n 個のバッククォートの並びで始まり、同じ行の中の同じ n 個の並びで閉じる区間をコードとする。
    閉じる並びがないか、開きのバッククォートがバックスラッシュでエスケープされていれば、ただの文字である。
    行をまたぐインラインコードは読まない。読み違えは、書き換えのあとの照らし合わせで見つかる。
    """
    out = list(line)
    i, n = 0, len(line)
    while i < n:
        if line[i] != "`":
            i += 1
            continue
        backslashes = 0
        while i - backslashes - 1 >= 0 and line[i - backslashes - 1] == "\\":
            backslashes += 1
        if backslashes % 2 == 1:
            i += 1
            continue
        j = i
        while j < n and line[j] == "`":
            j += 1
        run = j - i
        k, close = j, -1
        while k < n:
            if line[k] != "`":
                k += 1
                continue
            m = k
            while m < n and line[m] == "`":
                m += 1
            if m - k == run:
                close = k
                break
            k = m
        if close < 0:
            i = j
            continue
        for p in range(j, close):
            out[p] = "x"
        i = close + run
    return "".join(out)


def rewrite_links(body: str, root: Path, docs_dir: Path, base: str) -> tuple[str, list[str]]:
    """節の本文の相対リンクを、GitHub の URL に書き換える。戻り値は (本文, 警告の一覧)。

    最上位のコードフェンスの中と、インラインコードの中は変えない。
    """
    lines = body.split("\n")
    warnings: list[str] = []
    out: list[str] = []
    for line, fenced in zip(lines, fenced_lines(lines)):
        if fenced:
            out.append(line)
            continue
        masked = mask_code_spans(line)
        pieces: list[str] = []
        last = 0
        for m in LINK_RE.finditer(masked):
            raw = line[m.start(2):m.end(2)]
            try:
                resolved = link_url(raw_destination(raw), root, docs_dir, base, m.group(1).startswith("!"))
            except ValueError:
                warnings.append(f"{COMMANDS_DOC.as_posix()}: リンクがプラグインの外を指すので、書き換えない: {raw}")
                continue
            if resolved is None:
                continue
            url, file = resolved
            if not file.exists():
                warnings.append(f"{COMMANDS_DOC.as_posix()}: リンクの行き先がない: {raw}")
            pieces.append(line[last:m.start(2)])
            pieces.append(url)
            last = m.end(2)
        pieces.append(line[last:])
        out.append("".join(pieces))
    return "\n".join(out), warnings


# ---- 個別のヘルプ: 書き換えの照らし合わせ ----

def section_tokens(tokens: list, section: Section) -> list:
    """文書の全体を読んだトークンの列から、節の本文の行にある最上位のブロックのトークンを返す。"""
    picked, inside = [], False
    for t in tokens:
        if t.level == 0 and t.map is not None:
            inside = section.start <= t.map[0] < section.end
        if inside:
            picked.append(t)
    return picked


def token_shape(tokens: list) -> tuple[list, list]:
    """トークンの木を、(リンクの宛先を除いた形の並び, リンクと画像の宛先の並び) にする。"""
    shape: list = []
    dests: list = []

    def walk(items) -> None:
        for t in items:
            attrs = dict(t.attrs or {})
            if t.type == "link_open":
                dests.append(("link", attrs.pop("href", "")))
            elif t.type == "image":
                dests.append(("image", attrs.pop("src", "")))
            # inline の content は元の文字列なので、宛先の違いを含む。中身は children で比べる。
            content = "" if t.type == "inline" else t.content
            shape.append((t.type, t.tag, t.nesting, t.info, t.markup, content, sorted(attrs.items())))
            if t.children:
                walk(t.children)

    walk(tokens)
    return shape, dests


def verify_rewrite(doc_text: str, section: Section, rewritten: str, root: Path, docs_dir: Path, base: str) -> list[str]:
    """書き換えの前後を、同梱の Markdown のパーサーで読み、リンクの宛先のほかが同じかを確かめる。

    前は文書の全体を読んで節の行のトークンを取り（節の外にある参照リンクの定義も効く）、後は書き換えた本文だけを
    読む（チャットに出す形）。形が違うか、どれかの宛先が link_url() の答えと違えば、理由の一覧を返す。
    """
    parser = new_parser()
    before_shape, before_dests = token_shape(section_tokens(parser.parse(doc_text, {}), section))
    after_shape, after_dests = token_shape(parser.parse(rewritten, {}))
    if before_shape != after_shape or len(before_dests) != len(after_dests):
        return [f"{COMMANDS_DOC.as_posix()}: リンクの書き換えで、リンクの宛先のほかが変わった"
                "（参照の形のリンク、リストや引用の中のコードフェンスなど、書き換えが読めない書き方がある）"]
    problems: list[str] = []
    for (kind, before), (_, after) in zip(before_dests, after_dests):
        try:
            # パーサーの宛先は、エスケープと文字参照を戻し、百分率符号化したもの。
            resolved = link_url(before, root, docs_dir, base, kind == "image")
        except ValueError:
            resolved = None
        expected = resolved[0] if resolved else before
        if unquote(after) != unquote(expected):
            problems.append(f"{COMMANDS_DOC.as_posix()}: リンクを書き換えられなかった: {unquote(before)}")
    return problems


def command_details(root: Path, plugin: str, name: str) -> tuple[str | None, list[str]]:
    """`docs/commands.md` の節の本文を、リンクを書き換えて返す。戻り値は (本文か None, 警告の一覧)。

    書き換えを照らし合わせて食い違えば、書き換えずに節をそのまま返す（コードの例などを壊さないため）。
    """
    doc = root / COMMANDS_DOC
    try:
        text = doc.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return None, [f"コマンドの文書がない: {doc}"]
    section, warnings = find_doc_section(text, plugin, name)
    if section is None:
        return None, warnings + [f"{COMMANDS_DOC.as_posix()} に /{plugin}:{name} の節がない"]
    base, base_warnings = docs_base_url(root)
    if base is None:
        return section.body, warnings + base_warnings
    docs_dir = doc.parent.resolve()
    body, link_warnings = rewrite_links(section.body, root, docs_dir, base)
    problems = verify_rewrite(text, section, body, root, docs_dir, base)
    if problems:
        return section.body, warnings + link_warnings + problems + ["リンクを書き換えずに、節をそのまま出す"]
    return body, warnings + link_warnings


def render_detail(command: dict, details: str | None) -> str:
    """1 つのコマンドの詳しいヘルプを Markdown にする。末尾は改行で終わる。"""
    head = f"### {code_span(command['command'])}"
    if command["argument_hint"]:
        head = f"{head} {code_span(command['argument_hint'])}"
    parts = [head, command["description"], f"**引数**: {argument_text(command)}"]
    if details:
        parts += ["**詳細**", details]
    return "\n\n".join(parts) + "\n"


def default_plugin_root() -> Path:
    # scripts → info-hive → skills → プラグインのルート
    return Path(__file__).resolve().parents[3]


def print_names(commands: list[dict]) -> None:
    print("使えるコマンド: " + ", ".join(c["name"] for c in commands), file=sys.stderr)


def show_command(root: Path, plugin: str, commands: list[dict], raw: str, as_json: bool) -> int:
    """`--command` の処理。見つからなければ終了コード 1。"""
    try:
        name = normalize_command_name(raw, plugin)
    except HelpError as e:
        print(str(e), file=sys.stderr)
        print_names(commands)
        return 1
    command = find_command(commands, name)
    if command is None:
        print(f"コマンドが見つからない: {name}", file=sys.stderr)
        close = difflib.get_close_matches(name, [c["name"] for c in commands])
        if close:
            print("近い名前: " + ", ".join(close), file=sys.stderr)
        print_names(commands)
        return 1
    details, warnings = command_details(root, plugin, command["name"])
    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    if as_json:
        print(json.dumps({**command, "details": details}, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_detail(command, details))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="wiki プラグインのコマンドの一覧か、1 つのコマンドの詳しいヘルプを表示する。")
    parser.add_argument("plugin_root", nargs="?", type=Path, default=None,
                        help="プラグインのルート（commands/ と .claude-plugin/ がある場所）。省くとスクリプトの置き場所から求める")
    parser.add_argument("--json", action="store_true", help="Markdown ではなく JSON で出力する")
    parser.add_argument("--command", metavar="NAME", default=None,
                        help="このコマンドの詳しいヘルプを出す（query、/wiki:query、wiki:query のどれでもよい）。"
                             "省くか空なら一覧を出す。- で始まる値は --command=VALUE の形で渡す")
    localize_argparse(parser)
    args = parser.parse_args(argv)

    root = (args.plugin_root or default_plugin_root()).resolve()
    plugin = plugin_name(root)
    commands, warnings = collect_commands(root, plugin)
    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    if not commands:
        print(f"コマンドが見つからない: {root / 'commands'}", file=sys.stderr)
        return 1
    if args.command is not None and args.command.strip():
        return show_command(root, plugin, commands, args.command, args.json)
    if args.json:
        print(json.dumps(commands, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render(commands))
    return 0


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    sys.exit(main())
