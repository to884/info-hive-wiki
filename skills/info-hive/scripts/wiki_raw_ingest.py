#!/usr/bin/env python3
"""ローカルの資料を raw として保存し、出典に書くパスと版を返す。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from wiki_text import localize_argparse
import wiki_initialized
from wiki_encoding import detect
from wiki_fsguard import WriteGuard
from wiki_pages import absolute_user_path, user_directory_path, user_file_path
from wiki_raw import file_identity, raw_digest, within
from wiki_raw_roots import read_raw_roots, record_raw_root


def _spellings(root: Path) -> tuple[Path, Path]:
    return root, root.resolve()


def _recorded(wiki: Path) -> list[Path]:
    roots, unchecked = read_raw_roots(wiki)
    if unchecked:
        detail = "、".join(f"{item['path']}（{item['reason']}）" for item in unchecked)
        raise ValueError(f"過去の raw の根を確認できない: {detail}")
    return roots


def _containing(path: Path, roots) -> Path | None:
    return min((root for root in roots if within(path, root)), key=lambda root: len(root.parts), default=None)


def _target(raw: Path, name: str) -> Path:
    if Path(name).name != name or not name or name.startswith("."):
        raise ValueError("保存先には、ピリオドで始まらない単独の名前を指定する")
    return raw / name


def _ref(wiki: Path, path: Path, sha256: str, status: str) -> dict:
    # 通常の兄弟配置では、プロジェクトを移しても参照できるよう、wiki の親からの相対パスを使う。
    if within(path, wiki.parent / "raw") and wiki.resolve(strict=True) == wiki:
        ref = path.relative_to(wiki.parent)
    else:
        ref = path
    return {"status": status, "raw_path": str(path), "raw_ref": ref.as_posix(), "raw_sha256": sha256}


def _source_info(path: Path, wiki: Path, origin: Path | None) -> os.stat_result:
    if origin is not None and path != origin:
        info = WriteGuard(origin).entry_info(path)
        if info is None:
            raise FileNotFoundError(path)
    else:
        try:
            info = path.stat()
        except FileNotFoundError:
            user_file_path(path, wiki)
            raise
    directory = stat.S_ISDIR(info.st_mode)
    if directory:
        user_directory_path(path, wiki)
    else:
        user_file_path(path, wiki)
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError(f"通常のファイルでもディレクトリでもない: {path}")
    return info


def _copy_tree(source: Path, stage: Path, wiki: Path, roots: list[Path],
               guard: WriteGuard) -> None:
    stage_real = stage.resolve(strict=True)
    ancestors = set()

    def copy(path: Path, dst: Path) -> None:
        info = _source_info(path, wiki, _containing(path, roots))
        if stat.S_ISREG(info.st_mode):
            with path.open("rb") as stream:
                guard.write_stream(dst, stream)
            return
        identity = file_identity(path, info)
        if identity in ancestors:
            raise ValueError(f"ディレクトリのリンクが循環している: {path}")
        real = path.resolve(strict=True)
        if within(stage_real, real) or within(real, stage_real):
            raise ValueError(f"保存の一時ディレクトリと取り込み元が重なる: {path}")
        guard.ensure_directory(dst)
        ancestors.add(identity)
        try:
            for child in path.iterdir():
                copy(child, dst / child.name)
        finally:
            ancestors.remove(identity)

    copy(source, stage)


def save(wiki: Path, raw: Path, source: Path, name: str | None) -> dict:
    guard = WriteGuard(raw)
    roots = list(_spellings(raw))
    current = _containing(source, roots)
    if current is None:
        roots.extend(_recorded(wiki))
    info = _source_info(source, wiki, _containing(source, roots))
    if current is not None:
        if any(part.startswith(".") for part in source.relative_to(current).parts):
            raise ValueError(f"隠し項目は raw の出典にできない: {source}")
        sha256 = raw_digest(source, origin=current)
        record_raw_root(wiki, raw, writer="wiki_raw_ingest")
        return _ref(wiki, source, sha256, "existing")

    dst = _target(raw, name or source.name)
    directory = stat.S_ISDIR(info.st_mode)
    check = guard.directory_info if directory else guard.file_info
    if check(dst) is not None:
        raise FileExistsError(f"保存先がすでにある。保存済みの原本を確認するか、別の名前を指定する: {dst}")
    record_raw_root(wiki, raw, writer="wiki_raw_ingest")
    if not directory:
        with source.open("rb") as stream:
            guard.write_stream(dst, stream)
        return _ref(wiki, dst, raw_digest(dst, origin=raw), "created")

    # 同じ根へ書く取り込み同士が、POSIX の rename で空の保存先を置き換えないようにする。
    with guard.hold_lock(raw / ".wiki-raw-ingest.lock"):
        stage = guard.temporary_directory(raw, prefix=".wiki-raw-ingest-", suffix=".tmp")
        info = guard.directory_info(stage)
        if info is None:
            raise FileNotFoundError(stage)
        stage_id = file_identity(stage, info)
        try:
            _copy_tree(source, stage, wiki, roots, guard)
            sha256 = raw_digest(stage, origin=raw)
            guard.move_directory(stage, dst)
        finally:
            staged = guard.directory_info(stage)
            if staged is not None and file_identity(stage, staged) == stage_id:
                shutil.rmtree(stage)
    return _ref(wiki, dst, sha256, "created")


def encoding(wiki: Path, raw: Path, source: Path, expected: str, name: str | None) -> dict:
    origin = _containing(source, _spellings(raw))
    if origin is None or source == origin:
        raise ValueError(f"保存済みの raw ファイルを指定する: {source}")
    guard = WriteGuard(raw)
    if WriteGuard(origin).file_info(source) is None:
        raise FileNotFoundError(source)
    user_file_path(source, wiki)
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"保存した版から raw の内容が変わった: {source}")
    result = detect(data)
    if name is None or result["encoding"] in ("unknown", "binary"):
        return result
    dst = _target(raw, name)
    if dst == raw / source.relative_to(origin):
        raise ValueError("派生版には元のファイルと別の名前を指定する")
    converted = data.decode(result["encoding"]).encode("utf-8")
    sha256 = hashlib.sha256(converted).hexdigest()
    record_raw_root(wiki, raw, writer="wiki_raw_ingest")
    try:
        guard.write_bytes(dst, converted)
        status = "created"
    except FileExistsError:
        if raw_digest(dst, origin=raw) != sha256:
            raise ValueError(f"保存先に内容の異なる派生ファイルがある: {dst}") from None
        status = "existing"
    return {**result, **_ref(wiki, dst, sha256, status)}


def pdf(wiki: Path, raw: Path, source: Path) -> dict:
    origin = _containing(source, (*_spellings(wiki), *_spellings(raw), *_recorded(wiki)))
    if origin is not None:
        if any(part.startswith(".") for part in source.relative_to(origin).parts):
            raise ValueError(f"隠し項目は raw の出典にできない: {source}")
        if source == origin or WriteGuard(origin).file_info(source) is None:
            raise FileNotFoundError(source)
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError(f"PDF のファイルを確認できない: {source}")
    return {"source": str(source)}


def main(argv: list[str] | None = None) -> int:
    parser = localize_argparse(argparse.ArgumentParser(description=__doc__))
    sub = parser.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("save", "ローカルのファイルやディレクトリを保存する"),
        ("encoding", "保存した原本の文字コードを調べ、必要なら派生版を保存する"),
        ("pdf", "rag へ渡す PDF の指定を検査する"),
    ):
        command = localize_argparse(sub.add_parser(action, help=help_text))
        command.add_argument("--wiki", type=Path, required=True)
        command.add_argument("--raw-root", type=Path, required=True)
        command.add_argument("--source", type=Path, required=True)
        if action != "pdf":
            command.add_argument("--name", help="raw の根の直下に置くファイルかディレクトリの名前")
        if action == "encoding":
            command.add_argument("--expect-sha256", required=True, help="save が返した raw_sha256")
    args = parser.parse_args(argv)
    try:
        wiki = wiki_initialized.require(absolute_user_path(args.wiki))
        raw = absolute_user_path(args.raw_root)
        source = absolute_user_path(args.source)
        if args.action == "save":
            result = save(wiki, raw, source, args.name)
        elif args.action == "encoding":
            result = encoding(wiki, raw, source, args.expect_sha256, args.name)
        else:
            result = pdf(wiki, raw, source)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("encoding") in ("unknown", "binary") else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
