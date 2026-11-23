"""P3 の固定定義ファイルの実体保存。P1 の ready 検査から作り、上書きと参照の脱出を拒否する。定義の採用は行わない。

保存先 ontology/definitions/<定義内容 ID>/ には、定義内容だけで決まるもの（元の文書バイトと参照ロック）を置く。
処理条件（仕様ロック・実装の指紋）は processing_context レコードへ分け、同じ定義を別の処理条件で使えるようにする。
"""

from base64 import b64decode, b64encode
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import stat
from tempfile import TemporaryDirectory

from wiki_semantic_model import ModelError, Record, canonical, digest_id, fingerprint, load_json, relative_path, text, _unique_object


LOCK_NAME = "definition-lock.json"
FOLDER = "definitions"


def _document_name(raw):
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ModelError("定義文書を読み取れません") from error
    if not isinstance(doc, dict):
        raise ModelError("定義文書はオブジェクトにしてください")
    return text(doc.get("name"), "定義文書の name")


def _document_path(value):
    relative_path(value)
    if PurePosixPath(value).parts[0].casefold() == LOCK_NAME.casefold():
        raise ModelError("参照ロックと同じ名前の定義文書は保存できません")
    return value


def lock_bytes(definition_snapshot_id, manifest):
    """内容だけで決まる参照ロック。処理条件を含めない。"""
    return canonical({"definition_snapshot_id": digest_id(definition_snapshot_id), "manifest": manifest}) + b"\n"


@dataclass(frozen=True)
class FrozenDefinition:
    """承認の対象として固定した定義。採用（有効化）を意味しない。"""
    definition_snapshot_id: str
    processing_context_id: str
    document: str
    content: bytes
    manifest_json: bytes
    lock_json: bytes

    def __post_init__(self):
        digest_id(self.definition_snapshot_id)
        digest_id(self.processing_context_id)
        _document_path(self.document)
        if not isinstance(self.content, bytes):
            raise ModelError("定義文書は bytes で固定してください")
        manifest, lock = self.manifest, self.lock
        if manifest != {"documents": [{"path": self.document, "name": _document_name(self.content),
                                       "sha256": sha256(self.content).hexdigest()}]}:
            raise ModelError("定義文書と固定したマニフェストが一致しません")
        if fingerprint("definition-v1", manifest) != self.definition_snapshot_id:
            raise ModelError("定義内容 ID が文書と一致しません")
        if not isinstance(lock, dict) or fingerprint("processing-v1", lock) != self.processing_context_id:
            raise ModelError("処理条件 ID が仕様ロックと一致しません")
        if canonical(manifest) != self.manifest_json or canonical(lock) != self.lock_json:
            raise ModelError("定義の固定値が正規形ではありません")

    @property
    def manifest(self):
        return load_json(self.manifest_json)

    @property
    def lock(self):
        return load_json(self.lock_json)

    def records(self, wiki_id):
        return (Record.create(wiki_id, "definition_snapshot", {"manifest": self.manifest}),
                Record.create(wiki_id, "processing_context", {"lock": self.lock}))

    def plan_entry(self):
        return {"definition_snapshot_id": self.definition_snapshot_id,
                "processing_context_id": self.processing_context_id,
                "documents": self.manifest["documents"], "lock_sha256": sha256(self.lock_json).hexdigest()}

    def files(self):
        """保存先のフォルダーからの相対パスと内容。"""
        return {self.document: self.content, LOCK_NAME: lock_bytes(self.definition_snapshot_id, self.manifest)}

    def to_record(self):
        return {"definition_snapshot_id": self.definition_snapshot_id,
                "processing_context_id": self.processing_context_id, "document": self.document,
                "content": b64encode(self.content).decode("ascii"),
                "manifest": self.manifest, "lock": self.lock}

    @classmethod
    def from_record(cls, value):
        if not isinstance(value, dict) or value.keys() != {"definition_snapshot_id", "processing_context_id",
                                                          "document", "content", "manifest", "lock"}:
            raise ModelError("保存した定義の記録の形式が不正です")
        try:
            content = b64decode(value["content"], validate=True)
        except (TypeError, ValueError) as error:
            raise ModelError("保存した定義の内容を復元できません") from error
        return cls(value["definition_snapshot_id"], value["processing_context_id"], value["document"], content,
                   canonical(value["manifest"]), canonical(value["lock"]))


def freeze(inspection):
    """P1 の ready 検査からだけ固定する。元バイト・仕様ロック・実装の指紋を再計算して照合する。"""
    from wiki_ossie_contract import LOCK_FILE, _document, _implementation, _validate_lock, compile_contract

    contract = compile_contract(inspection)
    raw = inspection.document_bytes
    if sha256(raw).hexdigest() != contract.document_sha256:
        raise ModelError("検査した元バイトと契約の指紋が一致しません")
    manifest = {"documents": [{"path": _document_path(contract.document), "name": _document_name(raw),
                               "sha256": contract.document_sha256}]}
    try:
        specification = _document(LOCK_FILE.read_bytes())
        _validate_lock(specification)
        lock = {"specification": specification, "profile": contract.profile,
                "implementation": _implementation(), "identity_policy": "none"}
    except (OSError, ValueError) as error:
        raise ModelError(f"処理条件を固定できません: {error}") from error
    if fingerprint("processing-v1", lock) != contract.processing_context_id:
        raise ModelError("検査後に仕様ロックまたは実装が変わりました。検査をやり直してください")
    return FrozenDefinition(contract.definition_snapshot_id, contract.processing_context_id, contract.document,
                            raw, canonical(manifest), canonical(lock))


def reinspect(frozen, specification):
    """固定した元バイトを P1 の公式検査から読み直し、現在の処理条件 ID を返す。保存済みの ready は信用しない。"""
    from wiki_ossie_contract import compile_contract, inspect_bundle

    if not isinstance(frozen, FrozenDefinition):
        raise ModelError("固定した定義を指定してください")
    if specification is None:
        raise ModelError("定義の再検査には固定した公式仕様を指定してください")
    with TemporaryDirectory(prefix="info-hive-definition-check-") as name:
        target = Path(name).joinpath(*PurePosixPath(frozen.document).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(frozen.content)
        inspected = inspect_bundle(name, ontology=frozen.document, specification=specification)
        if inspected.state != "ready":
            raise ModelError("固定した定義が現在の公式検査で ready になりません")
        contract = compile_contract(inspected)
    if contract.definition_snapshot_id != frozen.definition_snapshot_id:
        raise ModelError("固定した定義の内容 ID が再検査と一致しません")
    return contract.processing_context_id


def _folder(ontology, definition_snapshot_id):
    return ontology / FOLDER / digest_id(definition_snapshot_id)


def _inside(folder, target):
    if not target.is_relative_to(folder) or target == folder:
        raise ModelError("定義の保存先がフォルダーの外を指しています")
    return target


def write(guard, ontology, frozen):
    """候補ルートの WriteGuard で書く。既存の異なる内容は上書きせず、同じ内容なら何もしない。"""
    if not isinstance(frozen, FrozenDefinition):
        raise ModelError("固定した定義を指定してください")
    ontology = Path(ontology)
    folder = _folder(ontology, frozen.definition_snapshot_id)
    files = frozen.files()
    for name, data in files.items():
        target = _inside(folder, folder.joinpath(*PurePosixPath(name).parts))
        existing = guard.read_bytes(target)
        if existing is not None:
            if existing != data:
                raise ModelError(f"保存済みの固定定義と内容が異なります。上書きできません: {name}")
            continue
        guard.ensure_directory(target.parent)
        guard.write_bytes(target, data, replace=False)
    _verify_folder(guard, folder, files)


def _verify_folder(guard, folder, files):
    actual = {}
    for path, info in guard.tree_entries(folder):
        if stat.S_ISDIR(info.st_mode):
            continue
        actual[path.relative_to(folder).as_posix()] = path
    if set(actual) != set(files):
        raise ModelError("固定定義のフォルダーに余分なファイルがあるか、ファイルが欠けています")
    for name, data in files.items():
        if guard.read_bytes(actual[name]) != data:
            raise ModelError(f"固定定義の内容が保存時と一致しません: {name}")


def verify_stored(guard, ontology, value):
    """意味スナップショットの定義レコードと、保存した実体の集合・指紋を照合する。公式検査は reinspect で行う。"""
    ontology = Path(ontology)
    base = ontology / FOLDER
    expected = {}
    for record in value.records:
        if record.kind != "definition_snapshot":
            continue
        manifest = record.payload["manifest"]
        key = fingerprint("definition-v1", manifest)
        files = {LOCK_NAME: lock_bytes(key, manifest)}
        for doc in manifest["documents"]:
            files[_document_path(doc["path"])] = None
        expected[key] = (manifest, files)
    if guard.directory_info(base) is None:
        if expected:
            raise ModelError("定義レコードに対応する固定定義の実体がありません")
        return
    present = {path.name for path, _ in guard.tree_entries(base) if path.parent == base}
    if present != set(expected):
        raise ModelError("固定定義のフォルダーの一覧が意味スナップショットの定義と一致しません")
    for key, (manifest, files) in expected.items():
        folder = _folder(ontology, key)
        actual = {path.relative_to(folder).as_posix(): path for path, info in guard.tree_entries(folder)
                  if not stat.S_ISDIR(info.st_mode)}
        if set(actual) != set(files):
            raise ModelError("固定定義のファイルの一覧が定義マニフェストと一致しません")
        if guard.read_bytes(actual[LOCK_NAME]) != files[LOCK_NAME]:
            raise ModelError("固定定義の参照ロックが一致しません")
        for doc in manifest["documents"]:
            data = guard.read_bytes(actual[doc["path"]])
            if data is None or sha256(data).hexdigest() != doc["sha256"] or _document_name(data) != doc["name"]:
                raise ModelError(f"固定定義の文書の指紋が一致しません: {doc['path']}")
