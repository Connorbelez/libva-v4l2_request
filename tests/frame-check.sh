#!/bin/sh
# Software-only regression for native-size hashing. Optional first argument
# selects a driver build for a separate guarded hardware comparison.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cc -Wall -Wextra -Werror -o "$work/check" "$script_dir/frame-check.c" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswscale)
for size in 64x48 128x96; do
    ffmpeg -nostdin -v error -f lavfi -i "testsrc2=size=$size:rate=1" \
        -frames:v 2 -c:v libx264 -preset ultrafast -f h264 "$work/$size.h264"
    ffmpeg -nostdin -v error -i "$work/$size.h264" -pix_fmt yuv420p \
        -f rawvideo "$work/$size.yuv"
done
cat "$work/64x48.h264" "$work/128x96.h264" > "$work/change.h264"
cat "$work/64x48.yuv" "$work/128x96.yuv" > "$work/expected.yuv"
expected=$(md5sum "$work/expected.yuv" | cut -d' ' -f1)
"$work/check" software "$work/change.h264" yuv420p > "$work/software"
grep -q "^MD5=$expected$" "$work/software"
test "$(grep -c '^frame ' "$work/software")" -eq 4
test "$(grep -c '64x48' "$work/software")" -eq 2
test "$(grep -c '128x96' "$work/software")" -eq 2
ffmpeg -nostdin -v error -i "$work/64x48.h264" -c copy \
    -bsf:v h264_metadata=crop_left=2:crop_right=2:crop_top=2:crop_bottom=2 "$work/crop.h264"
# Older FFmpeg CLI decoders round metadata crops to pointer alignment. Build
# the reference with an explicit pixel crop of the original, uncropped video.
ffmpeg -nostdin -v error -i "$work/64x48.h264" -vf crop=60:44:2:2 -pix_fmt yuv420p \
    -f rawvideo "$work/crop.yuv"
expected_crop=$(md5sum "$work/crop.yuv" | cut -d' ' -f1)
"$work/check" software "$work/crop.h264" yuv420p > "$work/crop-software"
grep -q "^MD5=$expected_crop$" "$work/crop-software"
test "$(grep -c '60x44' "$work/crop-software")" -eq 2
head -c 12 "$work/change.h264" > "$work/truncated.h264"
if "$work/check" software "$work/truncated.h264" yuv420p > "$work/bad" 2>/dev/null; then
    echo 'Truncated input unexpectedly succeeded' >&2
    exit 1
fi
if grep -q '^MD5=' "$work/bad"; then exit 1; fi
if [ "$#" -gt 0 ]; then
    export LIBVA_DRIVERS_PATH=$(realpath "$1") LIBVA_DRIVER_NAME=v4l2_request
    timeout -k 5 30 "$work/check" vaapi "$work/change.h264" yuv420p > "$work/hardware"
    cmp "$work/software" "$work/hardware"
    timeout -k 5 30 "$work/check" vaapi "$work/crop.h264" yuv420p > "$work/crop-hardware"
    cmp "$work/crop-software" "$work/crop-hardware"
fi
echo 'Native-size hashing: resolution change and crop match; truncated input fails.'
