#!/bin/sh
# Disposable-root install/uninstall smoke test (issue #34). Builds and
# installs into a throwaway DESTDIR, never touching the host, and checks:
# default driverdir matches libva's pkg-config variable, a custom -Ddriverdir
# override is honoured, only the driver module is installed (no test
# binaries), the installed copy embeds no build/worktree paths and no debug
# info, the vendor/version marker and ABI entrypoint symbol are present and
# match the libva this driver was configured against, and uninstall removes
# every installed file and now-empty directory, leaving the disposable root
# empty.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
src_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM

# cd into the source tree so `meson setup <builddir>` (no explicit
# sourcedir argument, matching how README.md documents the build) picks it
# up from the working directory. Passing the source dir as a second
# positional argument is rejected by some Meson releases.
cd "$src_dir"

fail() { echo "install-smoke: $*" >&2; exit 1; }

module_name=v4l2_request_drv_video.so

# dlopen(RTLD_NOW) the installed module and dlsym its ABI entrypoint: this
# resolves every shared-library dependency (libva/libdrm/pthread) eagerly,
# so a missing or mismatched dependency fails here with dlerror() rather
# than surfacing only inside a real VA-API client.
cat > "$work/dlopen-check.c" <<'EOF'
#include <dlfcn.h>
#include <stdio.h>
int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: dlopen-check <module> <entrypoint>\n"); return 2; }
    void *h = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!h) { fprintf(stderr, "dlopen failed: %s\n", dlerror()); return 1; }
    dlerror();
    void *sym = dlsym(h, argv[2]);
    char *err = dlerror();
    if (err) { fprintf(stderr, "dlsym(%s) failed: %s\n", argv[2], err); return 1; }
    (void)sym;
    printf("loaded %s, resolved %s\n", argv[1], argv[2]);
    return 0;
}
EOF
cc -Wall -Wextra -Werror -o "$work/dlopen-check" "$work/dlopen-check.c" -ldl

# --- default driverdir: build, install, verify, uninstall -----------------
build_default="$work/build-default"
destdir_default="$work/destdir-default"
meson setup "$build_default" --prefix=/usr >"$work/setup-default.log" 2>&1 \
    || { cat "$work/setup-default.log" >&2; fail "meson setup (default driverdir) failed"; }
meson compile -C "$build_default" >"$work/compile-default.log" 2>&1 \
    || { cat "$work/compile-default.log" >&2; fail "meson compile (default driverdir) failed"; }

expected_driverdir=/usr/lib/dri
if pkg-config --exists libva; then
    pkg_driverdir=$(pkg-config --variable=driverdir libva)
    [ -n "$pkg_driverdir" ] && expected_driverdir=$pkg_driverdir
fi

DESTDIR="$destdir_default" meson install -C "$build_default" >"$work/install-default.log" 2>&1 \
    || { cat "$work/install-default.log" >&2; fail "meson install (default driverdir) failed"; }
verify_install() {
    stage=$1
    build=$2
    install_log=$3
    driverdir=$4
    grep -q "^Stripping target " "$install_log" \
        || fail "installed module was not stripped (strip=true default expected)"
    installed="$stage$driverdir/$module_name"
    [ -f "$installed" ] || fail "expected $installed after install, not found"
    found_count=$(find "$stage" -type f | wc -l)
    [ "$found_count" -eq 1 ] || fail "expected exactly 1 installed file, found $found_count"

    # Inspect contents in both layouts, not just the install command's message.
    strings "$installed" > "$work/installed-strings.txt"
    if grep -qE '^/(tmp|home|root|build|usr/src)' "$work/installed-strings.txt" ||
       grep -qF "$src_dir" "$work/installed-strings.txt" ||
       grep -qF "$build" "$work/installed-strings.txt"; then
        fail "installed module embeds an absolute build/worktree path"
    fi
    readelf -SW "$installed" > "$work/installed-sections.txt"
    if grep -qE '\.(debug|zdebug)_' "$work/installed-sections.txt"; then
        fail "installed module still contains DWARF debug sections"
    fi
    readelf -SW "$build/src/$module_name" > "$work/build-sections.txt"
    grep -q '\.debug_info' "$work/build-sections.txt" \
        || fail "build-tree module unexpectedly lost its debug info"
    grep -q '^v4l2-request (omarchy-m1-video ' "$work/installed-strings.txt" \
        || fail "installed module is missing its vendor/version marker"

    va_ver=$(pkg-config --modversion libva)
    va_major=${va_ver%%.*}
    va_minor_rest=${va_ver#*.}
    va_minor=${va_minor_rest%%.*}
    entry="__vaDriverInit_${va_major}_${va_minor}"
    "$work/dlopen-check" "$installed" "$entry" \
        || fail "installed module failed to load/resolve the build-header ABI $entry"
    # Verify the checker actually rejects an unavailable entrypoint. This is
    # a loader-only test; it does not initialize libva or open decoder devices.
    if "$work/dlopen-check" "$installed" __vaDriverInit_0_0 >"$work/abi-negative.log" 2>&1; then
        fail "ABI loader check accepted a missing entrypoint"
    fi
    grep -q 'dlsym(__vaDriverInit_0_0) failed' "$work/abi-negative.log" \
        || fail "negative ABI check failed for an unexpected reason"
}
verify_install "$destdir_default" "$build_default" "$work/install-default.log" "$expected_driverdir"

# Uninstall must remove every installed file and now-empty directory.
DESTDIR="$destdir_default" ninja -C "$build_default" uninstall >"$work/uninstall-default.log" 2>&1 \
    || { cat "$work/uninstall-default.log" >&2; fail "ninja uninstall (default driverdir) failed"; }
[ -e "$destdir_default" ] && fail "disposable root not fully removed by uninstall: $destdir_default"

# --- custom -Ddriverdir override -------------------------------------------
build_custom="$work/build-custom"
destdir_custom="$work/destdir-custom"
custom_driverdir=/opt/custom-va-drivers
meson setup "$build_custom" --prefix=/usr "-Ddriverdir=$custom_driverdir" \
    >"$work/setup-custom.log" 2>&1 \
    || { cat "$work/setup-custom.log" >&2; fail "meson setup (custom driverdir) failed"; }
meson compile -C "$build_custom" >"$work/compile-custom.log" 2>&1 \
    || { cat "$work/compile-custom.log" >&2; fail "meson compile (custom driverdir) failed"; }
DESTDIR="$destdir_custom" meson install -C "$build_custom" >"$work/install-custom.log" 2>&1 \
    || { cat "$work/install-custom.log" >&2; fail "meson install (custom driverdir) failed"; }
verify_install "$destdir_custom" "$build_custom" "$work/install-custom.log" "$custom_driverdir"
DESTDIR="$destdir_custom" ninja -C "$build_custom" uninstall >"$work/uninstall-custom.log" 2>&1 \
    || { cat "$work/uninstall-custom.log" >&2; fail "ninja uninstall (custom driverdir) failed"; }
[ -e "$destdir_custom" ] && fail "disposable root not fully removed by uninstall: $destdir_custom"

echo "Install smoke: default driverdir ($expected_driverdir) and custom -Ddriverdir both install a single stripped, marked module and fully uninstall from a disposable root."
