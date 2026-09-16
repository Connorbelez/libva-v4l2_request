/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <sys/resource.h>
#include "v4l2_request.h"

static unsigned long reject;
static uint32_t capture_format;
static int controls_seen;

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (request == reject) {
        errno = EINVAL;
        return -1;
    }
    if (request == VIDIOC_S_FMT) {
        struct v4l2_format *format = arg;
        /* Simulate a decoder which clamps the coded dimensions. */
        if (V4L2_TYPE_IS_MULTIPLANAR(format->type)) {
            format->fmt.pix_mp.width = 1280;
            format->fmt.pix_mp.height = 720;
        } else {
            format->fmt.pix.width = 1280;
            format->fmt.pix.height = 720;
        }
        return 0;
    }
    if (request == VIDIOC_S_EXT_CTRLS) {
        struct v4l2_ext_controls *controls = arg;
        assert(controls->count == 1);
        assert(controls->controls[0].id == V4L2_CID_STATELESS_H264_SPS);
        struct v4l2_ctrl_h264_sps *sps = controls->controls[0].ptr;
        assert(sps->profile_idc == 110 && sps->chroma_format_idc == 1);
        assert(sps->bit_depth_luma_minus8 == 2 && sps->bit_depth_chroma_minus8 == 2);
        assert(sps->pic_width_in_mbs_minus1 == 79);
        assert(sps->pic_height_in_map_units_minus1 == 44);
        controls_seen++;
        return 0;
    }
    assert(request == VIDIOC_ENUM_FMT);
    struct v4l2_fmtdesc *format = arg;
    if (format->index) {
        errno = EINVAL;
        return -1;
    }
    format->pixelformat = capture_format;
    return 0;
}

int main(void)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
/* The probe needs the H.264 control UAPI, a libva new enough to carry the
 * High 10 profile and the P010 capture format in the kernel headers
 * (first in Linux 6.0) for its 10-bit CAPTURE enumeration. */
#if HAVE_V4L2_CTRL_H264 && HAVE_V4L2_PIX_FMT_P010 && VA_CHECK_VERSION(1, 18, 0)
    capture_format = V4L2_PIX_FMT_P010;
    assert(v4l2r_probe_h264_10bit(123, V4L2_BUF_TYPE_VIDEO_OUTPUT));
    assert(v4l2r_probe_h264_10bit(123, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE));
    assert(controls_seen == 2);
    capture_format = V4L2_PIX_FMT_NV12;
    assert(!v4l2r_probe_h264_10bit(123, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE));
    capture_format = V4L2_PIX_FMT_P010;
    reject = VIDIOC_S_EXT_CTRLS;
    assert(!v4l2r_probe_h264_10bit(123, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE));
    reject = VIDIOC_S_FMT;
    assert(!v4l2r_probe_h264_10bit(123, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE));

    struct v4l2r_driver drv = {.nb_decoders = 1, .converter_probed = true};
    struct VADriverContext va = {.pDriverData = &drv};
    drv.decoders[0].pixelformats[0] = V4L2_PIX_FMT_H264_SLICE;
    drv.decoders[0].nb_pixelformats = 1;
    drv.decoders[0].h264_10bit = true;
    VAEntrypoint entry;
    int count;
    assert(v4l2r_QueryConfigEntrypoints(&va, VAProfileH264High10, &entry, &count) ==
           VA_STATUS_SUCCESS);
    assert(count == 0);
    drv.h264_high10 = V4L2R_H264_HIGH10_NATIVE;
    assert(v4l2r_QueryConfigEntrypoints(&va, VAProfileH264High10, &entry, &count) ==
           VA_STATUS_SUCCESS);
    assert(count == 1 && entry == VAEntrypointVLD);
    drv.decoders[0].h264_10bit = false;
    drv.h264_high10 = V4L2R_H264_HIGH10_FFMPEG;
    assert(v4l2r_QueryConfigEntrypoints(&va, VAProfileH264High10, &entry, &count) ==
           VA_STATUS_SUCCESS);
    assert(count == 0);
#endif
    return 0;
}
