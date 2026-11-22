"""検査した記事 ZIP から受け手の原本と wiki_apply の下書きを用意する。"""

from datetime import date
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

from wiki_apply import resolve_publication
from wiki_fsguard import WriteGuard
from wiki_initialized import require
import wiki_live_apply as live
from wiki_lint import parse_frontmatter
from wiki_pages import absolute_user_path, ref_values, walk_index_files, walk_pages
from wiki_raw import raw_digest
import wiki_raw_ingest

SECTIONS = {"sources": "ソース", "entities": "エンティティ", "concepts": "コンセプト", "synthesis": "シンセシス"}


def prepare(bundle, expected, wiki, raw, *, sections=None, index_files=None, use_existing=()):
    from wiki_bundle import receive

    wiki = absolute_user_path(require(wiki))
    raw = absolute_user_path(raw)
    view = resolve_publication(wiki)
    result = receive(bundle, expected)
    folder = Path(result["directory"])
    try:
        return stage(folder, result["manifest"], expected, wiki, raw, view, sections or {},
                     index_files or {}, set(use_existing))
    finally:
        shutil.rmtree(folder)


def same_import(before, text, refs, hashes, wiki, raw, files):
    """別の ZIP で保存済みなら、以前の原本・画像と、パスを合わせた記事を照合する。"""
    from wiki_bundle import raw_images, relocate

    existing = before.decode("utf-8-sig").replace("\r\n", "\n")
    meta, _, malformed = parse_frontmatter(existing)
    old_refs = ref_values(meta.get("raw"))
    if (malformed or not refs or len(old_refs) != len(refs)
            or relocate(text, old_refs, hashes, drop_graph=True) != existing):
        return False
    guard = WriteGuard(raw, strict_root=True)
    for ref, old_ref, sha in zip(refs, old_refs, hashes):
        old = wiki.parent / Path(old_ref)
        if not old.is_relative_to(raw):
            return False
        rel = old.relative_to(raw)
        if (len(rel.parts) < 2 or not re.fullmatch(r"import-[0-9a-f]{64}", rel.parts[0])
                or Path(*rel.parts[1:]) != Path(ref).relative_to("originals")
                or raw_digest(old, origin=raw) != sha):
            return False
        if old.suffix.lower() == ".md" and old.is_file():
            for image in raw_images(old, guard):
                name = (Path(ref).parent / image.relative_to(old.parent)).as_posix()
                if name in files and raw_digest(image, origin=raw) != files[name]["sha256"]:
                    return False
    return True


def index_target(name, available, sections, overrides):
    kind = Path(name).parts[0]
    target = overrides.get(name, overrides.get(kind))
    if target is None:
        shards = sorted(p for p in available if p.startswith(f"indexes/{kind}-"))
        if f"indexes/{kind}.md" in available:
            target = f"indexes/{kind}.md"
        elif len(shards) > 1:
            raise ValueError(f"{name} の索引を --index-file で指定してください: {shards}")
        else:
            target = shards[0] if shards else "index.md"
    if target not in available:
        raise ValueError(f"受け手の索引にありません: {target}")
    heading = sections.get(name, sections.get(kind, "## " + SECTIONS[kind] if target == "index.md" else None))
    return target, heading


def stage(folder, manifest, checksum, wiki, raw, view, sections, index_files, use_existing):
    from wiki_bundle import article_links, relocate
    from wiki_pages import build_page_index, link_candidates, nfc

    source = WriteGuard(view.root if view else wiki)
    inventory = walk_pages(source.root)
    if inventory.unlistable or inventory.unknown:
        raise ValueError("取り込み先の記事名をすべて確認できません")
    slug_paths = {}
    for entry in inventory.files:
        slug_paths.setdefault(nfc(Path(entry.rel).stem.casefold()), set()).add(entry.rel)
    saved = raw / ("import-" + checksum)
    # raw の保存コマンドが使う wiki からの表記を、書き込む前の照合にも使う。
    prefix = Path(wiki_raw_ingest._ref(wiki, saved, checksum, "existing")["raw_ref"])
    planned = {}
    reviews = {(item["page"], item["link"]) for item in manifest["omitted_links"]}
    lookup = build_page_index([{"rel_path": key[6:], "slug": nfc(Path(key).stem)} for key in manifest["raw"]])
    versions = {}
    already = []
    needs_raw = False
    for key, refs in manifest["raw"].items():
        name = key.removeprefix("pages/")
        slug = nfc(Path(name).stem.casefold())
        same = slug_paths.setdefault(slug, set())
        if same - {name}:
            raise ValueError(f"同じ記事名があります: {name}、{sorted(same)}")
        same.add(name)
        text = (folder / key).read_text(encoding="utf-8-sig")
        meta, body, malformed = parse_frontmatter(text)
        if malformed or ref_values(meta.get("raw")) != refs:
            raise ValueError(f"原本の対応表と記事が一致しません: {name}")
        hashes = []
        local_refs = []
        for ref in refs:
            if ref not in versions:
                versions[ref] = raw_digest(folder / ref, origin=folder)
            hashes.append(versions[ref])
            local_refs.append((prefix / Path(ref).relative_to("originals")).as_posix())
        if ref_values(meta.get("raw_sha256")) != hashes:
            raise ValueError(f"記事が指す原本の版が違います: {name}")
        for link in article_links(meta, body):
            if len(link_candidates(link, *lookup)) != 1:
                reviews.add((name, link))
        data = relocate(text, local_refs, hashes, drop_graph=True).encode("utf-8")
        path = live.canonical_target(source, Path(name))
        before = source.read_bytes(path)
        if before is not None:
            if before == data:
                needs_raw |= bool(refs)
            elif name not in use_existing and not same_import(before, text, refs, hashes, wiki, raw, manifest["files"]):
                raise ValueError(f"既存記事と内容が異なります: {name}。既存記事を使うと確認した場合は "
                                 f"--use-existing でこの相対パスを指定してください")
            already.append(name)
        else:
            needs_raw |= bool(refs)
            planned[name] = (data, " ".join(str(meta.get("title") or Path(name).stem).split()))

    if use_existing - set(already):
        raise ValueError("--use-existing には ZIP と受け手の両方に存在する記事を指定してください")
    indexes = {f.rel for f in walk_index_files(source.root).files}
    destinations = {name: index_target(name, indexes, sections, index_files) for name in planned}
    originals = folder / "originals" if needs_raw else None
    result = {"pages": sorted(planned), "existing": sorted(already),
              "references_to_review": [{"page": name, "link": link} for name, link in sorted(reviews) if name in planned],
              "raw_directory": str(saved) if originals else None}
    if not planned and not needs_raw:
        return result
    # 受け手の wiki と raw の所属を確認してから原本を書き込む。
    session = live.begin(wiki, raw, "記事 ZIP の取り込み", release_id=view.release_id if view else None,
                         allow_publication=True)
    work = Path(session["apply_dir"])
    try:
        register_pages(work, planned, destinations, checksum)
        if originals:
            save_originals(originals, saved, wiki, raw)
    except BaseException:
        # begin が今回作り、まだ利用者に渡していない専用の作業領域だけを取り除く。
        shutil.rmtree(live.work_directory(work).root)
        raise
    if not planned:
        shutil.rmtree(live.work_directory(work).root)
        return result
    return {**session, **result}


def register_pages(work, planned, destinations, checksum):
    def operation(command, **fields):
        return live.operate(SimpleNamespace(cmd=command, apply_dir=work, **fields))

    for name, (data, title) in planned.items():
        draft = operation("new", path=name)
        Path(draft["path"]).write_bytes(data)
        index, section = destinations[name]
        operation("index-add", file=index, section=section, nth=None,
                  line=f"- [[{Path(name).stem}]]: {title}")
    if planned:
        operation("log-entry", op="ingest", title="記事 ZIP の取り込み", date=date.today().isoformat(),
                  line=["ZIP の指紋 " + checksum])
        # 索引の見出しや既存項目の衝突は、原本を保存する前に知らせる。
        operation("diff")


def save_originals(originals, saved, wiki, raw):
    expected_raw = raw_digest(originals, origin=originals.parent)
    try:
        result = wiki_raw_ingest.save(wiki, raw, originals, saved.name)
    except FileExistsError:
        if raw_digest(saved, origin=raw) != expected_raw:
            raise ValueError(f"同名の原本が異なる内容です: {saved}") from None
        result = wiki_raw_ingest.save(wiki, raw, saved, None)
    if result["raw_sha256"] != expected_raw:
        raise ValueError(f"同名の原本が異なる内容です: {saved}")
