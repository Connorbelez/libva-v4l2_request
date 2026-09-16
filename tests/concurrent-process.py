#!/usr/bin/env python3
"""Multi-process schedule driver for the concurrent API stress harness.

Runs N copies of tests/concurrent-stress in process mode behind a start
barrier (one stdin byte per worker), derives every worker's expected
digest independently from the declared seed/frame recipe and compares
exactly, and enforces a deadline that covers process creation, the
barrier release and the run itself. This is the offline process-
isolation half of the 1/2/4-process schedules from
libva-v4l2_request#36: every worker owns its whole VA driver instance,
so the run proves per-process integrity, exact per-worker hashes and
clean teardown with no shared state. Contention for one real decoder is
the separate guarded hardware gate and is not claimed here.

Usage:
  concurrent-process.py --self-test [WORKER_BIN]
  concurrent-process.py WORKER_BIN [--processes N] [--frames K]
                        [--reps R] [--seed S] [--deadline SECONDS]

--self-test validates the parsing, deadline and derivation logic offline
and, given a worker binary, additionally runs real negative fixtures
(stalled worker, inherited stdout holder, launch failure) and a real
small process schedule whose digests must match the independent
derivation.
"""
import argparse
import hashlib
import os
import re
import signal
import subprocess
import sys
import time

MASK64 = (1 << 64) - 1
WORKER_LINE = re.compile(r"^worker (\d+) frames=(\d+) MD5=([0-9a-f]{32})$")

# Bounded supported ranges: the schedules are 1/2/4 processes, the
# harness caps frames at 256 and worker ids are single recipe bytes.
MAX_PROCESSES = 4
MAX_FRAMES = 256
MAX_REPS = 1000


def splitmix64(state):
    """The harness's PRNG step (tests/concurrent-stress.c)."""
    state = (state + 0x9E3779B97F4A7C15) & MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return z ^ (z >> 31), state


def fnv1a64(data):
    hash_ = 0xCBF29CE484222325
    for byte in data:
        hash_ = ((hash_ ^ byte) * 0x100000001B3) & MASK64
    return hash_


def frame_bytes(seed, stream, frame):
    """The harness's identifiable slice content for one frame."""
    state = (seed ^ (0x100000001 * (stream + 1)) ^
             (frame * 0xACE5ACE5)) & MASK64
    out = bytearray(96)
    for i in range(0, 96, 8):
        value, state = splitmix64(state)
        out[i:i + 8] = value.to_bytes(8, "little")
    out[0] = 0xC7
    out[1] = stream
    out[2] = frame
    return bytes(out)


def rep_seed(base_seed, rep):
    """Per-repetition seed derivation from the harness main()."""
    mixed = base_seed ^ (0x5BF03635 ^ rep)
    value, _ = splitmix64(mixed)
    return value


def expected_digest(base_seed, worker, frames, rep):
    """Independently derived per-worker digest: the MD5 over the
    little-endian 64-bit frame hashes the harness records."""
    seed = rep_seed(base_seed, rep)
    payload = bytearray()
    for frame in range(frames):
        hash_ = fnv1a64(frame_bytes(seed, worker, frame))
        payload += hash_.to_bytes(8, "little")
    return hashlib.md5(bytes(payload)).hexdigest()


def parse_worker_output(text):
    """Return the (id, frames, md5) tuples parsed from worker stdout."""
    results = []
    for line in text.splitlines():
        match = WORKER_LINE.match(line)
        if match:
            results.append((int(match.group(1)), int(match.group(2)),
                            match.group(3)))
    return results


def check_run(lines, count, expected_frames, deadline_seen, expected):
    """Validate one repetition: one result line per worker, exact frame
    counts and the independently derived digest each. Returns an error
    string or None."""
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
    if seen != set(range(count)):
        return "worker ids %s != expected %s" % (
            sorted(seen), list(range(count)))
    for worker, frames, digest in lines:
        if digest != expected[worker]:
            return ("worker %d digest %s does not match the derived %s" %
                    (worker, digest, expected[worker]))
    return None


def kill_process_group(proc):
    """Bounded, owned cleanup: kill the worker's whole process group so
    a descendant holding stdout cannot outlive or hang the run. Workers
    run with start_new_session, so the group id is the worker's own pid
    and stays valid for killpg even after the worker itself has exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def run_processes(worker_cmd, count, frames, reps, seed, deadline):
    """Run the process schedule once per repetition. worker_cmd is the
    command prefix; the worker arguments are appended per worker. Each
    repetition passes a per-repetition base seed (the same mixing the
    harness main() applies per repetition) and every worker derives its
    own repetition-0 seed from it internally, exactly like this
    derivation; the expected digests follow the same chain."""
    for rep in range(reps):
        base = rep_seed(seed, rep)
        expected = {worker: expected_digest(base, worker, frames, 0)
                    for worker in range(count)}
        # The deadline covers process creation, the barrier release and
        # the run, not just the tail of it.
        limit = time.monotonic() + deadline
        workers = []
        outputs = []
        breached = False
        try:
            try:
                for worker_id in range(count):
                    workers.append(subprocess.Popen(
                        worker_cmd + ["worker", str(worker_id), str(frames),
                                      str(base)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True,
                        start_new_session=True))
            except (OSError, ValueError) as error:
                print("processes-%d rep %d FAILED to launch: %s" %
                      (count, rep, error))
                return 1
            for proc in workers:
                try:
                    proc.stdin.write("S")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            for proc in workers:
                remaining = max(0.0, limit - time.monotonic())
                try:
                    out, _ = proc.communicate(timeout=remaining)
                except subprocess.TimeoutExpired:
                    breached = True
                    kill_process_group(proc)
                    try:
                        out, _ = proc.communicate(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        kill_process_group(proc)
                        out, _ = proc.communicate()
                outputs.append((proc.returncode, out))
        finally:
            for proc in workers:
                if proc.poll() is None:
                    kill_process_group(proc)
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        kill_process_group(proc)
                        proc.wait()
        lines = []
        for returncode, out in outputs:
            if returncode != 0:
                print("worker failed (exit %d):\n%s" % (returncode, out))
                return 1
            lines.extend(parse_worker_output(out))
        error = check_run(lines, count, frames, breached, expected)
        if error:
            print("processes-%d rep %d FAILED: %s" % (count, rep, error))
            return 1
        for worker, frames_ok, digest in sorted(lines):
            print("worker %d frames=%d MD5=%s (derived: %s)" %
                  (worker, frames_ok, digest, expected[worker]))
    print("PASS processes-%d reps=%d seed=%#x (digests derived and matched)"
          % (count, reps, seed))
    return 0


def self_test(worker_cmd=None):
    sample = "\n".join([
        "schedule worker streams=1 frames=4 reps=1 seed=0x1",
        "stream 0 frames=4 MD5=0123456789abcdef0123456789abcdef",
        "worker 0 frames=4 MD5=0123456789abcdef0123456789abcdef",
        "PASS worker",
    ])
    lines = parse_worker_output(sample)
    assert len(lines) == 1 and lines[0][0] == 0 and lines[0][1] == 4, lines
    expected = {0: "0123456789abcdef0123456789abcdef"}
    assert check_run(lines, 1, 4, False, expected) is None
    assert "expected 1 worker results, got 0" in check_run([], 1, 4, False,
                                                           expected)
    assert "verified 3 frames" in check_run(
        [(0, 3, "0123456789abcdef0123456789abcdef")], 1, 4, False, expected)
    assert "deadline breached" in check_run(lines, 1, 4, True, expected)
    bad_ids = [(1, 4, "0123456789abcdef0123456789abcdef")]
    assert "worker ids" in check_run(bad_ids, 1, 4, False, expected)
    wrong_digest = {0: "ffffffffffffffffffffffffffffffff"}
    assert "does not match the derived" in check_run(
        lines, 1, 4, False, wrong_digest)
    # The derivation must reject a digest that does not follow the
    # declared recipe (e.g. the empty MD5 of a worker that verified
    # nothing).
    empty = [(0, 4, "d41d8cd98f00b204e9800998ecf8427e")]
    assert "does not match the derived" in check_run(empty, 1, 4, False,
                                                     expected)
    # Determinism of the derivation itself.
    assert (expected_digest(0xC0FFEE, 0, 12, 0) ==
            expected_digest(0xC0FFEE, 0, 12, 0))
    assert expected_digest(0xC0FFEE, 0, 12, 0) != \
        expected_digest(0xC0FFEE, 1, 12, 0)
    print("self-test: parsing, validation and digest derivation OK")

    if not worker_cmd:
        return 0

    # Real negative fixtures: each must fail, and bounded.
    stall = [sys.executable, "-c", "import time; time.sleep(60)"]
    holder = [sys.executable, "-c",
              "import subprocess, sys; "
              "subprocess.Popen(['sleep', '60']); sys.exit(0)"]
    fixtures = [
        ("stalled worker", stall, 2.0),
        ("inherited stdout holder", holder, 2.0),
        ("launch failure", ["/nonexistent/concurrent-stress"], 2.0),
    ]
    for name, command, deadline in fixtures:
        started = time.monotonic()
        code = run_processes(command, 1, 4, 1, 0xC0FFEE, deadline)
        elapsed = time.monotonic() - started
        assert code == 1, "%s: expected failure, got %d" % (name, code)
        assert elapsed < 30.0, "%s: cleanup not bounded (%.1fs)" % (
            name, elapsed)
        print("self-test: %s failed and cleaned up in %.1fs" %
              (name, elapsed))

    # A real schedule must match the independent derivation exactly.
    code = run_processes([worker_cmd], 2, 4, 1, 0xC0FFEE, 60.0)
    assert code == 0, "real process schedule failed"
    print("self-test: real 2-process schedule matched derived digests")
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
        return self_test(args.worker)
    if not args.worker or not (1 <= args.processes <= MAX_PROCESSES) or \
            not (2 <= args.frames <= MAX_FRAMES) or \
            not (1 <= args.reps <= MAX_REPS) or \
            not (args.deadline > 0 and args.deadline < float("inf")) or \
            not (0 <= args.seed <= MASK64):
        parser.print_usage(sys.stderr)
        return 2
    return run_processes([args.worker], args.processes, args.frames,
                         args.reps, args.seed, args.deadline)


if __name__ == "__main__":
    sys.exit(main())
