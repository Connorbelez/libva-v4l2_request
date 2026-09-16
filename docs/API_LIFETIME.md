# Public VA object lifetime audit (#23)

This audit covers config, context, surface, buffer and image entrypoints. The
reference is [libva 2.24.0's public contract](https://github.com/intel/libva/blob/2.24.0/va/va.h):
context render targets are hints; clients own the Begin/Render/End ordering and
must destroy ordinary buffers; image buffers are managed by Create/Derive/DestroyImage.
Context destruction and surface destruction are separate operations. All handles
are opaque; clients must stop using a destroyed handle even if its number is later
reissued. This is not a claim of an exploit through a media file.

## Initial baseline and reproductions

Baseline: `8848956e14b61addf760f8cd2352dd0596409b8c`, native aarch64 GCC 16.1.1,
libva pkg-config 1.24.0, libdrm 2.4.134, Meson 1.12.0. All 53 existing
ASan/UBSan cases and both software frame scripts pass inside a namespace without
decoder devices or network access.

`tests/api-lifecycle.c` adds public-entrypoint sequences with an intercepted codec
and memfd-backed image pixels. At baseline 18 of its 19 cases fail; the independent
context/surface lifetime control passes. Failures include null output dereferences,
invalid create arguments being accepted, partial destruction on a duplicate list,
foreign/orphaned parameter buffers being submitted, direct deletion of an image's
owned buffer, and a missing EndPicture being reported as successful surface output.

The initial reproductions are commit `477b483`; CI also fails that commit.
The first fixes are `1b21910` (arguments/destruction validation) and `1c844f5`
(buffer ownership/abandoned pictures). Three further cases reproduce premature
surface access, derived-image source destruction and destruction of a pending
VPP input; `6feca1b` fixes them. There are now **22 lifecycle cases** (21 failing
sequence cases reproduced before their fixes plus one isolation control), and
**75 total offline cases**. Additional assertions cover recovery, orphaned-buffer
mapping, abandoned-picture flush suppression and VPP error cleanup.

## Object and ordering contract

The status names below abbreviate `VA_STATUS_ERROR_*`. They describe this
driver's deterministic validation behavior; callers must still provide valid
memory for non-null arrays and synchronize simultaneous use of the same objects.
No new display-wide lock was added. The existing public surface/context lock and
short object-table critical sections protect the ownership checks.

| Object / operation | Allowed lifetime and transition | Rejection and recovery |
| --- | --- | --- |
| Config | Create, query, create contexts, destroy. A context snapshots configuration and may outlive its config. Existing optional outputs in QueryConfigAttributes remain optional. | Negative attribute counts, a missing nonempty input list or required creation/query output: `INVALID_PARAMETER`. Destroyed/wrong-class config: `INVALID_CONFIG`. A following valid create/query still works. |
| Context | Create using a live config; target arrays are hints. Begin → zero or more Render calls → End; repeated pictures and independently owned contexts remain usable. | Existing nested/missing Begin/End checks remain. Destroying a context with an open picture marks that target failed, skips flushing that incomplete picture, and detaches surviving surfaces. Previously completed surfaces and other contexts retain their state. |
| Ordinary buffer | Create against a live context, map/unmap, optionally resize, render through that context, explicitly destroy. Context teardown orphans the buffer; it remains mappable/destroyable for caller cleanup. | Missing required output, zero element size/count or overflow: `INVALID_PARAMETER`; nonexistent/wrong-class context: `INVALID_CONTEXT`. Rendering another context's or an orphaned buffer: `INVALID_BUFFER`, retained through EndPicture. Reissuing the context's numeric ID cannot adopt an orphan. |
| Image buffer | CreateImage and DeriveImage own their buffer until DestroyImage. Clients access pixels through MapBuffer/UnmapBuffer. | Direct DestroyBuffer or BufferSetNumElements on image-owned storage: `INVALID_BUFFER`. This prevents a live image from retaining a buffer ID recycled for unrelated data. DestroyImage frees its own buffer once; repeated destruction returns `INVALID_IMAGE`. |
| Surface creation | Positive dimensions within the existing advertised 1–65536 bounds; optional attributes require a list when their count is nonzero. A zero-length creation with otherwise valid arguments is a no-op. | Negative legacy arguments, missing nonempty output/list, out-of-range dimensions or a non-integer pixel-format attribute: `INVALID_PARAMETER`. Actual device/format qualification is separate from these allocation bounds. |
| Surface destruction | Destroy only after current picture, derived-image and pending VPP-source uses end. Validate the entire list before releasing any entry. | Missing or duplicate ID: `INVALID_SURFACE`; active target, derived-image source or retained VPP source: `SURFACE_BUSY`. Every entry remains live on these validation failures. Empty destruction succeeds; negative/null nonempty lists fail. |
| Surface access | A target reserved by BeginPicture reports `VASurfaceRendering` until EndPicture. Finished output can be synchronized, exported, derived or copied. | Sync/readback/upload/export/derive during picture assembly: `SURFACE_BUSY`, before touching backing storage. The same operations work after a successful EndPicture. Required null output pointers fail before synchronization/allocation. |
| Derived image | Owns an independent mapping and retains its source surface. Multiple images may derive from one surface; context destruction does not destroy them. | The source cannot be destroyed until all derived images are gone. GetImage/PutImage using an image derived from that same surface returns `SURFACE_BUSY`, as required by libva. Transfers involving a different surface remain valid. |
| VPP source | RenderPicture retains its input surface until the VPP picture ends or its context is destroyed, even when the input belongs to another context. | Destroying the retained source rejects the whole list with `SURFACE_BUSY`. Context destruction or a failed EndPicture releases the reservation. Actual converter operation is not qualified by this no-device test. |

Numeric VA IDs may be reused, as before. A caller cannot infer object identity
from a recycled number. The audit prevents the driver's own context/image
relationships from following such a recycled ID. It does not add generation bits,
change the handle ABI, or promise detection of an application using an old number
after that number has legitimately been reissued.

## Coverage map

All names below are Meson cases. Public methods reached indirectly through a
shared helper are listed as well as the directly edited entrypoints.

| Entry points | Boundary, lifetime and recovery evidence |
| --- | --- |
| QueryConfigProfiles, QueryConfigEntrypoints | `api-config-query-null` |
| CreateConfig, GetConfigAttributes; existing Destroy/QueryConfigAttributes | `api-config-parameters`, `api-config-null-output`, every fixture's config snapshot/destruction control |
| CreateSurfaces / CreateSurfaces2 | `api-surface-parameters`, `api-surface-null-output` |
| DestroySurfaces | `api-surface-duplicates`, `api-image-surface-lifetime`, `api-vpp-source-lifetime`, existing `picture-target-lifetime` |
| QuerySurfaceAttributes / QuerySurfaceStatus | `api-surface-attributes-null`, `api-surface-query-null`, `api-surface-active-access` |
| CreateBuffer / MapBuffer / UnmapBuffer | `api-buffer-context`, `api-buffer-null-output`, `api-buffer-null-map`, `api-buffer-recycled-context` |
| BufferSetNumElements / DestroyBuffer | `api-image-buffer-lifetime`, `api-buffer-context`, existing `buffer-zero` |
| RenderPicture / EndPicture | `api-buffer-owner`, `api-buffer-recycled-context`, existing `picture-render-errors` / `picture-begin-errors` |
| DestroyContext / surviving surfaces | `api-context-abort-reuse`, `api-context-isolation`, `api-vpp-source-lifetime`, existing `context-lifetime`, `context-error`, `context-timeout` |
| QueryImageFormats / CreateImage / DestroyImage | `api-image-query-null`, `api-image-buffer-lifetime`, `api-image-surface-lifetime`, existing `image-dimensions` |
| DeriveImage / GetImage / PutImage / ExportSurfaceHandle / SyncSurface | `api-image-derived-null`, `api-image-surface-lifetime`, `api-surface-active-access`, `api-surface-export-null`, existing image bounds/copy, decode/error, timeout and export tests |

Run in a disposable environment with no decoder devices:

```sh
meson setup build -Db_sanitize=address,undefined
meson compile -C build
meson test -C build --print-errorlogs
sh tests/frame-check.sh
sh tests/shared-contexts.sh
```

Original reproduced failures remain in the first draft commit and its failing
[CI run](https://github.com/iconidentify/libva-v4l2_request/actions/runs/35123586654).
The implementation passes the complete hosted matrix at
[6feca1b](https://github.com/iconidentify/libva-v4l2_request/actions/runs/35124576087):
GCC/Clang on x86_64/aarch64, historical API tiers, disabled/mixed codec options,
configure rejection, static analysis, corpus and required aggregate.

## Validation limits and follow-up

Allocation/ioctl failure-point exhaustiveness is #35; threaded stress is #36;
cross-backend NV12/P010/converter qualification is #40. These remain separate
acceptance gates. The VPP retention test does not open a converter and makes no
claim for untested converter hardware. Application pointers must reference valid
memory; arbitrary dangling pointers supplied by the caller are outside libva's
handle-validation contract.

The [M1 regression record](api-lifetime-validation-2026-09-16.json) identifies
the tested source tree and driver binary, commands, guards and exact pass-set
digests. All 864 generated hardware frames match, including shared contexts and
early export. Full-suite passing sets are unchanged: HEVC 144/147, AVC 73/135,
FR-EXT 27/69 and VP9 216/305; the separately scoped VP9 High10 and profile-override
sets remain 1/1 and 5/5. All four strict comparison reports are identical to the
preceding merged driver's reports. AVC remains non-green because `FM1_FT_E`
refuses a software-fallback frame; it is not counted as hardware success. Known
HEVC failures and their checksum/category details are retained in the record.
Both exclusive hardware guards finish idle with no new fault or timeout.

This task makes no boot/display qualification or support-tier promotion, and changes
no installed driver, kernel patch, module, or reboot setting. Review is a maintainer
self-review unless an independent reviewer is explicitly recorded in the PR.
