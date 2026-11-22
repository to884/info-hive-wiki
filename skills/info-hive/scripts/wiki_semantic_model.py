"""P0 の固定値と識別規則。保存・採用・既存コマンドへの接続は行わない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
import uuid


SCHEMA_VERSION = 2
SEMANTIC_WRITES_ENABLED = False
HEX = re.compile(r"[0-9a-f]{64}")
LIFECYCLES = frozenset({"accepted", "disputed", "withdrawn", "superseded"})
CHECK_STATES = frozenset({"pass", "fail", "not_run", "unsupported"})
COMPATIBILITIES = frozenset({"compatible", "incompatible", "not_checked"})
EVIDENCE_STATES = frozenset({"verified", "affected", "unavailable", "not_checked"})


class ModelError(ValueError):
    """共通モデルの構造・識別規則に反する値。"""


def text(value, label="値"):
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{label}は空でない文字列にしてください")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ModelError(f"{label}は UTF-8 で表せません") from error
    return value


def digest_id(value):
    if not isinstance(value, str) or HEX.fullmatch(value) is None:
        raise ModelError("指紋は小文字16進数64桁にしてください")
    return value


def relative_path(value):
    text(value, "相対パス")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or ".." in path.parts
            or any(ch in value for ch in "\\:\0") or not path.parts):
        raise ModelError("バンドル内の正規の相対パスを指定してください")
    return value


def _json_value(value, active=None):
    active = set() if active is None else active
    if value is None or type(value) in (bool, int):
        return
    if isinstance(value, str):
        value.encode("utf-8")
        return
    if type(value) not in (list, dict) or id(value) in active:
        raise ModelError("内部 JSON は循環のない文字列・整数・真偽値・null・配列・辞書に限定します")
    active.add(id(value))
    try:
        if isinstance(value, dict):
            for key in value:
                if not isinstance(key, str):
                    raise ModelError("JSON のキーは文字列にしてください")
                key.encode("utf-8")
            values = value.values()
        else:
            values = value
        for item in values:
            _json_value(item, active)
    finally:
        active.remove(id(value))


def canonical(value):
    """表示名・IRI・配列順序を正規化せず、内部 JSON のバイト列を固定する。"""
    try:
        _json_value(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (UnicodeError, RecursionError) as error:
        raise ModelError("内部 JSON を UTF-8 で固定できません") from error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelError(f"JSON のキーが重複しています: {key}")
        result[key] = value
    return result


def load_json(data):
    """内部 JSON を、重複キーや非整数を許さず読む。"""
    def reject(value):
        raise ModelError(f"内部 JSON の数値の対応範囲外です: {value}")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object, parse_float=reject, parse_constant=reject)
        canonical(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ModelError("内部 JSON を読めません") from error


def fingerprint(namespace, value):
    return sha256(canonical([text(namespace), value])).hexdigest()


def new_entity_id():
    """表示名や資料名を入力に取らず、準備時に割り当てる。再送では保存済み ID を使う。"""
    return uuid.uuid4().hex


def activation_id(wiki_id, operation_id, previous, definition_snapshot_id):
    if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise ModelError("操作 ID は小文字16進数32桁にしてください")
    if previous is not None:
        digest_id(previous)
    return fingerprint("activation-v1", [text(wiki_id), operation_id, previous, digest_id(definition_snapshot_id)])


@dataclass(frozen=True)
class DefinitionRef:
    definition_snapshot_id: str
    document: str
    pointer: str
    kind: str

    def __post_init__(self):
        digest_id(self.definition_snapshot_id)
        relative_path(self.document)
        if not isinstance(self.kind, str) or self.kind not in {"concept", "relationship"}:
            raise ModelError("定義参照の種類が不正です")
        pointer = r"/ontology/(0|[1-9][0-9]*)"
        if self.kind == "relationship":
            pointer += r"/relationships/(0|[1-9][0-9]*)"
        if not isinstance(self.pointer, str) or re.fullmatch(pointer, self.pointer) is None:
            raise ModelError("定義参照には添字の表記が一意な宣言位置を指定してください")

    def to_dict(self):
        return dict(definition_snapshot_id=self.definition_snapshot_id, document=self.document,
                    pointer=self.pointer, kind=self.kind)


@dataclass(frozen=True)
class RoleRef:
    relationship: DefinitionRef
    ordinal: int

    def __post_init__(self):
        if not isinstance(self.relationship, DefinitionRef) or self.relationship.kind != "relationship":
            raise ModelError("役割には関係への参照が必要です")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ModelError("役割位置は0以上の整数にしてください")

    def to_dict(self):
        return {"relationship": self.relationship.to_dict(), "ordinal": self.ordinal}


@dataclass(frozen=True)
class RoleBinding:
    role: RoleRef
    entity_id: str

    def __post_init__(self):
        if not isinstance(self.role, RoleRef):
            raise ModelError("役割参照が必要です")
        text(self.entity_id, "実体 ID")


@dataclass(frozen=True)
class TemporalScope:
    """unknown と、日付の半開区間（None は明示的な開放端）を区別する。"""
    kind: str = "unknown"
    start: str | None = None
    end: str | None = None

    def __post_init__(self):
        if self.kind == "unknown":
            if self.start is not None or self.end is not None:
                raise ModelError("期間不明には開始・終了を指定できません")
            return
        if self.kind != "date_interval":
            raise ModelError("期間の精度は日付に限定します")
        for point in (self.start, self.end):
            if point is not None:
                try:
                    if not isinstance(point, str) or date.fromisoformat(point).isoformat() != point:
                        raise ValueError()
                except (ValueError, TypeError) as error:
                    raise ModelError("期間の日付は YYYY-MM-DD にしてください") from error
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ModelError("半開区間の終了は開始より後にしてください")

    def to_dict(self):
        return {"kind": self.kind} if self.kind == "unknown" else {
            "kind": self.kind, "start": self.start, "end": self.end}


def claim_payload(wiki_id, relationship, bindings, *, polarity="unknown", temporal=None, qualifiers=None):
    if not isinstance(relationship, DefinitionRef) or relationship.kind != "relationship":
        raise ModelError("主張には関係への参照が必要です")
    bindings = tuple(bindings)
    if not bindings or any(not isinstance(b, RoleBinding) or b.role.relationship != relationship for b in bindings):
        raise ModelError("同じ関係の役割割当を指定してください")
    ordered = sorted(bindings, key=lambda b: b.role.ordinal)
    if [b.role.ordinal for b in ordered] != list(range(len(ordered))):
        raise ModelError("役割位置の欠落または重複があります")
    if not isinstance(polarity, str) or polarity not in {"positive", "negative", "unknown"}:
        raise ModelError("主張の極性が不正です")
    temporal = TemporalScope() if temporal is None else temporal
    if not isinstance(temporal, TemporalScope):
        raise ModelError("時間範囲を指定してください")
    qualifiers = {} if qualifiers is None else qualifiers
    if not isinstance(qualifiers, dict):
        raise ModelError("限定条件は辞書にしてください")
    value = {"wiki_id": text(wiki_id), "relationship": relationship.to_dict(),
             "role_bindings": [{"ordinal": b.role.ordinal, "entity_id": b.entity_id} for b in ordered],
             "polarity": polarity, "temporal_scope": temporal.to_dict(), "qualifiers": qualifiers}
    return load_json(canonical(value))


def claim_id(wiki_id, relationship, bindings, **options):
    """処理条件・根拠・表示名・有効化を含めず、命題の表現だけを識別する。"""
    return fingerprint("claim-v1", claim_payload(wiki_id, relationship, bindings, **options))


def current_eligible(*, lifecycle, decision_activation, assessment_activation, current_activation,
                     assessment_context, current_context, compatibility, evidence_state, dependencies_verified=False):
    """現行評価の必要条件。根拠の意味判断や採用処理そのものは行わない。"""
    for value, choices in ((lifecycle, LIFECYCLES), (compatibility, COMPATIBILITIES),
                           (evidence_state, EVIDENCE_STATES)):
        if not isinstance(value, str) or value not in choices:
            raise ModelError("評価の状態が不正です")
    ids = (decision_activation, assessment_activation, current_activation, assessment_context, current_context)
    for value in ids:
        if value is not None:
            digest_id(value)
    if type(dependencies_verified) is not bool:
        raise ModelError("依存する実体改訂・根拠の照合結果を真偽値で指定してください")
    return (dependencies_verified and None not in ids and lifecycle == "accepted" and compatibility == "compatible"
            and evidence_state == "verified" and decision_activation == assessment_activation == current_activation
            and assessment_context == current_context)


@dataclass(frozen=True)
class Record:
    """構造の固定用。create は採用・永続化を意味しない。payload はコピーを返す。"""
    wiki_id: str
    kind: str
    payload_json: bytes

    def __post_init__(self):
        text(self.wiki_id, "wiki ID")
        if not isinstance(self.kind, str) or self.kind not in RECORD_FIELDS:
            raise ModelError("レコードの種類が不正です")
        if not isinstance(self.payload_json, bytes):
            raise ModelError("固定ペイロードは bytes にしてください")
        value = load_json(self.payload_json)
        required, optional = RECORD_FIELDS[self.kind]
        if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
            raise ModelError(f"{self.kind} の必須項目または未知の項目を確認してください")
        _validate_payload(self.wiki_id, self.kind, value)
        if canonical(value) != self.payload_json:
            raise ModelError("ペイロードが正規の内部 JSON ではありません")

    @classmethod
    def create(cls, wiki_id, kind, payload):
        return cls(wiki_id, kind, canonical(payload))

    @property
    def payload(self):
        return load_json(self.payload_json)

    @property
    def record_id(self):
        return fingerprint("record-v2", [self.wiki_id, self.kind, self.payload])

    def to_dict(self):
        return {"schema_version": SCHEMA_VERSION, "wiki_id": self.wiki_id, "kind": self.kind,
                "record_id": self.record_id, "payload": self.payload}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or value.keys() != {"schema_version", "wiki_id", "kind", "record_id", "payload"}
                or type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION):
            raise ModelError("レコード形式が不正です")
        record = cls.create(value["wiki_id"], value["kind"], value["payload"])
        if record.record_id != value["record_id"]:
            raise ModelError("レコードの指紋が一致しません")
        return record


def _fields(required, optional=""):
    return frozenset(required.split()), frozenset(optional.split())


# P0 は固定レコードの外形を定める。参照の解決と採用条件は後続の検査で行う。
RECORD_FIELDS = {
    "definition_snapshot": _fields("manifest"),
    "processing_context": _fields("lock"),
    "definition_activation": _fields("activation_id definition_snapshot_id previous_id operation_id"),
    "entity": _fields("entity_id operation_id"),
    "entity_revision": _fields("entity_id previous_id display type_refs"),
    "claim": _fields("claim_id relationship role_bindings polarity temporal_scope qualifiers"),
    "claim_revision": _fields("claim_id previous_id lifecycle evidence_links"),
    "source_snapshot": _fields("reference sha256 media_type", "article_revision_id"),
    "evidence": _fields("source_id quote locator extraction"),
    "evidence_assessment": _fields("claim_id evidence_id definition_snapshot_id processing_context_id position support"),
    "decision": _fields("targets proposal_sha256 activation_id processing_context_id reports disposition actor reason operation_id recorded_at"),
    "validation_report": _fields("input_sha256 processing_context_id checks"),
    "record_link": _fields("relation from_id to_id reason", "evidence_id decision_id"),
    "article_revision": _fields("page sha256 title origin derived_from"),
    "current_assessment": _fields("revision_id activation_id processing_context_id decision_id reports dependencies compatibility evidence_state"),
    "legacy_record": _fields("format release_id table legacy_id original"),
    "migration_link": _fields("legacy_record_id target operation_id reason"),
}


def _definition_ref(value, kind):
    if not isinstance(value, dict) or value.keys() != {"definition_snapshot_id", "document", "pointer", "kind"}:
        raise ModelError("定義参照の形式が不正です")
    ref = DefinitionRef(**value)
    if ref.kind != kind:
        raise ModelError("概念と関係の参照を区別してください")
    return ref


def _validate_payload(wiki_id, kind, value):
    """外形・値型・ローカルな識別規則を検査する。記録集合の参照解決は行わない。"""
    string_fields = {"entity_id", "operation_id", "actor", "reason", "recorded_at", "page", "title",
                     "quote", "media_type", "relation", "origin", "disposition", "format", "table", "legacy_id",
                     "release_id", "position", "support"}
    array_fields = {"type_refs", "role_bindings", "evidence_links", "targets", "reports", "derived_from"}
    object_fields = {"manifest", "lock", "display", "locator", "extraction", "dependencies", "qualifiers", "checks",
                     "reference", "target"}
    for key, item in value.items():
        if key in string_fields:
            text(item, key)
        if key in array_fields and not isinstance(item, list):
            raise ModelError(f"{key} は配列にしてください")
        if key in object_fields and not isinstance(item, dict):
            raise ModelError(f"{key} は辞書にしてください")
        if key == "operation_id" and re.fullmatch(r"[0-9a-f]{32}", item) is None:
            raise ModelError("操作 ID は小文字16進数32桁にしてください")
        if (key.endswith("_id") and key not in {"entity_id", "operation_id", "legacy_id", "release_id"}) or key.endswith("sha256"):
            if item is None and (key == "previous_id" or key in RECORD_FIELDS[kind][1]):
                continue
            digest_id(item)
    choices = {"lifecycle": LIFECYCLES, "compatibility": COMPATIBILITIES, "evidence_state": EVIDENCE_STATES,
               "origin": {"original", "compilation", "generated", "unknown"},
               "disposition": {"accept", "defer", "reject"}, "position": CHECK_STATES,
               "support": {"supports", "opposes", "context", "unknown"},
               "relation": {"supersedes", "contradicts", "merged_into", "split_into", "equivalent"}}
    for key, values in choices.items():
        if key in value and (not isinstance(value[key], str) or value[key] not in values):
            raise ModelError(f"{key} の状態が不正です")
    if "recorded_at" in value:
        try:
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value["recorded_at"]) is None:
                raise ValueError()
            datetime.fromisoformat(value["recorded_at"])
        except (ValueError, AttributeError) as error:
            raise ModelError("記録日時は秒精度の YYYY-MM-DDTHH:MM:SSZ にしてください") from error
    if "checks" in value:
        for name, check in value["checks"].items():
            text(name, "検査名")
            if (not isinstance(check, dict) or "state" not in check or check.keys() - {"state", "reason", "location"}
                    or not isinstance(check["state"], str) or check["state"] not in CHECK_STATES):
                raise ModelError("検査項目には state と任意の reason・location を指定してください")
            for key in check.keys() - {"state"}:
                text(check[key], key)
    if kind == "source_snapshot":
        reference = value["reference"]
        if reference.keys() != {"uri", "version"}:
            raise ModelError("原本参照には uri と固定した version を指定してください")
        for key, item in reference.items():
            text(item, key)
    if kind == "migration_link":
        target = value["target"]
        if target.keys() != {"kind", "id"} or not isinstance(target["kind"], str) or target["kind"] not in {"record", "entity"}:
            raise ModelError("変換先には record または entity の ID を指定してください")
        if target["kind"] == "record":
            digest_id(target["id"])
        else:
            text(target["id"], "変換先実体 ID")
    if "type_refs" in value:
        for item in value["type_refs"]:
            _definition_ref(item, "concept")
    for key in ("reports", "targets", "derived_from"):
        if key in value:
            for item in value[key]:
                digest_id(item)
    if "evidence_links" in value:
        for item in value["evidence_links"]:
            if (not isinstance(item, dict) or item.keys() != {"evidence_id", "kind"}
                    or not isinstance(item["kind"], str) or item["kind"] not in {"supports", "opposes", "context"}):
                raise ModelError("根拠リンクの形式が不正です")
            digest_id(item["evidence_id"])
    if kind == "evidence":
        locator = value["locator"]
        if (locator.keys() != {"text_sha256", "start_byte", "end_byte"}
                or any(type(locator[k]) is not int for k in ("start_byte", "end_byte"))
                or not 0 <= locator["start_byte"] < locator["end_byte"]):
            raise ModelError("引用位置には固定したテキストの UTF-8 バイト範囲を指定してください")
        digest_id(locator["text_sha256"])
    if kind == "claim":
        ref = _definition_ref(value["relationship"], "relationship")
        bindings = []
        for entry in value["role_bindings"]:
            if not isinstance(entry, dict) or entry.keys() != {"ordinal", "entity_id"}:
                raise ModelError("主張の役割割当の形式が不正です")
            bindings.append(RoleBinding(RoleRef(ref, entry["ordinal"]), entry["entity_id"]))
        scope = value["temporal_scope"]
        if not isinstance(scope, dict) or scope.keys() - {"kind", "start", "end"} or "kind" not in scope:
            raise ModelError("時間範囲の形式が不正です")
        if scope["kind"] == "date_interval" and scope.keys() != {"kind", "start", "end"}:
            raise ModelError("区間の開放端は null で明示してください")
        if scope["kind"] == "unknown" and scope.keys() != {"kind"}:
            raise ModelError("期間不明と区間を区別してください")
        computed = claim_id(wiki_id, ref, bindings, polarity=value["polarity"],
                            temporal=TemporalScope(**scope), qualifiers=value["qualifiers"])
        if computed != value["claim_id"]:
            raise ModelError("主張の ID が命題と一致しません")


def require_semantic_writes():
    """P0〜P1 から意味の移行・採用を有効にする経路は提供しない。"""
    raise ModelError("意味レコードの書込みは未提供です。P0〜P1 は読込・検査だけを行います")
