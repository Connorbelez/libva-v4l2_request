#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline check of the pinned RPS_E research fixture. No decoder."""
from __future__ import annotations

import json
import sys
from pathlib import Path

R11_VA = "b09ac8e0bd31a96d8354505d7c2ebdd5"
EXPECTED = "c30d38bbd7ea483dc7ffe81325118e16"
RPS_B = "6d1ed392b067050ebd3a24a37281da03"
VA_WRONG = [31, 73, 74, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 117, 123, 221]
GST_WRONG = [26, 28, 29, 30, 31, 74, 76, 77, 78, 79, 80, 81, 82, 83, 85, 88, 91, 94, 181, 185, 217, 218, 220, 221, 283]


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "tests" / "fixtures" / "rps-e" / "rps-e-research.json").is_file():
            return candidate
    raise SystemExit("missing tests/fixtures/rps-e/rps-e-research.json")


def main() -> int:
    root = repo_root()
    data = json.loads((root / "tests" / "fixtures" / "rps-e" / "rps-e-research.json").read_text())
    errors: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    check(data["expected_md5"] == EXPECTED, "expected software MD5 drifted")
    check(data["software_md5"] == EXPECTED, "software MD5 drifted")
    check(data["vaapi_md5"] == R11_VA == data["r11_vaapi_md5"], "VA-API MD5 is not the r11 baseline")
    check(data["gst_v4l2_md5"] != EXPECTED, "GStreamer V4L2 must not be recorded as a pass")
    check(data["gst_v4l2_md5"] != R11_VA, "GStreamer V4L2 must remain a different wrong hash than VA-API")
    check(data["vaapi_wrong_indices"] == VA_WRONG, "VA-API wrong-frame list drifted")
    check(data["gst_v4l2_wrong_indices"] == GST_WRONG, "GStreamer wrong-frame list drifted")
    check(data["rps_b"]["vaapi_md5"] == RPS_B, "RPS_B VA-API checksum drifted")
    check(data["rps_b"]["gst_v4l2_md5"] == RPS_B, "RPS_B GStreamer checksum drifted")
    check(data["rps_b"]["wrong_frames"] == 0, "RPS_B must stay 300/300")
    check(data["stream"]["frames"] == 300, "RPS_E frame count")
    check(VA_WRONG[0] == 31, "VA-API first mismatch must be frame 31")
    check(GST_WRONG[0] == 26, "GStreamer first mismatch must be frame 26")
    names = {row["index"] for row in data["mismatches"]}
    check(names == set(VA_WRONG) | set(GST_WRONG), "mismatch table is not the union of both paths")
    check((root / "docs" / "RPS_E.md").is_file(), "missing docs/RPS_E.md")
    if errors:
        print("\n".join(f"- {e}" for e in errors), file=sys.stderr)
        return 1
    print(
        "rps-e research fixture ok: VA 26 B-frames (r11 hash), "
        "V4L2 25 B-frames (different hash), RPS_B 300/300"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
