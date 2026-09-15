#!/bin/sh
# Hardware regression check. Usage: early-export.sh BUILD/src [FFmpeg hw_decode.c]
# Requires a supported VA-API device, C compiler, pkg-config, FFmpeg development
# headers and ffmpeg with libx264, libx265 and libvpx-vp9. Generates its own clips.
set -eu
export LIBVA_DRIVERS_PATH=$(realpath "$1")
export LIBVA_DRIVER_NAME=v4l2_request
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
example=${2:-/usr/share/ffmpeg/examples/hw_decode.c}
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT HUP INT TERM
cc -shared -fPIC -o "$test_dir/check.so" "$script_dir/early-export.c" \
    $(pkg-config --cflags --libs libva) -ldl
cc -o "$test_dir/hw-decode" "$example" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil)
# Use the synchronous hw_decode sample so frame downloads finish before context
# destruction. The hook exports before vaBeginPicture and syncs after vaEndPicture.
for spec in h264:640x360 h264:640x384 h264:1920x1080 hevc:640x384 vp9:640x384; do
    codec=${spec%:*}
    size=${spec#*:}
    case "$codec" in
        h264) set -- -c:v libx264 -preset medium ;;
        hevc) set -- -c:v libx265 -preset ultrafast -x265-params pools=2 ;;
        vp9) set -- -c:v libvpx-vp9 -deadline realtime -cpu-used 6 ;;
    esac
    ffmpeg -v error -y -f lavfi -i "testsrc2=size=$size:rate=30" \
        -frames:v 30 "$@" -pix_fmt yuv420p "$test_dir/clip.mkv" \
        2>"$test_dir/encode.log"
    ffmpeg -v error -y -i "$test_dir/clip.mkv" -pix_fmt nv12 \
        -f rawvideo "$test_dir/software.nv12"
    expected=$(( ${size%x*} * ${size#*x} * 3 / 2 * 30 ))
    test "$(wc -c < "$test_dir/software.nv12")" -eq "$expected"
    for mode in normal early; do
        if [ "$mode" = early ]; then export LD_PRELOAD="$test_dir/check.so"; fi
        if ! timeout -k 5 30 "$test_dir/hw-decode" vaapi "$test_dir/clip.mkv" \
            "$test_dir/hardware.nv12" 2>"$test_dir/decode.log"; then
            cat "$test_dir/decode.log" >&2
            exit 1
        fi
        unset LD_PRELOAD
        if [ "$mode" = early ]; then
            test "$(grep -c EARLY_EXPORT_PASS "$test_dir/decode.log")" -eq 30
        fi
        cmp "$test_dir/software.nv12" "$test_dir/hardware.nv12"
        printf '%s %s %s: 30 frames, identical pixels\n' "$codec" "$size" "$mode"
    done
done
