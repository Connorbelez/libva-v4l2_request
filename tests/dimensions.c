/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Reuse the resource-accounted model device, with finite dimension replies. */
#define main failure_cleanup_main
#define __wrap_ioctl model_ioctl
#include "failure-cleanup.c"
#undef __wrap_ioctl
#undef main

static bool avd;
static int enum_error;
static unsigned int size_count, formats_set, requests_allocated;
static struct v4l2_frmsizeenum sizes[3];
static struct v4l2r_codec dimension_codec;

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    void *arg = NULL;
    if (request != MEDIA_REQUEST_IOC_REINIT && request != MEDIA_REQUEST_IOC_QUEUE) {
        va_list ap; va_start(ap, request); arg = va_arg(ap, void *); va_end(ap);
    }
    if (request == VIDIOC_QUERYCAP) {
        struct v4l2_capability *cap = arg;
        strcpy((char *)cap->driver, avd ? "avd" : "generic");
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
    } else if (!strcmp(test, "generic")) {
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
    } else if (!strcmp(test, "invalid")) {
        range(10, 100, 0);
        create(10, 10, false);
        enum_error = EIO;
        create(64, 64, false);
        VASurfaceAttrib attrs[8]; unsigned int count = 8;
        assert(table.vaQuerySurfaceAttributes(&va, cfg, attrs, &count) != VA_STATUS_SUCCESS);
    } else {
        assert(!"unknown dimension fixture");
    }
    teardown();
    return 0;
}
