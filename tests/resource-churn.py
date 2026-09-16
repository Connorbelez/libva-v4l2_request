#!/usr/bin/env python3
"""Record process-local resource bounds without inspecting other applications."""

import argparse
import json
import mmap
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time


STATUS_KEYS = ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem")


def _read_identity(pid):
    fields = Path("/proc").joinpath(str(pid), "stat").read_text().rsplit(") ", 1)[1].split()
    return fields[0], fields[19]


def _read_status(pid):
    values = {key: None for key in STATUS_KEYS}
    state = None
    for line in Path("/proc").joinpath(str(pid), "status").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator and key in values:
            fields = value.split()
            values[key] = int(fields[0]) if fields else None
        elif separator and key == "State":
            state = value.strip().split()[0]
    result = {key.lower() + "_kib": value for key, value in values.items()}
    result["process_state"] = state
    return result


def _read_maps(pid):
    count = 0
    mapped_bytes = 0
    for line in Path("/proc").joinpath(str(pid), "maps").read_text().splitlines():
        address = line.split(None, 1)[0]
        start, end = address.split("-", 1)
        count += 1
        mapped_bytes += int(end, 16) - int(start, 16)
    return count, mapped_bytes


def _read_dmabufs(pid, fds):
    references = []
    readable = 0
    unresolved_candidate = False
    for fd in fds:
        info_path = Path("/proc").joinpath(str(pid), "fdinfo", fd)
        target_path = Path("/proc").joinpath(str(pid), "fd", fd)
        try:
            target = os.readlink(target_path)
            inode = os.stat(target_path).st_ino
            fields = {}
            for line in info_path.read_text().splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    fields[key.strip()] = value.strip()
            if os.readlink(target_path) != target or os.stat(target_path).st_ino != inode:
                unresolved_candidate = True
                continue
            readable += 1
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            unresolved_candidate = True
            continue
        if "exp_name" not in fields:
            if "dmabuf" in target.lower():
                unresolved_candidate = True
            continue
        size = fields.get("size")
        inode = fields.get("ino") or fields.get("inode")
        references.append({
            "fd": int(fd),
            "exporter": fields.get("exp_name"),
            "size_bytes": int(size) if size and size.isdigit() else None,
            "identity": inode,
        })

    if not readable or unresolved_candidate:
        return {"status": "unavailable", "references": None,
                "unique_objects": None, "unique_bytes": None}
    identities = {}
    for reference in references:
        identity = reference["identity"] or "fd:{}".format(reference["fd"])
        identities.setdefault(identity, reference["size_bytes"])
    known_sizes = [size for size in identities.values() if size is not None]
    return {
        "status": "observed" if references else "none-observed",
        "references": len(references),
        "unique_objects": len(identities),
        "unique_bytes": sum(known_sizes) if len(known_sizes) == len(identities) else None,
    }


def snapshot(pid):
    proc = Path("/proc").joinpath(str(pid))
    state, start_time = _read_identity(pid)
    if state == "Z":
        raise ProcessLookupError("process {} is a zombie".format(pid))
    status = _read_status(pid)
    if status["process_state"] == "Z":
        raise ProcessLookupError("process {} is a zombie".format(pid))
    fds = sorted(entry.name for entry in proc.joinpath("fd").iterdir())
    map_count, mapped_bytes = _read_maps(pid)
    sample = {
        "type": "sample",
        "timestamp_ns": time.time_ns(),
        "pid": pid,
        "fd_count": len(fds),
        "map_count": map_count,
        "mapped_bytes": mapped_bytes,
        "dmabuf": _read_dmabufs(pid, fds),
    }
    sample["dmabuf_references"] = sample["dmabuf"]["references"]
    sample["dmabuf_objects"] = sample["dmabuf"]["unique_objects"]
    sample["dmabuf_bytes"] = sample["dmabuf"]["unique_bytes"]
    sample.update(status)
    final_state, final_start_time = _read_identity(pid)
    if final_state == "Z" or final_start_time != start_time:
        raise ProcessLookupError("process {} changed while sampled".format(pid))
    return sample


def _growth(samples, key):
    baseline = samples[0].get(key)
    values = [sample.get(key) for sample in samples]
    if baseline is None or any(value is None for value in values):
        return None
    return max(values) - baseline


def _monotonic(samples, key):
    values = [sample.get(key) for sample in samples]
    if any(value is None for value in values):
        return None
    return values[-1] > values[0] and all(a <= b for a, b in zip(values, values[1:]))


def summarize(samples, limits, acceptance=False):
    if not samples:
        raise ValueError("no resource samples")
    if acceptance and limits.get("vmrss_kib") is None:
        raise ValueError("acceptance mode requires --max-rss-growth-kib")
    keys = {
        "fd_count": "fds",
        "map_count": "maps",
        "mapped_bytes": "mapped_bytes",
        "vmrss_kib": "vmrss_kib",
        "dmabuf_references": "dmabuf_references",
        "dmabuf_objects": "dmabuf_objects",
        "dmabuf_bytes": "dmabuf_bytes",
    }
    growth = {name: _growth(samples, key) for key, name in keys.items()}
    monotonic = {name: _monotonic(samples, key) for key, name in keys.items()}
    violations = []
    for name, value in growth.items():
        limit = limits.get(name)
        if limit is not None and value is None:
            violations.append("{} growth is unavailable".format(name))
        elif limit is not None and value > limit:
            violations.append("{} growth {} exceeds {}".format(name, value, limit))
    if any(sample["dmabuf"]["status"] == "unavailable" for sample in samples):
        dmabuf_status = "unavailable"
    elif any(sample["dmabuf"]["status"] == "observed" for sample in samples):
        dmabuf_status = "observed"
    else:
        dmabuf_status = "none-observed"
    if acceptance and dmabuf_status == "unavailable":
        violations.append("dma-buf fdinfo accounting is unavailable")
    return {
        "type": "summary",
        "samples": len(samples),
        "limits": limits,
        "growth": growth,
        "monotonic_growth": monotonic,
        "dmabuf_status": dmabuf_status,
        "violations": violations,
        "passed": not violations,
    }


def write_record(stream, record):
    stream.write(json.dumps(record, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def record(pid, output, count, interval, limits, acceptance=False, process=None,
           warmup=0.0, exit_timeout=1.0):
    samples = []
    metadata = {
        "type": "metadata",
        "source_commit": os.environ.get("V4L2R_SOURCE_COMMIT"),
        "kernel": platform.release(),
        "platform": platform.platform(),
        "pid": pid,
        "interval_seconds": interval,
        "requested_samples": count,
        "warmup_seconds": warmup,
        "exit_timeout_seconds": exit_timeout if process is not None else None,
        "command": process.args if process is not None else None,
    }
    with output.open("w") as stream:
        write_record(stream, metadata)
        if warmup:
            time.sleep(warmup)
        for index in range(count):
            if process is not None and process.poll() is not None:
                break
            try:
                sample = snapshot(pid)
            except (FileNotFoundError, ProcessLookupError):
                break
            sample["index"] = index
            samples.append(sample)
            write_record(stream, sample)
            if index + 1 < count:
                time.sleep(interval)
        try:
            result = summarize(samples, limits, acceptance)
        except ValueError as error:
            result = {
                "type": "summary", "samples": len(samples), "limits": limits,
                "violations": [str(error)], "passed": False,
            }
        if len(samples) != count:
            result["violations"].append(
                "recorded {} of {} requested samples".format(len(samples), count))
            result["passed"] = False
        if process is not None:
            try:
                result["exit_status"] = process.wait(timeout=exit_timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=exit_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=exit_timeout)
                    except subprocess.TimeoutExpired:
                        result["violations"].append(
                            "workload did not terminate after SIGKILL")
                result["exit_status"] = None
                result["violations"].append(
                    "workload remained live after the sampling window")
                result["passed"] = False
            if result["exit_status"] not in (None, 0):
                result["violations"].append(
                    "workload exited with status {}".format(result["exit_status"]))
                result["passed"] = False
        write_record(stream, result)
    return result


def _child():
    descriptors = []
    mapping = None
    print("READY", flush=True)
    for command in sys.stdin:
        command = command.strip()
        if command == "allocate":
            descriptors = [os.open("/dev/null", os.O_RDONLY) for _ in range(4)]
            mapping = mmap.mmap(-1, 2 * mmap.PAGESIZE)
            print("ALLOCATED", flush=True)
        elif command == "release":
            mapping.close()
            mapping = None
            for descriptor in descriptors:
                os.close(descriptor)
            descriptors = []
            print("RELEASED", flush=True)
        elif command == "exit":
            return 0
        else:
            return 2
    return 0


def self_test():
    child = subprocess.Popen(
        [sys.executable, __file__, "_child"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "READY"
    baseline = snapshot(child.pid)
    child.stdin.write("allocate\n")
    child.stdin.flush()
    assert child.stdout.readline().strip() == "ALLOCATED"
    allocated = snapshot(child.pid)
    assert allocated["fd_count"] >= baseline["fd_count"] + 4
    assert allocated["map_count"] >= baseline["map_count"] + 1
    child.stdin.write("release\n")
    child.stdin.flush()
    assert child.stdout.readline().strip() == "RELEASED"
    released = snapshot(child.pid)
    assert released["fd_count"] == baseline["fd_count"]
    assert released["map_count"] == baseline["map_count"]
    child.stdin.write("exit\n")
    child.stdin.flush()
    assert child.wait() == 0

    bounded = summarize([baseline, allocated, released], {
        "fds": allocated["fd_count"] - baseline["fd_count"],
        "maps": allocated["map_count"] - baseline["map_count"],
        "mapped_bytes": allocated["mapped_bytes"] - baseline["mapped_bytes"],
        "vmrss_kib": max(0, allocated["vmrss_kib"] - baseline["vmrss_kib"]),
    }, acceptance=True)
    assert bounded["passed"]
    leaking = [dict(baseline), dict(baseline), dict(baseline)]
    for index, sample in enumerate(leaking):
        sample["fd_count"] += index
    assert not summarize(leaking, {"fds": 0}, acceptance=False)["passed"]
    try:
        summarize([baseline], {"fds": 0}, acceptance=True)
    except ValueError:
        pass
    else:
        raise AssertionError("acceptance summary allowed a missing RSS threshold")
    unavailable = dict(baseline)
    unavailable["dmabuf"] = {"status": "unavailable", "references": None,
                              "unique_objects": None, "unique_bytes": None}
    unavailable["dmabuf_references"] = None
    unavailable["dmabuf_objects"] = None
    unavailable["dmabuf_bytes"] = None
    result = summarize([unavailable], {"fds": 0}, acceptance=False)
    assert result["dmabuf_status"] == "unavailable"
    assert not summarize([unavailable], {"fds": 0, "vmrss_kib": 0},
                         acceptance=True)["passed"]

    with tempfile.TemporaryDirectory() as directory:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        partial = record(process.pid, Path(directory) / "partial.jsonl", 2, 0.01,
                         {"fds": 0, "maps": 0, "mapped_bytes": 0,
                          "vmrss_kib": 1024, "dmabuf_references": 0,
                          "dmabuf_objects": 0, "dmabuf_bytes": 0},
                         acceptance=True, process=process, warmup=0.05)
        assert not partial["passed"]
    print("resource-churn self-test: PASS")
    return 0


def limits_from_args(args):
    return {
        "fds": args.max_fd_growth,
        "maps": args.max_map_growth,
        "mapped_bytes": args.max_mapped_growth_kib * 1024,
        "vmrss_kib": args.max_rss_growth_kib,
        "dmabuf_references": args.max_dmabuf_reference_growth,
        "dmabuf_objects": args.max_dmabuf_object_growth,
        "dmabuf_bytes": args.max_dmabuf_growth_kib * 1024,
    }


def add_record_options(parser):
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-fd-growth", type=int, default=0)
    parser.add_argument("--max-map-growth", type=int, default=0)
    parser.add_argument("--max-mapped-growth-kib", type=int, default=0)
    parser.add_argument("--max-rss-growth-kib", type=int)
    parser.add_argument("--max-dmabuf-reference-growth", type=int, default=0)
    parser.add_argument("--max-dmabuf-object-growth", type=int, default=0)
    parser.add_argument("--max-dmabuf-growth-kib", type=int, default=0)
    parser.add_argument("--warmup-seconds", type=float, default=0.0)
    parser.add_argument("--acceptance", action="store_true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("self-test")
    subparsers.add_parser("_child")
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--pid", type=int, required=True)
    monitor_parser = subparsers.add_parser("monitor")
    monitor_parser.add_argument("--pid", type=int, required=True)
    add_record_options(monitor_parser)
    run_parser = subparsers.add_parser("run")
    add_record_options(run_parser)
    run_parser.add_argument("--exit-timeout", type=float, default=1.0)
    run_parser.add_argument("workload", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "self-test":
        return self_test()
    if args.command == "_child":
        return _child()
    if args.command == "snapshot":
        print(json.dumps(snapshot(args.pid), sort_keys=True))
        return 0
    if args.command == "monitor":
        result = record(args.pid, args.output, args.samples, args.interval,
                        limits_from_args(args), args.acceptance,
                        warmup=args.warmup_seconds)
        return 0 if result["passed"] else 1
    if not args.workload:
        parser.error("run requires a workload after --")
    workload = args.workload[1:] if args.workload[0] == "--" else args.workload
    process = subprocess.Popen(workload)
    result = record(process.pid, args.output, args.samples, args.interval,
                    limits_from_args(args), args.acceptance, process,
                    args.warmup_seconds, args.exit_timeout)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
