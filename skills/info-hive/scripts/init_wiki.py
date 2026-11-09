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

<base-dir> がまだなく、その親のディレクトリがあれば、初期化では <base-dir> を作る（~/info-hive の初回など）。
親もないときは、パスの誤りとみなして止める。アップグレードでは <base-dir> が必要である。

例:
    python init_wiki.py .
    python init_wiki.py . --upgrade
    python init_wiki.py ~/info-hive
    python init_wiki.py ~/research --wiki-dir kb --raw-dir sources
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import date

# スクリプトとして実行したときも、テストから importlib で読み込んだときも、
# 同じディレクトリの wiki_text を import できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wiki_text import display_width, ljust_display, localize_argparse  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_ROOT / "assets"


SUBDIRS = ["sources", "entities", "concepts", "synthesis", "graph", "assets"]

# --upgrade が、特定のプラグインのバージョンで加わった SCHEMA.md のセクションを
# 検出するためのマーカー。各項目は (heading_marker, version_label,
# template_anchor, blurb) である。marker と anchor はテンプレートの英語の見出しや行と一致させるので訳さない。
# label は利用者に見せる説明なので日本語で書く。
SCHEMA_SECTION_MARKERS = [
    {
        "marker": "## Claim destinations",
        "version": "0.8.0",
        "anchor": "## Claim destinations",
        "label": "ソースの要約ページの主張と行き先の表",
    },
    {
        "marker": "## Skill evolution",
        "version": "3.2.0",
        "anchor": "## Skill evolution",
        "label": "任意の経験の記録と、テストで確かめるスキルの改良",
    },
    {
        "marker": "## Optional graph metadata",
        "version": "0.3.0",
        "anchor": "## Optional graph metadata",
        "label": "任意のグラフのメタデータ（frontmatter の graph: キー）",
    },
    {
        "marker": "## Graph layer",
        "version": "0.3.0",
        "anchor": "## Graph layer",
        "label": "グラフ層（正本と生成物の扱いの方針）",
    },
    {
        "marker": "Graph lint + extract",
        "version": "0.3.0",
        "anchor": "- Graph lint + extract:",
        "label": "グラフの lint と抽出の頻度（lint の頻度の節）",
    },
    {
        "marker": "## Retrieval",
        "version": "2.0.0",
        "anchor": "## Retrieval",
        "label": "検索（セクション検索とキャッシュ）",
    },
    {
        "marker": "- Semantic backend: local FastEmbed + sqlite-vec",
        "version": "3.0.0",
        "anchor": "- Semantic backend: local FastEmbed + sqlite-vec",
        "label": "ローカルの埋め込み検索（FastEmbed と sqlite-vec）",
    },
]


def copy_template(src: Path, dst: Path, substitutions: dict | None = None) -> bool:
    """テンプレートのファイルを dst にコピーする。ファイルを作成したら True、すでにあれば False を返す。"""
    if dst.exists():
        return False
    text = src.read_text(encoding="utf-8")
    if substitutions:
        for key, value in substitutions.items():
            text = text.replace(key, value)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    return True


def detect_schema_gaps(schema_path: Path) -> list[dict]:
    """SCHEMA_SECTION_MARKERS のうち、ユーザーの SCHEMA.md にない項目を返す。"""
    if not schema_path.exists():
        return []
    text = schema_path.read_text(encoding="utf-8-sig")

    def marker_present(marker: str) -> bool:
        if marker.startswith("## "):
            return any(line.strip() == marker for line in text.splitlines())
        return marker in text

    return [entry for entry in SCHEMA_SECTION_MARKERS if not marker_present(entry["marker"])]


def print_schema_upgrade_guidance(schema_path: Path, gaps: list[dict]) -> None:
    template_path = TEMPLATES / "SCHEMA.md.template"
    print()
    print("=" * 64)
    # commands/upgrade.md がこの行を目印に読むので、英語のまま残す。
    print(f"Upgrade required: {schema_path}")
    print("=" * 64)
    print(
        "この SCHEMA.md には、新しいバージョンのプラグインで加わったセクションや方針のマーカーが欠けている。\n"
        "下の項目を手作業でマージする。\n"
        "SCHEMA.md はユーザーと一緒に改訂していくものなので、このスクリプトは上書きしない。"
    )
    print()
    print("欠けているセクション:")
    for m in gaps:
        print(f"  - [{m['version']}] {m['label']}")
    print()
    print(f"参照するテンプレート: {template_path}")
    print(
        "SCHEMA.md をテンプレートと比べ、欠けているセクションを写す。\n"
        "/wiki:upgrade を実行すれば、Claude が編集を対話しながら提案する\n"
        "（1 セクションずつ提案し、黙って書き換えることはない）。"
    )


def install_runtime(wiki: Path) -> None:
    """バージョンを固定した依存パッケージをインストールし、モデルをキャッシュし、wiki のインデックスを同期する。"""
    uv = shutil.which("uv")
    if not uv:
        print(
            "エラー: バージョンを固定した LLM Wiki の実行環境を入れるには uv が必要である。"
            "https://docs.astral.sh/uv/getting-started/installation/ の手順で uv をインストールし、"
            "このコマンドを実行し直す。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    setup_script = Path(__file__).resolve().with_name("setup_wiki.py")
    print()
    print("ローカルの検索の実行環境をインストールして検証する……", flush=True)
    try:
        subprocess.run(
            [uv, "run", "--script", str(setup_script), "--wiki", str(wiki), "--cache"],
            check=True,
            # 子の Python も UTF-8 モードで動かし、cp932 の既定に左右されないようにする。
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"エラー: ローカルの検索の実行環境のセットアップに失敗した（終了コード {exc.returncode}）。",
            file=sys.stderr,
        )
        raise SystemExit(exc.returncode) from exc


def init_wiki(project_root: Path, wiki_dir: str, raw_dir: str, upgrade: bool = False) -> None:
    project_root = project_root.resolve()
    created_base = False
    if not project_root.exists():
        # 初回のユーザースコープの構成（~/info-hive など）では、基準のディレクトリ自体がまだない。
        # 親のディレクトリがあれば作る。親もないときはパスの誤りとみなして止める。
        # アップグレードは既存の wiki が対象なので、基準のディレクトリがなければ止める。
        if upgrade or not project_root.parent.is_dir():
            print(f"エラー: 基準のディレクトリがない: {project_root}", file=sys.stderr)
            sys.exit(1)
        project_root.mkdir()
        created_base = True

    wiki = project_root / wiki_dir
    raw = project_root / raw_dir

    action = "アップグレードする" if upgrade else "初期化する"
    print(f"LLM Wiki を{action}: {project_root}")
    labels = [("wiki のディレクトリ:", wiki), ("raw ソースのディレクトリ:", raw)]
    width = max(display_width(label) for label, _ in labels)
    for label, path in labels:
        print(f"  {ljust_display(label, width)} {path}")
    if created_base:
        print(f"  基準のディレクトリがなかったので作った: {project_root}")
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
            print(f"警告: テンプレートがない: {src}", file=sys.stderr)
            continue
        if copy_template(src, dst):
            created.append(str(dst.relative_to(project_root)))
        else:
            skipped.append(str(dst.relative_to(project_root)))

    # 結果を報告する
    if created:
        print("作成した:")
        for path in created:
            print(f"  + {path}")
    if skipped:
        print("すでにあったので飛ばした:")
        for path in skipped:
            print(f"  = {path}")

    install_runtime(wiki)

    if upgrade:
        gaps = detect_schema_gaps(wiki / "SCHEMA.md")
        if gaps:
            print_schema_upgrade_guidance(wiki / "SCHEMA.md", gaps)
        else:
            print()
            print("SCHEMA.md は現在のテンプレートに追いついている。手作業でマージするものはない。")
        return

    print()
    print("次にすること:")
    print(f"  1. {wiki_dir}/SCHEMA.md を読み、扱う分野に合わせて書き換える。")
    print(f"  2. （任意）{wiki_dir}/graph/ontology.yaml に、分野に固有の述語を加える。")
    print(f"  3. 最初のソースを {raw_dir}/ に置く。")
    print("  4. Claude にそのソースの取り込みを頼む。")


def main():
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter))
    parser.add_argument("project_root", type=Path, metavar="BASE_DIR", help="wiki と raw ソースのディレクトリを置く基準のディレクトリ（プロジェクトのルートや ~/info-hive など）。初期化では、なければ作る（親のディレクトリが必要）")
    parser.add_argument("--wiki-dir", default="wiki", help="wiki のサブディレクトリの名前（既定: wiki）")
    parser.add_argument("--raw-dir", default="raw", help="raw ソースのサブディレクトリの名前（既定: raw）")
    parser.add_argument("--upgrade", action="store_true",
                        help="既存の wiki をアップグレードする。足りないファイルを冪等に加え、手作業でマージすべき SCHEMA.md のセクションを示す")
    args = parser.parse_args()
    init_wiki(args.project_root, args.wiki_dir, args.raw_dir, upgrade=args.upgrade)


if __name__ == "__main__":
    # Windows の既定の文字コード（cp932 など）ではなく、UTF-8 で入出力する。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    main()
