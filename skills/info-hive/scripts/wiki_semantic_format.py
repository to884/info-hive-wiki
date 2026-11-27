"""意味保存形式の識別。P2 は形式2・未知形式を既存 writer へ渡さない。"""

from io import BytesIO
import json
from wiki_semantic_model import ModelError, _unique_object


FORMAT_PATHS = ("ontology/definition.json", "ontology/ossie/release.json", "ontology/ossie/records.jsonl")


def detect(guard):
    try:
        declarations = tuple(guard.read_bytes(guard.root / name)
                             for name in FORMAT_PATHS[:2])
        with guard.open_read(guard.root / FORMAT_PATHS[2], missing_ok=True) as stream:
            return _detect(declarations, stream if stream is not None else ())
    except OSError as error:
        raise ModelError("意味保存形式を判定できません") from error


def detect_bytes(catalog, manifest, records):
    """指紋を照合済みのバイト列を、writer と同じ規則で判定する。"""
    with BytesIO(records) as stream:
        return _detect((catalog, manifest), stream)


def _detect(declarations, rows):
    versions = []
    try:
        for raw in declarations:
            if raw is None:
                continue
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise ModelError("意味形式の宣言がオブジェクトではありません")
            if "semantic_format" in value:
                versions.append(value["semantic_format"])
        for line in rows:
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
