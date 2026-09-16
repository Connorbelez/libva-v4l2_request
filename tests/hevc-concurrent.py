#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Seeded multi-process HEVC VA-API runner for concurrency research.

Must run under tests/hwguard.py. Does not unload modules. Stop on the first
hash mismatch. This script does not close historical corruption as fixed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from multiprocessing import Barrier, Process, Queue, set_start_method
from pathlib import Path

LEASE_ENV = "LIBVA_HW_GUARD_LEASE"

VECTORS = {
    "SLIST_B_Sony_9": {
        "rel": "SLIST_B_Sony_9/SLIST_B_Sony_9/SLIST_B_Sony_9.bin",
        "expected": "312b4ebb3e885587d5e12b78aa95977a",
        "frames": 65,
    },
    "SLIST_D_Sony_9": {
        "rel": "SLIST_D_Sony_9/SLIST_D_Sony_9/str.bin",
        "expected": "8d38ce43b17627ff1f86e526bfb7ffc4",
        "frames": 65,
    },
    "RAP_B_Bossen_2": {
        "rel": "RAP_B_Bossen_2/RAP_B_Bossen_2.bit",
        "expected": "f38befcc280f2fd0d23f48f807f843c2",
        "frames": 80,
    },
    "RAP_A_docomo_6": {
        "rel": "RAP_A_docomo_6/RAP_A_docomo_6.bit",
        "expected": "8a536a80ed42b37b1ac5810bc046f82a",
        "frames": 86,
    },
}

SCHEDULES = {
    "pair-bd": ["SLIST_B_Sony_9", "SLIST_D_Sony_9"],
    "pair-br": ["SLIST_B_Sony_9", "RAP_B_Bossen_2"],
    "pair-dr": ["SLIST_D_Sony_9", "RAP_B_Bossen_2"],
    "four-named": ["SLIST_B_Sony_9", "SLIST_D_Sony_9", "RAP_B_Bossen_2", "RAP_A_docomo_6"],
    "four-slist-b": ["SLIST_B_Sony_9"] * 4,
}


def parse_md5(text: str) -> tuple[str | None, int]:
    digest = None
    frames = 0
    for line in text.splitlines():
        if line.startswith("frame "):
            frames += 1
        elif line.startswith("MD5="):
            digest = line.split("=", 1)[1].strip()
    return digest, frames


def worker(slot: int, name: str, bit: Path, expected: str, frame_check: Path,
           driver: Path, out_dir: Path, barrier: Barrier, queue: Queue) -> None:
    env = os.environ.copy()
    env["LIBVA_DRIVERS_PATH"] = str(driver)
    env["LIBVA_DRIVER_NAME"] = "v4l2_request"
    frames_path = out_dir / f"slot{slot}-{name}.frames"
    log_path = out_dir / f"slot{slot}-{name}.log"
    barrier.wait(timeout=30)
    started = time.monotonic()
    with frames_path.open("w") as stdout, log_path.open("w") as stderr:
        proc = subprocess.run(
            [str(frame_check), "vaapi", str(bit), "yuv420p"],
            env=env, stdout=stdout, stderr=stderr, timeout=120,
        )
    elapsed = time.monotonic() - started
    digest, frames = parse_md5(frames_path.read_text())
    queue.put({
        "slot": slot,
        "vector": name,
        "expected": expected,
        "actual": digest,
        "frames": frames,
        "returncode": proc.returncode,
        "match": digest == expected and proc.returncode == 0,
        "elapsed_s": round(elapsed, 3),
    })


def run_schedule(names: list[str], resources: Path, frame_check: Path,
                 driver: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(names)
    barrier = Barrier(n)
    queue: Queue = Queue()
    procs = []
    for slot, name in enumerate(names):
        info = VECTORS[name]
        bit = resources / info["rel"]
        if not bit.is_file():
            raise SystemExit(f"missing bitstream {bit}")
        proc = Process(
            target=worker,
            args=(slot, name, bit, info["expected"], frame_check, driver, out_dir, barrier, queue),
        )
        procs.append(proc)
        proc.start()
    results = []
    for _ in procs:
        results.append(queue.get(timeout=180))
    for proc in procs:
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
    results.sort(key=lambda item: item["slot"])
    return {
        "vectors": names,
        "ok": all(item["match"] for item in results),
        "slots": results,
    }


def software_check(names: list[str], resources: Path, frame_check: Path, out_dir: Path) -> dict:
    out = {}
    for name in names:
        info = VECTORS[name]
        bit = resources / info["rel"]
        text = subprocess.check_output(
            [str(frame_check), "software", str(bit), "yuv420p"],
            timeout=120, text=True,
        )
        digest, frames = parse_md5(text)
        (out_dir / f"software-{name}.frames").write_text(text)
        out[name] = {
            "expected": info["expected"],
            "actual": digest,
            "frames": frames,
            "match": digest == info["expected"],
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--resources", type=Path)
    parser.add_argument("--frame-check", type=Path)
    parser.add_argument("--schedule", choices=sorted(SCHEDULES), default="pair-bd")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seed", default="1")
    parser.add_argument("--output", type=Path, required=False)
    parser.add_argument("--software-only", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        for name, info in VECTORS.items():
            if len(info["expected"]) != 32:
                print(f"{name} expected md5 is not 32 hex", file=sys.stderr)
                return 1
        if set(SCHEDULES["four-named"]) != set(VECTORS):
            print("four-named does not cover VECTORS", file=sys.stderr)
            return 1
        print("hevc-concurrent self-test ok: four named vectors, five schedules")
        return 0
    if not os.environ.get(LEASE_ENV) and not args.software_only:
        print("ungarded hardware run refused; wrap with tests/hwguard.py", file=sys.stderr)
        return 2
    if not args.resources or not args.frame_check or not args.output:
        parser.error("--resources, --frame-check and --output are required")
    if not args.software_only and not args.driver:
        parser.error("--driver is required for hardware")
    args.output.mkdir(parents=True, exist_ok=True)
    names = SCHEDULES[args.schedule]
    record = {
        "seed": args.seed,
        "schedule": args.schedule,
        "repeat": args.repeat,
        "vectors": names,
        "kernel_patch_0006": (
            "HEVC next-request slice-count race is already applied in the "
            "omarchy-m1-video patchset; this harness looks for remaining pixel mismatches"
        ),
        "software": software_check(sorted(set(names)), args.resources, args.frame_check, args.output),
        "runs": [],
    }
    if not all(item["match"] for item in record["software"].values()):
        print("software reference failed; aborting hardware", file=sys.stderr)
        (args.output / "summary.json").write_text(json.dumps(record, indent=2) + "\n")
        return 1
    if args.software_only:
        (args.output / "summary.json").write_text(json.dumps(record, indent=2) + "\n")
        print("software references match")
        return 0
    mismatch = False
    try:
        for n in range(args.repeat):
            run_dir = args.output / f"run-{n:02d}"
            result = run_schedule(names, args.resources, args.frame_check, args.driver, run_dir)
            result["repeat_index"] = n
            record["runs"].append(result)
            print(f"run {n} {args.schedule} ok={result['ok']}", flush=True)
            if not result["ok"]:
                mismatch = True
                break
    finally:
        (args.output / "summary.json").write_text(json.dumps(record, indent=2) + "\n")
        fd = os.open(args.output / "summary.json", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return 1 if mismatch else 0


if __name__ == "__main__":
    try:
        set_start_method("fork")
    except RuntimeError:
        pass
    sys.exit(main())
