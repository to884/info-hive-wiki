"""検索用のトークン化（計画の方針 E）。標準ライブラリだけで書き、重いモジュールを読み込まない。

検索（wiki_search）、MCP サーバーの索引（wiki_index）、プロンプトのフック（wiki_prompt_context）が、
同じトークン化を使うために 1 か所に置く。wiki_search は、ここの名前をそのまま読み込み直して公開する。
"""
from __future__ import annotations

import re
import unicodedata

# 文字種ごとの並び。英数字は小文字にしたあとの ASCII だけを語とみなす（移植元と同じ）。
# 漢字には 々 と 〆、CJK 統合漢字の拡張 B 以降（𠮷 など）を含める。
# カタカナには長音符 ー を含め、中黒 ・ は区切りとして扱う。
SCRIPT_RUN_RE = re.compile(
    r"(?P<ascii>[a-z0-9]+)"
    r"|(?P<kanji>[\u3005\u3006\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]+)"
    r"|(?P<hiragana>[\u3041-\u309f]+)"
    r"|(?P<katakana>[\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff]+)"
)


def tokenize(text: str) -> list[str]:
    """検索用のトークンに分ける（計画の方針 E）。

    NFKC で正規化して小文字にしたあと、文字種（英数字、漢字、ひらがな、カタカナ）の境界で区切る。
    英数字は語単位のまま、かな・漢字は 2 文字ずつ重ねて切り出す（1 文字だけの並びは 1 文字）。
    「税」のような単漢字のクエリが「消費税」に当たるよう、漢字は 1 文字単位でも加える。
    """
    tokens = []
    for match in SCRIPT_RUN_RE.finditer(unicodedata.normalize("NFKC", text).lower()):
        run = match.group()
        if match.lastgroup == "ascii":
            tokens.append(run)
            continue
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        if match.lastgroup == "kanji":
            tokens.extend(run)
    return tokens
