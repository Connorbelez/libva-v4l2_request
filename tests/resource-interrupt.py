#!/usr/bin/env python3
"""Interrupt our own decoder process; preserve failure and guard health evidence.

The surrounding hwguard checks for new faults and an idle final device. This
script never treats process-exit reclamation as in-process resource acceptance.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
if not os.environ.get('LIBVA_HW_GUARD_LEASE'):
    parser.error('hardware interruption requires hwguard')
if args.output.exists():
    parser.error('output must be fresh')
command = [sys.executable, str(Path(__file__).with_name('resource-campaign.py')), 'run',
           '--directory', str(args.directory), '--mode', 'early', '--cycles', '1000000',
           '--output', str(args.output)]
with args.output.with_suffix('.coordinator.log').open('x') as log:
    coordinator = subprocess.Popen(command, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 30
        selected = None
        while time.monotonic() < deadline and coordinator.poll() is None:
            if args.output.exists():
                for line in args.output.read_text().splitlines():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get('type') == 'sample' and record.get('cycle', 0) >= 2:
                        selected = record
                        break
            if selected:
                break
            time.sleep(0.02)
        if selected is None:
            raise RuntimeError('no completed warmup/checkpoint before interrupt deadline')
        # Use a pidfd, so even a racing exit/PID reuse cannot signal another app.
        handle = os.pidfd_open(selected['pid'])
        try:
            stat = Path('/proc', str(selected['pid']), 'stat').read_text().rsplit(') ', 1)[1].split()
            if stat[19] != selected['process_start_time'] or int(stat[1]) != coordinator.pid:
                raise RuntimeError('target is not the recorded decoder child')
            time.sleep(0.02)
            signal.pidfd_send_signal(handle, signal.SIGKILL)
        finally:
            os.close(handle)
        result = coordinator.wait(timeout=10)
        summary = json.loads(args.output.read_text().splitlines()[-1])
        if not result or summary.get('passed') is not False:
            raise RuntimeError('interrupted run was incorrectly accepted')
        if Path('/proc', str(selected['pid'])).exists():
            raise RuntimeError('interrupted decoder still exists')
        print(json.dumps({'interrupted_client': 'SIGKILL via bound pidfd',
                          'campaign_expected_failure': True,
                          'coordinator_returncode': result,
                          'last_complete_cycle': summary.get('cycles'),
                          'kernel_health_and_final_idle': 'owned by outer hwguard'}))
    finally:
        if coordinator.poll() is None:
            coordinator.terminate()
            try:
                coordinator.wait(timeout=5)
            except subprocess.TimeoutExpired:
                coordinator.kill()
                coordinator.wait(timeout=5)
