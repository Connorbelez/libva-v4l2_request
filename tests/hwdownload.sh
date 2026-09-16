#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Hardware test. Run with no other video clients; BUILD/src selects a local driver.
# Requires ffmpeg with libx264/libx265, a C compiler and libva development files.
# Usage: hwdownload.sh BUILD/src [MAIN10_CLIP]
# MAIN10_CLIP replaces the generated 10-bit clip on 8-bit-only x265 builds.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=require-hw-guard.sh
. "$script_dir/require-hw-guard.sh"
require_hw_guard "$@" || exit $?
export LIBVA_DRIVERS_PATH=$(realpath "$1")
export LIBVA_DRIVER_NAME=v4l2_request
main10=${2:-}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cc -shared -fPIC "$script_dir/force-get-image.c" -o "$work/get.so" \
    $(pkg-config --cflags --libs libva) -ldl
for spec in h264:640x360:8 h264:1920x1080:8 hevc:640x360:8 hevc:640x360:10; do
    codec=${spec%%:*}
    rest=${spec#*:}
    size=${rest%:*}
    depth=${rest##*:}
    pix=nv12
    encode_pix=yuv420p
    if [ "$depth" = 10 ]; then pix=p010le; encode_pix=yuv420p10le; fi
    case "$codec" in
        h264) set -- -c:v libx264 -preset ultrafast ;;
        hevc) set -- -c:v libx265 -preset ultrafast -x265-params pools=2 ;;
    esac
    clip=$work/clip.mkv
    if [ "$depth" = 10 ] && [ -n "$main10" ]; then
        clip=$main10
    elif ! ffmpeg -nostdin -v error -y -f lavfi -i "testsrc2=size=$size:rate=30,format=$encode_pix" \
        -frames:v 30 "$@" -pix_fmt "+$encode_pix" "$clip" 2>"$work/encode.log"; then
        cat "$work/encode.log" >&2
        echo 'An 8-bit-only x265 build needs MAIN10_CLIP as the second argument.' >&2
        exit 1
    fi
    actual_pix=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt \
        -of default=nw=1:nk=1 "$clip")
    test "$actual_pix" = "$encode_pix"
    size=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
        -of csv=p=0:s=x "$clip")
    spec=$codec:$size:$depth
    ffmpeg -nostdin -v error -y -i "$clip" -an -frames:v 30 -pix_fmt "$pix" \
        -f rawvideo "$work/software"
    expected=$(( ${size%x*} * ${size#*x} * 3 / 2 * 30 ))
    if [ "$depth" = 10 ]; then expected=$(( expected * 2 )); fi
    test "$(wc -c < "$work/software")" -eq "$expected"
    for mode in normal get-image; do
        if [ "$mode" = get-image ]; then export LD_PRELOAD="$work/get.so"; fi
        if ! timeout -k 5 30 ffmpeg -nostdin -v verbose -y -hwaccel vaapi \
            -hwaccel_output_format vaapi -i "$clip" -an -frames:v 30 \
            -vf "hwdownload,format=$pix" -f rawvideo "$work/hardware" 2>"$work/decode.log"; then
            cat "$work/decode.log" >&2
            exit 1
        fi
        unset LD_PRELOAD
        grep -q "decoding $codec via" "$work/decode.log"
        if [ "$mode" = get-image ]; then
            # FFmpeg may download prefetched frames beyond the output limit.
            test "$(grep -c GET_IMAGE_PASS "$work/decode.log")" -ge 30
        fi
        cmp "$work/software" "$work/hardware"
        printf '%s %s: 30 frames, identical pixels\n' "$spec" "$mode"
    done
done
