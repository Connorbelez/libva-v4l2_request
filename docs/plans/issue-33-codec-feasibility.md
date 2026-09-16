# Codec expansion feasibility research plan

Related: [issue #33](https://github.com/iconidentify/libva-v4l2_request/issues/33),
under [roadmap #7](https://github.com/iconidentify/libva-v4l2_request/issues/7).

This plan records the original investigation scope. The completed
[source inventory and feasibility decision](../CODEC_FEASIBILITY.md) now maps
the candidates to pinned evidence and existing implementation children. Neither
document changes a support claim.

## Prerequisite and starting point

Issue #33 lists [issue #15](https://github.com/iconidentify/libva-v4l2_request/issues/15)
as its only dependency. That support-contract task was completed in
[PR #51](https://github.com/iconidentify/libva-v4l2_request/pull/51), merge commit
`12e0efd90d91eaee6a363259b6d150b8df32c432`. The contract and its validators are
present at the research base, `6f835ad79faf7a1ae949bc07a7f9455072731b18`.

The research uses [SUPPORT.md](../SUPPORT.md),
[support-matrix.json](../support-matrix.json) and
[r11-pass-sets.json](../r11-pass-sets.json) as its existing evidence boundary.
No codec becomes supported because its profile constant or source file exists.

## Inventory and evidence

Start with the requested MJPEG/JPEG and newer-codec families, then 12-bit,
monochrome, 4:2:2 and 4:4:4 variants. Include the existing AV1, VP8 and MPEG-2
backends as qualification candidates rather than assuming compiled code works
on a particular device. Name each profile, bit depth and chroma combination
separately whenever its API, hardware or evidence differs.

For each candidate, record:

| Field | Required evidence |
| --- | --- |
| VA representation | Exact profile, entrypoint, picture/slice parameters and surface formats in an inspected libva revision |
| V4L2 interface | Required controls, request semantics and output formats in an inspected kernel UAPI revision |
| Driver translation | Existing implementation, missing translation or an API mismatch, with source locations |
| Backend and firmware | Named device, kernel driver and firmware evidence; distinguish documented absence from unknown capability |
| Reference corpus | Origin, license/acquisition decision, pinned identity and independent output reference |
| Decision | Candidate for implementation, candidate for qualification, blocked pending evidence, or deferred with reason |
| Owner and next step | Owning layer, existing/new child ticket, hardware requirement and promotion gate |

Use separate evidence fields for hardware absence, API absence, missing
translation and untested support. More than one can apply. A missing document
is an unknown, not evidence that the hardware lacks the feature.

Pin primary source links to the inspected commits or release revisions, and
record the inspection date. Link the exact symbols or documentation passages
supporting each conclusion. Distinguish those conclusions from assumptions and
open questions. Host-installed headers alone cannot establish another backend's
capabilities, and a header version cannot establish firmware behavior.

## Investigation sequence

1. Map the existing profile and format discovery in `src/driver.c` and each
   `src/codec_*.c` backend to the support contract. Record client workarounds and
   backend restrictions without generalizing M1 results to other devices.
2. Inspect pinned primary libva, Linux media UAPI and backend sources for each
   additional family. Classify gaps at the layer that owns them; do not propose
   generic VA profile advertising as a substitute for missing kernel support.
3. Map legal reference inputs to each candidate. Reuse and coordinate with
   [corpus issue #16](https://github.com/iconidentify/libva-v4l2_request/issues/16)
   rather than editing its in-flight manifest or inventing redistribution rights.
   Unavailable corpus or hardware remains an explicit evidence gap.
4. Prioritize viable candidates using documented user benefit, API completeness,
   available hardware/corpus and implementation dependencies. Reuse existing
   qualification tasks [#30](https://github.com/iconidentify/libva-v4l2_request/issues/30),
   [#31](https://github.com/iconidentify/libva-v4l2_request/issues/31) and
   [#32](https://github.com/iconidentify/libva-v4l2_request/issues/32) where their
   scope matches. Create and link a bounded implementation child for each new
   proposed expansion, with owning layer, reference corpus, hardware and
   acceptance evidence. Explicitly defer candidates that lack a viable path.
5. Publish the map and decision for maintainer review. Record alternatives for
   unavailable paths, including client-selected software decoding where
   appropriate. Keep software fallback distinct from hardware success.

## Boundaries and completion

The map concerns decoding. It does not promise encoding, container parsing,
content decryption or DRM licensing, proprietary metadata processing, HDR tone
mapping, display import or boot stability. Those belong to other layers.

This source inventory needs no installation, kernel changes or decoder access.
Any later hardware qualification must use the repository's exclusive guard and
the owning ticket's resource and authorization requirements.

Before the research task can close, every candidate needs its evidence-backed
classification; every proposed expansion needs a concrete implementation child
and evidence plan; deferred paths need a reason and an alternative where one
exists. The accepted feasibility decision must be merged into the owning
default branch. Research closure must not promote any support-matrix row.

The final PR must map every acceptance criterion in issue #33 to evidence and
state the source identities, tests run and remaining limits. The accompanying
[decision](../CODEC_FEASIBILITY.md) supplies the inventory, conclusions and
child-task decisions; qualification remains in those children.
