# Advanced H.264 syntax boundary — 16 September 2026

This source investigation completes
[issue #27](https://github.com/iconidentify/libva-v4l2_request/issues/27) and answers
one question per feature: **FMO, arbitrary slice order, data partitions, SP/SI and
redundant slices** — can this API stack express it, and if not, how does the driver
fail? It changes no advertised profile, support tier or hardware qualification.

Measurement is in [h264-advanced-syntax-matrix.json](h264-advanced-syntax-matrix.json),
produced by [`../tests/h264-syntax-probe.py`](../tests/h264-syntax-probe.py). The
decision below is derived from that measurement plus the pinned sources, not from
vector names.

## Measured inventory of the 62 r11-failing AVC vectors

| Feature | Vectors | Detail |
| --- | --- | --- |
| interlace-driven (field coding 25 + MBAFF 27) | 52 | handed to [#14](https://github.com/iconidentify/libva-v4l2_request/issues/14)/[#10](https://github.com/iconidentify/libva-v4l2_request/issues/10); not this ticket's scope |
| FMO (slice groups) | 3 | `FM1_BT_B`, `FM1_FT_E`, `FM2_SVA_C`, all Baseline; `FM1_BT_B` carries all seven map types 0–6 |
| slice order not monotonic within a picture | 6 | the three FMO vectors plus `CAMASL3_Sony_B` and two field vectors |
| SP/SI slices | 2 | `SP1_BT_A`, `sp2_bt_b`, Extended |
| plain slice types only | 5 | exactly the five profile-override vectors of [#37](https://github.com/iconidentify/libva-v4l2_request/issues/37) |
| data partitions (NAL 2/3/4) | **0** | no r11-failing vector exercises them at all |

Two measurement results drive the decisions below:

1. **Every FMO vector declares its slice-group map only in the picture parameter set.**
   The sequence parameter set reports `num_slice_groups_minus1 = 0` while the eight PPS
   NALs of `FM1_BT_B` carry 2/3/7/8 groups across map types 0–6. A detector that reads
   one parameter set finds nothing.
2. **No vector exercises data partitions.** That part of the boundary therefore has no
   conformance evidence available and must be decided from the API and covered by an
   authored test — it cannot be closed by pointing at a vector.

## Inspected sources

| ID | Identity and what was read |
| --- | --- |
| VA | libva 2.20.0-2ubuntu0.2 (`pkg-config --modversion libva` = 1.20.0), `va.h`: `VAPictureParameterBufferH264` and `VASliceParameterBufferH264` |
| K | `linux-libc-dev` 6.8.0-139.139, `linux/v4l2-controls.h`: `v4l2_ctrl_h264_sps`, `v4l2_ctrl_h264_pps`, `v4l2_ctrl_h264_slice_params` |
| A | Asahi Linux source `77cb8f24c2381a8abb7272d7bbdec548d6426a8a`, `drivers/media/platform/apple/avd/h264.c` and `avd-v4l2.c` |
| D | This fork, `src/codec_h264.c` at the branch base `59cc67a` |
| F | FFmpeg n6.1 client behaviour: `libavcodec/h264_ps.c`, `libavcodec/vaapi_h264.c` |
| R | r11 pass sets, `docs/r11-pass-sets.json` (62 failing AVC vectors) |
| C | Fluster `f3ad284a9e6cac70dc01b02e0de71c2994181d34` corpus, acquired and hashed through the #61 policy |

## Feature decisions

| Feature | Bitstream signal | VA | K | A | D today | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| **FMO / slice groups** | SPS `num_slice_groups_minus1`, and (in real vectors) the PPS copy plus `slice_group_map_type` 0–6 | `// FMO is not supported.` — `num_slice_groups_minus1`, `slice_group_map_type` and `slice_group_change_rate_minus1` are all `va_deprecated`; `VASliceParameterBufferH264` has no slice-group field at all | `v4l2_ctrl_h264_pps.num_slice_groups_minus1` exists but there is **no map, no map type and no change rate**, so the backend cannot be told how to build groups; `v4l2_ctrl_h264_sps` carries none | no slice-group or FMO handling anywhere in the H.264 validation path | rejects when the client reports groups (`h264_has_slice_groups`), with no diagnostic, at picture-buffer render only | **Unsupported, documented.** Unrepresentable in both VA and the kernel interface, and the backend has no path. Rejection stays, now with a diagnostic naming FMO, and stays client-dependent (see below) |
| **Arbitrary slice order** | `first_mb_in_slice` not increasing within a picture | no ordering field: slices arrive as an ordered submission list | `v4l2_ctrl_h264_slice_params.first_mb_in_slice` per slice; ordering is the driver's business | no ordering validation | no ordering check; slices are submitted in the order the client sends them | **Not a rejection boundary.** ASO without slice groups is legal H.264. Six vectors show non-monotonic order, of which three are FMO (already rejected) and `CAMASL3_Sony_B` is the one non-FMO candidate. Adjudicating it needs a decode-comparison run on hardware, so it is recorded as untested rather than closed |
| **Data partitions A/B/C** | NAL unit types 2/3/4 instead of a monolithic slice | no partition representation; a VA slice data buffer holds one slice | `V4L2_CID_STATELESS_H264_SLICE_PARAMS` describes a whole slice | no partition handling | parser ignored any NAL that is not type 1/5, so a partitioned picture failed later as an invalid slice header — indistinguishable from corrupt data | **Unsupported, enforced.** Now recognised explicitly and rejected with `VA_STATUS_ERROR_UNIMPLEMENTED` and a diagnostic naming the NAL type, so a client falls back instead of treating its own buffer as broken |
| **Extension NALs 20/21** | MVC/SVC extension units | not representable in the base-profile interface | none | none | ignored as above | **Unsupported, enforced** with the same explicit status |
| **SP/SI slices** | `slice_type` 3/4 (8/9) | `VASliceParameterBufferH264.slice_type` carries them; the driver's parser already reads SP/SI syntax | `v4l2_ctrl_h264_slice_params` carries the type | AVD path unqualified | parsed, but the two vectors fail for reasons already owned elsewhere | **No new boundary.** `SP1_BT_A` and `sp2_bt_b` stay with the Extended-profile work in #37; no support is advertised |
| **Redundant slices** | PPS `redundant_pic_cnt_present_flag`, slice `redundant_pic_cnt` | `VASliceParameterBufferH264` has no redundant-count field | `v4l2_ctrl_h264_slice_params` has `redundant_pic_cnt` | none | parser reads the count, submission ignores it | **Untested, unchanged.** No failing vector depends on it; not claimed either way |
| **Interlaced / MBAFF** | SPS `frame_mbs_only_flag = 0`, `mb_adaptive_frame_field_flag` | VA carries both flags | carried in `v4l2_ctrl_h264_sps.flags` | kernel rejects the format | zero-copy path unsupported | **Out of scope here** — 52 of the 62 vectors, owned by #14/#10 |

## Client visibility, which bounds what the driver can catch

FMO rejection is inherently client-dependent, and that is worth stating plainly:

- **FFmpeg** rejects FMO while parsing the PPS (`avpriv_report_missing_feature(avctx, "FMO")`,
  `AVERROR_PATCHWELCOME`) and never fills the deprecated VA slice-group fields, so an
  FMO stream never reaches this driver through FFmpeg. Its VA-API H.264 decoder has no
  reference to `slice_group` at all.
- Other clients can fill `num_slice_groups_minus1`, and that is when the picture-buffer
  check fires.
- Because every real FMO vector declares its map in the **PPS**, a client that forwarded
  only SPS-derived values would under-report. The driver cannot see the PPS; it only sees
  VA buffers. The honest boundary is therefore: reject what the client reports, document
  the dependency, and rely on the slice-header parse failing for the rest.

## What changed in the driver

`src/codec_h264.c`:

- the slice-header parser now separates **recognised unsupported syntax** (NAL types
  2/3/4 partitions, 20/21 extensions) from **invalid data**;
- `h264_process_slice` returns `VA_STATUS_ERROR_UNIMPLEMENTED` with a diagnostic naming
  the form for the first case, and keeps `VA_STATUS_ERROR_INVALID_BUFFER` for the second;
- the failure stays sticky for the picture (`codec->failed`), so a rejected picture cannot
  be completed by a later valid slice, while the next picture stays usable;
- the FMO picture-buffer rejection keeps `VA_STATUS_ERROR_UNIMPLEMENTED` and now emits a
  diagnostic naming FMO.

No support is added, no profile is advertised, and the supported H.264 paths are
untouched: the full offline suite passes, including the pinned pass-set comparator.

## Regression coverage

`tests/h264-submission.c` adds three device-free cases: `unsupported-partition`
(NAL 2 rejects as unimplemented, the picture cannot complete, the next picture still
decodes), `unsupported-extension` (NAL 20), and `unsupported-fmo` (a picture buffer
declaring one slice group). They join the existing `incomplete`, `slice-count` and
`references` cases in `tests/meson.build`.

## Promotion gates and open work

1. Nothing here becomes a support row. FMO, partitions and extension NALs are recorded as
   unsupported with the API evidence above; a future implementation would need VA and
   kernel interfaces that can carry a slice-group map, which do not exist today.
2. `CAMASL3_Sony_B` (non-FMO, non-monotonic slice order) and the two field vectors with
   the same signature need a hardware decode comparison before they can be called either
   wrong or unsupported. That is hardware work, and it is not claimed here.
3. `tests/corpus/manifest.json` currently classifies these vectors under one
   `ki-avc-unimplemented-syntax` class. Measurement shows 57 of 62 are multi-label, so the
   classification should become multi-label; that is proposed as a manifest refinement
   rather than changed silently, since the manifest is the shared corpus contract.
4. The probe is a research instrument: it is not yet part of the offline Meson suite,
   because it needs the pinned Fluster corpus. A hermetic self-test over a crafted
   bitstream is the natural follow-up if it should gate CI.

## Scope and validation

Decoding only. No hardware, no decoder, no kernel module and no installer action was
performed for this research: this host has no Asahi/Linux partition. Verified offline in
an Ubuntu 24.04 aarch64 container (GCC 13.3, libva 1.20.0, FFmpeg 6.1.1) with the pinned
Fluster corpus: 62 vectors probed with 0 parse failures, and the full Meson suite with the
three new cases passing. Existing r11 totals, pass sets and unsupported/software outcomes
are unchanged.
