#!/usr/bin/env python3
"""
wiki_help.py — wiki プラグインのコマンドの一覧を、説明と引数つきで表示する。

`commands/*.md` の frontmatter から `description`、`argument-hint`、`argument-description` を読み、
コマンドごとに 3 行の Markdown の箇条書きにする。`/wiki:help` から呼び、書式付きで表示する。

    - **`/wiki:init`** `[--user] [--wiki-dir <name>] [--raw-dir <name>]`
      - ユーザースコープの LLM Wiki、またはこのプロジェクトの LLM Wiki を初期化する。
      - `--user` を付けると ...（argument-description の文）
    - **`/wiki:stats`**
      - wiki の規模、構成、リンク密度と、スケーリングのどの閾値に達しているかを表示する。
      - 引数はない。

使い方:
    python wiki_help.py [<plugin-root>] [--json]

<plugin-root> を省くと、このスクリプトの置き場所からプラグインのルートを求める。
"""

import argparse
import json
import re
import sys
from pathlib import Path

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import _fm_unquote, localize_argparse  # noqa: E402


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
# コマンドの frontmatter のキーには `argument-hint` のようにハイフンが入るので、
# ページ用の wiki_text.parse_frontmatter_text（`\w+` のキーだけ）は使わず、ここで読む。
KEY_RE = re.compile(r"^([\w-]+):\s*(.*)$")

# README の表と同じワークフローの順。ここにないコマンドは、名前順で後ろに並べる。
PREFERRED_ORDER = ["init", "ingest", "query", "lint", "stats", "graph", "learn", "evolve", "upgrade", "help"]

DEFAULT_PLUGIN_NAME = "wiki"
NO_ARGUMENTS = "引数はない。"
MISSING_ARGUMENT_DESCRIPTION = "（引数の説明は未記入）"


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


def plugin_name(root: Path) -> str:
    """`.claude-plugin/plugin.json` の name を返す。読めなければ既定の名前を返す。"""
    manifest = root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_PLUGIN_NAME
    name = data.get("name") if isinstance(data, dict) else None
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


def render(commands: list[dict]) -> str:
    """コマンドごとに 3 行の Markdown の箇条書きにする。末尾は改行で終わる。

    コマンド名は太字のインラインコード、引数の書式はインラインコードにする。
    `<質問>` のような山かっこや `|` が、HTML のタグや表の区切りとして解釈されないようにするためである。
    """
    lines: list[str] = []
    for c in commands:
        head = f"- **{code_span(c['command'])}**"
        if c["argument_hint"]:
            lines.append(f"{head} {code_span(c['argument_hint'])}")
            lines.append(f"  - {c['description']}")
            lines.append(f"  - {c['argument_description'] or MISSING_ARGUMENT_DESCRIPTION}")
        else:
            lines.append(head)
            lines.append(f"  - {c['description']}")
            lines.append(f"  - {NO_ARGUMENTS}")
    return "\n".join(lines) + ("\n" if lines else "")


def default_plugin_root() -> Path:
    # scripts → info-hive → skills → プラグインのルート
    return Path(__file__).resolve().parents[3]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="wiki プラグインのコマンドの一覧を、説明と引数つきで表示する。")
    parser.add_argument("plugin_root", nargs="?", type=Path, default=None,
                        help="プラグインのルート（commands/ と .claude-plugin/ がある場所）。省くとスクリプトの置き場所から求める")
    parser.add_argument("--json", action="store_true", help="箇条書きではなく JSON の配列で出力する")
    localize_argparse(parser)
    args = parser.parse_args(argv)

    root = (args.plugin_root or default_plugin_root()).resolve()
    commands, warnings = collect_commands(root, plugin_name(root))
    for w in warnings:
        print(f"警告: {w}", file=sys.stderr)
    if not commands:
        print(f"コマンドが見つからない: {root / 'commands'}", file=sys.stderr)
        return 1
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
