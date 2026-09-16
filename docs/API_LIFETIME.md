# Public VA object lifetime audit (#23)

This audit covers config, context, surface, buffer and image entrypoints. The
reference is [libva 2.24.0's public contract](https://github.com/intel/libva/blob/2.24.0/va/va.h):
context render targets are hints; clients own the Begin/Render/End ordering and
must destroy ordinary buffers; image buffers are managed by Create/Derive/DestroyImage.
Context destruction and surface destruction are separate operations. All handles
are opaque; clients must stop using a destroyed handle even if its number is later
reissued. This is not a claim of an exploit through a media file.

## Initial baseline and reproductions

Baseline: `8848956e14b61addf760f8cd2352dd0596409b8c`, native aarch64 GCC 16.1.1,
libva pkg-config 1.24.0, libdrm 2.4.134, Meson 1.12.0. All 53 existing
ASan/UBSan cases and both software frame scripts pass inside a namespace without
decoder devices or network access.

`tests/api-lifecycle.c` adds public-entrypoint sequences with an intercepted codec
and memfd-backed image pixels. At baseline 18 of its 19 cases fail; the independent
context/surface lifetime control passes. Failures include null output dereferences,
invalid create arguments being accepted, partial destruction on a duplicate list,
foreign/orphaned parameter buffers being submitted, direct deletion of an image's
owned buffer, and a missing EndPicture being reported as successful surface output.

Current implementation and acceptance evidence are pending in the draft PR.
No kernel patch, installation, module operation, hardware test or reboot is part
of this initial reproduction. Hardware regression requirements will be assessed
against the final behavior changes before this ticket can close.
