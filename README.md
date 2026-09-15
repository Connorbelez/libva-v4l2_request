# libva-v4l2_request-avd (unofficial fork)

An unofficial fork of the VA-API driver for V4L2 stateless decoders, carrying the Apple Video
Decoder (AVD) support used by Asahi Linux. It lets VA-API applications (mpv, Chromium, FFmpeg)
decode video on the AVD hardware in Apple Silicon Macs.

**This fork is not affiliated with or endorsed by the authors of the projects it is based on.**
Please don't report problems with it to those projects; open an issue here instead.

To set up hardware video decoding on an Omarchy Mac with this driver and the matching kernel
driver patches, use [omarchy-m1-video](https://github.com/iconidentify/omarchy-m1-video).

The original documentation is in [README](README).

## Upstream and credits

- **libva-v4l2_request** by Ondřej Jirman (megi): the driver this all builds on.
- **[sofus13/libva-v4l2_request](https://github.com/sofus13/libva-v4l2_request)**, tag `1.3`
  (`cfe6c2a`): AVD support. This fork keeps that full history and its tags.
- **Chromium green-frame fix**, from branch `fix-avd-early-export` of
  [Ante042/libva-v4l2_request](https://github.com/Ante042/libva-v4l2_request), kept as the original
  commits:
  - `201bc71` by Igor Ryzhkov: decode into surfaces the client exported before the first decode
  - `9e6d750` and `386956d` by Ante042: keep exported dimensions, reserve codec tail storage, and a test

## Changes carried by this fork

On top of sofus13's tag `1.3`:

- `201bc71`, `9e6d750`, `386956d` (Igor Ryzhkov, Ante042): Chromium creates each VA surface, exports it
  as a dma-buf and imports it into the GPU before decoding into it. Version 1.3 decoded into different
  buffers, so Chromium showed solid green video. With these commits the decoder writes into the
  exported buffers.
- H.264: drop trailing zero bytes from slice data. They made the AVD firmware hang on pictures with
  several CAVLC slices.
- HEVC: fix the slice header parser that finds entry point offsets (VA-API does not carry them); tiled
  and wavefront streams hung on their first P picture.
- P010: size the exported backing of 10-bit surfaces for the decoder's reference data; 10-bit HEVC
  decoded to blank frames.
- Log why an exported surface backing does not fit the CAPTURE format.
- HEVC: reject out-of-range reference counts, exp-Golomb codes and CTB sizes in untrusted slice
  headers; a crafted slice could overflow the stack.

Version `1.3.r6` also:

- Reports failed CAPTURE buffers as `VA_STATUS_ERROR_DECODING_ERROR` and propagates flush,
  request, conversion and buffer-reuse wait failures.
- Fixes FFmpeg `hwdownload` crashing when context destruction unmaps a frame during `vaGetImage`.
  Surface operations and context teardown are serialized; live surfaces keep their frame storage,
  and derived images own their mappings. Pending frames finish before teardown, with errors
  retained on the surviving surfaces.
- Bounds image copies, preserves the last UV pair on odd-width NV12/P010 images, rejects invalid
  image dimensions, and avoids a dangling buffer after a zero-element resize.
- Checks HEVC entry-point capacity and offset lengths, resets offsets between request batches,
  and rejects malformed headers before submission.
- Includes offline regression tests, sanitizer CI, and hardware pixel-comparison scripts.

Tested on an M1 (T8103) with the kernel patches from omarchy-m1-video: `JCT-VC-HEVC_V1` 143/147 and
`JVT-AVC_V1` 73/135 bit-exact through FFmpeg VA-API. Earlier Chrome 152 H.264 playback tests
matched software rendering; this revision adds direct early-export pixel regressions.

## Known problems

As of 2026-09-15:

- **Vulkan output in mpv** (`gpu-api=vulkan`) shows a green/pink ghost picture: Mesa's
  Vulkan driver for Apple GPUs ignores the plane offsets of imported frames. Use `gpu-api=opengl`.
- **H.264 profiles:** only Constrained Baseline, Main and High are offered, so 4:2:2 and 10-bit H.264
  decode in software. Interlaced H.264 is offered but the AVD kernel driver does not support it, so it
  fails.
- **Early export needs DMABUF import.** Once a context decodes into client-exported surfaces, a later
  surface whose layout does not match fails instead of falling back to separate buffers.
- **Kernel driver bugs** in AVD itself can hang or crash the system; the kernel patches in
  [omarchy-m1-video](https://github.com/iconidentify/omarchy-m1-video) fix several of them.

The remaining HEVC mismatches, boot-reset investigation, Chrome colour issue and Firefox
validation are tracked in [omarchy-m1-video's gap status](https://github.com/iconidentify/omarchy-m1-video/blob/fix/playback-gaps/docs/GAP_STATUS.md).
A successful decode call cannot detect a firmware-produced wrong picture without an error flag;
the conformance failures remain open.

## Building

```sh
meson setup build
meson compile -C build
```

To try the driver without installing it:

```sh
LIBVA_DRIVERS_PATH=$PWD/build/src LIBVA_DRIVER_NAME=v4l2_request mpv --hwdec=vaapi-copy video.mp4
```

## Tests

See [tests/README.md](tests/README.md) for the offline sanitizer suite and guarded hardware
checks. `vainfo --display drm` identifies this build as
`v4l2-request (omarchy-m1-video 1.3.r6)`.

## License

GPL-3.0-or-later, see [COPYING](COPYING).
