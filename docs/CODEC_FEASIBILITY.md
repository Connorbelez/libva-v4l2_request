# Codec expansion feasibility decision — 16 September 2026

This source investigation completes the inventory proposed in
[the research plan](plans/issue-33-codec-feasibility.md), for
[issue #33](https://github.com/iconidentify/libva-v4l2_request/issues/33).
It changes no advertised profile, support tier or hardware qualification.
Original planning: David Fano. Inventory and review: AI-assisted maintainer work.

The decision is to qualify existing translations on suitable hardware first,
then investigate the already scoped H.264 monochrome/4:2:2 path. Defer new
JPEG/VVC translations and advanced profile promotion until their missing
backend, API and output-contract evidence exists. No new implementation child
is needed: the viable work maps to existing #26 and #30–#32; advanced boundaries
remain in #28/#29. A deferred path is not an implementation promise.

## Inspected sources and interpretation

All source inspections below are pinned. Symbol names locate the relevant
code even if line numbering changes in a rendered view.

| ID | Source identity and evidence |
| --- | --- |
| VA | [libva 2.24.0, commit `add80723247b8031fb8de14d8f599923d3759242`](https://github.com/intel/libva/blob/add80723247b8031fb8de14d8f599923d3759242/va/va.h): `VAProfile`, `VAEntrypointVLD`, `VA_RT_FORMAT_*`; associated `va_dec_jpeg.h`, `va_dec_hevc.h`, `va_dec_vp9.h`, `va_dec_av1.h`, `va_dec_vvc.h`. |
| K | [Linux v7.1, commit `8cd9520d35a6c38db6567e97dd93b1f11f185dc6`](https://github.com/torvalds/linux/blob/8cd9520d35a6c38db6567e97dd93b1f11f185dc6/include/uapi/linux/v4l2-controls.h): stateless codec control families; [videodev2.h](https://github.com/torvalds/linux/blob/8cd9520d35a6c38db6567e97dd93b1f11f185dc6/include/uapi/linux/videodev2.h) coded/output FOURCCs. This is an API inventory, not a claim that every backend implements every control. |
| A | [Asahi Linux source `77cb8f24c2381a8abb7272d7bbdec548d6426a8a`](https://github.com/AsahiLinux/linux/tree/77cb8f24c2381a8abb7272d7bbdec548d6426a8a/drivers/media/platform/apple/avd): `avd-v4l2.c` (`avd_coded_fmts`, decoded formats, capability filtering), `avd-drv.c` (`avd_t8103_variant`, `avd_t8122_variant`), codec validation/format callbacks. This newer upstream source is **not** asserted to be the module loaded for r11. |
| D | [Maintained VA fork `6f835ad79faf7a1ae949bc07a7f9455072731b18`](https://github.com/iconidentify/libva-v4l2_request/tree/6f835ad79faf7a1ae949bc07a7f9455072731b18/src): `driver.c` profile/RT-format mappings, `codec_*.c` profile arrays, controls and format checks; `surface.c` and `image.c` storage/export paths. |
| R | [r11 record, companion source `4e2f52d95fa92b3a6d5a57b6fb4fc6e217420128`](https://github.com/iconidentify/omarchy-m1-video/blob/4e2f52d95fa92b3a6d5a57b6fb4fc6e217420128/docs/codec-validation-r11-2026-09-15.json): Apple M1 T8103/MacBookPro17,1, linux-asahi 7.1.13.asahi3-1, userspace `db3014f9499694c6f186af7e023de07bd5bc3564`; package/patch hashes and exact vector sets remain in that record. |
| C | [Fluster `f3ad284a9e6cac70dc01b02e0de71c2994181d34`](https://github.com/fluendo/fluster/tree/f3ad284a9e6cac70dc01b02e0de71c2994181d34/test_suites): prospective test inputs. Existing acquisition/identity policy is [CORPUS.md](CORPUS.md), from #61. A suite definition is not permission to redistribute its inputs. |

The AVD source lists H.264, HEVC, VP9 and AV1 coded formats. Its T8103
capability mask contains the first three; T8122 adds AV1. Thus AV1 is unavailable
through this M1 backend. This is evidence about the named device/driver, not a
silicon-level proof about undocumented units. JPEG, VP8, MPEG-2 and VVC have no
coded-format entry in this AVD revision; classify that as an absent **backend
path**, not proof of absent hardware throughout all Apple SoCs.

`VAProfileH264High422` exists in the inspected libva. An older-header observation
that VA has no such enum would now be incorrect. The fork still has no matching
profile entry. Conversely, a 4:2:2 `format_infos` entry in D does not make a
profile negotiable: `v4l2r_profile_rt_format()` currently advertises 4:2:0 only.

## Codec families

All VA entries below refer to decode (`VAEntrypointVLD`). “Untested” means no
qualifying hardware result for that row; it does not mean expected failure.

| Candidate | VA and V4L2 representation | Translation/backend evidence | Decision, owner and evidence route |
| --- | --- | --- | --- |
| JPEG baseline / MJPEG, 8-bit 4:2:0 | VA `JPEGBaseline`, `VAPictureParameterBufferJPEGBaseline`, slice and quantization/Huffman buffers; K JPEG/MJPEG FOURCCs exist. No `V4L2_CID_STATELESS_JPEG_*` control family in K. | D has no JPEG codec translation; A has no JPEG coded format. A complete JPEG buffer decoder is not automatically compatible with this driver's slice/request architecture. Physical JPEG block capability and a usable Linux backend are unestablished here. | Defer. Kernel/backend discovery first, then a bounded userspace design. Client software JPEG decoding is the available alternative. Proposed corpus: generated baseline JPEG with pinned encoder and independent libjpeg decode; no fixture acquired or licensed here. |
| JPEG baseline, 8-bit 4:2:2 / 4:4:4 / monochrome | Same VA picture API, distinct sampling components and YUV422/YUV444/YUV400 RT formats. Same K interface gap. | Same missing translation/backend, plus unproved output/import formats for each sampling. | Defer each sampling separately; do not infer it from a future 4:2:0 pass. Same owning layers and software alternative. |
| AV1 profile 0, 8-bit 4:2:0 | VA `AV1Profile0`, `VADecPictureParameterBufferAV1`/tile parameters; K sequence/frame/tile-group/film-grain controls and `AV1_FRAME`. | D has translation. A has AV1 ops gated by device capability; T8103 lacks that flag. No r11 AV1 evidence. | Qualification candidate on a device exposing AV1, not this M1. Reuse [#30](https://github.com/iconidentify/libva-v4l2_request/issues/30). C AV1/CHROMIUM-8bit suites are candidates; verify terms and references before acquisition. |
| AV1 profile 0, 10-bit 4:2:0 | Same profile and controls, YUV420_10/P010 contract distinct from 8-bit. | D advertises 8/10-bit RT formats for profile 0; that is not proof of correct tiles, references, grain or export. | Same #30, separate 10-bit row and reference output; C CHROMIUM-10bit candidate. Hardware/corpus availability remains a prerequisite. |
| VP8 version 0–3, 8-bit 4:2:0 | VA `VP8Version0_3`; K `V4L2_CID_STATELESS_VP8_FRAME`, `VP8_FRAME`. | D `codec_vp8.c` exists. A has no VP8 path; no current named capable device qualified. | Reuse [#31](https://github.com/iconidentify/libva-v4l2_request/issues/31), generic V4L2 backend qualification. C `vp8/VP8-TEST-VECTORS.json`; terms, pinned assets and independent output needed. Software fallback on M1. |
| MPEG-2 Simple, 8-bit 4:2:0 | VA `MPEG2Simple`; K sequence/picture/quantisation controls and `MPEG2_SLICE`. | D implements it with chroma_format=1. A has no MPEG-2 path. | Reuse [#32](https://github.com/iconidentify/libva-v4l2_request/issues/32), named capable generic backend; C `mpeg2v/MPEG2_VIDEO-MAIN.json` selection must identify actual Simple inputs. Software alternative until qualification. |
| MPEG-2 Main, 8-bit 4:2:0 | VA `MPEG2Main`, same K controls; fields and B-picture ordering require separate evidence. | Existing translation, untested here. Compiled code cannot qualify field decoding. | #32 with complete displayed frame/field counts and exact references; same corpus candidate and hardware gate. MPEG-2 4:2:2 is outside the present translation. |
| VVC Main10 / Multilayer Main10, 10-bit 4:2:0 | VA profiles and `VAPictureParameterBufferVVC`/slice-related structures exist. No VVC stateless controls or VVC coded FOURCC in K. | No D translation or A path. Multilayer cannot be inferred from single-layer decoding. | Defer both. Kernel UAPI/backend ownership before userspace translation; client software VVC decoding where available. C draft6 suite is historical, not a final-standard qualification corpus; a suitable licensed final corpus is missing. |
| Other new families (EVC, AVS3, JPEG XS) | No corresponding VA profile or stateless control family in the inspected headers. | No D translation/A path inspected for them; physical capability unknown. | Defer pending concrete demand, complete API/backend design and a legal reference corpus. Client-selected external decoders remain outside this VA driver. |

## Advanced profile and output boundaries

These are separate from existing 4:2:0 experimental r11 rows. An API field
carrying a chroma/depth value does not prove firmware accepts it.

| Candidate | Evidence and missing layer | Decision / existing child |
| --- | --- | --- |
| H.264 High 4:2:2, 8-bit | VA `H264High422` and YUV422 exist; K H.264 SPS has chroma_format_idc. D lacks the profile/RT negotiation despite NV16 mappings. A has 4:2:2 format handling; prior direct-V4L2 evidence is not VA evidence. | Best next format investigation on available M1: [#26](https://github.com/iconidentify/libva-v4l2_request/issues/26), plus surface contract [#40](https://github.com/iconidentify/libva-v4l2_request/issues/40). Preserve all 21 previously software-decoded FRExt cases as software until genuine VA frames match. |
| H.264 High 4:2:2, 10-bit | Same profile family, YUV422_10; native CAPTURE layout, image conversion, export and client import each need proof. P010 is 4:2:0, not a valid substitute for P210/NV20 semantics. | #26, conditional on full-plane layout/reference proof. Defer promotion, particularly early export, until every plane matches without chroma resampling. |
| H.264 monochrome, 8-bit | SPS chroma_format_idc=0 and VA YUV400 exist. D maps its H.264 profiles to 4:2:0; a deliberately neutral-chroma output contract is not specified/qualified by that mapping. | #26, distinct grayscale reference and neutral-chroma tests. Current hardware capability remains unqualified through VA. |
| H.264 monochrome, 10-bit | SPS can represent depth; VA has no separate YUV400_10 RT macro in this revision. A normalized output contract must be designed, not assumed. | #26 investigates API/output contract first. No supported row proposed. |
| H.264 4:4:4 or 12-bit | No High444 profile entry in VA/D inspected here; A `avd_h264_validate_sps()` rejects chroma_format_idc>2 and unequal component depths. No demonstrated 12-bit output path. | Defer; missing VA/driver/backend contracts and hardware evidence. Software decoding is the alternative. No kernel workaround in generic profile advertising. |
| HEVC Main12, 12-bit 4:2:0 | VA profile/RT format exist; K SPS has component bit depths. D only advertises Main/Main10 and has no 12-bit image/export contract. A's format table supplies 8/10-bit paths, not a qualified 12-bit one. | [#28](https://github.com/iconidentify/libva-v4l2_request/issues/28) boundary research; defer implementation pending backend/output evidence. |
| HEVC Main422_10 (8/10-bit 4:2:2), Main422_12 (12-bit 4:2:2) | VA enums exist and K SPS describes chroma/depth; range-extension control completeness needs an audit. A contains 8/10-bit 4:2:2 format handling. D profile, RExt translation and complete surface contract are missing; 12-bit is additionally unproved. | #28, separately qualify each depth; no promotion from common controls or format table membership. C `JCT-VC-RExt.json` is prospective only. |
| HEVC Main444 / Main444_10 / Main444_12 | VA profiles/RT formats exist; generic chroma fields do not implement all RExt tools. No D profile or 4:4:4 output path, no qualified A 4:4:4 firmware evidence. | Defer each 8/10/12-bit row under #28 pending backend and translation design. |
| HEVC monochrome and unequal luma/chroma depths | SPS fields can represent them; that is insufficient. Unequal-depth `TSUNEQBD` is deliberately rejected in the shipped patchset after a historical firmware reset. | #28 records safe exclusion. Do not replay the known fault or remove its rejection without an approved kernel fix. Software alternative. |
| VP9 profile 1, 8-bit 4:2:2 / 4:4:4 | VA Profile1 and K subsampling flags exist. D advertises only profiles 0/2; A callback recognizes subsampled 4:2:2 but not a qualified 4:4:4 path. | [#29](https://github.com/iconidentify/libva-v4l2_request/issues/29) boundary research, each chroma separate. Defer support until profile/state/output contracts are implemented and tested. |
| VP9 profile 2, 12-bit 4:2:0 | VA Profile2 spans more than this driver's implemented subset. D explicitly requires depth=10 for profile2; A's callback selects 8 or 10-bit image formats, not 12. | #29; keep 12-bit rejected. Existing 10-bit evidence cannot qualify 12-bit. |
| VP9 profile 3, 10/12-bit 4:2:2 / 4:4:4 | VA Profile3, K depth/subsampling exist; D has no profile3 translation/negotiation. Neither depth/chroma combination is qualified on M1. | Defer each combination under #29. C high-bit-depth suite's five untested/unsupported entries must stay distinct from its one r11 10-bit 4:2:0 pass. |
| AV1 profile 0 monochrome; profile 1 8/10-bit 4:4:4; profile 2 8/10-bit 4:2:2 and 12-bit 4:2:0/4:2:2/4:4:4 | VA Profile1/Profile2 and K sequence depth/subsampling fields exist. D only negotiates profile0 4:2:0. A has no qualified 12-bit/4:4:4 output contract; its monochrome format callback returns unknown. | Defer advanced rows. #30 must qualify ordinary profile0 first and explicitly bound grain/monochrome. Separate implementation children are required before any later advanced expansion is proposed. |

## Priority and promotion gates

1. **Existing translations on suitable devices:** #30 AV1, #31 VP8, #32 MPEG-2
   offer the shortest translation path, but no suitable hardware has been
   reserved in this review. AVD AV1 on newer SoCs is a qualification candidate,
   not permission to infer M1 support. Choose actual work by available hardware.
2. **Available-machine format work:** #26 H.264 monochrome/4:2:2 has concrete
   user benefit and prior direct-V4L2 evidence. Its first gate is truthful VA
   negotiation and full native-plane image/export behavior, including #40.
3. **Boundary research:** #28/#29 separate 12-bit, RExt, high-chroma and state
   gaps. They must name kernel versus userspace ownership before proposing code.
4. **Deferred families:** JPEG/VVC/other new families need an API/backend path
   and legal corpus before a translation ticket is justified. Client software
   decoding is a separate fallback, never a successful hardware result.

Every reused child already carries the required resource and validation scope.
For #26 use FRExt plus generated depth/chroma clips; for #30 include tiles,
show-existing/reference reuse and explicit grain semantics; for #31 include
partition/reference refresh and recovery; for #32 include I/P/B ordering,
fields and sequence changes. Pin asset bytes and independent reference tools
through #16 before hardware tests, verify terms instead of inferring a license,
and test normal/early export under the exclusive hardware guard. Complete
checksums, exact existing pass sets, malformed-input recovery and truthful
capability rejection are mandatory promotion evidence.

No new samples were downloaded or redistributed for this inventory. Prospective
Fluster suites are source pointers, not licensed, acquired qualification
artifacts. Existing r11 totals and unsupported/software outcomes remain intact.

## Scope and validation

This decision concerns decoding only. Encoding, demux, content decryption/DRM
licensing, proprietary metadata, tone mapping, display import qualification and
boot reliability are separate tasks. A decode enum promises none of them.

Inspected on aarch64 Linux with Python 3.14 and Git 2.55.0. Sources above were
read at their exact revisions; profile/control searches and relative document
links were checked. No new-codec hardware test, firmware experiment, install,
module operation or patch edit was performed for this research. Unknown physical
capabilities and untested paths remain unknown. The support-matrix validator
passes and support-matrix/pass-set files are unchanged. Research acceptance
cannot turn a support row green; implementation and qualification stay in the
linked children.
