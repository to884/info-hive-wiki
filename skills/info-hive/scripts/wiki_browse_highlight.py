"""browse のコードフェンスの色分けと、配信するトークンの CSS。"""

import html
import re

try:
    from pygments import __version__ as VERSION
    from pygments import format as format_tokens
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_all_lexers, get_lexer_by_name
except ModuleNotFoundError as error:
    # uv のスクリプト依存を通らない Python で開いた場合も、従来どおり記事を読める。
    if error.name != "pygments":
        raise
    VERSION = ""
    HtmlFormatter = None

CLASS_PREFIX = "pg-"
MAX_CODE_CHARS = 32 * 1024
LEXER_ALIASES = (
    frozenset(alias.lower() for _, aliases, _, _ in get_all_lexers(plugins=False) for alias in aliases)
    if VERSION else frozenset()
)
TOKEN_SPAN = re.compile(r'<span class=" ?pg-[\w-]+(?: pg-[\w-]+)*">([^<]*)</span>')


def make_highlighter():
    """markdown-it が既存の pre と code で包む、コードの中身だけを返す。"""
    if HtmlFormatter is None:
        return None
    formatter = HtmlFormatter(nowrap=True, classprefix=CLASS_PREFIX)

    def highlight(source: str, language: str, _info: str) -> str:
        alias = language.lower()
        if alias in ("mermaid", "text") or alias not in LEXER_ALIASES or not source or len(source) > MAX_CODE_CHARS:
            return ""
        try:
            lexer = get_lexer_by_name(alias, stripnl=False, ensurenl=False)
            tokens = list(lexer.get_tokens(source))
            if "".join(value for _, value in tokens) != source:
                return ""
            rendered = format_tokens(tokens, formatter)
        except Exception:
            # 字句解析や整形に失敗したフェンスは markdown-it がエスケープして表示する。
            return ""
        return rendered if source.endswith("\n") else rendered.removesuffix("\n")

    return highlight


def stylesheet() -> bytes:
    """既存の配色ごとにトークンの色を決める。背景は browse の pre の色を使う。"""
    if HtmlFormatter is None:
        return b""
    rules = []
    for theme, name in (("light", "xcode"), ("dark", "one-dark")):
        formatter = HtmlFormatter(style=name, classprefix=CLASS_PREFIX)
        scope = "#_body pre > code"
        rules.extend(formatter.get_token_style_defs(f':root[data-theme="{theme}"] {scope}'))
        rules.append(f"@media (prefers-color-scheme: {theme}) {{")
        rules.extend(formatter.get_token_style_defs(f':root[data-theme="auto"] {scope}'))
        rules.append("}")
    return ("\n\n" + "\n".join(rules) + "\n").encode("utf-8")


def unspan(text: str) -> str | None:
    """色分けで分割した文字列を検査できる形に戻す。"""
    joined, count = TOKEN_SPAN.subn(r"\1", text)
    return html.unescape(joined) if count else None
