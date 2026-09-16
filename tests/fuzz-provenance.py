#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline check that fuzz seeds match tests/fuzz/provenance.json."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROVENANCE = ROOT / "fuzz" / "provenance.json"
HOST_PATH = re.compile(r"(/Users/|/home/|\\\\Users\\\\|~)")


def fail(msg: str) -> None:
    print("fuzz-provenance: " + msg, file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    data = json.loads(PROVENANCE.read_text())
    if data.get("schema_version") != "1":
        fail("unsupported schema_version")
    seeds = data.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        fail("seeds list missing")
    seen = set()
    for entry in seeds:
        rel = entry.get("path")
        if not rel or not isinstance(rel, str) or rel.startswith("/"):
            fail("seed path must be relative: %r" % rel)
        if HOST_PATH.search(json.dumps(entry)):
            fail("host path in provenance for %s" % rel)
        path = ROOT / rel
        if not path.is_file():
            fail("missing seed %s" % rel)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            fail("%s sha256 %s != %s" % (rel, digest, entry.get("sha256")))
        if entry.get("licence") != "GPL-3.0-or-later":
            fail("%s licence must be GPL-3.0-or-later" % rel)
        if entry.get("redistributable") is not True:
            fail("%s must be redistributable synthetic data" % rel)
        if entry.get("origin") not in ("in-tree-test-fixture", "generated-locally"):
            fail("%s origin %r" % (rel, entry.get("origin")))
        if rel in seen:
            fail("duplicate %s" % rel)
        seen.add(rel)
    on_disk = {str(p.relative_to(ROOT)) for p in (ROOT / "fuzz" / "seeds").rglob("*") if p.is_file()}
    missing = on_disk - seen
    if missing:
        fail("unlisted seed files: %s" % ", ".join(sorted(missing)))
    print("fuzz-provenance: %d seeds ok" % len(seen))


if __name__ == "__main__":
    main()
