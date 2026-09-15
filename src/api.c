/* SPDX-License-Identifier: GPL-3.0-or-later */
/* A decode context and its surfaces have independent VA lifetimes. FFmpeg
 * destroys a context on its decoder thread while its filter thread downloads
 * the last frames. Keep teardown from invalidating another entrypoint's view.
 * Internal calls use the unlocked implementations, so the lock is not recursive.
 */
#include "v4l2_request.h"

#define LOCKED(name, params, args) \
static VAStatus locked_##name params \
{ \
    struct v4l2r_driver *drv = v4l2r_driver(va); \
    pthread_mutex_lock(&drv->api_mutex); \
    VAStatus result = v4l2r_##name args; \
    pthread_mutex_unlock(&drv->api_mutex); \
    return result; \
}

LOCKED(CreateContext, (VADriverContextP va, VAConfigID config, int w, int h,
    int flags, VASurfaceID *targets, int n, VAContextID *id),
    (va, config, w, h, flags, targets, n, id))
LOCKED(DestroyContext, (VADriverContextP va, VAContextID id), (va, id))
LOCKED(DestroySurfaces, (VADriverContextP va, VASurfaceID *ids, int n), (va, ids, n))
LOCKED(BeginPicture, (VADriverContextP va, VAContextID id, VASurfaceID surface), (va, id, surface))
LOCKED(RenderPicture, (VADriverContextP va, VAContextID id, VABufferID *buffers, int n),
    (va, id, buffers, n))
LOCKED(EndPicture, (VADriverContextP va, VAContextID id), (va, id))
LOCKED(SyncSurface, (VADriverContextP va, VASurfaceID id), (va, id))
LOCKED(QuerySurfaceStatus, (VADriverContextP va, VASurfaceID id, VASurfaceStatus *status),
    (va, id, status))
LOCKED(ExportSurfaceHandle, (VADriverContextP va, VASurfaceID id, uint32_t type,
    uint32_t flags, void *desc), (va, id, type, flags, desc))
LOCKED(DeriveImage, (VADriverContextP va, VASurfaceID id, VAImage *image), (va, id, image))
LOCKED(GetImage, (VADriverContextP va, VASurfaceID id, int x, int y,
    unsigned int w, unsigned int h, VAImageID image), (va, id, x, y, w, h, image))
LOCKED(PutImage, (VADriverContextP va, VASurfaceID id, VAImageID image,
    int sx, int sy, unsigned int sw, unsigned int sh,
    int dx, int dy, unsigned int dw, unsigned int dh),
    (va, id, image, sx, sy, sw, sh, dx, dy, dw, dh))

void v4l2r_lock_surface_api(struct VADriverVTable *vtable)
{
#define WRAP(name) vtable->va##name = locked_##name
    WRAP(CreateContext);
    WRAP(DestroyContext);
    WRAP(DestroySurfaces);
    WRAP(BeginPicture);
    WRAP(RenderPicture);
    WRAP(EndPicture);
    WRAP(SyncSurface);
    WRAP(QuerySurfaceStatus);
    WRAP(ExportSurfaceHandle);
    WRAP(DeriveImage);
    WRAP(GetImage);
    WRAP(PutImage);
#undef WRAP
}
