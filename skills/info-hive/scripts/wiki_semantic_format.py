"""意味保存形式の識別。P2 は形式2・未知形式を既存 writer へ渡さない。"""

import json
from wiki_semantic_model import ModelError, _unique_object


def detect(guard):
    versions = []
    try:
        for name in ("ontology/definition.json", "ontology/ossie/release.json"):
            raw = guard.read_bytes(guard.root / name)
            if raw is None:
                continue
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise ModelError("意味形式の宣言がオブジェクトではありません")
            if "semantic_format" in value:
                versions.append(value["semantic_format"])
        with guard.open_read(guard.root / "ontology/ossie/records.jsonl", missing_ok=True) as stream:
            if stream is not None:
                for line in stream:
                    if not line.strip():
                        raise ModelError("空行を含む意味 JSONL の形式は判定できません")
                    row = json.loads(line, object_pairs_hook=_unique_object)
                    if not isinstance(row, dict):
                        raise ModelError("意味 JSONL の行が既知のオブジェクト形式ではありません")
                    if "table" in row and isinstance(row["table"], str) and row["table"].strip():
                        versions.append(1)
                    elif row.keys() == {"schema_version", "wiki_id", "kind", "record_id", "payload"}:
                        versions.append(row["schema_version"])
                    else:
                        raise ModelError("未知の意味 JSONL の形式です")
    except (UnicodeError, json.JSONDecodeError, RecursionError, OSError) as error:
        raise ModelError("意味保存形式を判定できません") from error
    if not versions:
        return 1
    if any(type(x) is not int or x not in {1, 2} for x in versions) or len(set(versions)) != 1:
        raise ModelError("未知または一致しない意味保存形式です")
    return versions[0]


def require_format1(guard):
    if detect(guard) != 1:
        raise ModelError("形式2の採用・保存はまだ有効化されていません。形式1の writer では更新できません")
