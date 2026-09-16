# Concurrent API stress schedules

**AI disclosure:** This document, including its title and all prose, was generated
by AI at the repository owner's request.

This is the schedule and evidence record for
[libva-v4l2_request#36](https://github.com/iconidentify/libva-v4l2_request/issues/36)
(*Stress concurrent API calls, context teardown and frame access*). It covers the
**offline** schedules: what runs, with which seeds, what the exact expected results
are, and what is deliberately **not** claimed here. The guarded-hardware campaign
(ten repetitions of each schedule on a qualified M1 device through
`tests/hwguard.py`) is the separate hardware gate of #36 and is documented as a
runbook below; it has not been run as part of the offline work.

## What the offline harness proves — and what it cannot

`tests/concurrent-stress.c` links the real driver sources against an in-memory
model V4L2 device. There is no decoder, no kernel module and no real sleeping:
the model completes queued requests after a seeded number of model-time ticks,
and time only advances when the driver polls or reads its monotonic clock, so
concurrent threads genuinely overlap inside the driver's wait paths while every
wait still terminates deterministically at its own deadline.

The schedules exercise the real public API surface the same way a threaded
client does:

* locked entrypoints from `v4l2r_lock_surface_api()` — `vaCreateContext`,
  `vaBeginPicture`, `vaRenderPicture`, `vaEndPicture`, `vaSyncSurface`,
  `vaQuerySurfaceStatus`, `vaExportSurfaceHandle`, `vaDeriveImage`,
  `vaGetImage`, `vaDestroyContext`, `vaDestroySurfaces` — called from many
  threads at once;
* deliberately interleaved with the **unlocked** entrypoints
  (`vaCreateSurfaces2`, `vaCreateBuffer`, `vaMapBuffer`, `vaUnmapBuffer`,
  `vaDestroyBuffer`, `vaCreateImage`, `vaDestroyImage`), the way FFmpeg splits
  decode and filter threads over one VA display;
* context teardown concurrent with decode and readback of other streams, and
  surviving derived images/surfaces read back through the preserved state
  after `vaDestroyContext`.

The model device is the decode oracle: the slice bytes a stream renders into a
picture are hashed (FNV-1a) when the request is queued, and the completion writes
a byte pattern derived from that hash into the target CAPTURE plane. Readers
recompute the same pattern from the frame bytes, so a lost frame, cross-stream
pixels or a stale buffer reuse all fail an exact byte comparison. The decoder
waits for a surface's previous frame to be read back before decoding into that
surface again, matching a real decoder surface pool (the kernel contract covers
decode completion, not client readback).

What this harness cannot prove: anything about a real AVD decoder, the kernel's
own serialization, real dma-buf fences, or throughput. Those need the guarded
hardware campaign. Offline schedules are seeded and deterministic in their
results; OS thread interleaving varies between runs (that is the point), and the
only intentionally interleaving-dependent output is the mid-decode teardown
victim's verified frame count, which is asserted to be at least half of its
frames with every verified frame byte-exact.

## Schedules and seeds

Registered Meson cases (10 repetitions each; per-repetition seeds are derived
deterministically from the recorded base seed and the repetition index and are
printed in each `rep` line):

| Meson test | Schedule | Base seed | Frames/stream |
| --- | --- | --- | --- |
| `concurrent-threads-1/2/4` | `threads N 12 10` | `549203187` | 12 |
| `concurrent-teardown-1/2/4` | `teardown N 12 10` | `812734691` | 12 |
| `concurrent-failure-4` | `failure 4 12 10` | `3372110043` | 12 |
| `concurrent-processes-1/2/4` | `concurrent-process.py` (3 reps) | `0xC0FFEE` | 12 |
| `concurrent-tsan` | TSan build, 6 schedules × 3 reps | per schedule | 12 |

Each stream uses its own mixed profile (H.264/HEVC/VP9 model codecs), surface
dimensions (64x48, 64x64, 128x96, 96x64), a seeded surface rotation and seeded
readback modes (client image with pitch-strided verification, or exported
dma-buf read directly through the model plane storage). Per-stream results are
printed as `stream <id> frames=<n> MD5=<hex>` where the MD5 covers the verified
frame sequence; the teardown victim prints its interleaving-dependent count.

`concurrent-process.py` runs the same single-stream worker (`concurrent-stress
worker ID FRAMES SEED`) in 1/2/4 separate processes behind a start barrier
(one stdin byte), aggregates the per-worker results and enforces deadlines.
Offline this proves per-process integrity and clean teardown with no shared
state; contention for one real decoder is the hardware gate.

`concurrent-tsan.sh` builds the stress harness with `-Db_sanitize=thread` in a
fresh build directory and runs the threaded, teardown and failure schedules.
Any ThreadSanitizer report fails the check; reports name their frames so
driver-scope findings (both accesses inside `src/`) can be turned into
regressions, while harness- or uninstrumented-library-scope reports are
classified during review rather than silently accepted. Where the toolchain or
the sandbox cannot run TSan (for example containers that block the ptrace the
TSan runtime needs), the check skips with exit 77 and prints the reason.

## Offline results

Recorded from the validation runs of this work (Ubuntu 24.04.5 LTS, aarch64,
GCC 13.3.0, meson 1.3.2, ninja 1.11.1, libva 1.20.0, libdrm 2.4.125,
`-Db_sanitize=address,undefined`; commands and raw output retained in the
evidence log):

* Baseline on the base commit before any change: **127/127** Meson cases,
  `tests/frame-check.sh` and `tests/shared-contexts.sh` software checks pass.
* With the new schedules: **139/139** Meson cases — the baseline set plus
  `concurrent-threads-1/2/4`, `concurrent-teardown-1/2/4`,
  `concurrent-failure-4`, `concurrent-processes`,
  `concurrent-processes-1/2/4` and `concurrent-tsan` (which ran, not skipped).
  The `threads` and `failure` schedules verify **12/12 frames per stream in
  every repetition** with stable per-stream MD5s; the `teardown` victims
  verified between 6 and 12 frames (at least half, byte-exact in every case).
  Flakiness check: 10 consecutive full runs each of `failure 4 12 10` and
  `teardown 4 12 10` (100 repetitions per schedule) with zero failures.
* Per-configuration registered counts (re-measured, including the new cases):
  139 on 6.8 UAPI with all codecs, 132 on 22.04/5.15, 123 on 20.04/5.4,
  128 with all codecs disabled, 135 with HEVC forced and VP9 disabled.
* Clang 18.1.3: the harness compiles warning-free, links and passes a
  threaded schedule run (the local image lacks clang's ASan runtime files,
  so the clang sanitizer combination itself is left to the CI matrix where
  the runtime is installed).
* ThreadSanitizer (GCC 13.3, `-Db_sanitize=thread`, fresh build): the
  threaded, teardown and failure schedules run with **zero reports** in the
  driver or the harness on this host.

## Guarded-hardware runbook (not run offline)

The hardware repetitions of these schedules need an exclusive `tests/hwguard.py`
lease on a qualified device and are not claimed by the offline work. The
offline-equivalent steps for a device owner:

1. Build an unsanitized driver (`meson setup build && meson compile -C build`).
2. Take the hardware guard lease with a finite deadline and a journal
   since-stamp; close all other video clients first.
3. Run the process schedule against the real driver with
   `LIBVA_DRIVERS_PATH=<build>/src` — `concurrent-process.py` with a worker
   binary that decodes through VA-API (the offline worker links the driver
   directly and cannot be used on hardware); record per-worker hashes,
   deadlines, guard logs and kernel journal deltas.
4. Ten repetitions of each declared schedule, comparing the exact passing sets
   and per-stream hashes against the software references; any kernel fault,
   stuck task or abandoned decoder holder fails the criterion and stops the
   campaign.
5. Historical HEVC corruption findings are reported to their dedicated ticket
   (#39/#43), never hidden inside this one.

## Limits

* No hardware was opened, installed, rebooted or kernel-tested for the offline
  work; the hardware criteria of #36 remain open until the guarded campaign
  above runs on a qualified device.
* The model decoder implements the V4L2 M2M request flow the driver uses
  (CREATE_BUFS/QUERYBUF/QBUF/DQBUF/requests/EXPBUF) — it does not model kernel
  scheduling, fences, or format conversion; converter paths are covered by
  other offline cases and the hardware matrix.
* TSan coverage depends on the runtime being permitted in the environment;
  the skip is loud (exit 77 with the reason), never silent.
