#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check downloaded Fluster vectors without resizing frames or allowing fallback.

Run hardware checks through tests/hwguard.py with a finite outer deadline.
This script builds frame-check.c, records every frame checksum, and stops on timeout.
It also writes a schema-validated summary.json for tests/compare-results.py.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformance_result import (
    SUITE_KEYS,
    SuiteResult,
    VectorResult,
    infer_category,
    parse_frame_log,
    redact_argv,
    redact_text,
    write_summary,
)


def git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True, timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path, help="Fluster suite JSON")
    parser.add_argument("resources", type=Path, help="Fluster resources directory")
    parser.add_argument("--driver", type=Path, help="build/src (omit for software)")
    parser.add_argument("--output", type=Path, required=True, help="new result directory")
    parser.add_argument("--vectors", nargs="+", help="only these vector names")
    parser.add_argument("--profile-mismatch", action="store_true",
                        help="explicitly allow FFmpeg's hardware profile override")
    parser.add_argument("--timeout", type=float, default=120, help="seconds per vector")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    suite = json.loads(args.suite.read_text())
    vectors = suite["test_vectors"]
    subset = None
    if args.vectors:
        missing = set(args.vectors) - {v["name"] for v in vectors}
        if missing:
            parser.error("unknown vectors: " + ", ".join(sorted(missing)))
        vectors = [v for v in vectors if v["name"] in args.vectors]
        subset = {
            "rationale": "explicit --vectors selection; not a full-suite total",
            "selected": [v["name"] for v in vectors],
        }
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    executable = output / "frame-check"
    flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "libavformat",
        "libavcodec", "libavutil", "libswscale"], text=True, timeout=15).split()
    subprocess.run(["cc", "-Wall", "-Wextra", "-O2", "-o", str(executable),
        str(Path(__file__).with_name("frame-check.c")), *flags], check=True, timeout=60)
    env = os.environ.copy()
    decoder = "software"
    mode = "software"
    if args.driver:
        if not os.environ.get("LIBVA_HW_GUARD_LEASE"):
            print("ungarded hardware run refused; use: "
                  "python3 tests/hwguard.py -- python3 tests/conformance.py ... --driver DIR",
                  file=sys.stderr)
            return 2
        decoder = "vaapi"
        mode = "hardware"
        env.update(LIBVA_DRIVERS_PATH=str(args.driver.resolve()), LIBVA_DRIVER_NAME="v4l2_request")
    failures = 0
    recorded: list[VectorResult] = []
    abort = None
    complete = False
    exit_status = 1
    with (output / "results.jsonl").open("x") as log:
        def record(value):
            log.write(json.dumps(value) + "\n")
            log.flush()
            os.fsync(log.fileno())
        record(dict(event="start", suite=suite["name"], decoder=decoder,
                    driver=redact_text(str(env.get("LIBVA_DRIVERS_PATH") or "")),
                    high10=env.get("LIBVA_V4L2_H264_HIGH10", "off"),
                    profile_mismatch=args.profile_mismatch))
        try:
            for index, vector in enumerate(vectors):
                path = args.resources / suite["name"] / vector["name"] / vector["input_file"]
                cmd = [str(executable), decoder, str(path.resolve()), vector["output_format"]]
                if args.profile_mismatch:
                    cmd.append("--allow-profile-mismatch")
                record(dict(event="start-vector", vector=vector["name"],
                            command=redact_argv(cmd)))
                # Numeric filenames also keep names from external suite files out of output paths.
                stem = f"{index:04d}"
                with (output / (stem + ".frames")).open("w") as stdout, \
                     (output / (stem + ".log")).open("w") as stderr:
                    proc = subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr)
                    try:
                        status = proc.wait(timeout=args.timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        record(dict(event="timeout", vector=vector["name"], files=stem))
                        recorded.append(VectorResult(
                            name=vector["name"], success=False, category="timeout",
                            expected_md5=vector.get("result"),
                        ))
                        abort = {"kind": "timeout", "vector": vector["name"]}
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            print("Child did not exit after SIGKILL; stop hardware testing.",
                                  file=sys.stderr)
                        exit_status = 2
                        return 2
                frames_text = (output / (stem + ".frames")).read_text()
                log_text = (output / (stem + ".log")).read_text()
                summaries, actual = parse_frame_log(frames_text)
                if actual is None:
                    digest = re.search(r"^MD5=([0-9a-f]{32})$", frames_text, re.MULTILINE)
                    actual = digest.group(1) if digest else None
                # The helper rejects non-VAAPI source frames in hardware
                # mode. Its printed format describes downloaded/hash pixels,
                # not the decoder that produced them.
                # FFmpeg's "Failed setup for format vaapi" means rejection,
                # not fallback: this helper refuses software frames. Use its
                # explicit source-frame verdict instead of a generic log hint.
                fallback = mode == "hardware" and (
                    "frame-check: software frame rejected in hardware mode" in log_text
                )
                success = status == 0 and actual == vector["result"] and not fallback
                failures += not success
                sizes = []
                for frame in summaries:
                    size = frame.size()
                    if size not in sizes:
                        sizes.append(size)
                category = infer_category(
                    mode, success, actual, vector.get("result"), fallback=fallback
                )
                mismatch = None
                if category == "checksum_mismatch" and summaries:
                    mismatch = {
                        "vector": vector["name"],
                        "reason": "stream_md5",
                        "expected_md5": vector.get("result"),
                        "candidate_md5": actual,
                        "index": summaries[0].index,
                        "candidate": summaries[0].to_dict(),
                    }
                recorded.append(VectorResult(
                    name=vector["name"],
                    success=success,
                    category=category,
                    expected_md5=vector.get("result"),
                    actual_md5=actual,
                    frames=len(summaries) if summaries else len(re.findall(r"^frame ", frames_text, re.MULTILINE)),
                    native_sizes=sizes,
                    frame_summaries=summaries,
                    first_differing_frame=mismatch,
                    returncode=status,
                ))
                record(dict(event="result", vector=vector["name"], returncode=status,
                            expected=vector["result"], actual=actual,
                            frames=recorded[-1].frames,
                            success=success, files=stem,
                            native_sizes=sizes, category=category,
                            software_fallback=fallback))
                print(f"{vector['name']}: {'PASS' if success else 'FAIL'}", flush=True)
            record(dict(event="finished", passed=len(vectors)-failures, total=len(vectors)))
            complete = True
            exit_status = int(failures != 0)
        except KeyboardInterrupt:
            abort = abort or {
                "kind": "user",
                "vector": recorded[-1].name if recorded else "",
            }
            exit_status = 2
            raise
        except Exception as exc:
            abort = abort or {
                "kind": "decode_fault",
                "vector": recorded[-1].name if recorded else "",
            }
            print(f"conformance run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_status = 2
            return 2
        finally:
            if not complete and abort is None:
                abort = {"kind": "user", "vector": recorded[-1].name if recorded else ""}
            result = SuiteResult(
                suite=str(suite["name"]),
                suite_key=SUITE_KEYS.get(suite["name"], ""),
                mode=mode,
                complete=complete and abort is None,
                abort=abort,
                passed=sum(1 for item in recorded if item.success),
                total=len(vectors) if vectors else max(len(recorded), 1),
                vectors=recorded,
                passing_frames=sum(item.frames for item in recorded if item.success),
                high10=str(env.get("LIBVA_V4L2_H264_HIGH10") or "off"),
                profile_mismatch=bool(args.profile_mismatch),
                subset=subset,
                provenance={
                    "source_commit": git_head(),
                    "driver": redact_text(str(env.get("LIBVA_DRIVERS_PATH") or "")),
                },
                command={
                    "redacted": True,
                    "argv": ["conformance.py", suite["name"], "<resources>", decoder],
                },
            )
            try:
                write_summary(output / "summary.json", result)
            except Exception as exc:
                print(f"summary not written: {exc}", file=sys.stderr)
                exit_status = 2
    print(f"{len(vectors)-failures}/{len(vectors)} passed; records: {output}")
    return exit_status


if __name__ == "__main__":
    sys.exit(main())
