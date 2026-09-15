#!/usr/bin/env python3
"""Check downloaded Fluster vectors without resizing frames or allowing fallback.

Run hardware checks through avd-lab's guard with a finite outer deadline.
This script builds frame-check.c, records every frame checksum, and stops on timeout.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


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
    if args.vectors:
        missing = set(args.vectors) - {v["name"] for v in vectors}
        if missing:
            parser.error("unknown vectors: " + ", ".join(sorted(missing)))
        vectors = [v for v in vectors if v["name"] in args.vectors]
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    executable = output / "frame-check"
    flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "libavformat",
        "libavcodec", "libavutil", "libswscale"], text=True, timeout=15).split()
    subprocess.run(["cc", "-Wall", "-Wextra", "-O2", "-o", str(executable),
        str(Path(__file__).with_name("frame-check.c")), *flags], check=True, timeout=60)
    env = os.environ.copy()
    mode = "software"
    if args.driver:
        mode = "vaapi"
        env.update(LIBVA_DRIVERS_PATH=str(args.driver.resolve()), LIBVA_DRIVER_NAME="v4l2_request")
    failures = 0
    with (output / "results.jsonl").open("x") as log:
        def record(value):
            log.write(json.dumps(value) + "\n")
            log.flush()
            os.fsync(log.fileno())
        record(dict(event="start", suite=suite["name"], decoder=mode,
                    driver=env.get("LIBVA_DRIVERS_PATH"),
                    high10=env.get("LIBVA_V4L2_H264_HIGH10", "off"),
                    profile_mismatch=args.profile_mismatch))
        for index, vector in enumerate(vectors):
            path = args.resources / suite["name"] / vector["name"] / vector["input_file"]
            cmd = [str(executable), mode, str(path.resolve()), vector["output_format"]]
            if args.profile_mismatch:
                cmd.append("--allow-profile-mismatch")
            record(dict(event="start-vector", vector=vector["name"], command=cmd))
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
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        print("Child did not exit after SIGKILL; stop hardware testing.", file=sys.stderr)
                    return 2
            frames = (output / (stem + ".frames")).read_text()
            digest = re.search(r"^MD5=([0-9a-f]{32})$", frames, re.MULTILINE)
            actual = digest.group(1) if digest else None
            success = status == 0 and actual == vector["result"]
            failures += not success
            record(dict(event="result", vector=vector["name"], returncode=status,
                        expected=vector["result"], actual=actual,
                        frames=len(re.findall(r"^frame ", frames, re.MULTILINE)),
                        success=success, files=stem))
            print(f"{vector['name']}: {'PASS' if success else 'FAIL'}", flush=True)
        record(dict(event="finished", passed=len(vectors)-failures, total=len(vectors)))
    print(f"{len(vectors)-failures}/{len(vectors)} passed; records: {output}")
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())
