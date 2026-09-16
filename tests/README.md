# Testing the AVD fork

## Offline tests

These compile the actual driver sources with a fake V4L2 device. They need Meson, Ninja,
GCC/Clang, libva and libdrm development headers, but no decoder, root access or kernel module.

```sh
meson setup build-test -Db_sanitize=address,undefined
meson test -C build-test --print-errorlogs
```

`python3 tests/support-matrix.py` (also the Meson `support-matrix` test) validates
the versioned support contract in `docs/SUPPORT.md` and `docs/support-matrix.json`.
It checks advertised VA profiles, the r11 raw suite totals, pinned pass sets, and
fixtures that reject a `supported` row without provenance or a result artifact.
It does not open a decoder.

Compare a candidate run with the pinned r11 pass sets (no decoder):

```sh
python3 tests/compare-results.py --report docs/r11-pass-sets.json
python3 tests/compare-results.py \
  --baseline docs/r11-pass-sets.json \
  --candidate /path/to/summary.json \
  --suite hevc
```

`compare-results.py` reconstructs HEVC 144/147, AVC 73/135, FRExt 27/69 and VP9
216/305 from the pinned record. It fails if one old pass is lost even when the
fraction is unchanged, and it will not treat software fallback, timeouts, aborts
or malformed JSON as a green hardware result. `conformance.py` writes
`summary.json` in the result directory for this command. The Meson
`conformance-result` test is the schema/comparator self-check.

The cases cover failed decode/export, CAPTURE/reference/GPU-reader/request timeouts,
invalid poll events, deferred flush failures, surviving surfaces and derived images after context destruction,
errors during teardown, grown OUTPUT indices, bitstream size overflow, odd-width NV12/P010
copies, truncated image backing, invalid dimensions and zero-element buffer resizing.
The HEVC case checks exact-capacity entry points, malformed headers and 24,000 deterministic
random inputs, AVD reference ordering and index remapping, retained long-term references,
unavailable references at random-access points, and failed-picture submission. H.264 adds another 24,000 parser inputs, truncated/unsupported NALs, slice-group
rejection, High 10 quantizer modes and fake-device capability checks. Three H.264 submission
cases cover missing slice data at EndPicture, slice-count overflow, invalid/missing active
references, contradictory slice types, preserving a staged slice's controls, and recovery
on the next picture. Submission is intercepted in-process; no device is opened.
Five `diag-*` cases cover the diagnostics in `docs/DIAGNOSTICS.md`: every failure category
(unsupported profile, client call order, oversized bitstream, CAPTURE allocation failure,
request timeout, rejected controls, decoder-flagged frame and invalid device poll) is forced
through the fake device in both text and JSON mode with identical VA status, and the VP9 and
H.264 reference cases check the `reference` category. They also check that reused VA context
IDs keep distinct `ctx` serials, path/URL redaction and size limits, per-category rate
limiting, and report a bounded synthetic logging overhead. `diag-schema` validates emitted
JSON against `tests/fixtures/diagnostics/schema.json`, rejects malformed fixture records,
and fails if the category lists in the code, fixture and documentation differ. There are
40 sanitizer Meson cases plus the `support-matrix`, `conformance-result`,
`diag-schema`, `hwguard`, `rps-e-research` and HEVC concurrency research checks.
The count-overflow case injects the boundary into codec state rather than
allocating billions of real slices; it is an arithmetic regression, not proof of a practical
malicious-video exploit.
Four VP9 cases cover malformed/incomplete headers, failed-submission state rollback, colour-range
inheritance, missing/cross-context references and 24,000 deterministic parser inputs. These join
the H.264 and HEVC inputs for 72,000 generated inputs across three registered parser cases.
Six shared-lifecycle cases add failed Render/Begin recovery, active-target lifetime, reference
ownership, invalid context arguments, and submission errors surviving a later buffer completion.
`picture.c` calls the public picture entrypoints with an intercepted codec. The original target
lifetime failure produces an ASan use-after-free when the active target is destroyed before
EndPicture; this does not establish that a media file can trigger the same API sequence.
CI also runs `frame-check.sh` in software to test resolution changes and truncated input;
hardware tests are separate.

CI also runs `sh tests/shared-contexts.sh` in software. With a driver-directory argument,
run it through the hardware guard to interleave H.264, HEVC and 8/10-bit VP9 decoders on
one shared VA display. The clips contain 24, 36, 48 and 60 frames, so earlier contexts are
destroyed while later ones continue. Each stream must match its independently decoded
software checksum. Normal and early-export runs compare 336 hardware output frames.
This interleaves work in one thread; it does not measure concurrent API calls or throughput.

## Hardware pixel comparisons

Use a normal, unsanitized build, close all video clients, and check that the decoder is idle
and the kernel has no existing decoder faults first. The scripts load the selected userspace
library through `LIBVA_DRIVERS_PATH`; they do not install it or reload the kernel module.
Run them through `python3 tests/hwguard.py`, which holds an exclusive OS lock for
the decoder identity, does a read-only idle/fault preflight, monitors new AVD
journal errors and foreign clients, and uses a finite child deadline. Stop after
a wedge; do not repeatedly open a stuck decoder. A userspace timeout cannot
recover a wedged kernel. The guard never unloads modules. Hardware scripts refuse
to run without `LIBVA_HW_GUARD_LEASE` from this wrapper. Publishable logs redact
paths and URLs; `--verbose-log` is a local opt-in.

```sh
python3 tests/hwguard.py --self-test
meson setup build
meson compile -C build
python3 tests/hwguard.py --deadline 180 -- sh tests/hwdownload.sh "$PWD/build/src" /path/to/main10.bit
python3 tests/hwguard.py --deadline 180 -- sh tests/early-export.sh "$PWD/build/src"
python3 tests/hwguard.py --deadline 180 -- sh tests/h264-high10.sh "$PWD/build/src"
```

`hwdownload.sh` creates short H.264 640x360/1920x1080 and HEVC 640x360 clips and compares the
first 30 decoded frames with software, both normally and with `vaDeriveImage` disabled to
force every frame through `vaGetImage`. NV12 and P010 must match byte for byte. The optional
second argument supplies a 10-bit HEVC clip with at least 30 frames when x265 only supports
8-bit encoding; otherwise the script generates one. The test rejects pixel-format fallback.
On the test M1, use `WPP_C_ericsson_MAIN10_2.bit` from the JCT-VC HEVC conformance suite.

`early-export.sh` needs FFmpeg development headers and its `hw_decode.c` example (optional
second argument gives its path). It compares ordinary decode with export-before-first-decode
for five H.264/HEVC/VP9 clips. It also needs the libvpx-vp9 encoder. The preload hook checks
the exported dma-buf pixels after each frame. AVD on the test M1 advertises VP9 profiles 0
and 2; this particular smoke test covers profile 0. The separate VP9 checks below cover
10-bit output and conformance vectors.

`h264-high10.sh` generates six 10-bit H.264 clips: CABAC/CAVLC, QP 1/21/51, four slices,
B pictures and a cropped 640x360 output. It checks each of 12 frames against software,
then repeats with export-before-decode and verifies stable dma-buf identity/layout.
It requires a 10-bit-capable libx264 build and enables the explicit FFmpeg compatibility
mode for this process. The checksum helper requires hardware frames in both modes.

## Full conformance

Wrap in-tree runners with the portable guard. Fluster suites still come from a
separate checkout:

```sh
python3 tests/hwguard.py --deadline 180 -- \
  python3 tests/conformance.py /path/to/fluster/test_suites/h.265/JCT-VC-HEVC_V1.json \
  /path/to/fluster/resources --driver /path/to/build/src --output /path/to/new-results
```

Record the driver commit, kernel package, installed patch digest, loaded-module provenance,
commands, pass/fail vector names and kernel log for each run. `modinfo` identifies the module
on disk selected for the next load, not necessarily the currently loaded binary. Kernel
changes and reboot tests require the consent and recovery procedure in omarchy-m1-video.

## Limits

Offline tests do not validate firmware, DMA coherence, display import or boot stability.
The per-display API mutex prevents teardown racing surface operations; it may serialize work
from separate contexts within one application. Independent processes remain concurrent.
Rockchip conversion/VPP, AV1 and other hardware are not validated by the M1 runs.

## Codec conformance without software fallback

`conformance.py` builds `frame-check.c` using the installed FFmpeg development libraries,
then reads an existing Fluster suite and downloaded resources. It requires actual VA-API
frames for a hardware pass, hashes each frame at its native resolution, and saves frame
checksums, logs and fsynced JSON results. Failed decode calls and corrupt frames fail the
test; a timeout stops the run. It needs `cc`, `pkg-config`, Python 3, libavformat, libavcodec,
libavutil and libswscale development files. Choose a new output directory for each run.

Run hardware commands below through the guard described above. For software, omit `--driver`.

```sh
LIBVA_V4L2_H264_HIGH10=ffmpeg python3 tests/conformance.py \
  /path/to/fluster/test_suites/h.264/JVT-FR-EXT.json /path/to/fluster/resources \
  --driver "$PWD/build/src" --output /path/to/new-results
```

For the five progressive Baseline/Extended streams rejected by FFmpeg's profile selection,
an explicit override decoded bit-exact on the M1:

```sh
python3 tests/conformance.py /path/to/fluster/test_suites/h.264/JVT-AVC_V1.json \
  /path/to/fluster/resources --driver "$PWD/build/src" --output /path/to/new-results \
  --profile-mismatch --vectors BA3_SVA_C MR2_TANDBERG_E MR3_TANDBERG_B \
  MR4_TANDBERG_C MR5_TANDBERG_C
```

The equivalent FFmpeg option is `-hwaccel_flags allow_profile_mismatch`. This is a per-file
workaround for those tested coding features, not full Baseline/Extended support. Interlacing,
FMO, data partitions and other unimplemented syntax still need software or further work.

Do not count a successful FFmpeg process as hardware evidence: the Fluster FFmpeg VA-API
decoder can silently use software for H.264 4:2:2. Its FRExt totals therefore require the
strict frame check above. Also, `VPSSPSPPS_A_MainConcept_1` loses pictures in FFmpeg's parameter-set
parser; preserving native sizes alone does not fix it. Direct GStreamer V4L2 passes that vector.

To test the checksum helper independently, run `sh tests/frame-check.sh`. The optional driver
argument is a guarded hardware comparison: `python3 tests/hwguard.py -- sh tests/frame-check.sh BUILD/src`.

## VP9 validation

Run `python3 tests/hwguard.py -- sh tests/vp9-matrix.sh /path/to/build/src`. It needs a
high-bit-depth-capable libvpx-vp9 encoder and the FFmpeg development libraries. Eight generated
640x360 clips cover 8/10-bit 4:2:0, limited/full range and lossy/lossless encoding, with tile
settings, alternate-reference encoding enabled and two keyframe intervals. Each 24-frame clip
is decoded normally and with early export: 384 hardware output-frame comparisons. The script
verifies encoded pixel format and range, requires actual hardware frames, checks early-export
backing and compares every output pixel with software. This is not a browser display test.

The official WebM vectors can be run with `conformance.py` using Fluster's
`test_suites/vp9/VP9-TEST-VECTORS.json` and `VP9-TEST-VECTORS-HIGH.json`. On the M1, r9 passed
216/305 in the first suite and the single 10-bit 4:2:0 vector (10 frames) in the second.
The high-bit-depth suite also contains five 12-bit or 4:2:2/4:4:4 vectors outside the tested
hardware formats; they are not included in that 1/1 result.

The 89 baseline failures comprise 60 sub-64-dimension streams, two unsupported profile-1
streams, two resize streams that triggered firmware timeouts, 24 inter-frame-resize checksum
mismatches and one scalable-video checksum mismatch. Software passes 88 of these 89; the
scalable-video stream also misses its reference checksum in software, with a different digest.
The candidate retains all 216 baseline passes and passes the 384-frame generated matrix.
With r10's reference checks, both timeout-producing resize streams are rejected in userspace
without new kernel messages. Their decoding support remains open. The installer repository's
[codec status](https://github.com/iconidentify/omarchy-m1-video/blob/main/docs/CODEC_STATUS.md)
records release-package results and exact vector lists.
