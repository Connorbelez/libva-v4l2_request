#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Documented 24 CPU-hour libFuzzer campaign. See docs/FUZZING.md.
# This is not a meson test and must not run on public GitHub-hosted PR CI.
set -eu

usage() {
	echo "usage: $0 <builddir> [hours_per_target]" >&2
	echo "  builddir is configured with -Dfuzzing=enabled -Db_sanitize=address,undefined" >&2
	exit 2
}

[ "${1-}" ] || usage
builddir=$1
hours=${2:-24}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)

echo "engine: libFuzzer"
echo "compiler: ${CC:-clang} $($CC --version 2>/dev/null | head -1 || clang --version | head -1)"
echo "source: $(git -C "$root" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "builddir: $builddir"
echo "hours_per_target: $hours"
echo "jobs: $jobs"
echo "max_len=65536 timeout=1 rss_limit_mb=2048 (ASan)"
echo "budget exhaustion is not proof of correctness"

run_one() {
	name=$1
	bin=$builddir/tests/$name
	seeds=$root/tests/fuzz/seeds/$2
	art=$builddir/fuzz-artifacts/$name
	corpus=$art/corpus
	if [ ! -x "$bin" ]; then
		echo "skip $name: $bin not built (need -Dfuzzing=enabled and this codec)" >&2
		return 0
	fi
	# Copy seeds: libFuzzer writes new units into the corpus directory.
	mkdir -p "$corpus"
	cp -a "$seeds"/. "$corpus"/
	echo "==== $name ${hours}h ===="
	# ASan quarantine inflates RSS well beyond the 16 MiB input budget;
	# 256 MiB is enough for unsanitized libFuzzer and too small here.
	timeout "${hours}h" "$bin" "$corpus" \
		-max_len=65536 -timeout=1 -rss_limit_mb=2048 \
		-jobs="$jobs" -workers="$jobs" \
		-artifact_prefix="$art/" \
		|| status=$?
	status=${status:-0}
	# timeout(1) uses 124 when the budget expires; that is expected.
	if [ "$status" -eq 124 ]; then
		echo "$name: budget exhausted (not a pass/fail of the parser)"
		return 0
	fi
	if [ "$status" -ne 0 ]; then
		echo "$name: libFuzzer exited $status" >&2
		return "$status"
	fi
}

run_one fuzz-h264 h264
run_one fuzz-hevc hevc
run_one fuzz-vp9 vp9
run_one fuzz-va-api va-api
