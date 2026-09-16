#!/bin/sh
# ThreadSanitizer pass over the concurrent API stress schedules, in a
# fresh sanitizer build (the regular suite builds ASan/UBSan). Skips with
# exit 77 when the toolchain or the runtime cannot support TSan here;
# the skip reason is printed so the evidence stays honest.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$script_dir/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM

command -v meson >/dev/null 2>&1 || {
    echo 'concurrent-tsan: SKIP (meson not available)'; exit 77; }
command -v cc >/dev/null 2>&1 || {
    echo 'concurrent-tsan: SKIP (no C compiler)'; exit 77; }

# Probe: can this toolchain build and RUN a multi-threaded TSan binary
# on this host? Sandboxes that block the ptrace TSan needs for its
# stop-the-world (default Docker seccomp) fail here, so the check skips
# instead of failing with a runtime error.
cat >"$work/probe.c" <<'EOF'
#include <pthread.h>
#include <stdatomic.h>
static atomic_long shared;
static void *fn(void *p) {
    for (int i = 0; i < 1000; i++)
        shared++;
    (void)p;
    return 0;
}
int main(void) {
    for (int round = 0; round < 3; round++) {
        pthread_t t[8];
        for (int i = 0; i < 8; i++)
            if (pthread_create(&t[i], 0, fn, 0))
                return 1;
        for (int i = 0; i < 8; i++)
            if (pthread_join(t[i], 0))
                return 1;
    }
    return shared < 0;
}
EOF
if ! cc -fsanitize=thread -pthread -o "$work/probe" "$work/probe.c" \
        2>"$work/probe.err"; then
    echo "concurrent-tsan: SKIP (toolchain cannot build TSan: $(head -1 "$work/probe.err"))"; exit 77
fi
if ! "$work/probe" >/dev/null 2>"$work/probe.run"; then
    echo "concurrent-tsan: SKIP (TSan runtime failed to start: $(head -1 "$work/probe.run"))"; exit 77
fi

meson setup "$work/build" "$root" -Db_sanitize=thread >/dev/null
meson compile -C "$work/build" tests/concurrent-stress >/dev/null
# Schedules: the threaded, mid-decode teardown and client-failure mixes.
# Any ThreadSanitizer report makes the binary exit nonzero and fails this
# check. The reports name their frames: findings whose racy accesses are
# both inside src/ are driver defects to turn into regressions+fixes;
# reports rooted in the harness or in uninstrumented third-party code
# (libva/libdrm internals) are classified during review, never silently
# accepted.
for schedule in "threads 1 12 3 1" "threads 2 12 3 2" "threads 4 12 3 4" \
                "teardown 2 12 3 812734691" "teardown 4 12 3 812734691" \
                "failure 4 12 3 3372110043"; do
    echo "tsan-schedule: concurrent-stress $schedule"
    # shellcheck disable=SC2086
    "$work/build/tests/concurrent-stress" $schedule >/dev/null
done
echo 'concurrent-tsan: PASS (no ThreadSanitizer reports in the driver or the harness)'
