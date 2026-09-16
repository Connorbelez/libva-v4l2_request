# Request and resource failure audit (#35)

Baseline: `3d620db0417467c55d8600f5c79947cca71fdcf6`. The existing 75
ASan/UBSan tests and both software frame scripts pass in an isolated namespace
without decoder devices or network access.

The model in `tests/failure-cleanup.c` intercepts allocation, descriptor,
mapping, ioctl and poll operations. It accounts for owned userspace resources
and models request-bound OUTPUT buffers separately from completed buffers.
The model's successful picture/control case must pass before failed cases
count as driver reproductions. It never opens a real decoder or converter.

Initial reproduced failures (before production changes): continued submission
after failed CAPTURE/request queueing; an initial dequeue error discarded before
a later successful dequeue; ignored converter error flags, source dequeue and
source-export failures; and an interrupted poll treated as a terminal failure.
These seven cases fail while the valid-operation control passes.

## Failure-point inventory and cleanup contract

This is the implementation checklist; coverage and evidence will be completed
before the ticket can close. Optional capability/probe failures must be kept
distinct from failed work. A failed hardware operation is injected only in the
offline model. Real hardware runs exercise valid operations through the guard.

| Phase / failure sites | Required ownership and recovery |
| --- | --- |
| Context handle/codec allocation, video/media open, capability/format/frame-size probes, codec controls | Publish no failed context. Close accepted descriptors and free codec/handle storage. A fresh context remains possible. Optional unsupported probes may choose another valid path. |
| OUTPUT CREATE_BUFS, QUERYBUF, mmap, request allocation; growth | Keep original storage until a replacement succeeds; clean partial replacement mappings/requests. Kernel allocations live with the video instance until close. Do not grow from zero or return unusable storage. |
| CAPTURE format/capability selection, converter setup, STREAMON, allocation/query, backing duplication | Never pretend a partially configured queue is ready; track ownership across partial start and clean teardown. Preserve existing exported backing identities. |
| Request poll/REINIT, controls, OUTPUT/CAPTURE QBUF, request QUEUE, failures after an earlier slice | Do not submit into ambiguous queue state or expose an incomplete frame as successful. Preserve failure after later completion; a fresh context can recover from a model failure. |
| OUTPUT/CAPTURE DQBUF, poll/EAGAIN/EINTR/timeout/invalid events; reader fences | Keep waits bounded, do not discard hard dequeue failures or reuse storage before readers/requests complete. A userspace timeout does not recover the kernel. |
| Converter allocation/open/formats/REQBUFS/STREAMON; backing/export; both queue/dequeue sides | Retain failure on affected surfaces and stop unsafe reuse. Reject bad completion flags/indices. Closing the converter owns cancellation and imported-buffer release. |
| VPP allocation/configuration, formats/controls/selection, queue/dequeue/poll | Tear down a failed instance; a new valid picture can reconfigure. Retain the failed destination status. |
| Standalone backing allocation/probes/CREATE_BUFS/QUERYBUF/EXPBUF, capture export/mmap, descriptor dup | Release unpublished descriptors/maps; retain explicitly owned cached resources for retry or teardown. A failed export must not transfer partial descriptor ownership. |
| Context destruction: wait, preservation allocation/export, STREAMOFF, close/unmap | Preserve completed surfaces or retain explicit failure. Release owned resources once. Teardown must not silently resubmit a failed picture. |

Request ownership follows the Linux [request reinitialization contract](https://docs.kernel.org/userspace-api/media/mediactl/media-request-ioc-reinit.html),
[stateless decoder sequence](https://docs.kernel.org/userspace-api/media/v4l/dev-stateless-decoder.html)
and [queue/dequeue contract](https://docs.kernel.org/userspace-api/media/v4l/vidioc-qbuf.html).
Reinitialization is valid for an idle or completed request, not an unfinished
queued request. Multi-slice OUTPUT completion alone does not establish that
the held CAPTURE frame is complete.

## Completion gates still pending

Operation-index sweeps, production fixes, recovery/resource balance assertions,
complete sanitizer/CI evidence and guarded M1 normal/early-export comparisons
are in progress. No converter hardware qualification or support-tier promotion
is implied by a model test. Package installation, module operations and reboot
are outside this ticket's claim.
