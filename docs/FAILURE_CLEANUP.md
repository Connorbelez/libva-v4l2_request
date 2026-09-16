# Request and resource failure audit (#35)

The driver now releases OUTPUT buffers attached to an idle request immediately
when submission fails, stops submitting through ambiguous CAPTURE state, and
retains decoder/converter errors through sync, export and readback. Interrupted
waits retry within a monotonic deadline. A timeout does not recover a kernel.

Issue: [#35](https://github.com/iconidentify/libva-v4l2_request/issues/35).
Implementation: [PR #68](https://github.com/iconidentify/libva-v4l2_request/pull/68).
Source/device provenance, per-operation injection results, hardware comparisons
and artifact hashes: [validation record](failure-cleanup-validation-2026-09-16.json).
This extends the [API lifetime contract](API_LIFETIME.md).

## Before and after

The unchanged merged baseline `3d620db0417467c55d8600f5c79947cca71fdcf6`
passed all 75 existing ASan/UBSan tests and both software frame scripts.
The model-device control then passed while seven initial failure cases failed:

- Failed CAPTURE QBUF stranded an OUTPUT attached to an idle request; wrapping
  the ring stalled. Immediate REINIT now releases it. The same context completes
  two ring rotations and reuses the failed surface.
- Failed request QUEUE left CAPTURE available for a later unrelated picture.
  That context now rejects work until destruction cancels its ownership.
- A hard CAPTURE dequeue error was discarded before a later successful dequeue.
  It now propagates and stops the damaged queue.
- Converter destination error flags, source dequeue failure, and source export
  failure could each produce false-success output. All three now fail affected
  surfaces and cancel importer ownership before releasing job references.
- EINTR terminated an otherwise successful poll. It now retries using the
  remaining deadline, including a finite limit for repeated interruptions.

Five further failing sequences were reproduced and fixed during sweeps/review:

- Successful VPP retry retained the previous destination error. Replacement
  pixels now reset that destination's status.
- Converter startup followed by failed decoder OUTPUT STREAMON allowed setup
  to overwrite and leak the converter. Partial startup now makes the context
  explicitly unusable; OUTPUT streaming is tracked independently for teardown.
- Rejection of a later client buffer allowed work while an earlier slice held
  CAPTURE. EndPicture now stops the context on that partial failure.
- Failed CAPTURE QUERYBUF left a kernel index hole whose untouched descriptor
  fields appeared to own fd 0. Every slot now starts with invalid descriptors.
- A dequeue error after the target completed made the first sync fail but a
  repeated sync succeed. Sync now retains its error on that target even when
  its queued bit cleared during the drain. Unrelated completed surfaces remain
  readable. Stronger assertions reproduced this in normal and early-export sweeps.

These are twelve observed failure sequences, not twelve security findings.
The final current-header build passes **123 Meson tests**, including **48 new
cases**. Twenty-three cases sweep **260 individually injected operation
failures**, with successful control traces, recovery and balanced resource
accounting. Injection count is separate from test count. Older UAPI builds omit
four held-slice cases when that capability is unavailable.

## Failure-point inventory and ownership

`tests/failure-cleanup.c` intercepts heap allocation, open/close, descriptor
duplication, mmap/munmap, ioctl and poll, including large-file and fortified
libc aliases. It never opens a real device. A model request owns attached OUTPUT
independently from completed OUTPUT; CAPTURE ownership is separate. Duplicate
queues, invalid descriptors, double frees/unmaps and remaining resources at
Terminate are assertions, not process-exit cleanup.

Each sweep records a successful operation trace, then fails each index once in
a fresh fixture. Required work must return an operation/allocation error;
optional CAPTURE/default-format/alternate-codec probes must complete through the
fixture's valid alternative. DestroyContext still succeeds after STREAMOFF
failure because closing the video instance owns final cancellation; preservation
failures remain errors on the surviving surface. The validation record enumerates
every index, operation and observed VAStatus.

| Phase and failure sites | Cases (`failure-` prefix) | Ownership and recovery assertion |
| --- | --- | --- |
| Context/codec/handle allocation; video/media open; QUERYCAP, ENUM_FMT/FRAMESIZES, S/G_FMT, CREATE_BUFS capabilities, QUERY_EXT_CTRL | `sweep-create`, `sweep-create-single`, `sweep-create-vpp`, `sweep-create-grown` | No failed context published; partial descriptors/storage released; fresh context/picture succeeds. Includes handle realloc failure. |
| OUTPUT CREATE_BUFS, QUERYBUF, mmap, request allocation; growth | creation sweeps; `sweep-grow` | Partial maps/requests released; failed growth keeps original pointer/content. Kernel buffers remain owned by the video instance until close. |
| CAPTURE selection, converter setup, STREAMON, CREATE_BUFS/QUERYBUF, backing fcntl-dup, reader fence poll | `sweep-decode`, `sweep-decode-early`, `sweep-decode-convert`, `sweep-attach-pair` | Partial startup cannot restart unsafely; acquired backing remains owned until cleanup. Index holes never own fd 0. Recoverable bind retry works. |
| Request REINIT, controls, OUTPUT/CAPTURE QBUF, request QUEUE | decode sweeps; `capture-queue`, `request-queue`, `rollback-error` | Idle OUTPUT rolls back immediately. Failed rollback or queued CAPTURE stops new submission. Recoverable same-context reuse or fresh-context recovery succeeds. |
| Failure after an earlier slice: invalid client buffer, controls, OUTPUT QBUF, request QUEUE | `partial-client`, `partial-controls`, `partial-output`, `partial-request` | Later BeginPicture and queue count prove no new submission; repeated sync/export/DeriveImage/GetImage retain error; fresh context works. |
| OUTPUT/CAPTURE DQBUF and invalid index; hard errors before/after completion | decode sweeps; `dequeue-error`, `dequeue-index` | No unknown slot is reused. Error remains on the sync target and pending work. Failed contexts reject further submission. |
| EINTR, EAGAIN, short poll timeout, readiness followed by repeated EAGAIN | `dequeue-eintr`, `dequeue-eagain`, `dequeue-timeout`, `dequeue-stale-ready`, `poll-interrupted`, `poll-interrupt-deadline` | Transient success remains success; stale readiness/repeated interruptions reach a finite deadline. Existing poll-event regressions remain enabled. |
| Converter allocation/open/S_FMT/REQBUFS/STREAMON, backing/export, both QBUF/DQBUF sides | `sweep-convert-setup`, `sweep-convert-kick`, `sweep-convert-wait`, `sweep-decode-convert`; `converter-error-flag`, `converter-source-error`, `converter-export-error`, `converter-index`, `converter-source-index` | Setup failure publishes no converter. Job failure closes its fd, retains a failed-chain marker, clears job references/pending ownership, retains surface errors. Failed surface can be destroyed before context. |
| Converter transient dequeue/readiness | `converter-eintr`, `converter-eagain`, `converter-source-eagain`, `converter-stale-ready` | Destination and paired source both complete before slot reuse; bounded wait/error behavior. |
| VPP open/formats, stride renegotiation, REQBUFS/STREAMON, controls/selections, both queue/dequeue sides | `sweep-vpp`, `sweep-vpp-stride` | Failed instance is destroyed; valid next picture reconfigures and syncs. Source ownership stays valid. |
| Standalone backing allocation/probes/CREATE_BUFS/QUERYBUF/EXPBUF, output dup; capture export/mapping | `sweep-export`, `sweep-export-single`, `sweep-export-probes`, `sweep-view`, `sweep-view-dmabuf`, `sweep-view-pair`, `sweep-export-pair` | No partial descriptor ownership transferred. Cached maps/exports remain explicitly owned for retry/teardown. Read/export retries succeed. |
| Destruction, STREAMOFF, preservation allocation/EXPBUF | `sweep-preserve`, `sweep-preserve-pair`; every teardown | Surviving completed surfaces retain backing or explicit error. Failed contexts never flush new work. Close/unmap/free accounting returns to zero. |

Pair fixtures model two memory planes for partial acquisition cleanup; they do
not qualify a hardware layout. close/munmap are accounted release operations;
the model does not simulate a kernel refusing to release resources on close.
Sweeps exercise the listed shared resource paths, not every codec-specific
control combination or arbitrary simultaneous failures. Failed idle rollback
has a separate two-fault case.

Ownership follows Linux's [request reinitialization contract](https://docs.kernel.org/userspace-api/media/mediactl/media-request-ioc-reinit.html),
[stateless decoder sequence](https://docs.kernel.org/userspace-api/media/v4l/dev-stateless-decoder.html)
and [queue/dequeue contract](https://docs.kernel.org/userspace-api/media/v4l/vidioc-qbuf.html).
REINIT is valid for idle/completed requests; it cannot cancel an unfinished
queued request. Multi-slice OUTPUT completion does not prove held CAPTURE is
complete. Fresh-model-device success does not prove real kernel-wedge recovery.

## Validation and limits

Offline validation uses an unprivileged disposable namespace without decoder
devices/network, with only this source/build tree writable:

```sh
meson setup build-offline -Db_sanitize=address,undefined
meson compile -C build-offline -j3
meson test -C build-offline --print-errorlogs
sh tests/frame-check.sh
sh tests/shared-contexts.sh
```

Both software scripts pass: native-size resize/crop comparisons, expected
truncation rejection and four streams / 168 exact software frames. Hosted CI
also covers GCC/Clang on x86_64/aarch64, old dependencies/UAPI, codec feature
options, static analysis and corpus gates. The first implementation's unused
fixture variable failed an old-header static build; its use is now unconditional.
The required aggregate correctly rejected that failed run. The exact final PR
head must pass the protected `userspace` gate before merge.

Hardware uses an unsanitized candidate selected by `LIBVA_DRIVERS_PATH` through
`tests/hwguard.py`, with exclusive ownership, deadlines, foreign-client/new-fault
monitoring and idle final checks. The journal boundary is the previously
validated September 16 midnight window; historical September 15 faults remain.
Final results and exact tested identities are in the validation record.

The final M1 build passes 864 generated frame comparisons (336 shared-context,
144 H.264 High10, 384 VP9) across normal/early-export paths, plus native-size
resize/crop and expected truncation rejection. Both guards finish clean and idle.
Exact passing sets remain HEVC **144/147**, AVC **73/135**, opt-in High10 FRExt
**27/69**, and VP9 **216/305**. Selected VP9 High10 **1/1** and profile overrides
**5/5** are subsets, not full-suite qualification. All four strict comparison
reports match the preceding merged build. AVC remains non-green for known
`FM1_FT_E` software fallback with zero accepted frames; it is never counted as
a hardware pass. Known HEVC failure classifications are retained in the record.

Review was **maintainer self-review**, not independent review. The final pass
checked partial-completion errors, idle request rollback, converter cancellation
before releasing surface references, partial initialization, descriptor sentinels
and bounded interrupted waits. Stronger status/recovery assertions found the
last sync bug.

No real-device failure injection, module operation, installation or reboot
occurs in this task. Non-M1 converter/VPP hardware is unavailable; model coverage
does not promote its support tier (#40). True thread/process schedules remain
#36, sustained resource measurements #41 and client fallback #49. Codec gaps stay
visible; decoded pixels do not qualify Chrome display, suspend/resume or boot
stability. No kernel recovery or new codec support is claimed.
