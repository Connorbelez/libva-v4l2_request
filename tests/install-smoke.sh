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
grep -q "^Stripping target " "$work/install-default.log" \
    || fail "installed module was not stripped (strip=true default expected, see meson.build)"

installed="$destdir_default$expected_driverdir/$module_name"
[ -f "$installed" ] || fail "expected $installed after install, not found"

# Only the driver module may land under the driverdir; no test binaries.
found_count=$(find "$destdir_default" -type f | wc -l)
[ "$found_count" -eq 1 ] || {
    find "$destdir_default" -type f >&2
    fail "expected exactly 1 installed file, found $found_count"
}

# No absolute build/worktree/host paths embedded in the installed copy.
if strings "$installed" | grep -qE '^/(tmp|home|root|build|usr/src)'; then
    strings "$installed" | grep -E '^/(tmp|home|root|build|usr/src)' >&2
    fail "installed module embeds an absolute build/worktree path"
fi

# No DWARF debug info in the installed copy (stripped); the build-tree copy
# keeps it for LIBVA_DRIVERS_PATH development.
file "$installed" | grep -q 'not stripped' && fail "installed module is not stripped"
file "$build_default/src/$module_name" | grep -q 'with debug_info' \
    || fail "build-tree module unexpectedly lost its debug info"

# Vendor/version marker.
strings "$installed" | grep -q '^v4l2-request (omarchy-m1-video ' \
    || fail "installed module is missing its vendor/version marker string"

# ABI entrypoint symbol matches the libva this build was configured against,
# and the installed module actually loads: dlopen(RTLD_NOW) resolves every
# shared-library dependency and dlsym finds the entrypoint libva looks up.
if pkg-config --exists libva; then
    va_ver=$(pkg-config --modversion libva)
    va_major=${va_ver%%.*}
    va_minor_rest=${va_ver#*.}
    va_minor=${va_minor_rest%%.*}
    entry="__vaDriverInit_${va_major}_${va_minor}"
    nm -D "$build_default/src/$module_name" 2>/dev/null | grep -q " T ${entry}\$" \
        || fail "expected exported ABI entrypoint $entry for libva $va_ver, not found (rebuild against matching libva-dev headers)"
    "$work/dlopen-check" "$installed" "$entry" \
        || fail "installed module at $installed failed to dlopen/dlsym $entry (see dlerror() above; a missing shared-library dependency or ABI mismatch would surface exactly like this to libva)"
fi

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
[ -f "$destdir_custom$custom_driverdir/$module_name" ] \
    || fail "custom -Ddriverdir=$custom_driverdir was not honoured on install"
DESTDIR="$destdir_custom" ninja -C "$build_custom" uninstall >"$work/uninstall-custom.log" 2>&1 \
    || { cat "$work/uninstall-custom.log" >&2; fail "ninja uninstall (custom driverdir) failed"; }
[ -e "$destdir_custom" ] && fail "disposable root not fully removed by uninstall: $destdir_custom"

echo "Install smoke: default driverdir ($expected_driverdir) and custom -Ddriverdir both install a single stripped, marked module and fully uninstall from a disposable root."
