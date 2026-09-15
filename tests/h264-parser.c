/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <assert.h>
#include <sys/resource.h>
#define v4l2r_codec_h264 v4l2r_test_codec_h264
#include "../src/codec_h264.c"

static void qp_translation(void)
{
    struct v4l2r_driver drv = {0};
    struct h264_context codec = {0};
    struct v4l2r_context ctx = {.drv = &drv, .codec_priv = &codec,
                               .profile = VAProfileH264High};
    VAPictureParameterBufferH264 pic = {0};
    pic.seq_fields.bits.frame_mbs_only_flag = 1;
    pic.seq_fields.bits.chroma_format_idc = 1;
    pic.bit_depth_luma_minus8 = pic.bit_depth_chroma_minus8 = 2;
    pic.pic_init_qp_minus26 = 7; /* FFmpeg's QP 21 + 12 - 26. */
    pic.pic_init_qs_minus26 = 12;
    drv.h264_high10 = V4L2R_H264_HIGH10_NATIVE;
    h264_fill_sps_pps(&ctx, &pic);
    assert(codec.pps.pic_init_qp_minus26 == 7);
    assert(codec.pps.pic_init_qs_minus26 == 12);
    drv.h264_high10 = V4L2R_H264_HIGH10_FFMPEG;
    h264_fill_sps_pps(&ctx, &pic);
    assert(codec.pps.pic_init_qp_minus26 == -5);
    assert(codec.pps.pic_init_qs_minus26 == 0);
    pic.bit_depth_luma_minus8 = pic.bit_depth_chroma_minus8 = 0;
    h264_fill_sps_pps(&ctx, &pic);
    assert(codec.pps.pic_init_qp_minus26 == 7); /* 8-bit is unaffected. */
#if VA_CHECK_VERSION(1, 18, 0)
    ctx.profile = VAProfileH264High10;
    h264_fill_sps_pps(&ctx, &pic);
    assert(codec.sps.profile_idc == 110 && !codec.sps.constraint_set_flags);
    assert(v4l2r_profile_bit_depth(ctx.profile) == 10);
    assert(v4l2r_profile_rt_format(ctx.profile) == VA_RT_FORMAT_YUV420_10);
#endif

    struct v4l2r_buffer buffer = {.type = VAPictureParameterBufferType,
        .data = &pic, .nb_elements = 1, .element_size = sizeof(pic)};
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    pic.num_slice_groups_minus1 = 1;
#pragma GCC diagnostic pop
    assert(h264_render_buffer(&ctx, &buffer) == VA_STATUS_ERROR_UNIMPLEMENTED);
    assert(!codec.have_pic);
}

int main(void)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    qp_translation();
    /* IDR I slice: first_mb=0, type=2, PPS=0, frame_num=0, idr_pic_id=0,
     * POC=0, reference flags=0, QP delta=0. */
    uint8_t data[128] = {0x65, 0xb8, 0x40, 0x80};
    struct h264_context codec = {0};
    codec.va_pic.seq_fields.bits.frame_mbs_only_flag = 1;
    VASliceParameterBufferH264 slice = {.slice_data_size = 4};
    struct h264_slice_info info;
    h264_parse_slice_header(&codec, &slice, data, 4, &info);
    assert(info.valid && info.idr && info.slice_type == SLICE_I);
    assert(info.pic_parameter_set_id == 0 && info.header_bit_size == 25);
    for (unsigned int n = 0; n < 4; n++) {
        h264_parse_slice_header(&codec, &slice, data, n, &info);
        assert(!info.valid);
    }
    data[0] |= 0x80;
    h264_parse_slice_header(&codec, &slice, data, 4, &info);
    assert(!info.valid);
    data[0] = 0x67; /* SPS is not slice data. */
    h264_parse_slice_header(&codec, &slice, data, 4, &info);
    assert(!info.valid);
    data[0] = 0x65;
    slice.num_ref_idx_l0_active_minus1 = 32;
    h264_parse_slice_header(&codec, &slice, data, 4, &info);
    assert(!info.valid);

    struct v4l2r_context ctx = {.codec_priv = &codec};
    codec.have_pic = true;
    slice.slice_data_offset = UINT32_MAX;
    assert(h264_process_slice(&ctx, &slice, data, 4) == VA_STATUS_ERROR_INVALID_BUFFER);
    slice.slice_data_offset = 0;
    assert(h264_process_slice(&ctx, &slice, data, 4) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(!codec.num_slices); /* Invalid headers never reach submission. */

    codec.num_slices = 1; /* A valid prefix was staged before this bad slice. */
    codec.staged = true;
    codec.va_slices = &slice;
    codec.nb_va_slices = 1;
    struct v4l2r_buffer buffer = {.type = VASliceDataBufferType,
        .data = data, .nb_elements = 1, .element_size = 4};
    assert(h264_render_buffer(&ctx, &buffer) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(codec.failed);
    assert(h264_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(h264_begin_picture(&ctx) == VA_STATUS_SUCCESS && !codec.failed);

    uint32_t seed = 0x26410;
    for (unsigned int n = 0; n < 24000; n++) {
        for (unsigned int i = 0; i < sizeof(data); i++) {
            seed = seed * 1664525u + 1013904223u;
            data[i] = seed >> 24;
        }
        codec.va_pic.seq_fields.value = data[0] | data[1] << 8;
        codec.va_pic.pic_fields.value = data[2] | data[3] << 8;
        slice.num_ref_idx_l0_active_minus1 = data[4];
        slice.num_ref_idx_l1_active_minus1 = data[5];
        h264_parse_slice_header(&codec, &slice, data, n % sizeof(data), &info);
    }
    return 0;
}
