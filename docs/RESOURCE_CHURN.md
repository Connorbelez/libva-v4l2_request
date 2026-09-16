# Resource churn and ownership bounds

Issue [#41](https://github.com/iconidentify/libva-v4l2_request/issues/41)
separates exact userspace ownership from real-device resource measurements. The
offline model proves that the driver releases what it owns while the initialized
VA display remains alive. It does not prove that a kernel releases video buffers,
that firmware remains healthy, or that RSS has a universal platform-independent
limit.

## Offline ownership model

`tests/failure-cleanup.c` wraps allocation, mapping, descriptor and fake V4L2
operations. Its resource snapshot records:

- live model descriptors by video, media, request, dma-buf and converter kind;
- mapping count and mapped bytes;
- non-handle heap allocation count and bytes;
- live config, context, surface, buffer and image objects; and
- handle-table backing arrays and bytes as a separate bounded high-water cache.

The initialized-driver baseline contains one config and the five initial handle
tables. Each table starts with 32 slots. Tables may grow geometrically to the
peak concurrent object count and retain that capacity until `vaTerminate`; live
objects are still required to return to zero. Per-context OUTPUT/CAPTURE storage,
cached exports and mappings must disappear when that context lifecycle ends.

Three sanitizer cases each run 1,000 complete lifecycles in one initialized
driver and assert exact ownership balance after every cycle:

- `failure-churn-normal`: decode, synchronize, export, close caller-owned export
  descriptors, destroy the surface and destroy the context;
- `failure-churn-early-export`: export and close a standalone backing before the
  first decode, decode into it, then destroy the lifecycle; and
- `failure-churn-held-image`: derive and map an image, destroy the context, prove
  the surviving image remains readable, then destroy the image and surface.

Each case prints checkpoints at cycles 100, 500 and 1,000. Model descriptors are
generation-tagged and closed slots are reused, so the campaign cannot hide a
stale or double close and does not exhaust a monotonically increasing fixture ID.
The existing operation-index failure sweeps also destroy all contexts, images,
buffers and surfaces without terminating the driver, assert lifecycle balance,
complete a fresh decode, and assert balance again. Final `vaTerminate` accounting
remains a separate zero-resource check rather than the only cleanup proof.

```sh
meson setup build-resource -Db_sanitize=address,undefined
meson test -C build-resource --print-errorlogs \
  failure-churn-normal failure-churn-early-export failure-churn-held-image
```

## Process-local measurements

`tests/resource-churn.py` reads only `/proc/<pid>` for the selected process. It
records descriptor count, mapping count and virtual bytes, `VmRSS`, `VmHWM`, the
RSS components reported by the kernel, and dma-buf fdinfo when present. It never
reads global debugfs dma-buf data or another application's media. Duplicate
dma-buf descriptors are counted as references and deduplicated by fdinfo identity
where the kernel exposes one. Missing identity makes unique-object/byte metrics
unavailable; it is not converted into a made-up identity per descriptor.

The dma-buf result is `observed`, `none-observed`, or `unavailable`; unavailable
accounting is never reported as zero and fails acceptance mode. Reference,
unique-object and byte growth have separate thresholds. Every JSONL metadata,
sample and summary record is flushed and fsynced. Acceptance mode requires an
explicit RSS-growth threshold because libc and sanitizer retention make a
universal threshold misleading. It also fails when the target exits before every
requested sample is recorded, so a short run cannot stand in for a soak.

The campaign binds to the process start time before warmup and rejects identity
changes across samples. Timing options must be finite, with positive intervals
and at least two samples; invalid options fail before a workload starts. Sample
records include wall and monotonic clocks, and the summary records the elapsed
sample span. `/proc` reads are not an atomic process snapshot: detected races fail,
but exact resource checkpoints still require the workload to be quiescent.
Only the executable basename is recorded, not command arguments that might contain
media paths or credentials. Preserve reviewed workload/input provenance separately.

```sh
python3 tests/resource-churn.py self-test

V4L2R_SOURCE_COMMIT=$(git rev-parse HEAD) \
python3 tests/resource-churn.py monitor \
  --pid "$PID" --samples 11 --interval 1 --acceptance \
  --warmup-seconds <declared-warmup> \
  --max-fd-growth 0 --max-map-growth 0 --max-mapped-growth-kib 0 \
  --max-dmabuf-reference-growth 0 --max-dmabuf-object-growth 0 \
  --max-dmabuf-growth-kib 0 \
  --max-rss-growth-kib <reviewed-bound> \
  --output resource-churn.jsonl
```

For a long-lived command, `run` starts the workload and records its process until
the requested sample count is reached. Its explicit warmup precedes the baseline,
so expected one-time process and decoder allocations are separated from measured
steady-state growth. Choose the interval and sample count to cover the declared
duration; an early or nonzero workload exit fails the summary. The command must
exit after its bounded campaign. A workload still running after the final sample
and `--exit-timeout` is terminated and the result fails rather than hanging the
sampler. `run` owns a new process group and cleans up that group on logging or
sampling exceptions as well as on timeout; forced cleanup fails acceptance. SIGINT
and SIGTERM propagate through bounded cleanup, including a guard's stop signal.
An interrupted campaign is incomplete even if it has no final summary.
`monitor --pid` only observes and never terminates its target. Descendant resource
usage is not sampled, so the measured decoder must be the selected process, not a
wrapper that runs the decoder in a child. If output cannot be written, the command
fails and cleans up, but cannot promise a durable summary on that broken output.

```sh
V4L2R_SOURCE_COMMIT=$(git rev-parse HEAD) \
python3 tests/resource-churn.py run \
  --samples 61 --interval 60 --acceptance \
  --max-fd-growth <reviewed-bound> --max-map-growth <reviewed-bound> \
  --max-mapped-growth-kib <reviewed-bound> \
  --max-rss-growth-kib <reviewed-bound> \
  --output soak.jsonl -- <workload> <arguments>
```

## Hardware boundary

The acceptance campaign still requires a capable M1, an exclusive lease through
`tests/hwguard.py`, an unsanitized selected userspace build, pinned valid media,
correct sample hashes, checkpoints at cycles 100/500/1,000, and a separate
60-minute soak. Record the driver commit, device, kernel/package/module identity,
guard log, raw resource JSONL and exact hashes. Stop at the first new decoder
fault or wedge and leave the decoder idle; do not reopen it as automatic
recovery.

Offline exact ownership is not hardware qualification. Process exit reclaim is
not accepted as proof of in-process cleanup, and RSS alone is not proof that
dma-bufs or kernel request descriptors were released.
