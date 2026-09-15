#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Guarded hardware test: h264-high10.sh BUILD/src
# Requires a 10-bit-capable libx264 encoder and FFmpeg/libva development files.
set -eu
export LIBVA_DRIVERS_PATH=$(realpath "$1")
export LIBVA_DRIVER_NAME=v4l2_request
export LIBVA_V4L2_H264_HIGH10=ffmpeg
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cc -Wall -Wextra -O2 -o "$work/frame-check" "$script_dir/frame-check.c" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswscale)
cc -shared -fPIC -o "$work/early.so" "$script_dir/early-export.c" \
    $(pkg-config --cflags --libs libva) -ldl
# 360 lines also exercises the cropped bottom of a 368-line coded frame.
for cabac in 0 1; do
    for qp in 1 21 51; do
        ffmpeg -nostdin -v error -y -f lavfi \
            -i 'testsrc2=size=640x360:rate=30,format=yuv420p10le' \
            -frames:v 12 -c:v libx264 -preset fast -qp "$qp" \
            -x264-params "cabac=$cabac:slices=4:bframes=2" \
            -pix_fmt +yuv420p10le "$work/clip.mkv"
        pix=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt \
            -of default=nw=1:nk=1 "$work/clip.mkv")
        test "$pix" = yuv420p10le
        "$work/frame-check" software "$work/clip.mkv" yuv420p10le >"$work/software"
        test "$(grep -c '^frame ' "$work/software")" -eq 12
        for mode in normal early; do
            if [ "$mode" = early ]; then export LD_PRELOAD="$work/early.so"; fi
            if ! timeout -k 5 30 "$work/frame-check" vaapi "$work/clip.mkv" yuv420p10le \
                >"$work/hardware" 2>"$work/decode.log"; then
                cat "$work/decode.log" >&2
                exit 1
            fi
            unset LD_PRELOAD
            if [ "$mode" = early ]; then
                test "$(grep -c EARLY_EXPORT_PASS "$work/decode.log")" -eq 12
            fi
            if ! cmp "$work/software" "$work/hardware"; then
                cat "$work/decode.log" >&2
                exit 1
            fi
            printf 'High 10 cabac=%s QP=%s %s: 12 frames, identical pixels\n' "$cabac" "$qp" "$mode"
        done
    done
done
