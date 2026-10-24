#!/usr/bin/env python3
"""Build Linux and Windows source release archives.

Usage:
    python packaging/build_release.py

Outputs (in dist/):
    cyberyinyu-<version>-linux.tar.gz
    cyberyinyu-<version>-windows.zip
    SHA256SUMS
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
NAME = f"cyberyinyu-{VERSION}"
DIST = ROOT / "dist"
STAGE = DIST / f"_stage-{os.getpid()}"

_SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".venv", "venv", "logs", "data"}
_SKIP_REL = {
    "memory/global.json",
    "dataset/messages.jsonl",
}


def _ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    base = Path(directory)
    for name in names:
        path = base / name
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = name
        if name in _SKIP_DIRS:
            ignored.add(name)
        elif rel in _SKIP_REL:
            ignored.add(name)
        elif rel.startswith(("memory/groups", "dataset/import", "dataset/cleaned")):
            ignored.add(name)
        elif name == "otowa.env" or name.startswith("otowa.env."):
            ignored.add(name)
        elif name.endswith((".pyc", ".pyo", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
            ignored.add(name)
        elif rel.startswith("personality/stickers/") and name != "README.md":
            ignored.add(name)
    return ignored


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)


def _overlay(src_dir: Path, dst: Path, names: tuple[str, ...]) -> None:
    for name in names:
        source = src_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"缺少打包文件: {source}")
        shutil.copy2(source, dst / name)


def _normalise_bat(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text.replace("\r\n", "\n").replace("\n", "\r\n"))


def _build_tar(source: Path, output: Path) -> None:
    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if info.name.endswith(".sh"):
            info.mode = 0o755
        return info

    with tarfile.open(output, "w:gz") as archive:
        archive.add(source, arcname=NAME, filter=_filter)


def _build_zip(source: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(NAME) / path.relative_to(source)).as_posix())


def _clean_dist() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    for child in DIST.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            print(f"  warning: 未能清理 {child}: {exc}")


def main() -> int:
    if not VERSION:
        raise SystemExit("VERSION 文件为空")

    _clean_dist()
    linux_root = STAGE / "linux" / NAME
    windows_root = STAGE / "windows" / NAME

    print(f"building {NAME}")
    _copy_tree(ROOT, linux_root)
    _copy_tree(ROOT, windows_root)

    _overlay(ROOT / "packaging" / "linux", linux_root,
             ("install.sh", "cyberyinyu.service", "run.sh"))
    _overlay(ROOT / "packaging" / "windows", windows_root,
             ("install.ps1", "start_bot.bat", "view_logs.bat"))
    for path in windows_root.rglob("*.bat"):
        _normalise_bat(path)

    linux_archive = DIST / f"{NAME}-linux.tar.gz"
    windows_archive = DIST / f"{NAME}-windows.zip"
    _build_tar(linux_root, linux_archive)
    _build_zip(windows_root, windows_archive)

    checksums = []
    for artifact in (linux_archive, windows_archive):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        checksums.append(f"{digest}  {artifact.name}")
    with (DIST / "SHA256SUMS").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(checksums) + "\n")

    try:
        shutil.rmtree(STAGE)
    except Exception as exc:  # noqa: BLE001 - 某些环境会拦截批量删除
        print(f"  warning: 未能清理临时目录 {STAGE}: {exc}")
        shutil.rmtree(STAGE, ignore_errors=True)
    for artifact in (linux_archive, windows_archive):
        print(f"  {artifact.name}  {artifact.stat().st_size / 1024:.1f} KiB")
    print(f"  SHA256SUMS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
