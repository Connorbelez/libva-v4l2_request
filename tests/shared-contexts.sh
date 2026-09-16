#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Without an argument, validate the helper in software. Hardware needs a guard.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cc -Wall -Wextra -O2 -o "$work/shared" "$script_dir/shared-contexts.c" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswscale)
cc -Wall -Wextra -O2 -o "$work/single" "$script_dir/frame-check.c" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswscale)
ffmpeg -nostdin -v error -y -f lavfi -i testsrc2=size=640x360:rate=30 \
    -frames:v 24 -c:v libx264 -preset ultrafast -bf 2 -g 12 -pix_fmt +yuv420p "$work/h264.mkv"
ffmpeg -nostdin -v error -y -f lavfi -i testsrc2=size=640x360:rate=25 \
    -frames:v 36 -c:v libx265 -preset ultrafast -x265-params log-level=error:pools=2 \
    -pix_fmt +yuv420p "$work/hevc.mkv" 2>"$work/encode.log"
for depth in 8 10; do
    fmt=yuv420p
    [ "$depth" -eq 8 ] || fmt=yuv420p10le
    ffmpeg -nostdin -v error -y -f lavfi -i "testsrc2=size=640x360:rate=$((depth * 2)),format=$fmt" \
        -frames:v "$((depth * 6))" -c:v libvpx-vp9 -deadline good -cpu-used 5 \
        -threads 2 -g 12 -lag-in-frames 8 -pix_fmt "+$fmt" "$work/vp9-$depth.webm"
done
set -- "${1:-}" "$work/h264.mkv" yuv420p "$work/hevc.mkv" yuv420p \
    "$work/vp9-8.webm" yuv420p "$work/vp9-10.webm" yuv420p10le
driver=$1
if [ -n "$driver" ]; then
    # shellcheck source=require-hw-guard.sh
    . "$script_dir/require-hw-guard.sh"
    require_hw_guard "$@" || exit $?
fi
shift
# Each stream's independent software result is the expected digest/count.
i=0
: >"$work/expected"
for clip in h264.mkv hevc.mkv vp9-8.webm vp9-10.webm; do
    fmt=yuv420p
    [ "$clip" != vp9-10.webm ] || fmt=yuv420p10le
    "$work/single" software "$work/$clip" "$fmt" >"$work/single.out"
    frames=$(grep -c '^frame ' "$work/single.out")
    test "$frames" -eq "$((24 + 12 * i))"
    digest=$(sed -n 's/^MD5=//p' "$work/single.out")
    printf 'stream %s frames=%s MD5=%s\n' "$i" "$frames" "$digest" >>"$work/expected"
    i=$((i + 1))
done
"$work/shared" software "$@" >"$work/software"
grep '^stream ' "$work/software" | sort >"$work/actual"
cmp "$work/expected" "$work/actual"
printf 'Shared contexts, software: four streams, 168 frames match independent decoders\n'
[ -n "$driver" ] || exit 0
export LIBVA_DRIVERS_PATH=$(realpath "$driver") LIBVA_DRIVER_NAME=v4l2_request
cc -shared -fPIC -o "$work/early.so" "$script_dir/early-export.c" \
    $(pkg-config --cflags --libs libva) -ldl
for mode in normal early; do
    if [ "$mode" = early ]; then export LD_PRELOAD="$work/early.so"; fi
    if ! timeout -k 5 60 "$work/shared" vaapi "$@" >"$work/hardware" 2>"$work/decode.log"; then
        cat "$work/decode.log" >&2; exit 1
    fi
    unset LD_PRELOAD
    grep '^stream ' "$work/hardware" | sort >"$work/actual"
    cmp "$work/expected" "$work/actual"
    if [ "$mode" = early ]; then test "$(grep -c EARLY_EXPORT_PASS "$work/decode.log")" -ge 168; fi
    printf 'Shared VA display, %s: four streams, 168 frames match independent software\n' "$mode"
done
