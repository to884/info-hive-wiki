"""P1 の読込専用 Ossie 接続口。wiki・公開設定・採否を書き換えない。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

from wiki_semantic_model import (DefinitionRef, ModelError, RoleBinding, RoleRef, canonical,
                                 digest_id, fingerprint, relative_path, text, _unique_object)


PROFILE = "info-hive-ossie-text-binary-v1"
LOCK_FILE = Path(__file__).with_name("ossie-spec-lock.json")
_INSPECTED = object()


@dataclass(frozen=True)
class Diagnostic:
    code: str
    location: str
    message: str


@dataclass(frozen=True)
class Concept:
    ref: DefinitionRef
    name: str
    iri: str | None
    expanded_iri: str | None


@dataclass(frozen=True)
class Role:
    ref: RoleRef
    concept: DefinitionRef
    name: str


@dataclass(frozen=True)
class Relationship:
    ref: DefinitionRef
    name: str
    roles: tuple[Role, ...]
    iri: str | None
    expanded_iri: str | None


@dataclass(frozen=True)
class DefinitionContract:
    definition_snapshot_id: str
    processing_context_id: str
    profile: str
    concepts: tuple[Concept, ...]
    relationships: tuple[Relationship, ...]
    document: str
    document_sha256: str
    specification_sha256: str

    def __post_init__(self):
        for value in (self.definition_snapshot_id, self.processing_context_id, self.document_sha256,
                      self.specification_sha256):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ModelError("契約の指紋が不正です")
        relative_path(self.document)
        if (self.profile != PROFILE or type(self.concepts) is not tuple or type(self.relationships) is not tuple
                or any(not isinstance(c, Concept) for c in self.concepts)
                or any(not isinstance(r, Relationship) or type(r.roles) is not tuple for r in self.relationships)):
            raise ModelError("処理契約には固定した初期プロファイルと不変の宣言を指定してください")
        concepts = {c.ref for c in self.concepts}
        for relation in self.relationships:
            if (len(relation.roles) != 2 or any(not isinstance(r, Role) for r in relation.roles)
                    or [r.ref.ordinal for r in relation.roles] != [0, 1]
                    or any(r.ref.relationship != relation.ref or r.concept not in concepts for r in relation.roles)):
                raise ModelError("処理契約の役割参照が不正です")
        for ref in (*concepts, *(r.ref for r in self.relationships)):
            if ref.definition_snapshot_id != self.definition_snapshot_id or ref.document != self.document:
                raise ModelError("処理契約に別の定義版の参照が混ざっています")

    def check_bindings(self, relationship, bindings, entity_types):
        """型・役割だけを検査する。原文の支持や採用を判定しない。"""
        relation = next((r for r in self.relationships if r.ref == relationship), None)
        if relation is None:
            return (Diagnostic("reference_unresolved", "relationship", "採用した契約にない関係です"),)
        if not isinstance(entity_types, Mapping):
            return (Diagnostic("reference_unresolved", "entity_types", "実体の型参照を指定してください"),)
        bindings = tuple(bindings)
        if (len(bindings) != len(relation.roles) or any(not isinstance(b, RoleBinding) for b in bindings)
                or {b.role for b in bindings} != {r.ref for r in relation.roles}):
            return (Diagnostic("reference_unresolved", "role_bindings", "役割が欠落・重複しているか別の関係です"),)
        problems = []
        for binding in bindings:
            role = next(r for r in relation.roles if r.ref == binding.role)
            if binding.entity_id not in entity_types:
                problems.append(Diagnostic("reference_unresolved", str(role.ref.ordinal), "実体の参照がありません"))
            elif not isinstance(entity_types[binding.entity_id], (tuple, list, frozenset, set)) or role.concept not in entity_types[binding.entity_id]:
                problems.append(Diagnostic("role_type_mismatch", str(role.ref.ordinal), "役割の型と実体の型が一致しません"))
        return tuple(problems)


@dataclass(frozen=True)
class InspectionResult:
    state: str
    diagnostics: tuple[Diagnostic, ...]
    official_state: str
    definition_snapshot_id: str | None = None
    processing_context_id: str | None = None
    contract: DefinitionContract | None = None
    document_bytes: bytes = b""
    _receipt: tuple | None = field(default=None, init=False, repr=False, compare=False)


def compile_contract(inspection):
    if (not isinstance(inspection, InspectionResult) or inspection.state != "ready"
            or inspection.official_state != "pass" or inspection.diagnostics
            or not isinstance(inspection.contract, DefinitionContract) or not inspection._receipt
            or inspection._receipt[0] is not _INSPECTED
            or inspection._receipt[1] is not inspection.contract
            or inspection._receipt[2:] != (inspection.document_bytes, inspection.definition_snapshot_id,
                                          inspection.processing_context_id)):
        raise ModelError("ready の読込検査からだけ処理契約を作れます")
    return inspection.contract


def _read(root, name):
    relative_path(name)
    path = root
    for part in Path(name).parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ModelError("バンドルのリンクをたどることはできません")
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
        raise ModelError("通常の4MiB以下の JSON・仕様ファイルを指定してください")
    return path.read_bytes()


def _document(data):
    def reject(value):
        raise ModelError(f"JSON で表せない数値です: {value}")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=reject)
    if not isinstance(value, dict):
        raise ModelError("Ossie 文書はオブジェクトにしてください")
    return value


def _validate_lock(lock):
    if lock.keys() != {"upstream", "commit", "version", "python_minor", "files", "dependencies"}:
        raise ModelError("仕様ロックの項目が不正です")
    for key in ("upstream", "commit", "version", "python_minor"):
        text(lock[key], key)
    if len(lock["commit"]) != 40 or any(c not in "0123456789abcdef" for c in lock["commit"]):
        raise ModelError("仕様ロックのコミットが不正です")
    for key in ("files", "dependencies"):
        if not isinstance(lock[key], dict) or not lock[key]:
            raise ModelError("仕様ロックのファイル・依存は空でない辞書にしてください")
    for name, digest in lock["files"].items():
        relative_path(name)
        digest_id(digest)
    for name, version in lock["dependencies"].items():
        text(name, "依存名")
        text(version, "依存の版")


# この固定ブリッジだけを実行する。入力文書からプログラムや依存を取得しない。
_BRIDGE = r'''
import importlib.util, io, json, sys
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path
root=Path(sys.argv[1])
lock=json.loads((root/'lock.json').read_text(encoding='utf-8'))
try:
    if '.'.join(map(str,sys.version_info[:2])) != lock['python_minor']:
        raise ValueError('Python の版が仕様ロックと異なります')
    for name, wanted in lock['dependencies'].items():
        if version(name) != wanted:
            raise ValueError('依存の版が仕様ロックと異なります: '+name)
    spec=importlib.util.spec_from_file_location('info_hive_ossie_validation',root/'validation/validate.py')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    data=json.load(sys.stdin)
    schema=json.loads((root/'ontology/ontology.json').read_text(encoding='utf-8'))
    capture=io.StringIO()
    with redirect_stdout(capture):
        issues=module.validate_document(data,schema)
    warnings=[str(x) for x in issues if isinstance(x,module.ValidationWarning)]
    errors=[str(x) for x in issues if not isinstance(x,module.ValidationWarning)]
    if capture.getvalue(): warnings.append(capture.getvalue())
    expanded={}
    if not errors:
        prefixes=data.get('prefixes',{})
        # 固定版の undeclared_qname_prefix と同じ規則。既知スキームは公式の集合を使う。
        for concept in data['ontology']:
            for entry in [concept,*concept.get('relationships',[])]:
                iri=entry.get('iri')
                if iri is None: continue
                prefix,separator,local=iri.partition(':')
                full=local.startswith('//') or ':' in local or prefix.lower() in module.URI_SCHEMES
                expanded[iri]=prefixes[prefix]+local if separator and not full and prefix in prefixes else iri
    print(json.dumps({'state':'fail' if errors else 'pass','errors':errors,'warnings':warnings,'expanded_iris':expanded}))
except Exception as error:
    print(json.dumps({'state':'not_run','errors':[str(error)],'warnings':[],'expanded_iris':{}}))
'''


def _official(data, checkout, lock):
    """固定バイト列を一時領域へ写し、公式検査器を隔離した子プロセスで実行する。"""
    try:
        root = Path(checkout).resolve(strict=True)
        sources = {name: _read(root, name) for name in lock["files"]}
        if any(sha256(value).hexdigest() != lock["files"][name] for name, value in sources.items()):
            return {"state": "not_run", "errors": ["公式仕様の指紋がロックと一致しません"], "warnings": []}
        with TemporaryDirectory(prefix="info-hive-ossie-check-") as temp:
            target = Path(temp)
            for name, content in sources.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (target / "lock.json").write_bytes(canonical(lock))
            run = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", _BRIDGE, str(target)],
                                 input=data, capture_output=True, timeout=30, cwd=target,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report = json.loads(run.stdout.decode("utf-8"))
            if run.returncode or run.stderr or not isinstance(report, dict) or report.keys() != {"state", "errors", "warnings", "expanded_iris"}:
                raise ValueError("公式検査器の結果を照合できません")
            if report["state"] not in {"pass", "fail", "not_run"} or any(
                    not isinstance(report[key], list) or any(not isinstance(x, str) for x in report[key])
                    for key in ("errors", "warnings")):
                raise ValueError("公式検査器の結果形式が不正です")
            if not isinstance(report["expanded_iris"], dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in report["expanded_iris"].items()):
                raise ValueError("公式の IRI 判定結果が不正です")
            return report
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"state": "not_run", "errors": [str(error)], "warnings": []}


def _expand_iri(value, expansions):
    if value is None:
        return None
    if value not in expansions:
        raise ModelError("固定した公式仕様の IRI 判定結果がありません")
    return expansions[value]


def _implementation():
    here = Path(__file__)
    return {name: sha256(here.with_name(name).read_bytes()).hexdigest()
            for name in (here.name, "wiki_semantic_model.py")}


def _capabilities(doc):
    problems = []
    def unsupported(path, message):
        problems.append(Diagnostic("capability_unsupported", path, message))
    for key in ("requires", "ontology_mappings", "annotations", "ai_context"):
        if doc.get(key):
            unsupported("/" + key, "この初期プロファイルでは解釈しません")
    for i, concept in enumerate(doc.get("ontology", [])):
        path = f"/ontology/{i}"
        if concept["type"] != "EntityType":
            unsupported(path, "初期プロファイルは EntityType に限定します")
        for key in ("identify_by", "extends", "requires", "derived_by", "annotations"):
            if concept.get(key):
                unsupported(path + "/" + key, "宣言を省略して採用することはできません")
        for j, relation in enumerate(concept.get("relationships", [])):
            rp = f"{path}/relationships/{j}"
            if len(relation.get("roles", [])) != 1:
                unsupported(rp, "初期プロファイルは二項関係に限定します")
            for role in relation.get("roles", []):
                if role.get("annotations"):
                    unsupported(rp + "/roles", "未知の役割注記は未対応です")
            for key in ("multiplicity", "requires", "derived_by", "annotations"):
                if relation.get(key):
                    unsupported(rp + "/" + key, "宣言の実データ評価は未対応です")
    return problems


def inspect_bundle(directory, *, ontology="ontology.json", specification=None, profile=PROFILE,
                   core=None, mapping=None):
    """JSON バンドルを読む。仕様を指定しない場合も採用可能な成功にはしない。"""
    raw = b""
    official_state = "not_run"
    definition_id = context_id = None
    try:
        root = Path(directory).resolve(strict=True)
        raw = _read(root, ontology)
        doc = _document(raw)
        try:
            lock = _document(LOCK_FILE.read_bytes())
            _validate_lock(lock)
        except (OSError, ValueError) as error:
            return InspectionResult("unsupported", (Diagnostic("validation_unavailable", "specification", str(error)),), "not_run", document_bytes=raw)
        if profile != PROFILE:
            return InspectionResult("unsupported", (Diagnostic("capability_unsupported", "profile", "未知のプロファイルです"),), "not_run", document_bytes=raw)
        if core is not None or mapping is not None:
            # この入口で Mapping の外部参照を実行しない。
            return InspectionResult("unsupported", (Diagnostic("capability_unsupported", "mapping", "物理評価は初期プロファイルの範囲外です"),), "not_run", document_bytes=raw)
        if specification is None:
            return InspectionResult("unsupported", (Diagnostic("validation_unavailable", "specification", "固定した公式仕様を明示してください"),), "not_run", document_bytes=raw)
        report = _official(raw, specification, lock)
        official_state = report["state"]
        diagnostics = [Diagnostic("document_invalid" if report["state"] == "fail" else "validation_unavailable", "official", x)
                       for x in report["errors"]]
        diagnostics += [Diagnostic("validation_warning", "official", x) for x in report["warnings"]]
        if report["state"] == "fail":
            return InspectionResult("invalid", tuple(diagnostics), "fail", document_bytes=raw)
        if report["state"] != "pass" or diagnostics:
            return InspectionResult("unsupported", tuple(diagnostics), report["state"], document_bytes=raw)
        diagnostics = _capabilities(doc)
        declared = {c["concept"] for c in doc["ontology"] if c["type"] == "EntityType"}
        for i, concept in enumerate(doc["ontology"]):
            for j, relation in enumerate(concept.get("relationships", [])):
                for role in relation.get("roles", []):
                    if role["concept"] not in declared:
                        diagnostics.append(Diagnostic("capability_unsupported", f"/ontology/{i}/relationships/{j}", "組込み値型等の役割は未対応です"))
        definition_id = fingerprint("definition-v1", {"documents": [{"path": ontology, "name": doc["name"], "sha256": sha256(raw).hexdigest()}]})
        try:
            context_id = fingerprint("processing-v1", {"specification": lock, "profile": profile,
                                                       "implementation": _implementation(), "identity_policy": "none"})
        except OSError as error:
            return InspectionResult("unsupported", (Diagnostic("validation_unavailable", "implementation", str(error)),),
                                    official_state, definition_id, document_bytes=raw)
        if diagnostics:
            return InspectionResult("unsupported", tuple(diagnostics), "pass", definition_id, context_id, document_bytes=raw)
        concepts = []
        relationships = []
        expansions = report["expanded_iris"]
        seen_iris = set()
        refs = {c["concept"]: DefinitionRef(definition_id, ontology, f"/ontology/{i}", "concept")
                for i, c in enumerate(doc["ontology"])}
        for i, concept in enumerate(doc["ontology"]):
            iri = concept.get("iri")
            expanded = _expand_iri(iri, expansions)
            concepts.append(Concept(refs[concept["concept"]], concept["concept"], iri, expanded))
            if expanded is not None:
                if expanded in seen_iris:
                    raise ModelError("異なる宣言の IRI が衝突しています")
                seen_iris.add(expanded)
            for j, relation in enumerate(concept.get("relationships", [])):
                ref = DefinitionRef(definition_id, ontology, f"/ontology/{i}/relationships/{j}", "relationship")
                roles = [Role(RoleRef(ref, 0), refs[concept["concept"]], concept["concept"])]
                roles += [Role(RoleRef(ref, k + 1), refs[role["concept"]], role.get("name", role["concept"]))
                          for k, role in enumerate(relation["roles"])]
                if len({role.name for role in roles}) != len(roles):
                    raise ModelError("既定名を含め、同じ関係内の役割名を一意にしてください")
                iri = relation.get("iri")
                expanded = _expand_iri(iri, expansions)
                if expanded is not None:
                    if expanded in seen_iris:
                        raise ModelError("異なる宣言の IRI が衝突しています")
                    seen_iris.add(expanded)
                relationships.append(Relationship(ref, relation["name"], tuple(roles), iri, expanded))
        contract = DefinitionContract(definition_id, context_id, profile, tuple(concepts), tuple(relationships),
                                      ontology, sha256(raw).hexdigest(), sha256(canonical(lock)).hexdigest())
        result = InspectionResult("ready", (), "pass", definition_id, context_id, contract, raw)
        # メモリ内の生成元を照合するための印。永続的な採用証明としては使わない。
        object.__setattr__(result, "_receipt", (_INSPECTED, contract, raw, definition_id, context_id))
        return result
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        return InspectionResult("invalid", (Diagnostic("document_invalid", ontology, str(error)),), official_state,
                                definition_id, context_id, document_bytes=raw)
