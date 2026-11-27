#!/usr/bin/env python3
"""Extract release notes for a given tag from CHANGELOG.md.

Usage:
    python packaging/extract_release_notes.py v1.2.0 dist/RELEASE_NOTES.md
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
HEADER_RE = re.compile(r"^## \[([^\]]+)\](?:\s*-\s*\S+)?\s*$")


def extract_section(changelog_text: str, version: str) -> tuple[str, str | None]:
    lines = changelog_text.splitlines()
    collecting = False
    collected: list[str] = []
    previous_version: str | None = None

    for line in lines:
        match = HEADER_RE.match(line)
        if match:
            found = match.group(1).strip()
            if not collecting and found == version:
                collecting = True
                continue
            if collecting:
                if found.lower() != "unreleased":
                    previous_version = found
                break
        elif collecting:
            collected.append(line)

    body = "\n".join(collected).strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md 中未找到版本 [{version}] 的更新说明")
    return body, previous_version


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("用法: python packaging/extract_release_notes.py <tag> <output_file>")

    raw_tag = sys.argv[1].strip()
    version = raw_tag[1:] if raw_tag.startswith("v") else raw_tag
    tag_name = f"v{version}"
    output_path = Path(sys.argv[2])

    text = CHANGELOG.read_text(encoding="utf-8")
    body, prev_version = extract_section(text, version)
    repo = os.getenv("GITHUB_REPOSITORY", "MikuMikusuki/CyberYinYu-OSS").strip()

    parts = [f"# CyberYinYu {tag_name}", "", body]
    if prev_version:
        parts.extend([
            "",
            f"**Full Changelog**: https://github.com/{repo}/compare/v{prev_version}...{tag_name}",
        ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote release notes for {tag_name} -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
