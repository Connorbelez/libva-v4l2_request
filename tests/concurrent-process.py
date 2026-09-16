#!/usr/bin/env python3
"""Multi-process schedule driver for the concurrent API stress harness.

Runs N copies of tests/concurrent-stress in process mode behind a start
barrier (one stdin byte per worker), collects each worker's result line
and enforces a deadline. This is the offline process-isolation half of
the 1/2/4-process schedules from libva-v4l2_request#36: every worker
owns its whole VA driver instance, so the run proves per-process
integrity, exact per-stream hashes and clean teardown with no shared
state. Contention for one real decoder is the separate guarded hardware
gate and is not claimed here.

Usage:
  concurrent-process.py --self-test
  concurrent-process.py WORKER_BIN [--processes N] [--frames K]
                        [--reps R] [--seed S] [--deadline SECONDS]

--self-test validates the aggregation and deadline logic offline without
spawning anything and is the Meson-registered smoke case.
"""
import argparse
import re
import subprocess
import sys
import time

WORKER_LINE = re.compile(r"^worker (\d+) frames=(\d+) MD5=([0-9a-f]{32})$")


def parse_worker_output(text, expected_frames):
    """Return the (id, frames, md5) tuples parsed from worker stdout."""
    results = []
    for line in text.splitlines():
        match = WORKER_LINE.match(line)
        if match:
            results.append((int(match.group(1)), int(match.group(2)),
                            match.group(3)))
    return results


def check_run(lines, count, expected_frames, deadline_seen):
    """Validate one repetition: one result line per worker, exact frame
    count and a hex digest each. Returns an error string or None."""
    if deadline_seen:
        return "deadline breached before all workers finished"
    if len(lines) != count:
        return "expected %d worker results, got %d" % (count, len(lines))
    seen = set()
    for worker, frames, digest in lines:
        if worker in seen:
            return "duplicate worker result for %d" % worker
        seen.add(worker)
        if frames != expected_frames:
            return "worker %d verified %d frames, expected %d" % (
                worker, frames, expected_frames)
        if digest == "d41d8cd98f00b204e9800998ecf8427e":  # empty MD5
            return "worker %d reported the empty digest" % worker
    if seen != set(range(count)):
        return "worker ids %s != expected %s" % (
            sorted(seen), list(range(count)))
    return None


def self_test():
    sample = "\n".join([
        "schedule worker streams=1 frames=4 reps=1 seed=0x1",
        "stream 0 frames=4 MD5=0123456789abcdef0123456789abcdef",
        "worker 0 frames=4 MD5=0123456789abcdef0123456789abcdef",
        "PASS worker",
    ])
    lines = parse_worker_output(sample, 4)
    assert len(lines) == 1 and lines[0][0] == 0 and lines[0][1] == 4, lines
    assert check_run(lines, 1, 4, False) is None
    assert "expected 1 worker results, got 0" in check_run([], 1, 4, False)
    assert "verified 3 frames" in check_run(
        [(0, 3, "0123456789abcdef0123456789abcdef")], 1, 4, False)
    assert "deadline breached" in check_run(lines, 1, 4, True)
    bad_ids = [(1, 4, "0123456789abcdef0123456789abcdef")]
    assert "worker ids" in check_run(bad_ids, 1, 4, False)
    # The digest check must reject the empty MD5 (a worker that verified
    # nothing at all must never pass).
    empty = [(0, 4, "d41d8cd98f00b204e9800998ecf8427e")]
    assert "empty digest" in check_run(empty, 1, 4, False)
    print("self-test: worker output validation OK")
    return 0


def run_processes(worker, count, frames, reps, seed, deadline):
    rep_seed = seed
    for rep in range(reps):
        rep_seed = (rep_seed ^ 0x5bf03635 ^ rep) * 6364136223846793005 & \
            ((1 << 64) - 1)
        workers = []
        try:
            for worker_id in range(count):
                workers.append(subprocess.Popen(
                    [worker, "worker", str(worker_id), str(frames),
                     str(rep_seed)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True))
            # Start barrier: every worker blocks on one stdin byte.
            for proc in workers:
                proc.stdin.write("S")
                proc.stdin.flush()
            outputs = []
            breached = False
            limit = time.monotonic() + deadline
            for proc in workers:
                remaining = max(0.0, limit - time.monotonic())
                try:
                    out, _ = proc.communicate(timeout=remaining)
                except subprocess.TimeoutExpired:
                    breached = True
                    proc.kill()
                    out, _ = proc.communicate()
                outputs.append((proc.returncode, out))
        finally:
            for proc in workers:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        lines = []
        for returncode, out in outputs:
            if returncode != 0:
                print("worker failed (exit %d):\n%s" % (returncode, out))
                return 1
            lines.extend(parse_worker_output(out, frames))
        error = check_run(lines, count, frames, breached)
        if error:
            print("processes-%d rep %d FAILED: %s" % (count, rep, error))
            return 1
        for worker_id, frames_ok, digest in sorted(lines):
            print("worker %d frames=%d MD5=%s" % (worker_id, frames_ok,
                                                  digest))
    print("PASS processes-%d reps=%d seed=%#x" % (count, reps, seed))
    return 0


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("worker", nargs="?", default=None)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--seed", type=lambda x: int(x, 0), default=0xC0FFEE)
    parser.add_argument("--deadline", type=float, default=120.0)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.worker or args.processes < 1 or args.frames < 2 or \
            args.reps < 1:
        parser.print_usage(sys.stderr)
        return 2
    return run_processes(args.worker, args.processes, args.frames,
                         args.reps, args.seed, args.deadline)


if __name__ == "__main__":
    sys.exit(main())
