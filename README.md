# libva-v4l2_request-avd (unofficial fork)

An unofficial fork of the VA-API driver for V4L2 stateless decoders, carrying the Apple Video
Decoder (AVD) support used by Asahi Linux. It lets VA-API applications (mpv, Chromium, FFmpeg)
decode video on the AVD hardware in Apple Silicon Macs.

**This fork is not affiliated with or endorsed by the authors of the projects it is based on.**

The original documentation is in [README](README).

## Upstream and credits

- **libva-v4l2_request** by Ondřej Jirman (megi): the driver this all builds on.
- **[sofus13/libva-v4l2_request](https://github.com/sofus13/libva-v4l2_request)**, tag `1.3`
  (`cfe6c2a`): AVD support. This fork keeps that full history and its tags.
- **Chromium green-frame fix** on `main`, from branch `fix-avd-early-export` of
  [Ante042/libva-v4l2_request](https://github.com/Ante042/libva-v4l2_request), kept as the original
  commits:
  - `201bc71` by Igor Ryzhkov: decode into surfaces the client exported before the first decode
  - `9e6d750` and `386956d` by Ante042: keep exported dimensions, reserve codec tail storage, and a test

## What `main` changes

Chromium creates each VA surface, exports it as a dma-buf and imports it into the GPU before decoding
into it. Version 1.3 decoded into different buffers, so Chromium showed solid green video. With these
commits the decoder writes into the exported buffers.

Tested on an M1 (T8103) with Chrome 152: the rendered picture matched software decoding for H.264 and
HEVC, and a 5,400-frame H.264 decode through mpv (`--hwdec=vaapi-copy`) was bit-exact.

## Known problems

As of 2026-09-15:

- **Decode errors are ignored.** Dequeued capture buffers flagged `V4L2_BUF_FLAG_ERROR` are reported
  as successfully decoded, so failed frames show up as green or garbage instead of errors.
- **FFmpeg `hwdownload` crashes.** `ffmpeg -hwaccel vaapi -hwaccel_output_format vaapi ... -vf hwdownload,format=nv12`
  segfaults in `memcpy` inside `vaGetImage`. mpv's `--hwdec=vaapi-copy` works.
- **Wrong colors in some mpv renderers.** With `--hwdec=vaapi`, `--vo=gpu-next` on Vulkan (Mesa
  Honeykrisp) and `--vo=gpu` show a green/pink picture with a ghost image, as if the chroma plane
  offset were misread. `--vo=gpu-next --gpu-api=opengl` and Chromium display correctly. The cause
  (exported surface layout or the importers) is not known.
- **Kernel driver bugs** in AVD itself can hang or crash the system under load; see
  [apple-avd-driver](https://github.com/iconidentify/apple-avd-driver).

## Building

```sh
meson setup build
meson compile -C build
```

To try the driver without installing it:

```sh
LIBVA_DRIVERS_PATH=$PWD/build/src LIBVA_DRIVER_NAME=v4l2_request mpv --hwdec=vaapi-copy video.mp4
```

## License

GPL-3.0-or-later, see [COPYING](COPYING).
