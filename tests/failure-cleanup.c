/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Deterministic userspace failures against a model V4L2 device. No hardware. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>
#include <linux/media.h>
#include <va/va_drmcommon.h>
#include "v4l2_request.h"

#define LIMIT 1024
#define FD_BASE 1000
enum kind { VIDEO = 1, MEDIA, REQUEST, DMABUF, CONVERTER };
static struct {
    enum kind kind;
    bool live;
    unsigned int outputs, captures;
    uint32_t output_queued, output_ready;
    uint64_t capture_queued, capture_ready;
    int request_output, request_video;
    bool request_done;
    struct v4l2_format formats[2];
} fds[LIMIT];
static struct { void *ptr; size_t size; } maps[LIMIT], heaps[LIMIT];
static unsigned int fd_count, op, fail_at, injected, queues;
static bool armed, packed, single_plane, hold, dequeue_blocked;
static unsigned long fail_ioctl;
static int fail_type = -1, fail_errno = EIO;
static unsigned int fail_ioctl_nth, ioctl_matches;
static int capture_flags, capture_bad_index = -1, output_bad_index = -1;
static int poll_error, poll_timeouts;
static short poll_events;
static const char *operations[LIMIT];
static struct VADriverContext va;
static struct VADriverVTable table;
static struct v4l2r_driver *drv;
static VAConfigID cfg;

void *__real_malloc(size_t size);
void *__real_calloc(size_t n, size_t size);
void *__real_realloc(void *ptr, size_t size);
void __real_free(void *ptr);
void *__real_mmap(void *addr, size_t len, int prot, int flags, int fd, off_t offset);
int __real_munmap(void *addr, size_t len);

static bool fault(const char *name)
{
    if (!armed) return false;
    assert(op + 1 < LIMIT);
    operations[++op] = name;
    if (fail_at && op == fail_at) {
        injected++;
        errno = !strcmp(name, "calloc") || !strcmp(name, "malloc") ||
                !strcmp(name, "realloc") || !strcmp(name, "mmap") ? ENOMEM : EIO;
        return true;
    }
    return false;
}
static unsigned int heap_slot(void *ptr)
{
    for (unsigned int i = 0; i < LIMIT; i++) if (heaps[i].ptr == ptr) return i;
    assert(!"untracked heap pointer");
    return 0;
}
static void track_heap(void *ptr, size_t size)
{
    if (!ptr) return;
    unsigned int i = heap_slot(NULL);
    heaps[i].ptr = ptr; heaps[i].size = size;
}
void *__wrap_malloc(size_t size)
{
    if (fault("malloc")) return NULL;
    void *p = __real_malloc(size); track_heap(p, size); return p;
}
void *__wrap_calloc(size_t n, size_t size)
{
    if (fault("calloc")) return NULL;
    void *p = __real_calloc(n, size); track_heap(p, n * size); return p;
}
void *__wrap_realloc(void *ptr, size_t size)
{
    if (fault("realloc")) return NULL;
    unsigned int i = heap_slot(ptr);
    void *p = __real_realloc(ptr, size);
    if (p) { heaps[i].ptr = p; heaps[i].size = size; }
    return p;
}
void __wrap_free(void *ptr)
{
    if (!ptr) return;
    heaps[heap_slot(ptr)].ptr = NULL;
    __real_free(ptr);
}
static int new_fd(enum kind kind)
{
    assert(fd_count < LIMIT);
    unsigned int n = fd_count++;
    memset(&fds[n], 0, sizeof(fds[n]));
    fds[n].kind = kind; fds[n].live = true;
    fds[n].request_output = fds[n].request_video = -1;
    return FD_BASE + (int)n;
}
static unsigned int fd_slot(int fd)
{
    assert(fd >= FD_BASE && (unsigned int)(fd - FD_BASE) < fd_count);
    assert(fds[fd - FD_BASE].live);
    return (unsigned int)(fd - FD_BASE);
}
int __wrap_open(const char *path, int flags, ...)
{
    (void)flags;
    if (fault("open")) return -1;
    if (!strcmp(path, "model-video")) return new_fd(VIDEO);
    if (!strcmp(path, "model-media")) return new_fd(MEDIA);
    assert(!strcmp(path, "model-converter"));
    return new_fd(CONVERTER);
}
int __wrap_close(int fd)
{
    fds[fd_slot(fd)].live = false;
    return 0;
}
int __wrap_dup(int fd)
{
    unsigned int i = fd_slot(fd);
    if (fault("dup")) return -1;
    return new_fd(fds[i].kind);
}
int __wrap_fcntl(int fd, int cmd, ...)
{
    unsigned int i = fd_slot(fd);
    assert(cmd == F_DUPFD_CLOEXEC);
    if (fault("fcntl-dup")) return -1;
    return new_fd(fds[i].kind);
}
int __wrap_open64(const char *path, int flags, ...) { return __wrap_open(path, flags); }
int __wrap___open_2(const char *path, int flags) { return __wrap_open(path, flags); }
int __wrap___open64_2(const char *path, int flags) { return __wrap_open(path, flags); }
int __wrap_fcntl64(int fd, int cmd, ...) { return __wrap_fcntl(fd, cmd); }
void *__wrap_mmap(void *addr, size_t len, int prot, int flags, int fd, off_t offset)
{
    (void)addr; (void)flags; (void)offset;
    fd_slot(fd);
    if (fault("mmap")) return MAP_FAILED;
    void *p = __real_mmap(NULL, len, prot, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(p != MAP_FAILED);
    for (unsigned int i = 0; i < LIMIT; i++) if (!maps[i].ptr) {
        maps[i].ptr = p; maps[i].size = len; return p;
    }
    abort();
}
void *__wrap_mmap64(void *addr, size_t len, int prot, int flags, int fd, off_t offset)
{
    return __wrap_mmap(addr, len, prot, flags, fd, offset);
}
int __wrap_munmap(void *addr, size_t len)
{
    for (unsigned int i = 0; i < LIMIT; i++) if (maps[i].ptr == addr) {
        assert(maps[i].size == len); maps[i].ptr = NULL;
        return __real_munmap(addr, len);
    }
    assert(!"untracked mapping"); return -1;
}
static void format_fill(struct v4l2_format *f)
{
    if (V4L2_TYPE_IS_MULTIPLANAR(f->type)) {
        if (!f->fmt.pix_mp.width) f->fmt.pix_mp.width = 64;
        if (!f->fmt.pix_mp.height) f->fmt.pix_mp.height = 64;
        if (!f->fmt.pix_mp.pixelformat) f->fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
        f->fmt.pix_mp.num_planes = 1;
        f->fmt.pix_mp.plane_fmt[0].bytesperline = 64;
        if (!f->fmt.pix_mp.plane_fmt[0].sizeimage)
            f->fmt.pix_mp.plane_fmt[0].sizeimage = 64 * 96;
    } else {
        if (!f->fmt.pix.width) f->fmt.pix.width = 64;
        if (!f->fmt.pix.height) f->fmt.pix.height = 64;
        if (!f->fmt.pix.pixelformat) f->fmt.pix.pixelformat = V4L2_PIX_FMT_NV12;
        f->fmt.pix.bytesperline = 64;
        if (!f->fmt.pix.sizeimage) f->fmt.pix.sizeimage = 64 * 96;
    }
}
int __wrap_ioctl(int fd, unsigned long request, ...)
{
    unsigned int i = fd_slot(fd);
    void *arg = NULL;
    if (request != MEDIA_REQUEST_IOC_REINIT && request != MEDIA_REQUEST_IOC_QUEUE) {
        va_list ap; va_start(ap, request); arg = va_arg(ap, void *); va_end(ap);
    }
    int type = -1;
    if (request == VIDIOC_QBUF || request == VIDIOC_DQBUF)
        type = V4L2_TYPE_IS_OUTPUT(((struct v4l2_buffer *)arg)->type);
    const char *name = "ioctl";
#define OP(x) if (request == x) name = #x
    OP(VIDIOC_QUERYCAP); OP(VIDIOC_ENUM_FMT); OP(VIDIOC_ENUM_FRAMESIZES);
    OP(VIDIOC_S_FMT); OP(VIDIOC_G_FMT); OP(VIDIOC_CREATE_BUFS);
    OP(VIDIOC_QUERYBUF); OP(MEDIA_IOC_REQUEST_ALLOC); OP(VIDIOC_REQBUFS);
    OP(VIDIOC_STREAMON); OP(VIDIOC_STREAMOFF); OP(VIDIOC_S_EXT_CTRLS);
    OP(VIDIOC_QUERY_EXT_CTRL); OP(VIDIOC_QBUF); OP(VIDIOC_DQBUF);
    OP(VIDIOC_EXPBUF); OP(MEDIA_REQUEST_IOC_REINIT); OP(MEDIA_REQUEST_IOC_QUEUE);
    OP(VIDIOC_S_CTRL); OP(VIDIOC_S_SELECTION);
#undef OP
    if (fault(name)) return -1;
    if (armed && request == fail_ioctl && (fail_type < 0 || type == fail_type) &&
        ++ioctl_matches == fail_ioctl_nth) {
        injected++; errno = fail_errno; return -1;
    }
    if (request == VIDIOC_QUERYCAP) {
        struct v4l2_capability *c = arg;
        c->capabilities = single_plane ? V4L2_CAP_VIDEO_M2M : V4L2_CAP_VIDEO_M2M_MPLANE;
        return 0;
    }
    if (request == VIDIOC_ENUM_FMT) {
        struct v4l2_fmtdesc *f = arg;
        if (f->index) { errno = EINVAL; return -1; }
        f->pixelformat = V4L2_TYPE_IS_OUTPUT(f->type) ? V4L2_PIX_FMT_H264 : V4L2_PIX_FMT_NV12;
#ifdef V4L2_PIX_FMT_NV15
        if (packed && !V4L2_TYPE_IS_OUTPUT(f->type)) f->pixelformat = V4L2_PIX_FMT_NV15;
#endif
        return 0;
    }
    if (request == VIDIOC_ENUM_FRAMESIZES) {
        struct v4l2_frmsizeenum *f = arg;
        f->type = V4L2_FRMSIZE_TYPE_DISCRETE;
        f->discrete.width = f->discrete.height = 64; return 0;
    }
    if (request == VIDIOC_S_FMT || request == VIDIOC_G_FMT) {
        struct v4l2_format *f = arg;
        unsigned int side = V4L2_TYPE_IS_OUTPUT(f->type);
        if (request == VIDIOC_G_FMT && fds[i].formats[side].type)
            *f = fds[i].formats[side];
        format_fill(f); fds[i].formats[side] = *f; return 0;
    }
    if (request == VIDIOC_CREATE_BUFS) {
        struct v4l2_create_buffers *b = arg;
        b->capabilities = V4L2_BUF_CAP_SUPPORTS_REQUESTS;
#ifdef V4L2_BUF_CAP_SUPPORTS_M2M_HOLD_CAPTURE_BUF
        if (hold) b->capabilities |= V4L2_BUF_CAP_SUPPORTS_M2M_HOLD_CAPTURE_BUF;
#endif
        unsigned int *n = V4L2_TYPE_IS_OUTPUT(b->format.type) ? &fds[i].outputs : &fds[i].captures;
        b->index = *n; *n += b->count;
        if (b->count) fds[i].formats[V4L2_TYPE_IS_OUTPUT(b->format.type)] = b->format;
        return 0;
    }
    if (request == VIDIOC_QUERYBUF) {
        struct v4l2_buffer *b = arg;
        struct v4l2_format *f = &fds[i].formats[V4L2_TYPE_IS_OUTPUT(b->type)];
        if (V4L2_TYPE_IS_MULTIPLANAR(b->type)) {
            b->length = 1; b->m.planes[0].length = f->fmt.pix_mp.plane_fmt[0].sizeimage;
        } else b->length = f->fmt.pix.sizeimage;
        return 0;
    }
    if (request == MEDIA_IOC_REQUEST_ALLOC) { *(int *)arg = new_fd(REQUEST); return 0; }
    if (request == VIDIOC_EXPBUF) { ((struct v4l2_exportbuffer *)arg)->fd = new_fd(DMABUF); return 0; }
    if (request == VIDIOC_QUERY_EXT_CTRL) { ((struct v4l2_query_ext_ctrl *)arg)->default_value = 1; return 0; }
    if (request == VIDIOC_QBUF) {
        struct v4l2_buffer *b = arg;
        assert(b->index < 32); queues++;
        if (type) {
            assert(!(fds[i].output_queued & (1u << b->index)));
            fds[i].output_queued |= 1u << b->index;
            if (fds[i].kind == VIDEO) {
                unsigned int req = fd_slot(b->request_fd);
                fds[req].request_video = (int)i; fds[req].request_output = (int)b->index;
                /* Keep the target and final-slice bit with the request. */
                fds[req].captures = (unsigned int)b->timestamp.tv_usec - 1;
#ifdef V4L2_BUF_FLAG_M2M_HOLD_CAPTURE_BUF
                fds[req].outputs = !(b->flags & V4L2_BUF_FLAG_M2M_HOLD_CAPTURE_BUF);
#else
                fds[req].outputs = 1;
#endif
            } else fds[i].output_ready |= 1u << b->index;
        } else {
            assert(!(fds[i].capture_queued & (UINT64_C(1) << b->index)));
            fds[i].capture_queued |= UINT64_C(1) << b->index;
            if (fds[i].kind == CONVERTER) fds[i].capture_ready |= UINT64_C(1) << b->index;
        }
        return 0;
    }
    if (request == MEDIA_REQUEST_IOC_QUEUE) {
        assert(fds[i].request_video >= 0 && fds[i].request_output >= 0);
        unsigned int v = (unsigned int)fds[i].request_video;
        fds[v].output_ready |= 1u << fds[i].request_output;
        if (fds[i].outputs) fds[v].capture_ready |= UINT64_C(1) << fds[i].captures;
        fds[i].request_done = true; return 0;
    }
    if (request == MEDIA_REQUEST_IOC_REINIT) {
        if (fds[i].request_video >= 0 && fds[i].request_output >= 0) {
            unsigned int v = (unsigned int)fds[i].request_video;
            fds[v].output_queued &= ~(1u << fds[i].request_output);
            fds[v].output_ready &= ~(1u << fds[i].request_output);
        }
        fds[i].request_done = false; return 0;
    }
    if (request == VIDIOC_DQBUF) {
        struct v4l2_buffer *b = arg;
        uint64_t ready = type ? fds[i].output_ready : fds[i].capture_ready;
        if (!ready || dequeue_blocked) { errno = EAGAIN; return -1; }
        unsigned int n = (unsigned int)__builtin_ctzll(ready);
        if (type) {
            fds[i].output_ready &= ~(1u << n); fds[i].output_queued &= ~(1u << n);
            b->index = output_bad_index < 0 ? n : (unsigned int)output_bad_index;
        } else {
            fds[i].capture_ready &= ~(UINT64_C(1) << n); fds[i].capture_queued &= ~(UINT64_C(1) << n);
            b->index = capture_bad_index < 0 ? n : (unsigned int)capture_bad_index;
            b->flags = (unsigned int)capture_flags;
        }
        return 0;
    }
    if (request == VIDIOC_STREAMOFF) {
        if (V4L2_TYPE_IS_OUTPUT(*(enum v4l2_buf_type *)arg))
            fds[i].output_queued = fds[i].output_ready = 0;
        else fds[i].capture_queued = fds[i].capture_ready = 0;
        return 0;
    }
    if (request == VIDIOC_REQBUFS || request == VIDIOC_STREAMON ||
        request == VIDIOC_S_EXT_CTRLS || request == VIDIOC_S_CTRL || request == VIDIOC_S_SELECTION)
        return 0;
    fprintf(stderr, "unmodelled ioctl %s %#lx\n", name, request); abort();
}
int __wrap_poll(struct pollfd *p, nfds_t n, int timeout)
{
    (void)timeout;
    assert(n == 1); fd_slot(p[0].fd);
    if (fault("poll")) return -1;
    if (poll_error) { errno = poll_error; poll_error = 0; return -1; }
    if (poll_timeouts) { poll_timeouts--; return 0; }
    unsigned int i = fd_slot(p[0].fd);
    if (fds[i].kind == VIDEO && !fds[i].output_ready && !fds[i].capture_ready) return 0;
    p[0].revents = poll_events ? poll_events : p[0].events;
    return 1;
}
int __wrap___poll_chk(struct pollfd *p, nfds_t n, int timeout, size_t size)
{
    assert(n <= size / sizeof(*p)); return __wrap_poll(p, n, timeout);
}

static VAStatus codec_init(struct v4l2r_context *ctx)
{
    int64_t value;
    return v4l2r_query_control_default(ctx, 1, &value) < 0 ?
        VA_STATUS_ERROR_OPERATION_FAILED : VA_STATUS_SUCCESS;
}
static VAStatus codec_end(struct v4l2r_context *ctx)
{
    unsigned char data[64] = {0};
    VAStatus status = v4l2r_append_output(ctx, data, sizeof(data));
    if (status != VA_STATUS_SUCCESS) return status;
    struct v4l2_ext_control control = {.id = 1, .value = 1};
    return v4l2r_decode(ctx, &control, 1, true, true);
}
static const struct v4l2r_codec codec = {
    .name = "model", .pixelformat = V4L2_PIX_FMT_H264, .priv_size = 16,
    .init = codec_init, .end_picture = codec_end,
};
static void setup(void)
{
    drv = calloc(1, sizeof(*drv)); assert(drv); va.pDriverData = drv;
    assert(!pthread_mutex_init(&drv->mutex, NULL));
    assert(!pthread_mutex_init(&drv->api_mutex, NULL));
    assert(!v4l2r_handles_init(&drv->configs, V4L2R_ID_OFFSET_CONFIG));
    assert(!v4l2r_handles_init(&drv->contexts, V4L2R_ID_OFFSET_CONTEXT));
    assert(!v4l2r_handles_init(&drv->surfaces, V4L2R_ID_OFFSET_SURFACE));
    assert(!v4l2r_handles_init(&drv->buffers, V4L2R_ID_OFFSET_BUFFER));
    assert(!v4l2r_handles_init(&drv->images, V4L2R_ID_OFFSET_IMAGE));
    drv->nb_decoders = 1;
    strcpy(drv->decoders[0].video_path, "model-video");
    strcpy(drv->decoders[0].media_path, "model-media");
    drv->decoders[0].nb_pixelformats = 1;
    drv->decoders[0].pixelformats[0] = V4L2_PIX_FMT_H264;
    strcpy(drv->converter.video_path, "model-converter");
    drv->converter_probed = drv->has_converter = true;
    drv->converter.nb_pixelformats = 1;
    drv->converter.pixelformats[0] = V4L2_PIX_FMT_NV12;
    cfg = v4l2r_handles_alloc(&drv->configs, sizeof(struct v4l2r_config));
    assert(cfg != VA_INVALID_ID);
    V4L2R_CONFIG(drv, cfg)->codec = &codec;
    V4L2R_CONFIG(drv, cfg)->profile = VAProfileH264Main;
    v4l2r_lock_surface_api(&table);
}
static VASurfaceID surface(void)
{
    VASurfaceID sid;
    assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, &sid, 1, NULL, 0) == VA_STATUS_SUCCESS);
    return sid;
}
static VAContextID context(void)
{
    VAContextID id;
    assert(table.vaCreateContext(&va, cfg, 64, 64, 0, NULL, 0, &id) == VA_STATUS_SUCCESS);
    return id;
}
static VAStatus picture(VAContextID id, VASurfaceID sid)
{
    VAStatus status = table.vaBeginPicture(&va, id, sid);
    return status == VA_STATUS_SUCCESS ? table.vaEndPicture(&va, id) : status;
}
static void teardown(void)
{
    armed = false;
    assert(v4l2r_Terminate(&va) == VA_STATUS_SUCCESS);
    for (unsigned int i = 0; i < LIMIT; i++) {
        assert(!fds[i].live && !maps[i].ptr && !heaps[i].ptr);
    }
    memset(fds, 0, sizeof(fds)); fd_count = 0;
}
static void inject(unsigned long request, int type, unsigned int nth, int error)
{
    fail_ioctl = request; fail_type = type; fail_ioctl_nth = nth;
    fail_errno = error; ioctl_matches = injected = op = 0; armed = true;
}
static void failed_surface(VASurfaceID sid)
{
    assert(table.vaSyncSurface(&va, sid) != VA_STATUS_SUCCESS);
    assert(table.vaSyncSurface(&va, sid) != VA_STATUS_SUCCESS);
    VADRMPRIMESurfaceDescriptor desc;
    assert(table.vaExportSurfaceHandle(&va, sid, VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, 0, &desc) != VA_STATUS_SUCCESS);
    VAImage image;
    assert(table.vaDeriveImage(&va, sid, &image) != VA_STATUS_SUCCESS);
}
int main(int argc, char **argv)
{
    struct rlimit core = {0, 0}; assert(!setrlimit(RLIMIT_CORE, &core));
    assert(argc == 2); setup();
    const char *test = argv[1];
    VAContextID id = context(); VASurfaceID sid = surface();
    struct v4l2r_context *ctx = V4L2R_CONTEXT(drv, id);
    if (!strcmp(test, "control")) {
        assert(picture(id, sid) == VA_STATUS_SUCCESS);
        assert(table.vaSyncSurface(&va, sid) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "capture-queue") || !strcmp(test, "request-queue")) {
        inject(!strcmp(test, "capture-queue") ? VIDIOC_QBUF : MEDIA_REQUEST_IOC_QUEUE,
               !strcmp(test, "capture-queue") ? 0 : -1, 1, EIO);
        assert(picture(id, sid) != VA_STATUS_SUCCESS && injected == 1);
        armed = false; failed_surface(sid);
        if (!strcmp(test, "capture-queue")) {
            /* An idle request can be reinitialized. Exercise a full ring
             * twice: without release the stranded OUTPUT slot times out. */
            for (unsigned int n = 0; n < 2 * V4L2R_OUTPUT_BUFFERS; n++) {
                VASurfaceID next = surface();
                assert(picture(id, next) == VA_STATUS_SUCCESS);
                assert(table.vaSyncSurface(&va, next) == VA_STATUS_SUCCESS);
            }
        } else {
            unsigned int before = queues;
            /* CAPTURE already belongs to the queue: don't allow a later
             * request to consume the failed frame's destination. */
            assert(picture(id, surface()) != VA_STATUS_SUCCESS && queues == before);
        }
        assert(table.vaDestroyContext(&va, id) == VA_STATUS_SUCCESS);
        id = context(); assert(picture(id, surface()) == VA_STATUS_SUCCESS);
    } else if (!strcmp(test, "dequeue-error")) {
        assert(picture(id, sid) == VA_STATUS_SUCCESS);
        inject(VIDIOC_DQBUF, 0, 1, EIO);
        failed_surface(sid); assert(injected == 1);
    } else if (!strcmp(test, "poll-interrupted")) {
        poll_error = EINTR;
        assert(v4l2r_poll_one(ctx->media_fd, POLLIN, 10) == 0);
    } else if (!strcmp(test, "converter-error-flag") || !strcmp(test, "converter-source-error")) {
        assert(picture(id, sid) == VA_STATUS_SUCCESS);
        assert(table.vaSyncSurface(&va, sid) == VA_STATUS_SUCCESS);
        assert(v4l2r_convert_setup(ctx) == VA_STATUS_SUCCESS);
        v4l2r_convert_kick(ctx, 0);
        assert(V4L2R_SURFACE(drv, sid)->convert_pending);
        if (!strcmp(test, "converter-error-flag")) capture_flags = V4L2_BUF_FLAG_ERROR;
        else inject(VIDIOC_DQBUF, 1, 1, EIO);
        failed_surface(sid);
    } else if (!strcmp(test, "converter-export-error")) {
        assert(picture(id, sid) == VA_STATUS_SUCCESS);
        assert(table.vaSyncSurface(&va, sid) == VA_STATUS_SUCCESS);
        assert(v4l2r_convert_setup(ctx) == VA_STATUS_SUCCESS);
        /* Prepare destination so injection targets the source export. */
        assert(v4l2r_surface_convert_backing(drv, V4L2R_SURFACE(drv, sid)) == VA_STATUS_SUCCESS);
        inject(VIDIOC_EXPBUF, -1, 1, EIO);
        v4l2r_convert_kick(ctx, 0); assert(injected == 1);
        failed_surface(sid);
    } else { assert(!"unknown case"); }
    teardown(); return 0;
}
