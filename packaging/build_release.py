#!/usr/bin/env python3
"""Build Linux and Windows source release archives.

Usage:
    python packaging/build_release.py

Run from a Git working tree. Outputs (in dist/):
    cyberyinyu-<version>-linux.tar.gz
    cyberyinyu-<version>-windows.zip
    SHA256SUMS
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
NAME = f"cyberyinyu-{VERSION}"
DIST = ROOT / "dist"


def _git_paths(*options: str) -> set[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", *options],
            cwd=ROOT, check=True, capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SystemExit("请在 Git 工作区构建发布包，并确保 git 命令可用。") from exc
    return {Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}


def _copy_sources(destination: Path) -> None:
    # 配置备份即使被暂存，也继续按 Git 忽略规则排除。
    files = _git_paths() - _git_paths("--ignored", "--exclude-standard")
    for relative in sorted(files):
        source = ROOT / relative
        if source.resolve() != source or not source.is_file():
            raise SystemExit(f"打包文件缺失或为重定向路径：{relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


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


def main() -> int:
    if not VERSION:
        raise SystemExit("VERSION 文件为空")
    DIST.mkdir(parents=True, exist_ok=True)
    print(f"building {NAME}")
    with tempfile.TemporaryDirectory(prefix="_stage-", dir=DIST) as staging:
        stage = Path(staging)
        linux_root = stage / "linux" / NAME
        windows_root = stage / "windows" / NAME
        _copy_sources(linux_root)
        shutil.copytree(linux_root, windows_root)

        _overlay(linux_root / "packaging" / "linux", linux_root,
                 ("install.sh", "cyberyinyu.service", "run.sh"))
        _overlay(windows_root / "packaging" / "windows", windows_root,
                 ("install.ps1", "start_bot.bat", "view_logs.bat"))
        for path in windows_root.rglob("*.bat"):
            _normalise_bat(path)

        linux_archive = stage / f"{NAME}-linux.tar.gz"
        windows_archive = stage / f"{NAME}-windows.zip"
        _build_tar(linux_root, linux_archive)
        _build_zip(windows_root, windows_archive)

        checksum_file = stage / "SHA256SUMS"
        checksums = []
        for artifact in (linux_archive, windows_archive):
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            checksums.append(f"{digest}  {artifact.name}")
        checksum_file.write_text("\n".join(checksums) + "\n", encoding="utf-8", newline="\n")

        for artifact in (linux_archive, windows_archive):
            size_kib = artifact.stat().st_size / 1024
            artifact.replace(DIST / artifact.name)
            print(f"  {artifact.name}  {size_kib:.1f} KiB")
        checksum_file.replace(DIST / checksum_file.name)
        print(f"  {checksum_file.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
