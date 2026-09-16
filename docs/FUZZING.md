# Parser and VA-API sequence fuzzing

Offline coverage-guided fuzzing for the H.264, HEVC and VP9 bitstream parsers
and for legal/illegal VA-API picture sequences. Targets never open a decoder.
Generated input counts are not independent tests; the 72,000 deterministic
parser inputs in `h264-parser`, `hevc-parser` and `vp9-parser` stay regressions.

Issue: [iconidentify/libva-v4l2_request#22](https://github.com/iconidentify/libva-v4l2_request/issues/22).

## Two binaries per target

| Binary | Built when | Role |
| --- | --- | --- |
| `fuzz-<target>-replay` | default meson setup | Replay pinned seeds once (twice for identity). Registered as a meson test. |
| `fuzz-<target>` | `-Dfuzzing=enabled` with clang libFuzzer | Coverage-guided campaign. **Not** a meson test. |

Replay works with gcc or clang and `-Db_sanitize=address,undefined`. Campaign
binaries need clang, libFuzzer/compiler-rt, and `-fsanitize=fuzzer`. On
Debian/Ubuntu install `libclang-rt-dev` (or `libclang-rt-18-dev`) in addition
to clang; gcc cannot link these binaries.

```sh
meson setup build-test -Db_sanitize=address,undefined
meson test -C build-test fuzz-h264 fuzz-hevc fuzz-vp9 fuzz-va-api fuzz-budgets fuzz-provenance --print-errorlogs
```

```sh
CC=clang meson setup build-fuzz -Db_sanitize=address,undefined -Dfuzzing=enabled
meson compile -C build-fuzz
```

## Budgets and harness vs driver faults

Each `LLVMFuzzerTestOneInput` call:

- Truncates input to 64 KiB (oversize is not a harness fault).
- Limits wall time to 1 s in replay (`ITIMER_REAL`). Campaigns use libFuzzer `-timeout=1`.
- Caps live VA objects (8 extra surfaces/buffers) and, on the VA-API target, total `malloc`/`calloc`/`realloc` bytes (16 MiB). Exhaustion returns NULL / skips the opcode; it does not sanitizer-abort.
- Returns 0 for every well-bounded input. Parser rejection is success.

Harness faults print `fuzz-harness:` on stderr and `_exit(99)` (timeout, failed seed I/O). Sanitizer traps are parser/driver defects. The VA-API target wraps `open`/`open64`: `/dev/*` returns `ENODEV` and logs `fuzz-harness: device-open`; ioctls and poll fail closed with `ENODEV`.

## Pinned seeds

Synthetic in-tree files under `tests/fuzz/seeds/`. Licence and sha256 are in
`tests/fuzz/provenance.json` (checked by the `fuzz-provenance` meson test).
Nothing from the Fluster/ITU/WebM suites is committed: those terms are
`not-established` and stay download-on-demand via `tests/corpus.py fetch --smoke`.

To add smoke-corpus bytes on trusted infra without copying them into git:

```sh
python3 tests/corpus.py fetch --fluster /path/to/fluster --cache ~/.cache/libva-corpus --smoke
# Point libFuzzer at an extra corpus directory that contains copies or
# symlinks of the acquired files. Do not git-add them.
```

The HEVC `ue-overflow` seed is the minimized replay of the previous unchecked
32-leading-zero exp-Golomb / `pred_weight_table` VLA regression. The current
parser must reject it under ASan/UBSan; `hevc-parser` remains the original
assert-based case.

## 24 CPU-hour campaign (trusted infrastructure)

Public GitHub-hosted PR CI must not run this. GitHub-hosted jobs cap out at
six wall hours and this workflow is the required offline check, not a fuzzer
farm. Run on a trusted Linux machine:

```sh
CC=clang meson setup build-fuzz -Db_sanitize=address,undefined -Dfuzzing=enabled
meson compile -C build-fuzz
sh tests/fuzz-campaign.sh build-fuzz 24
```

`tests/fuzz-campaign.sh` copies the pinned seeds into `$builddir/fuzz-artifacts/<target>/corpus`
(libFuzzer writes new units there, never into `tests/fuzz/seeds/`). It records engine,
compiler, source SHA, jobs, `-max_len=65536`, `-timeout=1`, `-rss_limit_mb=2048` (ASan
quarantine needs more than 256 MiB) and the artifact prefix.
Each target is allotted 24 CPU-hours. **Budget exhaustion is not proof of
correctness.** A reproducible sanitizer failure is a defect: minimize, add the
seed under `tests/fuzz/seeds/`, record it in `provenance.json`, and (only then)
apply a bounded parser fix.

Coverage of critical parse/state branches should be published from the campaign
(`llvm-cov`/`gcov` over `src/bits.h`, `src/codec_h264.c`, `src/codec_hevc.c`,
`src/codec_vp9.c`, and the Begin/Render/End illegal sequences). Residual
unreachable paths include unimplemented H.264 data-partition NALs, non-4:2:0
VP9 profiles, and device-backed `CreateContext` (no decoder is attached). Do
not report the number of generated inputs as a test count.

A short smoke (`-max_total_time=30` per target) only proves the command starts.
It does not satisfy the 24 CPU-hour criterion.

## Crash artifacts

Follow [SECURITY.md](../SECURITY.md). Synthetic minimized seeds may live in
this tree. Do not attach core dumps, private media, host paths, or
exploit-shaped write-ups to a public issue or PR. If a finding looks like a
media-triggered memory bug, use the private-reporting route instead of
publishing the raw crash.
