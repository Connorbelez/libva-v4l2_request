/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Reuse the resource-accounted model device, with finite dimension replies. */
#define main failure_cleanup_main
#define __wrap_ioctl model_ioctl
#define __wrap_open model_open
#define __wrap_open64 model_open64
#define __wrap___open_2 model_open_2
#define __wrap___open64_2 model_open64_2
#include "failure-cleanup.c"
#undef __wrap_ioctl
#undef __wrap_open
#undef __wrap_open64
#undef __wrap___open_2
#undef __wrap___open64_2
#undef main

static bool avd;
static int enum_error;
static unsigned int size_count, formats_set, requests_allocated, controls_submitted;
static struct v4l2_frmsizeenum sizes[3];
static struct v4l2r_codec dimension_codec;
static bool second_device[LIMIT];
static bool destroy_during_query;

int __wrap_open(const char *path, int flags, ...)
{
    bool second = !strcmp(path, "model-video-second");
    int fd = model_open(second ? "model-video" : path, flags);
    if (fd >= 0) second_device[fd_slot(fd)] = second;
    return fd;
}
int __wrap_open64(const char *path, int flags, ...) { return __wrap_open(path, flags); }
int __wrap___open_2(const char *path, int flags) { return __wrap_open(path, flags); }
int __wrap___open64_2(const char *path, int flags) { return __wrap_open(path, flags); }

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    void *arg = NULL;
    if (request != MEDIA_REQUEST_IOC_REINIT && request != MEDIA_REQUEST_IOC_QUEUE) {
        va_list ap; va_start(ap, request); arg = va_arg(ap, void *); va_end(ap);
    }
    if (request == VIDIOC_QUERYCAP) {
        struct v4l2_capability *cap = arg;
        strcpy((char *)cap->driver, avd && !second_device[fd_slot(fd)] ? "avd" : "generic");
    }
    if (request == VIDIOC_ENUM_FMT) {
        struct v4l2_fmtdesc *f = arg;
        if (f->index) { errno = EINVAL; return -1; }
        f->pixelformat = V4L2_TYPE_IS_OUTPUT(f->type) ?
            dimension_codec.pixelformat : V4L2_PIX_FMT_NV12;
        return 0;
    }
    if (request == VIDIOC_ENUM_FRAMESIZES) {
        struct v4l2_frmsizeenum *f = arg;
        if (destroy_during_query) {
            destroy_during_query = false;
            assert(v4l2r_DestroyConfig(&va, cfg) == VA_STATUS_SUCCESS);
        }
        if (second_device[fd_slot(fd)]) {
            f->type = V4L2_FRMSIZE_TYPE_CONTINUOUS;
            f->stepwise = (struct v4l2_frmsize_stepwise){1, 8192, 1, 1, 8192, 1};
            return 0;
        }
        if (enum_error || f->index >= size_count) {
            errno = enum_error ? enum_error : EINVAL;
            return -1;
        }
        unsigned int index = f->index;
        *f = sizes[index];
        f->index = index;
        return 0;
    }
    formats_set += request == VIDIOC_S_FMT;
    requests_allocated += request == MEDIA_IOC_REQUEST_ALLOC;
    controls_submitted += request == VIDIOC_S_EXT_CTRLS;
    return model_ioctl(fd, request, arg);
}

static void range(unsigned int min, unsigned int max, unsigned int step)
{
    size_count = 1;
    sizes[0] = (struct v4l2_frmsizeenum) {
        .type = V4L2_FRMSIZE_TYPE_STEPWISE,
        .stepwise = {min, max, step, min, max, step},
    };
}

static void second_decoder(void)
{
    drv->nb_decoders = 2;
    drv->decoders[1] = drv->decoders[0];
    strcpy(drv->decoders[1].video_path, "model-video-second");
}

static void attributes(unsigned int min, unsigned int max)
{
    VASurfaceAttrib attrs[8];
    unsigned int count = 8, seen = 0;
    assert(table.vaQuerySurfaceAttributes(&va, cfg, attrs, &count) == VA_STATUS_SUCCESS);
    for (unsigned int i = 0; i < count; i++) {
        switch (attrs[i].type) {
        case VASurfaceAttribMinWidth: case VASurfaceAttribMinHeight:
            assert(attrs[i].value.value.i == (int)min); seen++; break;
        case VASurfaceAttribMaxWidth: case VASurfaceAttribMaxHeight:
            assert(attrs[i].value.value.i == (int)max); seen++; break;
        default: break;
        }
    }
    assert(seen == 4);
}

static void create(unsigned int width, unsigned int height, bool supported)
{
    VAContextID id = VA_INVALID_ID;
    unsigned int before_formats = formats_set, before_requests = requests_allocated;
    VAStatus status = table.vaCreateContext(&va, cfg, width, height, 0, NULL, 0, &id);
    if (supported) {
        assert(status == VA_STATUS_SUCCESS && id != VA_INVALID_ID);
        assert(formats_set > before_formats && requests_allocated > before_requests);
        assert(table.vaDestroyContext(&va, id) == VA_STATUS_SUCCESS);
    } else {
        assert(status != VA_STATUS_SUCCESS && id == VA_INVALID_ID);
        assert(formats_set == before_formats && requests_allocated == before_requests);
    }
    assert(!live_handles(&drv->contexts));
}

#if HAVE_V4L2_CTRL_VP9
static void picture_dimensions(unsigned int width, unsigned int height, bool supported)
{
    VAContextID id = context();
    VASurfaceID sid = surface();
    VABufferID bid;
    VADecPictureParameterBufferVP9 pic = {
        .frame_width = width, .frame_height = height, .bit_depth = 8,
    };
    assert(v4l2r_CreateBuffer(&va, id, VAPictureParameterBufferType,
                            sizeof(pic), 1, &pic, &bid) == VA_STATUS_SUCCESS);
    assert(table.vaBeginPicture(&va, id, sid) == VA_STATUS_SUCCESS);
    unsigned int before_controls = controls_submitted, before_queues = queues;
    unsigned int before_requests = requests_allocated;
    VAStatus result = table.vaRenderPicture(&va, id, &bid, 1);
    assert(result == (supported ? VA_STATUS_SUCCESS : VA_STATUS_ERROR_INVALID_BUFFER));
    /* No slice data was supplied, so even valid parameters must not submit. */
    assert(table.vaEndPicture(&va, id) != VA_STATUS_SUCCESS);
    assert(controls_submitted == before_controls && queues == before_queues &&
           requests_allocated == before_requests);
    assert(v4l2r_DestroyBuffer(&va, bid) == VA_STATUS_SUCCESS);
    assert(table.vaDestroyContext(&va, id) == VA_STATUS_SUCCESS);
    assert(table.vaDestroySurfaces(&va, &sid, 1) == VA_STATUS_SUCCESS);
}
#endif

int main(int argc, char **argv)
{
    assert(argc == 2);
    setup();
    dimension_codec = codec;
    /* VP9 FOURCC is stable even with old headers lacking its controls. */
    dimension_codec.pixelformat = v4l2_fourcc('V', 'P', '9', 'F');
    V4L2R_CONFIG(drv, cfg)->codec = &dimension_codec;
    drv->decoders[0].pixelformats[0] = dimension_codec.pixelformat;
    table.vaQuerySurfaceAttributes = v4l2r_QuerySurfaceAttributes;
    const char *test = argv[1];
    if (!strcmp(test, "avd") || !strcmp(test, "avd-missing")) {
        avd = true;
        range(1, 65536, 1); /* Deliberately lying minimum and maximum. */
        sizes[0].type = V4L2_FRMSIZE_TYPE_CONTINUOUS;
        if (!strcmp(test, "avd-missing")) enum_error = ENOTTY;
        attributes(64, 4096);
        create(8, 8, false); create(63, 64, false); create(64, 63, false);
        create(64, 64, true); create(66, 66, true);
        create(4096, 4096, true);
        create(4097, 64, false); create(64, 4097, false);
        if (!enum_error) {
            range(64, 2048, 1); /* Preserve a narrower advertised contract. */
            attributes(64, 2048);
            create(2048, 2048, true); create(2049, 64, false);
        }
    } else if (!strcmp(test, "generic") || !strcmp(test, "avd-other-codec")) {
        if (!strcmp(test, "avd-other-codec")) {
            avd = true;
            dimension_codec.pixelformat = V4L2_PIX_FMT_H264;
            drv->decoders[0].pixelformats[0] = dimension_codec.pixelformat;
        }
        range(1, 128, 1);
        attributes(1, 128);
        create(8, 8, true); create(128, 128, true); create(129, 128, false);
    } else if (!strcmp(test, "stepwise")) {
        range(10, 101, 8);
        attributes(10, 98);
        create(10, 10, true); create(18, 98, true);
        create(16, 18, false); create(18, 16, false); create(101, 98, false);
    } else if (!strcmp(test, "discrete")) {
        size_count = 2;
        sizes[0].type = sizes[1].type = V4L2_FRMSIZE_TYPE_DISCRETE;
        sizes[0].discrete = (struct v4l2_frmsize_discrete){32, 64};
        sizes[1].discrete = (struct v4l2_frmsize_discrete){64, 32};
        attributes(32, 64);
        create(32, 64, true); create(64, 32, true); create(32, 32, false);
    } else if (!strcmp(test, "missing")) {
        enum_error = ENOTTY;
        attributes(1, 65536);
        create(8, 8, true); create(65536, 65536, true); create(65537, 64, false);
    } else if (!strcmp(test, "multiple")) {
        avd = true;
        range(1, 128, 1);
        second_decoder();
        attributes(1, 8192);
        create(8, 8, true); create(64, 64, true);
        create(8192, 64, true); create(8193, 64, false);
        enum_error = EIO; /* An unusable first candidate must not hide the second. */
        attributes(1, 8192);
        create(64, 64, true);
    } else if (!strcmp(test, "query-destroy")) {
        range(1, 128, 1);
        /* Deterministically destroy the config while its dimension ioctl
         * is in progress. The query must own a snapshot across that call. */
        destroy_during_query = true;
        attributes(1, 128);
        VASurfaceAttrib attrs[8]; unsigned int count = 8;
        assert(table.vaQuerySurfaceAttributes(&va, cfg, attrs, &count) ==
               VA_STATUS_ERROR_INVALID_CONFIG);
    } else if (!strcmp(test, "invalid")) {
        range(10, 100, 0);
        create(10, 10, false);
        range(100, 10, 1);
        create(64, 64, false);
        range(0, 100, 1);
        create(64, 64, false);
        enum_error = EINVAL;
        create(64, 64, false);
        enum_error = EIO;
        create(64, 64, false);
        VASurfaceAttrib attrs[8]; unsigned int count = 8;
        assert(table.vaQuerySurfaceAttributes(&va, cfg, attrs, &count) != VA_STATUS_SUCCESS);
#if HAVE_V4L2_CTRL_VP9
    } else if (!strcmp(test, "picture")) {
        dimension_codec = *v4l2r_codec_for_profile(VAProfileVP9Profile0);
        range(1, 4096, 1);
        avd = true;
        picture_dimensions(8, 64, false); picture_dimensions(64, 8, false);
        picture_dimensions(4097, 64, false); picture_dimensions(64, 4097, false);
        picture_dimensions(64, 64, true); picture_dimensions(66, 66, true);
        avd = false;
        picture_dimensions(8, 8, true);
    } else if (!strcmp(test, "picture-narrow") || !strcmp(test, "picture-narrow-generic")) {
        dimension_codec = *v4l2r_codec_for_profile(VAProfileVP9Profile0);
        avd = !strcmp(test, "picture-narrow");
        range(32, 128, 1);
        attributes(avd ? 64 : 32, 128);
        picture_dimensions(8, 64, false);
        picture_dimensions(129, 64, false);
        picture_dimensions(64, 129, false);
        picture_dimensions(128, 128, true);
    } else if (!strcmp(test, "picture-stepwise") || !strcmp(test, "picture-stepwise-generic")) {
        dimension_codec = *v4l2r_codec_for_profile(VAProfileVP9Profile0);
        avd = !strcmp(test, "picture-stepwise");
        range(64, 128, 8);
        picture_dimensions(66, 64, false);
        picture_dimensions(64, 66, false);
        picture_dimensions(72, 72, true);
    } else if (!strcmp(test, "picture-discrete") || !strcmp(test, "picture-discrete-generic")) {
        dimension_codec = *v4l2r_codec_for_profile(VAProfileVP9Profile0);
        avd = !strcmp(test, "picture-discrete");
        size_count = 2;
        sizes[0].type = sizes[1].type = V4L2_FRMSIZE_TYPE_DISCRETE;
        sizes[0].discrete = (struct v4l2_frmsize_discrete){64, 64};
        sizes[1].discrete = (struct v4l2_frmsize_discrete){128, 128};
        picture_dimensions(64, 128, false);
        picture_dimensions(128, 64, false);
        picture_dimensions(128, 128, true);
    } else if (!strcmp(test, "picture-selected")) {
        dimension_codec = *v4l2r_codec_for_profile(VAProfileVP9Profile0);
        avd = true;
        range(1, 128, 1);
        second_decoder();
        attributes(1, 8192);
        /* 64x64 selects the first decoder: the second decoder's larger
         * envelope must not admit an unsupported picture on the first. */
        picture_dimensions(129, 64, false);
        picture_dimensions(128, 128, true);
#endif
    } else {
        assert(!"unknown dimension fixture");
    }
    teardown();
    return 0;
}
