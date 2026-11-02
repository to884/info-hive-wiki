#!/usr/bin/env python3
"""
check_encoding.py — テキストの読み書きで encoding の付け忘れを見つける静的検査。

Windows の Python は、encoding を省いた open() や subprocess の text=True で、
システムロケールの文字コード（日本語の環境では cp932、GitHub の Windows ランナーでは cp1252）を使う。
UTF-8 で書いた日本語のページやテンプレートは、それでは読み書きできない。
この検査は ast でソースを読み、次の呼び出しで encoding が渡されていない箇所を一覧にして、
1 か所でもあれば終了コード 1 で終わる。標準ライブラリだけで動く。

- テキストモードの open()、io.open()、Path.open()、os.fdopen()
- Path.read_text()、Path.write_text()
- io.TextIOWrapper()
- tempfile の TemporaryFile、NamedTemporaryFile、SpooledTemporaryFile をテキストモードで開くもの
- subprocess の run、Popen、call、check_call、check_output に text=True か universal_newlines=True を
  渡しているもの

バイナリモード（'rb'、'wb' など）と read_bytes()、write_bytes() は対象外である。
既定の文字コードでよいと判断した行には、`# encoding: default-ok` と書けば検査から外せる。
呼び出しが複数行にわたる場合は、そのどの行に書いてもよい。

使い方:
    python eval/check_encoding.py            # リポジトリの既定の範囲を検査する
    python eval/check_encoding.py a.py b.py  # 指定したファイルだけを検査する
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPT_OUT = "# encoding: default-ok"

# 検査の範囲。scripts は直下だけ、eval は下の階層まで見る。
DEFAULT_GLOBS = [
    ("skills/info-hive/scripts", "*.py"),
    ("eval", "**/*.py"),
]

SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}
TEMPFILE_FUNCS = {"TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile"}

# x.open(...) の x がこれらのモジュールなら、Path.open() ではないので対象から外す。
# gzip.open などはバイナリが既定で、os.open はファイル記述子を返す。
NON_PATH_OPEN_MODULES = {
    "os", "gzip", "bz2", "lzma", "tarfile", "zipfile", "webbrowser",
    "shelve", "dbm", "sqlite3", "wave", "aifc", "sunau", "codecs",
}


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _has_star_kwargs(call: ast.Call) -> bool:
    """**kwargs を渡していれば、encoding が含まれるかは静的にはわからないので対象から外す。"""
    return any(kw.arg is None for kw in call.keywords)


def _has_starargs(call: ast.Call) -> bool:
    return any(isinstance(a, ast.Starred) for a in call.args)


def _has_encoding(call: ast.Call, position: int) -> bool:
    """encoding がキーワードか、position 番目の位置引数で渡されていれば True を返す。"""
    if _kw(call, "encoding") is not None:
        return True
    if _has_star_kwargs(call) or _has_starargs(call):
        return True
    return len(call.args) > position


def _mode_arg(call: ast.Call, position: int) -> ast.expr | None:
    kw = _kw(call, "mode")
    if kw is not None:
        return kw.value
    if len(call.args) > position:
        return call.args[position]
    return None


def _is_text_mode(mode: ast.expr | None, default_text: bool = True) -> bool:
    """mode がテキストモードなら True を返す。定数でない mode はテキストとみなす（安全側）。"""
    if mode is None:
        return default_text
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "b" not in mode.value
    return True


def _is_true(node: ast.expr) -> bool:
    """定数の False と None 以外はテキストモードになりうるとみなす。"""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    return True


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


class _Imports(ast.NodeVisitor):
    """from subprocess import run のように、名前だけで呼べるようにした関数を集める。"""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module in {"subprocess", "tempfile", "io", "os"}:
            for alias in node.names:
                self.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"


def check_call(call: ast.Call, imported: dict[str, str]) -> str | None:
    """問題があれば説明を、なければ None を返す。"""
    func = call.func
    name = _dotted(func) or ""
    if isinstance(func, ast.Name) and func.id in imported:
        name = imported[func.id]

    # 組み込みの open() と io.open()。open(file, mode, buffering, encoding, ...)
    if name in {"open", "io.open"}:
        if _is_text_mode(_mode_arg(call, 1)) and not _has_encoding(call, 3):
            return f"{name}() をテキストモードで encoding なしに呼んでいる"
        return None

    # os.fdopen(fd, mode, buffering, encoding, ...)
    if name == "os.fdopen":
        if _is_text_mode(_mode_arg(call, 1)) and not _has_encoding(call, 3):
            return "os.fdopen() をテキストモードで encoding なしに呼んでいる"
        return None

    # io.TextIOWrapper(buffer, encoding, ...)
    if name == "io.TextIOWrapper":
        if not _has_encoding(call, 1):
            return "io.TextIOWrapper() に encoding がない"
        return None

    # tempfile.TemporaryFile(mode='w+b', buffering, encoding, ...) は mode を省くとバイナリになる。
    if name.startswith("tempfile.") and name.split(".", 1)[1] in TEMPFILE_FUNCS:
        position = 3 if name.endswith("SpooledTemporaryFile") else 2
        if _is_text_mode(_mode_arg(call, 1 if position == 3 else 0), default_text=False) and not _has_encoding(call, position):
            return f"{name}() をテキストモードで encoding なしに呼んでいる"
        return None

    # subprocess の text=True / universal_newlines=True。
    if name.startswith("subprocess.") and name.split(".", 1)[1] in SUBPROCESS_FUNCS:
        text = _kw(call, "text") or _kw(call, "universal_newlines")
        if text is not None and _is_true(text.value) and _kw(call, "encoding") is None and not _has_star_kwargs(call):
            return f"{name}() に text=True があり、encoding がない"
        return None

    if isinstance(func, ast.Attribute):
        owner = _dotted(func.value)
        # Path.read_text(encoding, errors)
        if func.attr == "read_text":
            if not _has_encoding(call, 0):
                return ".read_text() に encoding がない"
            return None
        # Path.write_text(data, encoding, errors, newline)
        if func.attr == "write_text":
            if not _has_encoding(call, 1):
                return ".write_text() に encoding がない"
            return None
        # Path.open(mode, buffering, encoding, ...)
        if func.attr == "open" and owner not in NON_PATH_OPEN_MODULES:
            if _is_text_mode(_mode_arg(call, 0)) and not _has_encoding(call, 2):
                return ".open() をテキストモードで encoding なしに呼んでいる"
            return None
    return None


def check_file(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8-sig")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"構文エラーで解析できない: {exc.msg}")]
    lines = source.splitlines()
    imports = _Imports()
    imports.visit(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        message = check_call(node, imports.names)
        if message is None:
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        span = lines[node.lineno - 1:end]
        if any(OPT_OUT in line for line in span):
            continue
        hits.append((node.lineno, message))
    return sorted(hits)


def default_files() -> list[Path]:
    files: set[Path] = set()
    for base, pattern in DEFAULT_GLOBS:
        files.update(p for p in (ROOT / base).glob(pattern) if p.is_file())
    return sorted(files)


def main(argv: list[str]) -> int:
    files = [Path(a).resolve() for a in argv] if argv else default_files()
    total = 0
    for path in files:
        for lineno, message in check_file(path):
            try:
                shown = path.relative_to(ROOT).as_posix()
            except ValueError:
                shown = str(path)
            print(f"{shown}:{lineno}: {message}")
            total += 1
    if total:
        sys.stdout.flush()
        print(f"\nencoding の付け忘れが {total} か所ある。encoding='utf-8' を渡すか、"
              f"既定の文字コードでよい行には `{OPT_OUT}` と書く。", file=sys.stderr)
        return 1
    print(f"{len(files)} ファイルを検査した。encoding の付け忘れはない。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
