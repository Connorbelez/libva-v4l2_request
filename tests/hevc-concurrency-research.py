#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline check of the pinned HEVC concurrency campaign. No decoder."""
from __future__ import annotations

import json
import sys
from pathlib import Path

EXPECTED = {
    "SLIST_B_Sony_9": "312b4ebb3e885587d5e12b78aa95977a",
    "SLIST_D_Sony_9": "8d38ce43b17627ff1f86e526bfb7ffc4",
    "RAP_B_Bossen_2": "f38befcc280f2fd0d23f48f807f843c2",
    "RAP_A_docomo_6": "8a536a80ed42b37b1ac5810bc046f82a",
}


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "tests" / "fixtures" / "hevc-concurrency" / "campaign.json").is_file():
            return candidate
    raise SystemExit("missing tests/fixtures/hevc-concurrency/campaign.json")


def main() -> int:
    root = repo_root()
    data = json.loads((root / "tests" / "fixtures" / "hevc-concurrency" / "campaign.json").read_text())
    docs = (root / "docs" / "HEVC_CONCURRENCY.md").read_text()
    errors: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    check(data["total_runs"] == 23, "campaign run count drifted")
    check(data["total_ok"] == 23, "campaign is not all-match")
    check(data["mismatches"] == 0, "campaign recorded a mismatch without a captured first-diff")
    check("0006" in data["kernel_patch_0006"], "0006 distinction missing")
    for name, digest in EXPECTED.items():
        check(digest in docs, f"docs missing {name} digest")
    for sched, block in data["schedules"].items():
        check(block["ok"], f"{sched} not all-ok")
        check(len(block["runs"]) == block["n"], f"{sched} n mismatch")
        for run in block["runs"]:
            for slot in run["slots"]:
                check(slot["match"] and slot["actual"] == slot["expected"],
                      f"{sched} slot {slot['vector']} did not match")
                if slot["vector"] in EXPECTED:
                    check(slot["expected"] == EXPECTED[slot["vector"]],
                          f"{sched} expected md5 drifted for {slot['vector']}")
    check("23/23" in docs, "docs missing 23/23 result")
    check("does **not** close" in docs or "does not close" in docs.lower(),
          "docs must not close historical corruption as fixed")
    if errors:
        print("\n".join(f"- {e}" for e in errors), file=sys.stderr)
        return 1
    print("hevc-concurrency research fixture ok (offline, no decoder): 23/23 clean runs, 0006 distinguished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
