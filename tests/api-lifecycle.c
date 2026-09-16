/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Public VA object sequences with an intercepted codec and memfd pixels.
 * These deliberately invalid client calls are not crafted-media exploits. */
#define _GNU_SOURCE
#include <assert.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>
#include <va/va_drmcommon.h>
#include "v4l2_request.h"

static struct VADriverContext va;
static struct v4l2r_driver *drv;
static struct VADriverVTable table;
static unsigned int renders, submissions;

static VAStatus render(struct v4l2r_context *ctx, struct v4l2r_buffer *buf)
{
    (void)ctx; (void)buf;
    renders++;
    return VA_STATUS_SUCCESS;
}
static VAStatus end(struct v4l2r_context *ctx)
{
    assert(ctx->pic.target);
    ctx->pic.target->decode_status = VA_STATUS_SUCCESS;
    submissions++;
    return VA_STATUS_SUCCESS;
}
static const struct v4l2r_codec codec = {.render_buffer = render, .end_picture = end};

static VAConfigID config(void)
{
    VAConfigID id = VA_INVALID_ID;
    assert(v4l2r_CreateConfig(&va, VAProfileNone, VAEntrypointVideoProc,
                            NULL, 0, &id) == VA_STATUS_SUCCESS);
    return id;
}
static VAContextID context(void)
{
    VAConfigID cfg = config();
    VAContextID id = VA_INVALID_ID;
    /* VPP allocation needs no device. Replace only its codec callbacks. */
    assert(table.vaCreateContext(&va, cfg, 64, 64, 0, NULL, 0, &id) == VA_STATUS_SUCCESS);
    struct v4l2r_context *ctx = V4L2R_CONTEXT(drv, id);
    v4l2r_vpp_destroy(ctx);
    ctx->codec = &codec;
    /* Contexts snapshot their config; it need not outlive them. */
    assert(v4l2r_DestroyConfig(&va, cfg) == VA_STATUS_SUCCESS);
    assert(v4l2r_DestroyConfig(&va, cfg) == VA_STATUS_ERROR_INVALID_CONFIG);
    return id;
}
static VASurfaceID surface(void)
{
    VASurfaceID id;
    assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64,
                               &id, 1, NULL, 0) == VA_STATUS_SUCCESS);
    return id;
}
static VABufferID buffer(VAContextID cid)
{
    VABufferID id;
    unsigned char bytes[16] = {1, 2, 3};
    assert(v4l2r_CreateBuffer(&va, cid, VAPictureParameterBufferType,
                             sizeof(bytes), 1, bytes, &id) == VA_STATUS_SUCCESS);
    return id;
}
static void picture(VAContextID cid, VASurfaceID sid, VABufferID bid)
{
    assert(table.vaBeginPicture(&va, cid, sid) == VA_STATUS_SUCCESS);
    assert(table.vaRenderPicture(&va, cid, &bid, 1) == VA_STATUS_SUCCESS);
    assert(table.vaEndPicture(&va, cid) == VA_STATUS_SUCCESS);
}
static void backing(VASurfaceID sid)
{
    struct v4l2r_surface_backing *b = calloc(1, sizeof(*b));
    assert(b);
    for (unsigned int i = 0; i < VIDEO_MAX_PLANES; i++) b->dmabuf_fd[i] = -1;
    b->width = b->height = b->pitch = 64;
    b->nb_planes = 1;
    b->pixelformat = V4L2_PIX_FMT_NV12;
    b->plane_size[0] = 64 * 96;
    b->dmabuf_fd[0] = memfd_create("api-lifetime", MFD_CLOEXEC);
    assert(b->dmabuf_fd[0] >= 0 && !ftruncate(b->dmabuf_fd[0], b->plane_size[0]));
    b->map[0] = mmap(NULL, b->plane_size[0], PROT_READ | PROT_WRITE,
                     MAP_SHARED, b->dmabuf_fd[0], 0);
    assert(b->map[0] != MAP_FAILED);
    memset(b->map[0], 0x42, b->plane_size[0]);
    V4L2R_SURFACE(drv, sid)->backing = b;
}

int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core) && argc == 2);
    drv = calloc(1, sizeof(*drv));
    assert(drv);
    va.pDriverData = drv;
    assert(!pthread_mutex_init(&drv->mutex, NULL));
    assert(!pthread_mutex_init(&drv->api_mutex, NULL));
    assert(!v4l2r_handles_init(&drv->configs, V4L2R_ID_OFFSET_CONFIG));
    assert(!v4l2r_handles_init(&drv->contexts, V4L2R_ID_OFFSET_CONTEXT));
    assert(!v4l2r_handles_init(&drv->surfaces, V4L2R_ID_OFFSET_SURFACE));
    assert(!v4l2r_handles_init(&drv->buffers, V4L2R_ID_OFFSET_BUFFER));
    assert(!v4l2r_handles_init(&drv->images, V4L2R_ID_OFFSET_IMAGE));
    drv->converter_probed = drv->has_converter = true;
    v4l2r_lock_surface_api(&table);
    VAContextID a = context(), b = context();
    VASurfaceID sid = surface();
    VABufferID bid = buffer(a);
    VAImageFormat format = {.fourcc = VA_FOURCC_NV12};
    const char *test = argv[1];

    if (!strcmp(test, "surface-duplicates")) {
        VASurfaceID spare = surface(), list[] = {sid, spare, sid};
        assert(table.vaDestroySurfaces(&va, list, 3) == VA_STATUS_ERROR_INVALID_SURFACE);
        assert(V4L2R_SURFACE(drv, sid) && V4L2R_SURFACE(drv, spare));
        list[2] = VA_INVALID_ID;
        assert(table.vaDestroySurfaces(&va, list, 3) == VA_STATUS_ERROR_INVALID_SURFACE);
        assert(V4L2R_SURFACE(drv, sid) && V4L2R_SURFACE(drv, spare));
        assert(table.vaDestroySurfaces(&va, list, 2) == VA_STATUS_SUCCESS);
        assert(table.vaDestroySurfaces(&va, &sid, 1) == VA_STATUS_ERROR_INVALID_SURFACE);
        assert(table.vaDestroySurfaces(&va, NULL, 0) == VA_STATUS_SUCCESS);
        assert(table.vaDestroySurfaces(&va, NULL, 1) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(table.vaDestroySurfaces(&va, &sid, -1) == VA_STATUS_ERROR_INVALID_PARAMETER);
    } else if (!strcmp(test, "surface-parameters")) {
        VASurfaceID out = VA_INVALID_ID;
        assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 0, 64, &out, 1, NULL, 0) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateSurfaces(&va, -1, 64, VA_RT_FORMAT_YUV420, 1, &out) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateSurfaces(&va, 64, 64, VA_RT_FORMAT_YUV420, -1, &out) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, &out, 1, NULL, 1) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(out == VA_INVALID_ID);
        assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, NULL, 0, NULL, 0) == VA_STATUS_SUCCESS);
        VASurfaceID recovery = surface();
        assert(table.vaDestroySurfaces(&va, &recovery, 1) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "surface-null-output")) {
        assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, NULL, 1, NULL, 0) == VA_STATUS_ERROR_INVALID_PARAMETER);
    } else if (!strcmp(test, "surface-query-null")) {
        assert(table.vaQuerySurfaceStatus(&va, sid, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        VASurfaceStatus status;
        assert(table.vaQuerySurfaceStatus(&va, sid, &status) == VA_STATUS_SUCCESS && status == VASurfaceReady);
    } else if (!strcmp(test, "surface-export-null")) {
        backing(sid);
        assert(table.vaExportSurfaceHandle(&va, sid, VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, 0, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        VADRMPRIMESurfaceDescriptor desc;
        assert(table.vaExportSurfaceHandle(&va, sid, VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, 0, &desc) == VA_STATUS_SUCCESS);
        for (unsigned int i = 0; i < desc.num_objects; i++) close(desc.objects[i].fd);
    } else if (!strcmp(test, "surface-attributes-null")) {
        VAConfigID cfg = config();
        assert(v4l2r_QuerySurfaceAttributes(&va, cfg, NULL, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        unsigned int n = 0;
        assert(v4l2r_QuerySurfaceAttributes(&va, cfg, NULL, &n) == VA_STATUS_SUCCESS && n);
        VASurfaceAttrib attrs[8];
        n = 1;
        assert(v4l2r_QuerySurfaceAttributes(&va, cfg, attrs, &n) == VA_STATUS_ERROR_MAX_NUM_EXCEEDED && n == 8);
        assert(v4l2r_QuerySurfaceAttributes(&va, cfg, attrs, &n) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "config-parameters")) {
        VAConfigID out = VA_INVALID_ID;
        assert(v4l2r_CreateConfig(&va, VAProfileNone, VAEntrypointVideoProc, NULL, -1, &out) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateConfig(&va, VAProfileNone, VAEntrypointVideoProc, NULL, 1, &out) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(out == VA_INVALID_ID);
        assert(v4l2r_GetConfigAttributes(&va, VAProfileNone, VAEntrypointVideoProc, NULL, 1) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_GetConfigAttributes(&va, VAProfileNone, VAEntrypointVideoProc, NULL, -1) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_GetConfigAttributes(&va, VAProfileNone, VAEntrypointVideoProc, NULL, 0) == VA_STATUS_SUCCESS);
        VAConfigID cfg = config();
        assert(v4l2r_QueryConfigAttributes(&va, cfg, NULL, NULL, NULL, NULL) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyConfig(&va, cfg) == VA_STATUS_SUCCESS);
        assert(v4l2r_QueryConfigAttributes(&va, cfg, NULL, NULL, NULL, NULL) == VA_STATUS_ERROR_INVALID_CONFIG);
    } else if (!strcmp(test, "config-null-output")) {
        assert(v4l2r_CreateConfig(&va, VAProfileNone, VAEntrypointVideoProc, NULL, 0, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
    } else if (!strcmp(test, "config-query-null")) {
        int n;
        VAProfile profiles[V4L2R_MAX_PROFILES];
        VAEntrypoint entries[V4L2R_MAX_ENTRYPOINTS];
        assert(v4l2r_QueryConfigProfiles(&va, NULL, &n) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_QueryConfigProfiles(&va, profiles, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_QueryConfigProfiles(&va, profiles, &n) == VA_STATUS_SUCCESS && n > 0);
        assert(v4l2r_QueryConfigEntrypoints(&va, VAProfileNone, NULL, &n) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_QueryConfigEntrypoints(&va, VAProfileNone, entries, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_QueryConfigEntrypoints(&va, VAProfileNone, entries, &n) == VA_STATUS_SUCCESS && n == 1);
    } else if (!strcmp(test, "buffer-null-output")) {
        assert(v4l2r_CreateBuffer(&va, a, VAPictureParameterBufferType, 16, 1, NULL, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
    } else if (!strcmp(test, "buffer-null-map")) {
        assert(v4l2r_MapBuffer(&va, bid, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        void *ptr;
        assert(v4l2r_MapBuffer(&va, bid, &ptr) == VA_STATUS_SUCCESS && ((unsigned char *)ptr)[0] == 1);
        assert(v4l2r_UnmapBuffer(&va, bid) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "buffer-context")) {
        VABufferID out = VA_INVALID_ID;
        assert(v4l2r_CreateBuffer(&va, VA_INVALID_ID, VAPictureParameterBufferType, 16, 1, NULL, &out) == VA_STATUS_ERROR_INVALID_CONTEXT);
        assert(v4l2r_CreateBuffer(&va, sid, VASliceDataBufferType, 16, 1, NULL, &out) == VA_STATUS_ERROR_INVALID_CONTEXT);
        assert(v4l2r_CreateBuffer(&va, a, VASliceDataBufferType, 0, 1, NULL, &out) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateBuffer(&va, a, VASliceDataBufferType, 16, 0, NULL, &out) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(out == VA_INVALID_ID);
        assert(v4l2r_DestroyBuffer(&va, bid) == VA_STATUS_SUCCESS);
        assert(v4l2r_MapBuffer(&va, bid, (void **)&out) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(v4l2r_DestroyBuffer(&va, bid) == VA_STATUS_ERROR_INVALID_BUFFER);
        VABufferID replacement = buffer(a);
        picture(a, sid, replacement);
    } else if (!strcmp(test, "buffer-owner")) {
        VABufferID foreign = buffer(b);
        assert(table.vaBeginPicture(&va, a, sid) == VA_STATUS_SUCCESS);
        assert(table.vaRenderPicture(&va, a, &foreign, 1) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(!renders && !submissions);
        assert(table.vaEndPicture(&va, a) == VA_STATUS_ERROR_INVALID_BUFFER);
        picture(a, sid, bid);
        VASurfaceID other = surface();
        picture(b, other, foreign);
    } else if (!strcmp(test, "buffer-recycled-context")) {
        assert(table.vaDestroyContext(&va, a) == VA_STATUS_SUCCESS);
        VAContextID replacement = context();
        /* A numeric context ID may be reissued; old buffers must not follow it. */
        assert(replacement == a);
        assert(table.vaBeginPicture(&va, replacement, sid) == VA_STATUS_SUCCESS);
        assert(table.vaRenderPicture(&va, replacement, &bid, 1) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(!renders && !submissions);
        assert(table.vaEndPicture(&va, replacement) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(v4l2r_DestroyBuffer(&va, bid) == VA_STATUS_SUCCESS);
        picture(replacement, sid, buffer(replacement));
    } else if (!strcmp(test, "image-buffer-lifetime")) {
        VAImage image;
        assert(v4l2r_CreateImage(&va, &format, 64, 64, &image) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyBuffer(&va, image.buf) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(v4l2r_BufferSetNumElements(&va, image.buf, 2) == VA_STATUS_ERROR_INVALID_BUFFER);
        void *ptr;
        assert(v4l2r_MapBuffer(&va, image.buf, &ptr) == VA_STATUS_SUCCESS);
        assert(v4l2r_UnmapBuffer(&va, image.buf) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va, image.image_id) == VA_STATUS_SUCCESS);
        assert(v4l2r_MapBuffer(&va, image.buf, &ptr) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(v4l2r_DestroyImage(&va, image.image_id) == VA_STATUS_ERROR_INVALID_IMAGE);
        assert(v4l2r_MapBuffer(&va, bid, &ptr) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "image-derived-null")) {
        backing(sid);
        assert(table.vaDeriveImage(&va, sid, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        VAImage image;
        assert(table.vaDeriveImage(&va, sid, &image) == VA_STATUS_SUCCESS);
        void *ptr;
        assert(v4l2r_MapBuffer(&va, image.buf, &ptr) == VA_STATUS_SUCCESS && ((unsigned char *)ptr)[0] == 0x42);
        assert(v4l2r_DestroyImage(&va, image.image_id) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "image-query-null")) {
        VAImageFormat formats[V4L2R_MAX_IMAGE_FORMATS];
        int n;
        assert(v4l2r_QueryImageFormats(&va, NULL, &n) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_QueryImageFormats(&va, formats, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_QueryImageFormats(&va, formats, &n) == VA_STATUS_SUCCESS && n == 2);
    } else if (!strcmp(test, "context-abort-reuse")) {
        assert(table.vaBeginPicture(&va, a, sid) == VA_STATUS_SUCCESS);
        assert(table.vaDestroyContext(&va, a) == VA_STATUS_SUCCESS);
        assert(table.vaSyncSurface(&va, sid) == VA_STATUS_ERROR_OPERATION_FAILED);
        assert(table.vaEndPicture(&va, a) == VA_STATUS_ERROR_INVALID_CONTEXT);
        assert(table.vaDestroyContext(&va, a) == VA_STATUS_ERROR_INVALID_CONTEXT);
        VAContextID replacement = context();
        picture(replacement, sid, buffer(replacement));
        assert(table.vaSyncSurface(&va, sid) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "context-isolation")) {
        VASurfaceID other = surface();
        VABufferID other_buffer = buffer(b);
        picture(a, sid, bid);
        picture(b, other, other_buffer);
        assert(table.vaDestroyContext(&va, a) == VA_STATUS_SUCCESS);
        assert(!V4L2R_SURFACE(drv, sid)->ctx);
        assert(V4L2R_SURFACE(drv, other)->ctx == V4L2R_CONTEXT(drv, b));
        assert(table.vaSyncSurface(&va, sid) == VA_STATUS_SUCCESS);
        picture(b, other, other_buffer);
        assert(table.vaDestroySurfaces(&va, &sid, 1) == VA_STATUS_SUCCESS);
    } else {
        assert(!"unknown case");
    }
    assert(v4l2r_Terminate(&va) == VA_STATUS_SUCCESS && !va.pDriverData);
    return 0;
}
