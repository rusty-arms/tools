#!/usr/bin/env python3
"""Check that relative links in tracked Markdown files point to files that exist.

Usage: check_links.py [FILE ...]   (default: every tracked *.md file)
Exits 1 if any link is broken. External links (http, https, mailto) and
pure #anchors are not checked. Standard library only.
"""

import os
import re
import subprocess
import sys

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP = ("http://", "https://", "mailto:", "#")


def tracked_markdown():
    out = subprocess.run(["git", "ls-files", "-z", "*.md"], check=True, capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def broken_links(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # Ignore links inside fenced code blocks.
    text = re.sub(r"```.*?```", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    for lineno, line in enumerate(text.splitlines(), 1):
        for target in LINK.findall(line):
            if target.startswith(SKIP):
                continue
            target = target.split("#", 1)[0]
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
            if not os.path.exists(resolved):
                yield lineno, target


def main(argv):
    os.chdir(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                            capture_output=True, text=True).stdout.strip())
    files = [p for p in argv if p.endswith(".md")] if argv else tracked_markdown()
    failures = [(p, n, t) for p in files if os.path.isfile(p) for n, t in broken_links(p)]
    for path, lineno, target in failures:
        print(f"{path}:{lineno}: broken link to {target}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
