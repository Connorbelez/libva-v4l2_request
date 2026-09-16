#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Run through a hardware guard: vp9-matrix.sh BUILD/src
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=require-hw-guard.sh
. "$script_dir/require-hw-guard.sh"
require_hw_guard "$@" || exit $?
export LIBVA_DRIVERS_PATH=$(realpath "$1")
export LIBVA_DRIVER_NAME=v4l2_request
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cc -Wall -Wextra -O2 -o "$work/frame-check" "$script_dir/frame-check.c" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswscale)
cc -shared -fPIC -o "$work/early.so" "$script_dir/early-export.c" \
    $(pkg-config --cflags --libs libva) -ldl
for depth in 8 10; do
    fmt=yuv420p
    [ "$depth" -eq 8 ] || fmt=yuv420p10le
    for range in tv pc; do
        for lossless in 0 1; do
            quality=32
            [ "$lossless" -eq 0 ] || quality=0
            ffmpeg -nostdin -v error -y -f lavfi \
                -i "testsrc2=size=640x360:rate=30,scale=in_range=tv:out_range=$range,format=$fmt" -frames:v 24 \
                -c:v libvpx-vp9 -deadline good -cpu-used 5 -threads 2 \
                -tile-columns 1 -tile-rows 1 -lag-in-frames 12 -auto-alt-ref 1 \
                -g 12 -lossless "$lossless" -crf "$quality" -b:v 0 -color_range "$range" \
                -pix_fmt "+$fmt" "$work/clip.webm"
            pix=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt \
                -of default=nw=1:nk=1 "$work/clip.webm")
            test "$pix" = "$fmt"
            colour=$(ffprobe -v error -select_streams v:0 -show_entries stream=color_range \
                -of default=nw=1:nk=1 "$work/clip.webm")
            test "$colour" = "$range"
            "$work/frame-check" software "$work/clip.webm" "$fmt" >"$work/software"
            test "$(grep -c '^frame ' "$work/software")" -eq 24
            for mode in normal early; do
                if [ "$mode" = early ]; then export LD_PRELOAD="$work/early.so"; fi
                if ! timeout -k 5 30 "$work/frame-check" vaapi "$work/clip.webm" "$fmt" \
                    >"$work/hardware" 2>"$work/decode.log"; then
                    cat "$work/decode.log" >&2
                    exit 1
                fi
                unset LD_PRELOAD
                if [ "$mode" = early ]; then
                    # Hidden reference pictures may be decoded in addition to displayed frames.
                    test "$(grep -c EARLY_EXPORT_PASS "$work/decode.log")" -ge 24
                fi
                if ! cmp "$work/software" "$work/hardware"; then
                    cat "$work/decode.log" >&2
                    exit 1
                fi
                printf 'VP9 depth=%s range=%s lossless=%s %s: 24 frames, identical pixels\n' \
                    "$depth" "$range" "$lossless" "$mode"
            done
        done
    done
done
