/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Read the actual P010 backing probe controls in a fake ioctl, without hardware. */
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <sys/resource.h>
#include "v4l2_request.h"

static unsigned int controls_seen;

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (request == VIDIOC_QUERYCAP) {
        ((struct v4l2_capability *)arg)->capabilities = V4L2_CAP_VIDEO_M2M;
        return 0;
    }
    if (request == VIDIOC_S_FMT) {
        if (V4L2_TYPE_IS_OUTPUT(((struct v4l2_format *)arg)->type))
            return 0;
        /* The regression only needs the preceding bit-depth control probe. */
        errno = EINVAL;
        return -1;
    }
    assert(request == VIDIOC_S_EXT_CTRLS);
    struct v4l2_ext_controls *controls = arg;
    assert(controls->count == 1);
    struct v4l2_ext_control *control = controls->controls;
#if HAVE_V4L2_CTRL_H264
    if (control->id == V4L2_CID_STATELESS_H264_SPS) {
        const struct v4l2_ctrl_h264_sps *sps = control->ptr;
        assert(control->size == sizeof(*sps));
        assert(sps->profile_idc == 110 && sps->chroma_format_idc == 1);
        assert(sps->bit_depth_luma_minus8 == 2 && sps->bit_depth_chroma_minus8 == 2);
        assert(sps->pic_width_in_mbs_minus1 == 39);
        assert(sps->pic_height_in_map_units_minus1 == 22);
        controls_seen++;
        return 0;
    }
#endif
#if HAVE_V4L2_CTRL_HEVC
    if (control->id == V4L2_CID_STATELESS_HEVC_SPS) {
        const struct v4l2_ctrl_hevc_sps *sps = control->ptr;
        assert(control->size == sizeof(*sps));
        assert(sps->pic_width_in_luma_samples == 640);
        assert(sps->pic_height_in_luma_samples == 360);
        assert(sps->chroma_format_idc == 1);
        assert(sps->bit_depth_luma_minus8 == 2 && sps->bit_depth_chroma_minus8 == 2);
        controls_seen++;
        return 0;
    }
#endif
    assert(!"unexpected control");
    return -1;
}

static void probe(uint32_t coded)
{
    struct v4l2r_driver drv = {.nb_decoders = 1};
    struct v4l2r_surface surface = {
        .fourcc = VA_FOURCC_P010, .width = 640, .height = 360,
    };
    snprintf(drv.decoders[0].video_path, sizeof(drv.decoders[0].video_path), "/dev/null");
    drv.decoders[0].pixelformats[0] = coded;
    drv.decoders[0].nb_pixelformats = 1;
    unsigned int before = controls_seen;
    assert(v4l2r_surface_alloc_backing(&drv, &surface) == VA_STATUS_ERROR_OPERATION_FAILED);
    assert(controls_seen == before + 1 && !surface.backing);
}

int main(void)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
#if HAVE_V4L2_CTRL_H264
    probe(V4L2_PIX_FMT_H264_SLICE);
#endif
#if HAVE_V4L2_CTRL_HEVC
    probe(V4L2_PIX_FMT_HEVC_SLICE);
#endif
    return 0;
}
