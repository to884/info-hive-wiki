"""形式2の配布条件と、親が注入する隔離能力の共通検査。"""

from pathlib import Path
from tempfile import gettempdir

import wiki_semantic_model as model


SAVE_PAGES = "semantic_page_save"
EXTRACT = "semantic_extract"
REPUBLISH = "semantic_republish"
RESTORE = "restore"  # 復元した本文・原本と現行評価の再照合に使う予約名。
RETURN_LIVE = "semantic_return_live"
SAVE_LIVE = "semantic_live_save"
# 通常保存は、公開版への保存と切り戻し後のライブ保存の両方を含む。
FORMAT2_REQUIRED = frozenset({SAVE_PAGES, EXTRACT, REPUBLISH, RESTORE, RETURN_LIVE, SAVE_LIVE})
FORMAT2_SUPPORTED = frozenset({SAVE_PAGES, EXTRACT, REPUBLISH, RETURN_LIVE, SAVE_LIVE})


def require_distributed_writes():
    """必要な能力を照合したうえで、別途承認するまで配布版の書込みを拒否する。"""
    missing = FORMAT2_REQUIRED - FORMAT2_SUPPORTED
    if missing:
        raise model.ModelError("意味の書込みは配布版では無効です。形式2に未対応の機能: "
                               + "、".join(sorted(missing)))
    raise model.ModelError("意味の書込みは配布版では無効です。プレビューだけを使えます")


def require_isolated(capability, *, operation, guard, operation_id):
    """OS の一時領域と、注入された根・実体・操作の許可を両方照合する。"""
    if model.SEMANTIC_WRITES_ENABLED is not False or capability is None:
        raise model.ModelError("形式2の成果物の書込みは隔離試験だけで扱えます")
    temp = Path(gettempdir()).resolve()
    root = guard.root.resolve(strict=True)
    if root == temp or not root.is_relative_to(temp):
        raise model.ModelError("意味の書込みは一時領域の隔離試験だけで扱えます")
    grant = getattr(capability, "grant", None)
    if not callable(grant) or grant(operation=operation, wiki=guard.root, wiki_identity=tuple(guard.root_id),
                                    operation_id=operation_id) is not True:
        raise model.ModelError("隔離試験の能力が、この wiki・操作を許可していません")
