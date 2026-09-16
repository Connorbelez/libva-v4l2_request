#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Documented 24 CPU-hour libFuzzer campaign. See docs/FUZZING.md.
# This is not a meson test and must not run a 24 CPU-hour job on public
# GitHub-hosted PR CI. --smoke is the bounded startup check.
set -eu

usage() {
	echo "usage: $0 [--smoke] <builddir> [cpu_hours_per_target]" >&2
	echo "  builddir is configured with -Dfuzzing=enabled -Db_sanitize=address,undefined" >&2
	echo "  cpu_hours_per_target is a positive integer (default 24)." >&2
	echo "  Wall time is cpu_hours / nproc so the CPU budget is not multiplied by cores." >&2
	exit 2
}

is_uint() {
	case ${1-} in
	''|*[!0-9]*) return 1 ;;
	esac
	[ "$1" -gt 0 ]
}

smoke=0
while [ $# -gt 0 ]; do
	case $1 in
	--smoke) smoke=1; shift ;;
	-h|--help) usage ;;
	--) shift; break ;;
	-*) usage ;;
	*) break ;;
	esac
done

[ "${1-}" ] || usage
[ -d "$1" ] || {
	echo "fuzz-campaign: builddir '$1' is not a directory" >&2
	exit 2
}
builddir=$(CDPATH= cd -- "$1" && pwd)
cpu_hours=${2:-24}
if [ "$smoke" -eq 0 ]; then
	is_uint "$cpu_hours" || {
		echo "fuzz-campaign: invalid cpu_hours_per_target '$cpu_hours'" >&2
		exit 2
	}
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
[ "$jobs" -gt 0 ] || jobs=1
compiler=${CC:-clang}
if ! command -v "$compiler" >/dev/null 2>&1; then
	compiler=clang
fi
compiler_ver=$("$compiler" --version 2>/dev/null | head -1 || echo unknown)
source_sha=$(git -C "$root" rev-parse HEAD 2>/dev/null || echo unknown)
workers=$jobs
if [ "$smoke" -eq 1 ]; then
	workers=1
	wall_secs=0
else
	wall_secs=$((cpu_hours * 3600 / workers))
	if [ "$wall_secs" -lt 1 ]; then
		workers=1
		wall_secs=$((cpu_hours * 3600))
	fi
fi

echo "engine: libFuzzer"
echo "compiler: $compiler $compiler_ver"
echo "source: $source_sha"
echo "builddir: $builddir"
echo "cpu_hours_per_target: $cpu_hours"
echo "workers: $workers"
echo "wall_seconds_per_target: $wall_secs"
echo "max_len=65536 timeout=1 rss_limit_mb=2048 (ASan)"
echo "budget exhaustion is not proof of correctness"
if [ "$smoke" -eq 1 ]; then
	echo "mode: smoke (-runs=1000, one worker)"
fi

have_timeout_kill=0
if [ "$smoke" -eq 0 ]; then
	if timeout --kill-after=1s 2s true >/dev/null 2>&1; then
		have_timeout_kill=1
	else
		echo "fuzz-campaign: GNU timeout --kill-after is required for a full campaign" >&2
		exit 2
	fi
fi

failed=0
missing=0
ran=0

record_header() {
	art=$1
	name=$2
	{
		echo "engine: libFuzzer"
		echo "compiler: $compiler $compiler_ver"
		echo "source: $source_sha"
		echo "target: $name"
		echo "cpu_hours_per_target: $cpu_hours"
		echo "workers: $workers"
		echo "wall_seconds: $wall_secs"
		echo "smoke: $smoke"
		echo "started_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
	} > "$art/campaign-record.txt"
}

findings_in() {
	art=$1
	set +e
	ls "$art"/crash-* "$art"/leak-* "$art"/oom-* "$art"/timeout-* >/dev/null 2>&1
	found=$?
	set -e
	[ "$found" -eq 0 ]
}

worker_failure() {
	art=$1
	# libFuzzer -jobs writes fuzz-N.log in the cwd (artifact dir).
	if grep -E -q 'ERROR:|DEADLYSIGNAL|SUMMARY: AddressSanitizer|SUMMARY: UndefinedBehaviorSanitizer|out-of-memory' \
		"$art"/fuzz-*.log "$art"/libfuzzer.stderr "$art"/libfuzzer.stdout 2>/dev/null; then
		return 0
	fi
	return 1
}

run_one() {
	name=$1
	seed_dir=$2
	bin=$builddir/tests/$name
	seeds=$root/tests/fuzz/seeds/$seed_dir
	art=$builddir/fuzz-artifacts/$name
	corpus=$art/corpus

	if [ ! -x "$bin" ]; then
		echo "fuzz-campaign: missing required binary $bin (need -Dfuzzing=enabled and this codec)" >&2
		missing=$((missing + 1))
		failed=$((failed + 1))
		return 0
	fi
	if [ ! -d "$seeds" ]; then
		echo "fuzz-campaign: missing seed directory $seeds" >&2
		failed=$((failed + 1))
		return 0
	fi

	mkdir -p "$corpus" "$art"
	# Copy seeds: libFuzzer writes new units into the corpus directory.
	cp -a "$seeds"/. "$corpus"/
	record_header "$art" "$name"
	echo "==== $name cpu-hours=$cpu_hours workers=$workers wall=${wall_secs}s smoke=$smoke ===="
	ran=$((ran + 1))

	# ASan quarantine inflates RSS well beyond the 16 MiB input budget.
	start_s=$(date +%s)
	status=0
	# Run with cwd=$art so libFuzzer worker logs (fuzz-N.log) land with the record.
	if [ "$smoke" -eq 1 ]; then
		set +e
		( cd "$art" && "$bin" "$corpus" \
			-runs=1000 -max_len=65536 -timeout=1 -rss_limit_mb=2048 \
			-jobs=1 -workers=1 \
			-artifact_prefix="$art/" \
			> "$art/libfuzzer.stdout" 2> "$art/libfuzzer.stderr" )
		status=$?
		set -e
	else
		set +e
		( cd "$art" && timeout --kill-after=60s "${wall_secs}s" \
			"$bin" "$corpus" \
			-max_len=65536 -timeout=1 -rss_limit_mb=2048 \
			-jobs="$workers" -workers="$workers" \
			-artifact_prefix="$art/" \
			> "$art/libfuzzer.stdout" 2> "$art/libfuzzer.stderr" )
		status=$?
		set -e
	fi
	end_s=$(date +%s)
	elapsed=$((end_s - start_s))
	{
		echo "finished_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
		echo "wall_elapsed_s: $elapsed"
		echo "exit_status: $status"
		echo "findings:"
		ls "$art"/crash-* "$art"/leak-* "$art"/oom-* "$art"/timeout-* 2>/dev/null || echo "(none)"
		echo "worker_logs:"
		ls "$art"/fuzz-*.log 2>/dev/null || echo "(none)"
	} >> "$art/campaign-record.txt"

	if findings_in "$art" || worker_failure "$art"; then
		echo "$name: sanitizer or libFuzzer finding (exit $status); see $art" >&2
		failed=$((failed + 1))
		return 0
	fi
	if [ "$smoke" -eq 1 ]; then
		if [ "$status" -ne 0 ]; then
			echo "$name: smoke exited $status" >&2
			failed=$((failed + 1))
		fi
		return 0
	fi
	# 124: GNU timeout SIGTERM after the CPU-hour wall budget.
	# 137: SIGKILL after --kill-after; cleanup, not a hidden crash if no artifacts.
	if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
		echo "$name: budget exhausted (not a pass/fail of the parser) exit=$status elapsed=${elapsed}s"
		echo "budget: exhausted exit=$status" >> "$art/campaign-record.txt"
		return 0
	fi
	if [ "$status" -ne 0 ]; then
		echo "$name: libFuzzer exited $status" >&2
		failed=$((failed + 1))
		return 0
	fi
	echo "$name: libFuzzer exited 0 before the budget (smoke-scale run or tiny corpus)"
}

run_one fuzz-h264 h264
run_one fuzz-hevc hevc
run_one fuzz-vp9 vp9
run_one fuzz-va-api va-api

echo "targets_run=$ran missing=$missing failed=$failed" >&2
if [ "$ran" -eq 0 ]; then
	echo "fuzz-campaign: no campaign binaries ran" >&2
	exit 1
fi
if [ "$failed" -ne 0 ]; then
	exit 1
fi
exit 0
