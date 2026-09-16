# SPDX-License-Identifier: GPL-3.0-or-later
# Source from hardware scripts. Refuse decoder work without tests/hwguard.py.
require_hw_guard() {
    if [ -z "${LIBVA_HW_GUARD_LEASE:-}" ]; then
        echo "ungarded hardware run refused; use: python3 tests/hwguard.py -- $0 $*" >&2
        echo "the guard holds an exclusive decoder lease and a finite deadline" >&2
        return 2
    fi
    return 0
}
