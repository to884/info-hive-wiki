"""選んだ記事と原本を持ち運ぶ ZIP の作成と検査。展開先は毎回新しい一時領域にする。"""

import argparse
from collections import deque
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from wiki_apply import checked, digest, encoded, resolve_publication, verify_file_names
from wiki_fsguard import WriteGuard, publish_exclusive
from wiki_initialized import require
from wiki_lint import FRONTMATTER_RE, parse_frontmatter
from wiki_live_apply import page_path, portable_path
from wiki_pages import (absolute_user_path, build_page_index, extract_wikilinks, link_candidates,
                        new_parser, ref_link, ref_values, walk_pages)
from wiki_raw import resolve_raw
from wiki_text import canonical_key, format_fm_value, localize_argparse, nfc

FORMAT = "info-hive-articles/1"
MAX_FILES = 4096
MAX_BYTES = 2 * 1024**3
MAX_MANIFEST = 4 * 1024**2
PAGE_DIRS = {"sources", "concepts", "entities", "synthesis"}


def member_path(name):
    path = portable_path(name)
    if path.parts[0] == "pages" and len(path.parts) >= 3 and path.parts[1] in PAGE_DIRS:
        page_path(Path(*path.parts[1:]).as_posix())
    elif path.parts[0] != "originals" or len(path.parts) < 2:
        raise ValueError(f"配布対象にできない名前です: {name}")
    return path


def transfer(source, destination=None):
    fingerprint = hashlib.sha256()
    size = 0
    while block := source.read(1024 * 1024):
        size += len(block)
        if size > MAX_BYTES:
            raise ValueError("ファイルのサイズが上限を超えました")
        fingerprint.update(block)
        if destination is not None:
            destination.write(block)
    return {"size": size, "sha256": fingerprint.hexdigest()}


def article_links(meta, body):
    refs = [ref_link(value) for key in ("sources", "sources_consulted", "entities", "concepts")
            for value in ref_values(meta.get(key))]
    return list(dict.fromkeys([*extract_wikilinks(body), *refs]))


def select_pages(root, requested, related):
    walk = walk_pages(root)
    if walk.unknown or walk.unlistable:
        raise ValueError("記事を一覧できません")
    records = [{"slug": nfc(Path(f.rel).stem), "rel_path": f.rel} for f in walk.files
               if f.rel.split("/")[0] in PAGE_DIRS]
    lookup = build_page_index(records, value=lambda record: record["rel_path"])
    roots = set()
    for key in requested:
        found = link_candidates(ref_link(key), *lookup)
        if len(found) != 1:
            raise ValueError(f"記事を一つに決められません: {key} {found}")
        roots.add(found[0])
    queue = deque(sorted(roots))
    pages = {}
    links = {}
    guard = WriteGuard(root)
    while queue:
        name = queue.popleft()
        if name in pages:
            continue
        if len(pages) >= MAX_FILES:
            raise ValueError("記事数が ZIP の上限を超えています")
        info = guard.file_info(root / name)
        if info is None:
            raise FileNotFoundError(root / name)
        if info.st_size > MAX_MANIFEST:
            raise ValueError(f"記事が 4 MiB を超えています: {name}")
        data = guard.read_bytes(root / name)
        meta, body, malformed = parse_frontmatter(data.decode("utf-8-sig").replace("\r\n", "\n"))
        if malformed:
            raise ValueError(f"frontmatter を読めません: {name}")
        pages[name] = (meta, digest(data))
        for link in article_links(meta, body):
            found = link_candidates(link, *lookup)
            links[name, link] = found
            if related and name in roots and len(found) == 1:
                queue.append(found[0])
    omitted = [{"page": name, "link": link, "matches": found} for (name, link), found in links.items()
               if len(found) != 1 or found[0] not in pages]
    return pages, omitted


def relocate(text, refs, hashes, *, drop_graph=False):
    """最上位の原本欄と送り元の rag ID だけを差し替え、本文と他のメタデータを保つ。"""
    match = FRONTMATTER_RE.match(text)
    if not match:
        if refs:
            raise ValueError("原本を付け替える frontmatter がありません")
        return text
    lines = []
    removing = False
    for line in match[1].split("\n"):
        key = re.match(r"""^(?:"([^"]+)"|'([^']+)'|([^\s:#][^:]*?))\s*:""", line)
        if key and not line.startswith(("- ", "]")):
            field = canonical_key(next(v for v in key.groups() if v is not None))
            removing = field in {"raw", "raw_sha256", "rag_job_id"} or (drop_graph and field == "graph")
        if not removing:
            lines.append(line)
    if refs:
        lines.append("raw: " + format_fm_value(refs[0] if len(refs) == 1 else refs))
        lines.append("raw_sha256: " + format_fm_value(hashes[0] if len(hashes) == 1 else hashes))
    return text[:match.start(1)] + "\n".join(lines) + text[match.end(1):]


def raw_images(path, guard):
    """Web 取り込みで raw 本文から相対参照する assets の画像を同梱する。"""
    if path.suffix.lower() != ".md" or guard.file_info(path).st_size > MAX_MANIFEST:
        return
    try:
        body = guard.read_bytes(path).decode("utf-8-sig")
    except UnicodeDecodeError:
        return
    for token in new_parser().parse(body):
        for child in token.children or ():
            if child.type == "image":
                url = urlsplit(child.attrs["src"])
                rel = unquote(url.path)
                if not (url.scheme or url.netloc) and rel.startswith("assets/"):
                    yield path.parent / portable_path(rel)


def raw_files(path, guard):
    """ディレクトリの原本は隠し項目を除いたファイルだけを扱う。"""
    info = guard.entry_info(path)
    if info is None:
        raise FileNotFoundError(path)
    if stat.S_ISREG(info.st_mode):
        return [path]
    files = []
    pending = [path]
    while pending:
        directory = pending.pop()
        if guard.directory_info(directory) is None:
            raise FileNotFoundError(directory)
        for child in directory.iterdir():
            if child.name.startswith("."):
                continue
            info = guard.entry_info(child)
            if info is None:
                raise FileNotFoundError(child)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            else:
                files.append(child)
    if not files:
        raise ValueError(f"原本のディレクトリに配布できるファイルがありません: {path}")
    return files


def make_plan(wiki, raw, requested, *, related=True, include_raw=True, allow_raw=()):
    wiki = absolute_user_path(require(wiki))
    publication = resolve_publication(wiki)
    root = publication.root if publication else wiki
    raw = absolute_user_path(raw)
    extras = {absolute_user_path(p) for p in allow_raw}
    for extra in extras:
        visible = extra.relative_to(wiki.parent) if extra.is_relative_to(wiki.parent) else extra
        if (any(part.startswith(".") for part in visible.parts) or wiki.is_relative_to(extra)
                or raw.is_relative_to(extra) or extra.is_relative_to(wiki)):
            raise ValueError(f"原本として個別指定できない場所です: {extra}")
        WriteGuard(extra.parent, strict_root=True).entry_info(extra)
    pages, omitted = select_pages(root, requested, related)
    files = {}
    locations = {}
    bindings = {}
    total = 0

    def add(name, path, guard):
        nonlocal total
        member_path(name)
        if name in locations:
            if locations[name][0] != path:
                raise ValueError(f"同梱する名前が重複します: {name}")
            return
        with guard.open_read(path) as stream:
            files[name] = transfer(stream)
        total += files[name]["size"]
        if total > MAX_BYTES or len(files) > MAX_FILES:
            raise ValueError("ZIP のサイズか件数が上限を超えています")
        locations[name] = (path, guard)

    for name, (meta, checksum) in sorted(pages.items()):
        key = "pages/" + name
        data = WriteGuard(root).read_bytes(root / name)
        if data is None or digest(data) != checksum:
            raise ValueError(f"読んでいる間に記事が変わりました: {name}")
        bindings[key] = []
        results = resolve_raw({**meta, "type": "source"}, root / name, root, wiki,
                              allowed_roots=[raw], allowed_paths=extras, raw_roots=[raw]) if include_raw else []
        versions = []
        for result in results:
            path = result.path
            if result.state != "resolved" or (not path.is_relative_to(raw) and path not in extras):
                raise ValueError(f"{name} の raw を同梱できません: {result.raw} ({result.state})。"
                                 "必要な外部原本は --allow-raw で個別指定できます")
            if path == raw or (path.is_relative_to(wiki) and not path.is_relative_to(raw)):
                raise ValueError(f"raw の根全体や wiki の管理ファイルは同梱できません: {path}")
            local = path.is_relative_to(raw)
            base = raw if local else path.parent
            guard = WriteGuard(base, strict_root=True)
            prefix = "originals/raw/" if local else "originals/external/"
            binding = prefix + path.relative_to(base).as_posix()
            bindings[key].append(binding)
            members = raw_files(path, guard)
            if local and members == [path]:
                members.extend(raw_images(path, guard))
            for file in members:
                add(prefix + file.relative_to(base).as_posix(), file, guard)
            # 解決と ZIP 用の読み取りの間に起きた差し替えも検出する。
            hashes = ref_values(meta.get("raw_sha256"))
            if binding in files:
                actual = files[binding]["sha256"]
            else:
                rows = [json.dumps([file.relative_to(path).as_posix(),
                                    files[prefix + file.relative_to(base).as_posix()]["sha256"]],
                                   ensure_ascii=False) for file in sorted(members, key=lambda f: f.as_posix())]
                actual = digest("\n".join(rows).encode("utf-8"))
            versions.append(actual)
            if hashes and (len(hashes) != len(results) or actual != hashes[len(versions) - 1].lower()):
                raise ValueError(f"{name} の raw_sha256 が一致しません: {result.raw}")
        if any(k in meta for k in ("raw", "raw_sha256", "rag_job_id")):
            data = relocate(data.decode("utf-8-sig").replace("\r\n", "\n"), bindings[key], versions).encode("utf-8")
        files[key] = {"sha256": digest(data), "size": len(data)}
        locations[key] = data
    manifest = {"format": FORMAT, "files": files, "raw": bindings,
                "omitted_links": [{"page": item["page"], "link": item["link"]} for item in omitted]}
    validate(manifest)
    return manifest, locations, omitted


def validate(manifest):
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("info-hive の記事 ZIP ではありません")
    files = manifest.get("files")
    bindings = manifest.get("raw")
    if (not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES
            or not isinstance(bindings, dict) or len(encoded(manifest)) > MAX_MANIFEST):
        raise ValueError("配布目録の形式か大きさが不正です")
    total = 0
    for name, info in files.items():
        member_path(name)
        if not isinstance(info, dict) or type(info.get("size")) is not int or info["size"] < 0:
            raise ValueError("配布目録のファイルサイズが不正です")
        checked(info.get("sha256"))
        if name.startswith("pages/") and info["size"] > MAX_MANIFEST:
            raise ValueError("記事が 4 MiB を超えています")
        total += info["size"]
    verify_file_names({name: info["sha256"] for name, info in files.items()})
    if total > MAX_BYTES:
        raise ValueError("ZIP の合計サイズが上限を超えています")
    pages = {name for name in files if name.startswith("pages/")}
    if not pages or set(bindings) != pages:
        raise ValueError("原本の対応表と記事の一覧が一致しません")
    originals = set()
    for name in files:
        if name.startswith("originals/"):
            originals.add(name)
            originals.update(p.as_posix() for p in Path(name).parents)
    omitted = manifest.get("omitted_links")
    if not isinstance(omitted, list) or any(not isinstance(item, dict) or
            not isinstance(item.get("page"), str) or "pages/" + item["page"] not in pages
            or not isinstance(item.get("link"), str) for item in omitted):
        raise ValueError("未同梱の参照一覧が不正です")
    for refs in bindings.values():
        if not isinstance(refs, list):
            raise ValueError("原本の対応表が不正です")
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("originals/"):
                raise ValueError("原本の参照先が不正です")
            member_path(ref)
            if ref not in originals:
                raise ValueError(f"目録にない原本です: {ref}")


def pack(output, manifest, locations, expected):
    if digest(encoded(manifest)) != expected:
        raise ValueError("確認した一覧から内容が変わりました")
    guard = WriteGuard(output.parent, strict_root=True)
    if guard.file_info(output) is not None:
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        archive = Path(temp) / "articles.zip"
        with zipfile.ZipFile(archive, "x") as target:
            target.writestr("manifest.json", encoded(manifest))
            for name, location in locations.items():
                stream = io.BytesIO(location) if isinstance(location, bytes) else location[1].open_read(location[0])
                with stream as source, target.open(name, "w", force_zip64=True) as dest:  # encoding: default-ok (ZIP 内のバイナリストリーム)
                    if transfer(source, dest) != manifest["files"][name]:
                        raise ValueError(f"作成中に内容が変わりました: {name}")
        publish_exclusive(archive, output)


def receive(bundle, expected=None):
    """同じファイルハンドルで一覧と本文を検証し、承認時だけ作業領域を残す。"""
    folder = None
    try:
        with zipfile.ZipFile(bundle) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES + 1 or any(item.compress_type != zipfile.ZIP_STORED
                    or item.flag_bits & 1 or item.filename != item.orig_filename
                    or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG) for item in entries):
                raise ValueError("配布用 ZIP の件数または形式が不正です")
            names = {item.filename for item in entries}
            if len(names) != len(entries) or "manifest.json" not in names:
                raise ValueError("配布目録がないか名前が重複しています")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
                raise ValueError("配布目録のサイズが上限を超えています")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            validate(manifest)
            files = manifest["files"]
            if names != {*files, "manifest.json"}:
                raise ValueError("配布目録と ZIP のファイルが一致しません")
            checksum = digest(encoded(manifest))
            if expected is not None:
                if checksum != expected:
                    raise ValueError("確認した ZIP と内容が違います")
                folder = Path(tempfile.mkdtemp(prefix="info-hive-import-"))
            for name, info in files.items():
                if archive.getinfo(name).file_size != info["size"]:
                    raise ValueError(f"サイズが一致しません: {name}")
                with archive.open(name) as source:  # encoding: default-ok (ZIP 内のバイナリストリーム)
                    if folder is None:
                        actual = transfer(source)
                    else:
                        path = folder / member_path(name)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("xb") as dest:
                            actual = transfer(source, dest)
                    if actual != info:
                        raise ValueError(f"原本または記事の指紋が一致しません: {name}")
        return {"manifest": manifest, "plan_sha256": checksum, **({"directory": str(folder)} if folder else {})}
    except BaseException:
        if folder is not None:
            shutil.rmtree(folder)
        raise


def main(argv=None):
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__))
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "pack", "inspect", "unpack", "prepare"):
        cmd = localize_argparse(sub.add_parser(action))
        if action in ("plan", "pack", "prepare"):
            cmd.add_argument("--wiki", type=Path, required=True)
            cmd.add_argument("--raw-root", type=Path, required=True)
        if action in ("plan", "pack"):
            cmd.add_argument("--page", action="append", required=True)
            cmd.add_argument("--only", action="store_true")
            cmd.add_argument("--without-raw", action="store_true")
            cmd.add_argument("--allow-raw", type=Path, action="append", default=[])
        if action != "plan":
            cmd.add_argument("--zip", type=Path, required=True)
        if action in ("pack", "unpack", "prepare"):
            cmd.add_argument("--expect-sha256", required=True)
        if action == "prepare":
            cmd.add_argument("--index-section", nargs=2, action="append", default=[],
                             metavar=("記事の種類", "見出し"), help="単一索引の登録先の見出しを指定する")
            cmd.add_argument("--index-file", nargs=2, action="append", default=[],
                             metavar=("記事または種類", "索引"), help="分割索引の登録先を指定する")
            cmd.add_argument("--use-existing", action="append", default=[],
                             help="内容を確認して再利用する既存記事の wiki 内の相対パス")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            from wiki_bundle_import import prepare
            result = prepare(args.zip, args.expect_sha256, args.wiki, args.raw_root,
                             sections=dict(args.index_section), index_files=dict(args.index_file),
                             use_existing=args.use_existing)
        elif args.action in ("inspect", "unpack"):
            result = receive(args.zip, getattr(args, "expect_sha256", None))
        else:
            manifest, paths, omitted = make_plan(args.wiki, args.raw_root, args.page, related=not args.only,
                                                 include_raw=not args.without_raw, allow_raw=args.allow_raw)
            result = {"manifest": manifest, "plan_sha256": digest(encoded(manifest)), "omitted_links": omitted}
            if args.action == "pack":
                output = absolute_user_path(args.zip)
                if any(output.resolve().is_relative_to(p.resolve()) for p in [args.wiki, args.raw_root, *args.allow_raw]):
                    raise ValueError("ZIP の保存先は wiki と原本の外に指定してください")
                pack(output, manifest, paths, args.expect_sha256)
                result["zip"] = str(output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, RecursionError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
