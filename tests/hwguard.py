#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exclusive hardware lease and deadline runner for decoder tests.

Behavioral reference (not copied): avd-lab/avdlab/guard.py inspects sysfs and
/proc, never opens the decoder to check health, never waits on a child without
a deadline, and fsyncs results. That tree has no declared license, so this
module is an independent implementation under this repository's GPL-3.0-or-later.

This module never loads or unloads kernel modules.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LEASE_ENV = "LIBVA_HW_GUARD_LEASE"
IDENTITY_ENV = "LIBVA_HW_GUARD_IDENTITY"
BUSY_EXIT = 75
AVD_JOURNAL_MARKERS = (
    "apple_avd",
    "avd firmware",
    "avd_timeout",
    "H2 timeout",
    "H3 timeout",
    "Unable to handle kernel",
    "Internal error: Oops",
    "kernel BUG at",
)
DECODER_WCHANS = ("video_do_ioctl", "v4l2_", "vb2_", "m2m", "avd_", "media_request")
REDACT = re.compile(r"(https?://\S+)|(/\S+)|([A-Za-z]:\\[^\s]+)")
WEDGE_GRACE_S = 10.0


class GuardError(RuntimeError):
    """Preflight or monitor failed; stop without touching the decoder."""


def redact_text(value: str) -> str:
    if not value:
        return value
    return REDACT.sub("<redacted>", value)


def redact_argv(argv: Iterable[str]) -> list[str]:
    return [redact_text(part) for part in argv]


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / "meson.build").is_file() and (candidate / "tests").is_dir():
            return candidate
    raise SystemExit("could not locate repository root from tests/hwguard.py")


def default_lock_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "libva-v4l2-hwguard"
    return Path("/tmp") / "libva-v4l2-hwguard"


@dataclass
class DecoderState:
    module_loaded: bool
    video_node: str | None
    media_node: str | None
    holders: list[dict[str, Any]] = field(default_factory=list)
    stuck_tasks: list[dict[str, Any]] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.holders)

    @property
    def wedged(self) -> bool:
        return any(task.get("state") == "D" for task in self.stuck_tasks)


class EventLog:
    def __init__(self, path: Path, *, verbose: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.verbose = verbose
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def write(self, **record: Any) -> None:
        record.setdefault("time", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        if not self.verbose:
            if "cmd" in record:
                record["cmd"] = redact_argv(record["cmd"]) if isinstance(record["cmd"], list) else "<redacted>"
            if "argv" in record:
                record["argv"] = redact_argv(record["argv"])
            for key in ("path", "url", "media"):
                if key in record and isinstance(record[key], str):
                    record[key] = redact_text(record[key])
            if "holders" in record:
                cleaned = []
                for holder in record["holders"]:
                    item = dict(holder)
                    if "cmd" in item:
                        item["cmd"] = "<redacted>" if not self.verbose else redact_text(str(item["cmd"]))
                    cleaned.append(item)
                record["holders"] = cleaned
        os.write(self._fd, (json.dumps(record) + "\n").encode())
        os.fsync(self._fd)

    def close(self) -> None:
        os.close(self._fd)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


class LinuxBackend:
    def state(self) -> DecoderState:
        video = media = None
        sys_v4l = Path("/sys/class/video4linux")
        if sys_v4l.is_dir():
            for dev in sorted(sys_v4l.glob("video*")):
                if _read(dev / "name") == "avd":
                    video = f"/dev/{dev.name}"
                    for node in (dev / "device").glob("media*"):
                        media = f"/dev/{node.name}"
        nodes = {n for n in (video, media) if n}
        holders: list[dict[str, Any]] = []
        if nodes:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                try:
                    fds = [os.readlink(fd) for fd in (pid_dir / "fd").iterdir()]
                except OSError:
                    continue
                if nodes.intersection(fds):
                    holders.append({"pid": int(pid_dir.name), "cmd": "<redacted>"})
        stuck: list[dict[str, Any]] = []
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            task_root = pid_dir / "task"
            if not task_root.is_dir():
                continue
            for task in task_root.iterdir():
                wchan = _read(task / "wchan") or ""
                if not any(token in wchan for token in DECODER_WCHANS):
                    continue
                stat = _read(task / "stat") or ""
                state = stat.rsplit(")", 1)[-1].split()[0] if ")" in stat else "?"
                stuck.append({
                    "pid": int(pid_dir.name),
                    "tid": int(task.name),
                    "state": state,
                    "wchan": wchan,
                })
        return DecoderState(
            module_loaded=Path("/sys/module/apple_avd").exists(),
            video_node=video,
            media_node=media,
            holders=holders,
            stuck_tasks=stuck,
            faults=self.journal_since("boot"),
        )

    def journal_since(self, since: str) -> list[str]:
        try:
            out = subprocess.run(
                ["journalctl", "-k", "--since", since, "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in out.splitlines() if any(m in line for m in AVD_JOURNAL_MARKERS)]


class FakeBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "journal").write_text("")
        (self.root / "holders.json").write_text("[]")
        (self.root / "stuck.json").write_text("[]")
        (self.root / "module").write_text("1")
        (self.root / "video").write_text("/dev/video-fake")
        (self.root / "media").write_text("/dev/media-fake")

    def state(self) -> DecoderState:
        holders = json.loads((self.root / "holders.json").read_text() or "[]")
        stuck = json.loads((self.root / "stuck.json").read_text() or "[]")
        faults = [
            line for line in (self.root / "journal").read_text().splitlines()
            if any(m in line for m in AVD_JOURNAL_MARKERS)
        ]
        return DecoderState(
            module_loaded=(self.root / "module").read_text().strip() == "1",
            video_node=(self.root / "video").read_text().strip(),
            media_node=(self.root / "media").read_text().strip(),
            holders=holders,
            stuck_tasks=stuck,
            faults=faults,
        )

    def journal_since(self, since: str) -> list[str]:
        del since
        return [
            line for line in (self.root / "journal").read_text().splitlines()
            if any(m in line for m in AVD_JOURNAL_MARKERS)
        ]

    def inject_fault(self, line: str) -> None:
        with (self.root / "journal").open("a") as handle:
            handle.write(line + "\n")

    def inject_holder(self, pid: int = 99999) -> None:
        (self.root / "holders.json").write_text(json.dumps([{"pid": pid, "cmd": "/usr/bin/mpv /secret/clip.mkv"}]))

    def inject_stuck(self) -> None:
        (self.root / "stuck.json").write_text(json.dumps([
            {"pid": 1, "tid": 1, "state": "D", "wchan": "avd_submit_job"}
        ]))


class Lease:
    def __init__(self, lock_dir: Path, identity: str, run_id: str) -> None:
        self.lock_dir = lock_dir
        self.identity = identity
        self.run_id = run_id
        self.lock_path = lock_dir / f"{identity}.lock"
        self.meta_path = lock_dir / f"{identity}.lease.json"
        self._fd: int | None = None

    def acquire(self) -> dict[str, Any] | None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = {}
            try:
                owner = json.loads(self.meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                owner = {"run_id": "unknown"}
            os.close(self._fd)
            self._fd = None
            return owner
        meta = {
            "run_id": self.run_id,
            "identity": self.identity,
            "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta) + "\n")
        os.fsync(os.open(tmp, os.O_RDONLY))
        tmp.replace(self.meta_path)
        return None

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if self.meta_path.is_file():
                self.meta_path.unlink()
        except OSError:
            pass
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


def preflight(backend: LinuxBackend | FakeBackend) -> DecoderState:
    state = backend.state()
    if not (state.module_loaded and state.video_node):
        raise GuardError("decoder is not present")
    if state.wedged:
        raise GuardError("decoder already wedged")
    if state.busy:
        raise GuardError("decoder already in use")
    if state.faults:
        raise GuardError("existing decoder/kernel faults on this boot")
    return state


def userspace_identity(driver_path: str | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "libva_drivers_path": redact_text(driver_path or os.environ.get("LIBVA_DRIVERS_PATH") or ""),
        "libva_driver_name": os.environ.get("LIBVA_DRIVER_NAME") or "v4l2_request",
        "loaded_module_limit": (
            "sysfs shows whether apple_avd is present; this guard cannot certify "
            "which module binary was loaded earlier or that every patch is active"
        ),
    }
    try:
        info["source_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True, timeout=5
        ).strip()
    except (OSError, subprocess.SubprocessError):
        info["source_commit"] = None
    return info


@dataclass
class RunStatus:
    status: str
    returncode: int | None
    timed_out: bool = False
    wedged: bool = False
    abort_reason: str | None = None
    busy_owner: dict[str, Any] | None = None


def _kill_group(proc: subprocess.Popen) -> bool:
    """Return True if the child survived SIGKILL (kernel-stuck)."""
    if proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    try:
        proc.wait(timeout=2)
        return False
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    try:
        proc.wait(timeout=3)
        return False
    except subprocess.TimeoutExpired:
        return True


def run_guarded(
    cmd: list[str],
    *,
    identity: str = "avd",
    deadline: float = 60.0,
    poll: float = 0.2,
    fake: bool = False,
    fake_root: Path | None = None,
    lock_dir: Path | None = None,
    log_path: Path | None = None,
    verbose: bool = False,
    inject: str | None = None,
    env: dict[str, str] | None = None,
) -> RunStatus:
    run_id = str(uuid.uuid4())
    lock_dir = lock_dir or default_lock_dir()
    log_path = log_path or (lock_dir / f"{identity}-{run_id}.jsonl")
    log = EventLog(log_path, verbose=verbose)
    backend: LinuxBackend | FakeBackend
    if fake:
        backend = FakeBackend(fake_root or (lock_dir / "fake" / run_id))
        if inject == "preflight-busy":
            backend.inject_holder()
        if inject == "preflight-fault":
            backend.inject_fault("apple_avd: firmware timeout H3")
    else:
        backend = LinuxBackend()
    lease = Lease(lock_dir, identity, run_id)
    busy = lease.acquire()
    if busy:
        log.write(event="busy", identity=identity, owner=busy)
        log.close()
        return RunStatus(status="busy", returncode=BUSY_EXIT, busy_owner=busy)
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    child_env[LEASE_ENV] = run_id
    child_env[IDENTITY_ENV] = identity
    child_env["LIBVA_HW_GUARD"] = "1"
    abort_reason: str | None = None
    stop = False

    def request_stop(reason: str) -> None:
        nonlocal abort_reason, stop
        if not stop:
            abort_reason = reason
            stop = True

    def handle_sigint(signum, frame) -> None:
        del signum, frame
        request_stop("signal")

    previous_int = signal.signal(signal.SIGINT, handle_sigint)
    previous_term = signal.signal(signal.SIGTERM, handle_sigint)
    proc: subprocess.Popen | None = None
    timed_out = False
    wedged = False
    returncode: int | None = None
    try:
        state = preflight(backend)
        log.write(
            event="preflight",
            run_id=run_id,
            identity=identity,
            idle=not state.busy,
            module_loaded=state.module_loaded,
            userspace=userspace_identity(child_env.get("LIBVA_DRIVERS_PATH")),
        )
        started = time.monotonic()
        journal_origin = time.strftime("%Y-%m-%d %H:%M:%S")
        log.write(event="start", run_id=run_id, cmd=cmd)
        proc = subprocess.Popen(cmd, env=child_env, start_new_session=True)
        deadline_at = started + deadline
        while proc.poll() is None:
            if inject == "timeout":
                request_stop("timeout")
                timed_out = True
            elif inject == "avd-error" and fake:
                backend.inject_fault("apple_avd: firmware timeout H3")
            elif inject == "foreign" and fake:
                backend.inject_holder()
            elif inject == "stuck-child":
                wedged = True
                request_stop("stuck-child")
            now = time.monotonic()
            if now >= deadline_at:
                timed_out = True
                request_stop("timeout")
            current = backend.state()
            if current.faults or (fake and backend.journal_since(journal_origin)):
                request_stop("avd-error")
            foreign = [h for h in current.holders if int(h.get("pid") or 0) != proc.pid]
            if foreign:
                request_stop("foreign-client")
            if stop:
                break
            try:
                proc.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                continue
        if proc.poll() is None:
            if inject == "stuck-child":
                wedged = True
            else:
                wedged = _kill_group(proc)
            if timed_out and abort_reason is None:
                abort_reason = "timeout"
        returncode = proc.poll()
        final = backend.state()
        if final.busy and not foreign_ok(final, proc.pid):
            # Child should be gone; remaining holders are foreign.
            pass
        status = "ok"
        if abort_reason == "signal":
            status = "signal"
        elif timed_out:
            status = "timeout"
        elif wedged:
            status = "wedged"
        elif abort_reason:
            status = "abort"
        elif returncode not in (0, None):
            status = "child-error"
        log.write(
            event="final",
            run_id=run_id,
            status=status,
            returncode=returncode,
            timed_out=timed_out,
            wedged=wedged,
            abort_reason=abort_reason,
            idle=not backend.state().busy,
        )
        return RunStatus(
            status=status,
            returncode=returncode,
            timed_out=timed_out,
            wedged=wedged,
            abort_reason=abort_reason,
        )
    except GuardError as exc:
        log.write(event="final", run_id=run_id, status="preflight-error", abort_reason=str(exc))
        return RunStatus(status="preflight-error", returncode=2, abort_reason=str(exc))
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        if proc is not None and proc.poll() is None:
            _kill_group(proc)
        lease.release()
        log.close()


def foreign_ok(state: DecoderState, child_pid: int) -> bool:
    return all(int(h.get("pid") or 0) in (child_pid, os.getpid()) for h in state.holders)


def run_self_test() -> int:
    errors: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            errors.append(message)

    root = repo_root()
    check((root / "tests" / "hwguard.py").is_file(), "hwguard.py missing")
    source = (root / "tests" / "hwguard.py").read_text()
    home = chr(47) + "home" + chr(47)
    check(home not in source, "hwguard.py contains an absolute home path")

    work = Path(os.environ.get("TMPDIR") or "/tmp") / f"hwguard-selftest-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    lock_dir = work / "locks"
    fake_root = work / "fake"

    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]

    holder_script = work / "holder.py"
    holder_script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, os.environ['HWGUARD_DIR'])\n"
        "import hwguard\n"
        "lease = hwguard.Lease(Path(os.environ['LOCK_DIR']), 'avd', 'holder')\n"
        "busy = lease.acquire()\n"
        "print('ACQUIRED' if busy is None else 'BUSY', flush=True)\n"
        "time.sleep(8)\n"
        "lease.release()\n"
    )
    env = os.environ.copy()
    env["HWGUARD_DIR"] = str(root / "tests")
    env["LOCK_DIR"] = str(lock_dir / "two")
    proc1 = subprocess.Popen(
        [sys.executable, str(holder_script)], env=env,
        stdout=subprocess.PIPE, text=True,
    )
    line = proc1.stdout.readline() if proc1.stdout else ""
    check("ACQUIRED" in line, f"first locker did not acquire: {line!r}")
    second = run_guarded(
        [sys.executable, "-c", "print('should-not-run')"],
        fake=True, fake_root=fake_root / "b", lock_dir=lock_dir / "two",
        deadline=2, log_path=work / "busy.jsonl",
    )
    check(second.status == "busy" and second.returncode == BUSY_EXIT,
          f"second process was not busy: {second}")
    check(second.busy_owner and second.busy_owner.get("run_id") == "holder",
          "busy result missing owner run_id")
    proc1.terminate()
    proc1.wait(timeout=5)

    timed = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "t", lock_dir=lock_dir / "t",
        deadline=0.4, poll=0.05, inject="timeout", log_path=work / "timeout.jsonl",
    )
    check(timed.status == "timeout" and timed.timed_out, f"timeout inject: {timed}")
    check(timed.abort_reason == "timeout", "timeout missing abort_reason")

    avd = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "e", lock_dir=lock_dir / "e",
        deadline=8, poll=0.05, inject="avd-error", log_path=work / "avd.jsonl",
    )
    check(avd.status == "abort" and avd.abort_reason == "avd-error", f"avd-error inject: {avd}")

    foreign = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "f", lock_dir=lock_dir / "f",
        deadline=8, poll=0.05, inject="foreign", log_path=work / "foreign.jsonl",
    )
    check(foreign.status == "abort" and foreign.abort_reason == "foreign-client",
          f"foreign inject: {foreign}")

    stuck = run_guarded(
        sleeper, fake=True, fake_root=fake_root / "s", lock_dir=lock_dir / "s",
        deadline=8, poll=0.05, inject="stuck-child", log_path=work / "stuck.jsonl",
    )
    check(stuck.status == "wedged" and stuck.wedged, f"stuck-child inject: {stuck}")

    # SIGINT: child sleeps, parent handler via inject=signal using a subprocess sending SIGINT
    sig_script = work / "sig.py"
    sig_script.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, os.environ['HWGUARD_DIR'])\n"
        "import hwguard\n"
        "from pathlib import Path\n"
        "status = hwguard.run_guarded(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    fake=True, fake_root=Path(os.environ['FAKE']), lock_dir=Path(os.environ['LOCK']),\n"
        "    deadline=20, poll=0.05, log_path=Path(os.environ['LOG']))\n"
        "print(status.status, status.abort_reason or '', flush=True)\n"
    )
    sig_env = os.environ.copy()
    sig_env["HWGUARD_DIR"] = str(root / "tests")
    sig_env["FAKE"] = str(fake_root / "sig")
    sig_env["LOCK"] = str(lock_dir / "sig")
    sig_env["LOG"] = str(work / "sig.jsonl")
    sig_proc = subprocess.Popen(
        [sys.executable, str(sig_script)], env=sig_env,
        stdout=subprocess.PIPE, text=True,
    )
    time.sleep(0.4)
    sig_proc.send_signal(signal.SIGINT)
    out, _ = sig_proc.communicate(timeout=10)
    check("signal" in out, f"SIGINT did not record signal status: {out!r}")

    # Redaction: foreign holder cmd must not appear in publishable log
    foreign_log = (work / "foreign.jsonl").read_text()
    check("/secret/clip.mkv" not in foreign_log, "publishable log leaked media path")
    check("https://" not in foreign_log, "publishable log leaked URL")

    # Scripts refuse unguarded hardware
    require = root / "tests" / "require-hw-guard.sh"
    check(require.is_file(), "missing require-hw-guard.sh")
    refused = subprocess.run(
        ["sh", "-c", f". {require}; require_hw_guard"],
        capture_output=True, text=True,
    )
    check(refused.returncode == 2, "require_hw_guard did not refuse unguarded mode")
    allowed = subprocess.run(
        ["sh", "-c", f". {require}; require_hw_guard"],
        capture_output=True, text=True,
        env={**os.environ, LEASE_ENV: "test-lease"},
    )
    check(allowed.returncode == 0, "require_hw_guard refused a valid lease")

    for script in (
        "hwdownload.sh", "early-export.sh", "h264-high10.sh", "vp9-matrix.sh",
        "frame-check.sh", "shared-contexts.sh",
    ):
        text = (root / "tests" / script).read_text()
        check("require-hw-guard.sh" in text or "LIBVA_HW_GUARD_LEASE" in text,
              f"{script} does not route through the guard")

    conf = (root / "tests" / "conformance.py").read_text()
    check("LIBVA_HW_GUARD_LEASE" in conf, "conformance.py --driver is not guard-gated")

    if errors:
        print(f"{len(errors)} hwguard self-test error(s):", file=sys.stderr)
        print("\n".join(f"- {item}" for item in errors), file=sys.stderr)
        return 1
    print(
        "hwguard ok: exclusive lease, timeout/AVD-error/foreign/SIGINT/stuck-child "
        "stop with final status, logs redacted, unguarded hardware refused"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    parser.add_argument("--identity", default="avd")
    parser.add_argument("--deadline", type=float, default=120.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--fake-root", type=Path)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--verbose-log", action="store_true",
                        help="include unredacted local command detail")
    parser.add_argument("--inject", choices=("timeout", "avd-error", "foreign", "stuck-child"))
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("pass a command after --, or --self-test")
    result = run_guarded(
        cmd,
        identity=args.identity,
        deadline=args.deadline,
        poll=args.poll,
        fake=args.fake,
        fake_root=args.fake_root,
        lock_dir=args.lock_dir,
        log_path=args.log,
        verbose=args.verbose_log,
        inject=args.inject,
    )
    payload = {
        "status": result.status,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "wedged": result.wedged,
        "abort_reason": result.abort_reason,
        "busy": result.status == "busy",
        "owner": result.busy_owner,
    }
    if result.status == "busy":
        print(json.dumps({"busy": True, "owner": result.busy_owner}))
        return BUSY_EXIT
    print(json.dumps(payload), file=sys.stderr)
    if result.status == "ok":
        return int(result.returncode or 0)
    return 1


if __name__ == "__main__":
    sys.exit(main())
