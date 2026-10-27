#!/usr/bin/env python3
"""Retail-data guard: refuse to commit game data, disc images or lab files.

Default mode checks the files staged for commit (as staged, not as on disk).
With --all it checks every tracked file plus untracked files that are not
ignored. Exits 1 if any file breaks a rule, 0 otherwise.

Rules (see docs/clean-room.md and docs/media-policy.md):
  - extension is a disc image, executable, archive, movie, save or capture type;
  - content starts with the MST archive magic (FANG or GNAF);
  - file is larger than 5 MB;
  - path contains the lab folder's name.

Do not weaken this script to make a check pass. Only the owner edits guard files.
Standard library only.
"""

import argparse
import os
import subprocess
import sys

BLOCKED_EXTENSIONS = {
    ".iso", ".gcm", ".rvz", ".ciso", ".wbfs", ".nkit",
    ".dol", ".rel", ".mst", ".bik", ".gci", ".sav", ".dtm",
}
BLOCKED_MAGIC = (b"FANG", b"GNAF")
MAX_BYTES = 5 * 1024 * 1024
LAB_FOLDER_NAME = "ma-lab"


def git(*args, binary=False):
    out = subprocess.run(["git", *args], check=True, capture_output=True).stdout
    return out if binary else out.decode("utf-8", "surrogateescape")


def split_nul(text):
    return [p for p in text.split("\0") if p]


def staged_files():
    """Yield (path, size, head) for each added, copied, modified or renamed staged file."""
    paths = split_nul(git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))
    for path in paths:
        spec = ":" + path
        size = int(git("cat-file", "-s", spec).strip())
        head = git("cat-file", "blob", spec, binary=True)[:4] if size else b""
        yield path, size, head


def tree_files():
    """Yield (path, size, head) for tracked and untracked, non-ignored files."""
    paths = split_nul(git("ls-files", "-z", "--cached", "--others", "--exclude-standard"))
    for path in sorted(set(paths)):
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4)
        yield path, size, head


def problems_for(path, size, head):
    found = []
    lower = path.lower()
    _, ext = os.path.splitext(lower)
    if ext in BLOCKED_EXTENSIONS:
        found.append(f"blocked file type {ext}")
    if head in BLOCKED_MAGIC:
        found.append(f"starts with MST archive magic {head.decode()}")
    if size > MAX_BYTES:
        found.append(f"larger than 5 MB ({size} bytes)")
    if LAB_FOLDER_NAME in lower:
        found.append(f"path contains the lab folder name '{LAB_FOLDER_NAME}'")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true",
                        help="check the whole tree instead of the staged files")
    args = parser.parse_args(argv)

    try:
        os.chdir(git("rev-parse", "--show-toplevel").strip())
        files = tree_files() if args.all else staged_files()
        failures = [(p, probs) for p, s, h in files if (probs := problems_for(p, s, h))]
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"check_no_retail: could not run git: {exc}", file=sys.stderr)
        return 2

    if failures:
        print("check_no_retail: retail data or lab files must never be committed.", file=sys.stderr)
        for path, probs in failures:
            print(f"  {path}: {'; '.join(probs)}", file=sys.stderr)
        print("See docs/clean-room.md. Do not edit this guard to get past it.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
