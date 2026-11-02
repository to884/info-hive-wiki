#!/usr/bin/env python3
"""
init_wiki.py — 指定したファイルシステム上の場所に LLM Wiki を初期化、またはアップグレードする。

通常の初期化では、ディレクトリ構成を作り、SCHEMA.md、index.md、log.md、ページのテンプレート、
任意のグラフ層（graph/ontology.yaml、graph/README.md、graph/.gitignore）のテンプレートを置く。
冪等なので、再実行しても既存のファイルは上書きしない。
どちらのモードでも、そのあと `uv` を通じて、バージョンを固定したローカルの実行環境、
モデルのキャッシュ、パースキャッシュ、現在の wiki の全セクションのベクトルをインストールし、検証する。

`--upgrade` モードは、古いバージョンのプラグインで初期化した wiki 向けである。
同じく冪等にファイルを作成したあと、既存の SCHEMA.md に新しいバージョンで加わった
セクションがあるかを調べ、手作業でマージすべき内容をわかりやすく表示する。
SCHEMA.md は決して上書きしない。スキーマはユーザーと一緒に改訂していくものだからである。

使い方:
    python init_wiki.py <base-dir> [--wiki-dir wiki] [--raw-dir raw] [--upgrade]

例:
    python init_wiki.py .
    python init_wiki.py . --upgrade
    python init_wiki.py ~/research --wiki-dir kb --raw-dir sources
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import date


SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_ROOT / "assets"


SUBDIRS = ["sources", "entities", "concepts", "synthesis", "graph"]

# --upgrade が、特定のプラグインのバージョンで加わった SCHEMA.md のセクションを
# 検出するためのマーカー。各項目は (heading_marker, version_label,
# template_anchor, blurb) である。
SCHEMA_SECTION_MARKERS = [
    {
        "marker": "## Skill evolution",
        "version": "3.2.0",
        "anchor": "## Skill evolution",
        "label": "Optional experience capture and tested skill evolution",
    },
    {
        "marker": "## Optional graph metadata",
        "version": "0.3.0",
        "anchor": "## Optional graph metadata",
        "label": "Optional graph metadata (Frontmatter section)",
    },
    {
        "marker": "## Graph layer",
        "version": "0.3.0",
        "anchor": "## Graph layer",
        "label": "Graph layer (canonical-vs-generated artifact policy)",
    },
    {
        "marker": "Graph lint + extract",
        "version": "0.3.0",
        "anchor": "- Graph lint + extract: after every ingest that adds typed `graph.relationships`.",
        "label": "Graph lint + extract cadence (Lint cadence section)",
    },
    {
        "marker": "## Retrieval",
        "version": "2.0.0",
        "anchor": "## Retrieval",
        "label": "Retrieval (section search and cache)",
    },
    {
        "marker": "- Semantic backend: local FastEmbed + sqlite-vec",
        "version": "3.0.0",
        "anchor": "- Semantic backend: local FastEmbed + sqlite-vec",
        "label": "Local semantic retrieval (FastEmbed + sqlite-vec)",
    },
]


def copy_template(src: Path, dst: Path, substitutions: dict | None = None) -> bool:
    """テンプレートのファイルを dst にコピーする。ファイルを作成したら True、すでにあれば False を返す。"""
    if dst.exists():
        return False
    text = src.read_text()
    if substitutions:
        for key, value in substitutions.items():
            text = text.replace(key, value)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    return True


def detect_schema_gaps(schema_path: Path) -> list[dict]:
    """SCHEMA_SECTION_MARKERS のうち、ユーザーの SCHEMA.md にない項目を返す。"""
    if not schema_path.exists():
        return []
    text = schema_path.read_text(encoding="utf-8")

    def marker_present(marker: str) -> bool:
        if marker.startswith("## "):
            return any(line.strip() == marker for line in text.splitlines())
        return marker in text

    return [entry for entry in SCHEMA_SECTION_MARKERS if not marker_present(entry["marker"])]


def print_schema_upgrade_guidance(schema_path: Path, gaps: list[dict]) -> None:
    template_path = TEMPLATES / "SCHEMA.md.template"
    print()
    print("=" * 64)
    print(f"Upgrade required: {schema_path}")
    print("=" * 64)
    print(
        "Your SCHEMA.md predates one or more sections or policy markers\n"
        "introduced by newer plugin versions. Merge the items below by hand.\n"
        "SCHEMA.md is co-evolved with you — this script never overwrites it."
    )
    print()
    print("Missing sections:")
    for m in gaps:
        print(f"  - [{m['version']}] {m['label']}")
    print()
    print(f"Reference template: {template_path}")
    print(
        "Diff your SCHEMA.md against the template and copy the missing\n"
        "sections in. Or run /wiki:upgrade and Claude will propose the edits\n"
        "interactively (one section at a time, never silent)."
    )


def install_runtime(wiki: Path) -> None:
    """バージョンを固定した依存パッケージをインストールし、モデルをキャッシュし、wiki のインデックスを同期する。"""
    uv = shutil.which("uv")
    if not uv:
        print(
            "Error: uv is required to install the pinned LLM Wiki runtime. "
            "Install it from https://docs.astral.sh/uv/getting-started/installation/ "
            "and rerun this command.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    setup_script = Path(__file__).resolve().with_name("setup_wiki.py")
    print()
    print("Installing and verifying the local retrieval runtime...", flush=True)
    try:
        subprocess.run(
            [uv, "run", "--script", str(setup_script), "--wiki", str(wiki), "--cache"],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"Error: local retrieval setup failed with exit code {exc.returncode}.",
            file=sys.stderr,
        )
        raise SystemExit(exc.returncode) from exc


def init_wiki(project_root: Path, wiki_dir: str, raw_dir: str, upgrade: bool = False) -> None:
    project_root = project_root.resolve()
    if not project_root.exists():
        print(f"Error: base directory does not exist: {project_root}", file=sys.stderr)
        sys.exit(1)

    wiki = project_root / wiki_dir
    raw = project_root / raw_dir

    mode = "Upgrading" if upgrade else "Initializing"
    print(f"{mode} LLM Wiki in: {project_root}")
    print(f"  Wiki directory: {wiki}")
    print(f"  Raw directory:  {raw}")
    print()

    created = []
    skipped = []

    # wiki のサブディレクトリを作る
    for subdir in SUBDIRS:
        d = wiki / subdir
        if not d.exists():
            d.mkdir(parents=True)
            created.append(f"{wiki_dir}/{subdir}/")
        else:
            skipped.append(f"{wiki_dir}/{subdir}/")

    # raw と raw/assets を作る
    for d, label in [(raw, raw_dir), (raw / "assets", f"{raw_dir}/assets")]:
        if not d.exists():
            d.mkdir(parents=True)
            created.append(f"{label}/")
        else:
            skipped.append(f"{label}/")

    # テンプレートをコピーする
    template_map = [
        ("SCHEMA.md.template", wiki / "SCHEMA.md"),
        ("index.md.template", wiki / "index.md"),
        ("log.md.template", wiki / "log.md"),
        ("page.md.template", wiki / ".page-template.md"),
        ("experience.json.template", wiki / ".experience-template.json"),
        ("pattern.md.template", wiki / ".pattern-template.md"),
        ("evolution_README.md.template", wiki / ".evolution" / "README.md"),
        ("ontology.yaml.template", wiki / "graph" / "ontology.yaml"),
        ("graph_README.md.template", wiki / "graph" / "README.md"),
        ("graph_gitignore.template", wiki / "graph" / ".gitignore"),
        ("wiki-cache_gitignore.template", wiki / ".wiki-cache" / ".gitignore"),
    ]
    for src_name, dst in template_map:
        src = TEMPLATES / src_name
        if not src.exists():
            print(f"Warning: template missing: {src}", file=sys.stderr)
            continue
        if copy_template(src, dst):
            created.append(str(dst.relative_to(project_root)))
        else:
            skipped.append(str(dst.relative_to(project_root)))

    # 結果を報告する
    if created:
        print("Created:")
        for path in created:
            print(f"  + {path}")
    if skipped:
        print("Already existed (skipped):")
        for path in skipped:
            print(f"  = {path}")

    install_runtime(wiki)

    if upgrade:
        gaps = detect_schema_gaps(wiki / "SCHEMA.md")
        if gaps:
            print_schema_upgrade_guidance(wiki / "SCHEMA.md", gaps)
        else:
            print()
            print("SCHEMA.md is up to date with the current template — no manual merge needed.")
        return

    print()
    print("Next steps:")
    print(f"  1. Read {wiki_dir}/SCHEMA.md and customize it for your domain.")
    print(f"  2. (Optional) Edit {wiki_dir}/graph/ontology.yaml to add domain-specific predicates.")
    print(f"  3. Drop your first source into {raw_dir}/.")
    print(f"  4. Ask Claude to ingest it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_root", type=Path, metavar="BASE_DIR", help="Base directory containing the wiki and raw-source paths (for example a project root or home directory).")
    parser.add_argument("--wiki-dir", default="wiki", help="Name of the wiki subdirectory (default: wiki).")
    parser.add_argument("--raw-dir", default="raw", help="Name of the raw sources subdirectory (default: raw).")
    parser.add_argument("--upgrade", action="store_true",
                        help="Upgrade an existing wiki: add missing files idempotently and surface SCHEMA.md sections to merge by hand.")
    args = parser.parse_args()
    init_wiki(args.project_root, args.wiki_dir, args.raw_dir, upgrade=args.upgrade)


if __name__ == "__main__":
    main()
