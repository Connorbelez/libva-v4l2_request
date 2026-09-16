/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Exercise the real VA entrypoints with an in-memory V4L2 device. No hardware. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/media.h>
#include <va/va_drmcommon.h>
#include "v4l2_request.h"

static struct v4l2r_driver drv;
static struct v4l2r_context ctx;
static struct VADriverContext va;
static struct v4l2r_surface *surface;
static VASurfaceID sid;
static int capture_available, capture_flags, poll_ready = 1, queued;
static int output_available, output_index;
static short poll_extra;
static unsigned char pixels[64 * 6];
static VAStatus failed_flush(struct v4l2r_context *context, struct v4l2r_surface *target)
{
    (void)context; (void)target;
    return VA_STATUS_ERROR_INVALID_BUFFER;
}

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    if (request == MEDIA_REQUEST_IOC_REINIT || request == MEDIA_REQUEST_IOC_QUEUE ||
        request == VIDIOC_STREAMOFF)
        return 0;
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (request == VIDIOC_DQBUF) {
        struct v4l2_buffer *b = arg;
        if (V4L2_TYPE_IS_OUTPUT(b->type) && output_available) {
            output_available = 0;
            b->index = output_index;
            return 0;
        }
        if (V4L2_TYPE_IS_OUTPUT(b->type) || !capture_available) {
            errno = EAGAIN;
            return -1;
        }
        capture_available = 0;
        b->index = 0;
        b->flags = capture_flags;
        return 0;
    }
    if (request == VIDIOC_QBUF) {
        queued++;
        return 0;
    }
    fprintf(stderr, "unexpected ioctl %#lx\n", request);
    abort();
}

int __wrap_poll(struct pollfd *fds, nfds_t count, int timeout)
{
    (void)timeout;
    for (nfds_t i = 0; i < count; i++)
        fds[i].revents = poll_ready ? fds[i].events | poll_extra : 0;
    return poll_ready ? (int)count : 0;
}

/* Fortified glibc builds may call this instead of the public poll symbol. */
int __wrap___poll_chk(struct pollfd *fds, nfds_t count, int timeout, size_t size)
{
    assert(count <= size / sizeof(*fds));
    return __wrap_poll(fds, count, timeout);
}

static void setup(void)
{
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!pthread_mutex_init(&drv.api_mutex, NULL));
    assert(!pthread_mutex_init(&ctx.mutex, NULL));
    assert(!v4l2r_handles_init(&drv.surfaces, V4L2R_ID_OFFSET_SURFACE));
    assert(!v4l2r_handles_init(&drv.images, V4L2R_ID_OFFSET_IMAGE));
    assert(!v4l2r_handles_init(&drv.buffers, V4L2R_ID_OFFSET_BUFFER));
    assert(!v4l2r_handles_init(&drv.contexts, V4L2R_ID_OFFSET_CONTEXT));
    va.pDriverData = &drv;
    assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 3, 3,
                               &sid, 1, NULL, 0) == VA_STATUS_SUCCESS);
    surface = V4L2R_SURFACE(&drv, sid);
    surface->ctx = &ctx;
    surface->capture_index = 0;
    ctx.drv = &drv;
    ctx.video_fd = 100;
    ctx.streaming = true;
    ctx.nb_captures = 1;
    ctx.capture_memory = V4L2_MEMORY_MMAP;
    ctx.capture_format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    ctx.capture_format.fmt.pix_mp = (struct v4l2_pix_format_mplane) {
        .width = 4, .height = 4, .pixelformat = V4L2_PIX_FMT_NV12,
        .num_planes = 1, .plane_fmt = {{ .bytesperline = 64, .sizeimage = sizeof(pixels) }},
    };
    ctx.captures[0].surface = surface;
    ctx.captures[0].nb_planes = 1;
    ctx.captures[0].plane_size[0] = sizeof(pixels);
    ctx.captures[0].map[0] = pixels;
    for (unsigned int i = 0; i < VIDEO_MAX_PLANES; i++)
        ctx.captures[0].dmabuf_fd[i] = -1;
    ctx.captures[0].dmabuf_fd[0] = 101;
    ctx.output_format.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    for (unsigned i = 0; i < sizeof(pixels); i++)
        pixels[i] = (unsigned char)i;
}

static void teardown(void)
{
    v4l2r_handles_destroy(&drv.surfaces);
    v4l2r_handles_destroy(&drv.images);
    v4l2r_handles_destroy(&drv.buffers);
    v4l2r_handles_destroy(&drv.contexts);
    pthread_mutex_destroy(&ctx.mutex);
    pthread_mutex_destroy(&drv.mutex);
    pthread_mutex_destroy(&drv.api_mutex);
}

int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    assert(argc == 2);
    setup();
    if (!strcmp(argv[1], "decode-error") || !strcmp(argv[1], "export-error") ||
        !strcmp(argv[1], "submission-error")) {
        ctx.queued_capture = 1;
        ctx.submitted = 1;
        surface->status = VASurfaceRendering;
        capture_available = 1;
        capture_flags = V4L2_BUF_FLAG_ERROR;
        VAStatus expected = VA_STATUS_ERROR_DECODING_ERROR;
        if (!strcmp(argv[1], "submission-error")) {
            /* An earlier slice completes successfully after EndPicture
             * rejected the incomplete picture. It must remain failed. */
            capture_flags = 0;
            surface->decode_status = expected = VA_STATUS_ERROR_INVALID_BUFFER;
        }
        if (strcmp(argv[1], "export-error")) {
            assert(v4l2r_SyncSurface(&va, sid) == expected);
            assert(v4l2r_SyncSurface(&va, sid) == expected);
            assert(ctx.completed == 1 && ctx.queued_capture == 0);
            /* Failed pictures still complete and their buffer can be reused. */
            assert(v4l2r_context_bind_surface(&ctx, surface) == VA_STATUS_SUCCESS);
            unsigned char bitstream[64] = {0};
            ctx.output[0].addr = bitstream;
            ctx.pic.output = &ctx.output[0];
            ctx.pic.target = surface;
            assert(v4l2r_decode(&ctx, NULL, 0, true, true) == VA_STATUS_SUCCESS);
            capture_available = 1;
            capture_flags = 0;
            assert(v4l2r_SyncSurface(&va, sid) == VA_STATUS_SUCCESS);
        } else {
            VADRMPRIMESurfaceDescriptor desc;
            assert(v4l2r_ExportSurfaceHandle(&va, sid,
                VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, 0, &desc) ==
                VA_STATUS_ERROR_DECODING_ERROR);
        }
    } else if (!strcmp(argv[1], "reuse-timeout") || !strcmp(argv[1], "reader-timeout") ||
               !strcmp(argv[1], "refwait-timeout")) {
        poll_ready = 0;
        if (!strcmp(argv[1], "reuse-timeout"))
            ctx.queued_capture = 1;
        if (!strcmp(argv[1], "refwait-timeout")) {
            ctx.queued_capture = 2;
            ctx.captures[0].last_ref_seq = 1;
        }
        assert(v4l2r_context_bind_surface(&ctx, surface) != VA_STATUS_SUCCESS);
        assert(queued == 0);
    } else if (!strcmp(argv[1], "export-timeout")) {
        poll_ready = 0;
        ctx.queued_capture = 1;
        VADRMPRIMESurfaceDescriptor desc;
        assert(v4l2r_ExportSurfaceHandle(&va, sid,
            VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, 0, &desc) == VA_STATUS_ERROR_OPERATION_FAILED);
    } else if (!strcmp(argv[1], "flush-error")) {
        struct v4l2r_codec codec = { .flush = failed_flush };
        ctx.codec = &codec;
        assert(v4l2r_SyncSurface(&va, sid) == VA_STATUS_ERROR_INVALID_BUFFER);
    } else if (!strcmp(argv[1], "image-dimensions")) {
        VAImage image;
        VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
        assert(v4l2r_CreateImage(&va, &format, -1, 3, &image) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateImage(&va, &format, 0, 3, &image) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_CreateImage(&va, &format, INT_MAX, INT_MAX, &image) == VA_STATUS_ERROR_INVALID_PARAMETER);
    } else if (!strcmp(argv[1], "odd-image-copy")) {
        VAImage image;
        VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
        assert(v4l2r_CreateImage(&va, &format, 3, 3, &image) == VA_STATUS_SUCCESS);
        struct v4l2r_buffer *b = V4L2R_BUFFER(&drv, image.buf);
        memset(b->data, 0, image.data_size);
        assert(v4l2r_GetImage(&va, sid, 0, 0, 3, 3, image.image_id) == VA_STATUS_SUCCESS);
        /* Odd widths still need both components of the last chroma pair. */
        assert(!memcmp((char *)b->data + image.offsets[1], pixels + 64 * 4, 4));
        assert(v4l2r_DestroyImage(&va, image.image_id) == VA_STATUS_SUCCESS);
#if HAVE_V4L2_PIX_FMT_P010
    } else if (!strcmp(argv[1], "image-bounds")) {
        VAImage image;
        VAImageFormat format = { .fourcc = VA_FOURCC_P010 };
        ctx.capture_format.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_P010;
        assert(v4l2r_CreateImage(&va, &format, 3, 3, &image) == VA_STATUS_SUCCESS);
        struct v4l2r_buffer *b = V4L2R_BUFFER(&drv, image.buf);
        memset(b->data, 0, image.data_size);
        assert(v4l2r_GetImage(&va, sid, 0, 0, 3, 3, image.image_id) == VA_STATUS_SUCCESS);
        assert(!memcmp((char *)b->data + image.offsets[1], pixels + 64 * 4, 8));
        ctx.captures[0].plane_size[0] = 64 * 4 + 1;
        assert(v4l2r_GetImage(&va, sid, 0, 0, 3, 3, image.image_id) != VA_STATUS_SUCCESS);
        assert(v4l2r_PutImage(&va, sid, image.image_id, 0, 0, 3, 3, 0, 0, 3, 3) != VA_STATUS_SUCCESS);
        ctx.captures[0].plane_size[0] = sizeof(pixels);
        b->element_size = 1;
        assert(v4l2r_GetImage(&va, sid, 0, 0, 3, 3, image.image_id) != VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va, image.image_id) == VA_STATUS_SUCCESS);
#endif
    } else if (!strcmp(argv[1], "buffer-zero")) {
        VABufferID id;
        assert(v4l2r_CreateBuffer(&va, VA_INVALID_ID, VAImageBufferType, 16, 1, NULL, &id) == VA_STATUS_SUCCESS);
        assert(v4l2r_BufferSetNumElements(&va, id, 0) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_DestroyBuffer(&va, id) == VA_STATUS_SUCCESS);
    } else if (!strcmp(argv[1], "output-grown")) {
        output_available = 1;
        output_index = 9;
        ctx.output[0].index = output_index;
        ctx.queued_output = 1u << output_index;
        poll_ready = 0;
        assert(v4l2r_picture_begin(&ctx, surface) == VA_STATUS_SUCCESS);
        assert(ctx.queued_output == 0);
    } else if (!strcmp(argv[1], "request-timeout")) {
        ctx.queued_request = 1;
        poll_ready = 0;
        ctx.captures[0].dmabuf_fd[0] = -1;
        ctx.pic.output = &ctx.output[0];
        ctx.pic.target = surface;
        assert(v4l2r_decode(&ctx, NULL, 0, true, true) != VA_STATUS_SUCCESS);
        assert(queued == 0 && ctx.queued_request == 1);
    } else if (!strcmp(argv[1], "invalid-poll")) {
        poll_extra = POLLNVAL;
        ctx.queued_capture = 1;
        assert(v4l2r_SyncSurface(&va, sid) == VA_STATUS_ERROR_OPERATION_FAILED);
        assert(v4l2r_wait_completed(&ctx, 1) == VA_STATUS_ERROR_OPERATION_FAILED);
        ctx.queued_capture = 0;
        ctx.queued_output = 1;
        assert(v4l2r_picture_begin(&ctx, surface) == VA_STATUS_ERROR_OPERATION_FAILED);
        ctx.queued_output = 0;
        ctx.queued_request = 1;
        ctx.captures[0].dmabuf_fd[0] = -1;
        ctx.pic.output = &ctx.output[0];
        ctx.pic.target = surface;
        assert(v4l2r_decode(&ctx, NULL, 0, true, true) != VA_STATUS_SUCCESS);
        assert(queued == 0 && ctx.queued_request == 1);
    } else if (!strcmp(argv[1], "bitstream-overflow")) {
        ctx.pic.output = &ctx.output[0];
        assert(v4l2r_append_output(&ctx, pixels, SIZE_MAX) == VA_STATUS_ERROR_INVALID_BUFFER);
        ctx.output[0].bytesused = UINT32_MAX - 1;
        assert(v4l2r_append_output(&ctx, pixels, 1) == VA_STATUS_ERROR_INVALID_BUFFER);
    } else if (!strcmp(argv[1], "derive-bounds")) {
        VAImage image;
        ctx.captures[0].plane_size[0] = 1;
        assert(v4l2r_DeriveImage(&va, sid, &image) == VA_STATUS_ERROR_OPERATION_FAILED);
    } else if (!strcmp(argv[1], "context-lifetime") ||
               !strcmp(argv[1], "context-error") || !strcmp(argv[1], "context-timeout")) {
        struct VADriverVTable table = {0};
        v4l2r_lock_surface_api(&table);
        VAContextID cid = v4l2r_handles_alloc(&drv.contexts, sizeof(ctx));
        struct v4l2r_context *life = V4L2R_CONTEXT(&drv, cid);
        assert(life);
        assert(!pthread_mutex_init(&life->mutex, NULL));
        life->drv = &drv;
        life->streaming = true;
        life->video_fd = -1;
        life->media_fd = -1;
        life->capture_format = ctx.capture_format;
        life->capture_memory = V4L2_MEMORY_MMAP;
        life->nb_captures = 1;
        life->captures[0] = ctx.captures[0];
        for (unsigned int i = 0; i < V4L2R_OUTPUT_BUFFERS; i++)
            life->output[i].request_fd = -1;
        int fd = memfd_create("frame", MFD_CLOEXEC);
        assert(fd >= 0 && !ftruncate(fd, sizeof(pixels)));
        void *mapping = mmap(NULL, sizeof(pixels), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        assert(mapping != MAP_FAILED);
        memcpy(mapping, pixels, sizeof(pixels));
        life->captures[0].dmabuf_fd[0] = fd;
        life->captures[0].map[0] = mapping;
        surface->ctx = life;
        VAImage derived, copied;
        VAImageFormat format = { .fourcc = VA_FOURCC_NV12 };
        assert(table.vaDeriveImage(&va, sid, &derived) == VA_STATUS_SUCCESS);
        life->queued_capture = 1;
        life->submitted = 1;
        surface->status = VASurfaceRendering;
        capture_available = 1;
        VAStatus expected = VA_STATUS_SUCCESS;
        if (!strcmp(argv[1], "context-error")) {
            capture_flags = V4L2_BUF_FLAG_ERROR;
            expected = VA_STATUS_ERROR_DECODING_ERROR;
        } else if (!strcmp(argv[1], "context-timeout")) {
            capture_available = poll_ready = 0;
            expected = VA_STATUS_ERROR_OPERATION_FAILED;
        }
        assert(table.vaDestroyContext(&va, cid) == VA_STATUS_SUCCESS);
        assert(!surface->ctx && surface->backing);
        for (unsigned int i = 1; i < VIDEO_MAX_PLANES; i++)
            assert(surface->backing->dmabuf_fd[i] == -1);
        assert(surface->status == VASurfaceReady);
        assert(table.vaSyncSurface(&va, sid) == expected);
        assert(v4l2r_CreateImage(&va, &format, 3, 3, &copied) == VA_STATUS_SUCCESS);
        assert(table.vaGetImage(&va, sid, 0, 0, 3, 3, copied.image_id) == expected);
        void *map;
        assert(v4l2r_MapBuffer(&va, derived.buf, &map) == VA_STATUS_SUCCESS);
        assert(!memcmp(map, pixels, 3));
        assert(v4l2r_DestroyImage(&va, derived.image_id) == VA_STATUS_SUCCESS);
        assert(v4l2r_DestroyImage(&va, copied.image_id) == VA_STATUS_SUCCESS);
        assert(table.vaDestroySurfaces(&va, &sid, 1) == VA_STATUS_SUCCESS);
    } else {
        abort();
    }
    teardown();
    return 0;
}
