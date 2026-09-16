#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline check of the pinned RPS_E research fixture. No decoder."""
from __future__ import annotations

import json
import sys
from pathlib import Path

R11_VA = "b09ac8e0bd31a96d8354505d7c2ebdd5"
EXPECTED = "c30d38bbd7ea483dc7ffe81325118e16"
GST_V4L2 = "53952960ec7512d9cb64f8c7020ece03"
RPS_B = "6d1ed392b067050ebd3a24a37281da03"
STREAM_SHA256 = "b82c5c8c251943cc41b59e7f63f72890641fdcdbcec8e916223e4ee28a16c7ad"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "tests" / "fixtures" / "rps-e" / "rps-e-research.json").is_file():
            return candidate
    raise SystemExit("missing tests/fixtures/rps-e/rps-e-research.json")


def main() -> int:
    root = repo_root()
    data = json.loads((root / "tests" / "fixtures" / "rps-e" / "rps-e-research.json").read_text())
    docs = (root / "docs" / "RPS_E.md").read_text()
    errors: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    va_wrong = data["vaapi_wrong_indices"]
    gst_wrong = data["gst_v4l2_wrong_indices"]
    check(data["expected_md5"] == EXPECTED, "expected software MD5 drifted")
    check(data["software_md5"] == EXPECTED, "software MD5 drifted")
    check(data["vaapi_md5"] == R11_VA == data["r11_vaapi_md5"], "VA-API MD5 is not the r11 baseline")
    check(data["gst_v4l2_md5"] == GST_V4L2, "GStreamer V4L2 MD5 drifted")
    check(data["gst_v4l2_md5"] != EXPECTED, "GStreamer V4L2 must not equal software")
    check(data["gst_v4l2_md5"] != R11_VA, "GStreamer V4L2 must not equal the VA-API baseline")
    check(data["stream"]["sha256"] == STREAM_SHA256, "RPS_E stream SHA-256 drifted")
    check(data["stream"]["frames"] == 300, "RPS_E frame count")
    check(data["rps_b"]["vaapi_md5"] == RPS_B, "RPS_B VA-API checksum drifted")
    check(data["rps_b"]["gst_v4l2_md5"] == RPS_B, "RPS_B GStreamer checksum drifted")
    check(data["rps_b"]["wrong_frames"] == 0, "RPS_B must stay 300/300")
    check(va_wrong and va_wrong[0] == 31, "VA-API first mismatch must be frame 31")
    check(gst_wrong and gst_wrong[0] == 26, "GStreamer first mismatch must be frame 26")
    check(len(va_wrong) == 26, "VA-API wrong-frame count")
    check(len(gst_wrong) == 25, "GStreamer wrong-frame count")

    by_index = {row["index"]: row for row in data["mismatches"]}
    check(set(by_index) == set(va_wrong) | set(gst_wrong),
          "mismatch table is not the union of both paths")
    for index in va_wrong:
        row = by_index[index]
        check(row["vaapi_md5"] != row["software_md5"],
              f"VA mismatch row {index} hashes equal software")
    for index in gst_wrong:
        row = by_index[index]
        check(row["gst_v4l2_md5"] != row["software_md5"],
              f"GST mismatch row {index} hashes equal software")
    for index, row in by_index.items():
        if index not in va_wrong:
            check(row["vaapi_md5"] == row["software_md5"],
                  f"VA-only-ok row {index} should match software")
        if index not in gst_wrong:
            check(row["gst_v4l2_md5"] == row["software_md5"],
                  f"GST-only-ok row {index} should match software")

    for digest in (EXPECTED, R11_VA, GST_V4L2, RPS_B, STREAM_SHA256):
        check(digest in docs, f"docs/RPS_E.md missing digest {digest}")

    if errors:
        print("\n".join(f"- {e}" for e in errors), file=sys.stderr)
        return 1
    print(
        "rps-e research fixture ok (offline, no decoder): "
        f"VA {len(va_wrong)} wrong frames (r11 hash), "
        f"V4L2 {len(gst_wrong)} wrong frames (pinned different hash), "
        "RPS_B checksums match"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
