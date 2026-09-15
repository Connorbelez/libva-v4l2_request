# Testing the AVD fork

## Offline tests

These compile the actual driver sources with a fake V4L2 device. They need Meson, Ninja,
GCC/Clang, libva and libdrm development headers, but no decoder, root access or kernel module.

```sh
meson setup build-test -Db_sanitize=address,undefined
meson test -C build-test --print-errorlogs
```

The 19 cases cover failed decode/export, CAPTURE/reference/GPU-reader/request timeouts,
deferred flush failures, surviving surfaces and derived images after context destruction,
errors during teardown, grown OUTPUT indices, bitstream size overflow, odd-width NV12/P010
copies, truncated image backing, invalid dimensions and zero-element buffer resizing.
The HEVC case checks exact-capacity entry points, malformed headers and 24,000 deterministic
random inputs. CI runs these on every push and pull request; hardware tests are separate.

## Hardware pixel comparisons

Use a normal, unsanitized build, close all video clients, and check that the decoder is idle
and the kernel has no existing decoder faults first. The scripts load the selected userspace
library through `LIBVA_DRIVERS_PATH`; they do not install it or reload the kernel module.
Run them through a hardware watchdog such as `avd-lab`'s `avdlab.guard.run`, with a finite
deadline and `wedge_monitor`. Stop after a wedge; do not repeatedly open a stuck decoder.
A userspace timeout cannot recover a wedged kernel.

```sh
meson setup build
meson compile -C build
sh tests/hwdownload.sh "$PWD/build/src" /path/to/main10.bit
sh tests/early-export.sh "$PWD/build/src"
```

`hwdownload.sh` creates short H.264 640x360/1920x1080 and HEVC 640x360 clips and compares the
first 30 decoded frames with software, both normally and with `vaDeriveImage` disabled to
force every frame through `vaGetImage`. NV12 and P010 must match byte for byte. The optional
second argument supplies a 10-bit HEVC clip with at least 30 frames when x265 only supports
8-bit encoding; otherwise the script generates one. The test rejects pixel-format fallback.
On the test M1, use `WPP_C_ericsson_MAIN10_2.bit` from the JCT-VC HEVC conformance suite.

`early-export.sh` needs FFmpeg development headers and its `hw_decode.c` example (optional
second argument gives its path). It compares ordinary decode with export-before-first-decode
for four H.264/HEVC clips. The preload hook checks the exported dma-buf pixels after each
frame. `TEST_VP9=1` adds VP9 for other supported devices; AVD does not support VP9.

## Full conformance

From the separate `avd-lab` checkout, with Fluster and its downloaded suites:

```sh
python -m avdlab.conformance -d FFmpeg-H.265-VAAPI -ts JCT-VC-HEVC_V1 \
  -j 1 --deadline 180 --libva-build /path/to/build/src
python -m avdlab.conformance -d FFmpeg-H.265-VAAPI -ts JCT-VC-HEVC_V1 \
  -j 4 --deadline 180 --libva-build /path/to/build/src
python -m avdlab.conformance -d FFmpeg-H.264-VAAPI -ts JVT-AVC_V1 \
  -j 4 --deadline 180 --libva-build /path/to/build/src
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
