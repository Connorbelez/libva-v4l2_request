#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, one-worker-per-target campaigns with measured CPU and durable outcomes."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import shutil

TARGETS = [('fuzz-h264', 'h264'), ('fuzz-hevc', 'hevc'),
           ('fuzz-vp9', 'vp9'), ('fuzz-va-api', 'va-api')]
FINDINGS = ('crash-*', 'leak-*', 'oom-*', 'timeout-*')
DIAGNOSTIC = re.compile(r'ERROR:|DEADLYSIGNAL|SUMMARY: (?:Address|UndefinedBehavior|Leak)Sanitizer|runtime error:|out-of-memory|fuzz-harness:')


def run_worker(command, directory, cpu_seconds, wall_seconds, smoke):
    """Keep the leader unreaped until its group is cleaned, then collect wait4 CPU.

    The real campaign launches the engine directly (no -jobs wrapper). RLIMIT_CPU
    therefore budgets that worker, instead of equating N*wall time with CPU time.
    """
    stopped = []
    def stop(sig, _frame):
        stopped.append(sig)
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    soft = max(1, math.ceil(cpu_seconds))
    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 5))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    started = time.monotonic()
    proc = None
    wall_exhausted = False
    output_exhausted = False
    try:
        with (directory / 'worker.log').open('wb') as log:
            proc = subprocess.Popen(command, cwd=str(directory), stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, preexec_fn=limits)
            while not stopped:
                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    break
                if time.monotonic() - started >= wall_seconds:
                    wall_exhausted = True
                    break
                if os.fstat(log.fileno()).st_size > 16 * 1024 * 1024:
                    output_exhausted = True
                    break
                time.sleep(.02)
            # The unreaped leader pins this PID/group ID. Signal it once only.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            cleanup_end = time.monotonic() + 5
            usage = None
            while time.monotonic() < cleanup_end:
                pid, status, got_usage = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    proc.returncode = os.waitstatus_to_exitcode(status) if hasattr(os, 'waitstatus_to_exitcode') else (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status))
                    usage = got_usage
                    break
                time.sleep(.01)
            cpu = usage.ru_utime + usage.ru_stime if usage else None
        log_text = (directory / 'worker.log').read_text(errors='replace')[:16 * 1024 * 1024]
        findings = sorted(p.name for pattern in FINDINGS for p in directory.glob(pattern))
        diagnostic = bool(DIAGNOSTIC.search(log_text))
        exhausted = proc.returncode == -signal.SIGXCPU and cpu is not None and cpu >= soft - .05
        ok = (not stopped and not wall_exhausted and not output_exhausted and usage is not None
              and not findings and not diagnostic and
              ((smoke and proc.returncode == 0) or (not smoke and exhausted)))
        return {'passed': ok, 'smoke': smoke, 'requested_cpu_seconds': cpu_seconds,
                'cpu_limit_seconds': soft, 'cpu_seconds': cpu, 'wall_seconds': time.monotonic() - started,
                'exit_code': proc.returncode, 'cpu_budget_exhausted': exhausted,
                'wall_deadline_exhausted': wall_exhausted, 'output_limit_exhausted': output_exhausted,
                'interrupted_by': stopped, 'findings': findings, 'diagnostic': diagnostic,
                'cleanup_complete': usage is not None, 'command': command}
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def provenance(root, build):
    try:
        source = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no'], stderr=subprocess.DEVNULL, text=True))
    except (OSError, subprocess.SubprocessError):
        source, dirty = 'unknown', None
    compilers = build / 'meson-info/intro-compilers.json'
    return {'engine': 'libFuzzer', 'source': source, 'source_dirty': dirty,
            'compilers': json.loads(compilers.read_text()) if compilers.exists() else 'unknown',
            'worker_count': 1, 'accounting': 'wait4 user+system CPU; direct engine process with RLIMIT_CPU'}


def self_test():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cases = [
            ('ok', 'pass', True, True),
            ('crash-artifact', "from pathlib import Path;Path('crash-only').write_text('x')", True, False),
            ('ubsan', "print('x.c:1: runtime error: overflow')", True, False),
            ('unknown-exit', 'raise SystemExit(7)', True, False),
            ('early-full-exit', 'pass', False, False),
            ('cpu', 'while True: pass', False, True),
        ]
        for name, code, smoke, expected in cases:
            target = root / name
            target.mkdir()
            result = run_worker([sys.executable, '-c', code], target, 1, 5, smoke)
            assert result['passed'] == expected, (name, result)
        target = root / 'wall'
        target.mkdir()
        result = run_worker([sys.executable, '-c', 'import time;time.sleep(30)'], target, 10, .1, False)
        assert not result['passed'] and result['wall_deadline_exhausted']
    print('campaign outcomes, single artifact, UBSan, CPU accounting and wall deadline: PASS')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('builddir', nargs='?')
    parser.add_argument('cpu_hours_per_target', nargs='?', type=float, default=24)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.builddir or not math.isfinite(args.cpu_hours_per_target) or not 0 < args.cpu_hours_per_target <= 168:
        parser.error('builddir and a finite CPU budget in (0, 168] hours are required')
    root = Path(__file__).resolve().parent.parent
    build = Path(args.builddir).resolve()
    for name, seed in TARGETS:
        if not os.access(str(build / 'tests' / name), os.X_OK) or not (root / 'tests/fuzz/seeds' / seed).is_dir():
            parser.error('missing required campaign binary or seed directory: ' + name)
    # Never overwrite a prior campaign's logs or mix its findings into this run.
    parent = build / 'fuzz-artifacts'
    parent.mkdir(exist_ok=True)
    campaign = Path(tempfile.mkdtemp(prefix='campaign-', dir=str(parent)))
    identity = provenance(root, build)
    records = []
    for name, seed in TARGETS:
        directory = campaign / name
        directory.mkdir()
        corpus = directory / 'corpus'
        shutil.copytree(root / 'tests/fuzz/seeds' / seed, corpus)
        binary = build / 'tests' / name
        command = [str(binary), str(corpus), '-max_len=65536', '-timeout=1', '-rss_limit_mb=2048',
                   '-artifact_prefix=' + str(directory) + '/', '-print_final_stats=1']
        if args.smoke:
            command.append('-runs=1000')
        cpu = 60 if args.smoke else args.cpu_hours_per_target * 3600
        header = dict(identity, target=name, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                      requested_cpu_seconds=cpu, smoke=args.smoke, command=command)
        record = directory / 'campaign-record.json'
        record.write_text(json.dumps(dict(header, state='running'), indent=2) + '\n')
        try:
            result = run_worker(command, directory, cpu, 60 if args.smoke else cpu * 3 + 60, args.smoke)
        except (OSError, subprocess.SubprocessError) as error:
            result = {'passed': False, 'error': str(error)}
        header.update(result)
        header['state'] = 'finished'
        record.write_text(json.dumps(header, indent=2) + '\n')
        records.append(header)
        print('%s: %s (%s)' % (name, 'completed' if header['passed'] else 'FAILED', record), flush=True)
        if header.get('interrupted_by'):
            break
    passed = len(records) == len(TARGETS) and all(row['passed'] for row in records)
    (campaign / 'summary.json').write_text(json.dumps({'passed': passed, 'targets': records}, indent=2) + '\n')
    print('Budget exhaustion is not proof of parser correctness.', flush=True)
    return 0 if passed else 1

if __name__ == '__main__':
    sys.exit(main())
