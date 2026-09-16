# Synthetic VP9 10-bit resource fixture

`vp9-10.webm` is a synthetic FFmpeg test pattern generated for this project.
The generated media is dedicated to the public domain under CC0-1.0. It contains
no external footage, personal media or third-party recording.

SHA-256: `cfc1d27ad161696e574020b910cd952725cf67d12e0913dedec7d202aae84463`

640x360, 24 frames, VP9 profile 2 / yuv420p10le. Generated using FFmpeg 9.0.1:

```sh
ffmpeg -nostdin -v error -y -f lavfi \
  -i 'testsrc2=size=640x360:rate=27,format=yuv420p10le' \
  -frames:v 24 -c:v libvpx-vp9 -deadline good -cpu-used 5 \
  -threads 2 -lag-in-frames 8 -g 12 -pix_fmt +yuv420p10le vp9-10.webm
```

The container bytes are pinned rather than assumed reproducible across encoder
versions/runs. The campaign computes independent software reference hashes on
each host. This fixture retains 10-bit decoding coverage when an older libvpx
build cannot encode it (including the Ubuntu 20.04 CI image).
