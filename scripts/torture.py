#!/usr/bin/env python3
"""Torture runner: freeze/thaw roundtrip against real markdown, byte-for-byte.

Point it at any directory (your own repos, cloned OSS projects):

    python3 scripts/torture.py path/to/repo [more paths...]

For every .md file: split into blocks, freeze every translatable block,
thaw the untouched template, and require the result to be byte-identical
to the original. Any diff is a protected-span pattern bug — the exact class
of bug that would let a model corrupt a command in someone's docs.

Exit 0 if every file roundtrips; 1 otherwise, listing each failure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from koine.blocks import freeze, split_blocks, thaw  # noqa: E402


def roundtrip_file(path: Path) -> list[str]:
    failures = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for b in split_blocks(text):
        if b.kind != "text":
            continue
        frozen = freeze(b.raw)
        try:
            restored = thaw(frozen, frozen.template)
        except ValueError as e:
            failures.append(f"{path}#block{b.index}: thaw failed: {e}")
            continue
        if restored != b.raw:
            failures.append(f"{path}#block{b.index}: roundtrip not byte-identical")
    return failures


def main(paths: list[str]) -> int:
    md_files: list[Path] = []
    for p in paths:
        root = Path(p)
        if root.is_file():
            md_files.append(root)
        else:
            md_files.extend(root.rglob("*.md"))
    if not md_files:
        print("no .md files found")
        return 1
    all_failures = []
    for f in sorted(md_files):
        all_failures.extend(roundtrip_file(f))
    print(f"{len(md_files)} files, {len(all_failures)} failures")
    for fail in all_failures:
        print(f"  - {fail}")
    return 1 if all_failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))
