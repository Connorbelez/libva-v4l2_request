/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Include the private parser under a distinct codec symbol. */
#include <assert.h>
#include <sys/resource.h>
#define v4l2r_codec_hevc v4l2r_test_codec_hevc
#include "../src/codec_hevc.c"

static uint8_t data[128];
static unsigned int pos;
static void bits(uint32_t value, unsigned int n)
{
    while (n--) {
        data[pos / 8] |= (n < 32 ? (value >> n) & 1 : 0) << (7 - pos % 8);
        pos++;
    }
}
static void ue(uint32_t value)
{
    unsigned int n = 0;
    uint32_t code = value + 1;
    for (uint32_t v = code; v > 1; v >>= 1)
        n++;
    bits(0, n);
    bits(code, n + 1);
}
static size_t idr_header(unsigned int count, unsigned int offset_bits)
{
    memset(data, 0, sizeof(data));
    pos = 0;
    bits(19 << 9 | 1, 16); /* IDR, temporal_id_plus1 = 1 */
    bits(1, 1); bits(0, 1); /* first slice, no_output_of_prior */
    ue(0); ue(2); ue(0); /* PPS, I slice, QP delta */
    ue(count);
    if (count) {
        ue(offset_bits - 1);
        for (unsigned int i = 0; i < count; i++)
            bits(i, offset_bits);
    }
    return (pos + 7) / 8;
}

static void reference_order(void)
{
    struct v4l2r_driver drv = {0};
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!v4l2r_handles_init(&drv.surfaces, 0));
    VASurfaceID ids[4];
    for (unsigned int i = 0; i < 4; i++) {
        ids[i] = v4l2r_handles_alloc(&drv.surfaces, sizeof(struct v4l2r_surface));
        V4L2R_SURFACE(&drv, ids[i])->capture_index = i;
    }
    struct hevc_context codec = {0};
    struct v4l2r_context ctx = {.drv = &drv, .codec_priv = &codec, .is_avd = true};
    VAPictureParameterBufferHEVC *pic = &codec.va_pic;
    for (unsigned int i = 0; i < 15; i++)
        pic->ReferenceFrames[i].flags = VA_PICTURE_HEVC_INVALID;
    pic->CurrPic = (VAPictureHEVC){.picture_id = ids[3], .pic_order_cnt = 6};
    pic->ReferenceFrames[0] = (VAPictureHEVC){.picture_id = ids[2], .pic_order_cnt = 4,
        .flags = VA_PICTURE_HEVC_RPS_ST_CURR_BEFORE};
    pic->ReferenceFrames[1] = (VAPictureHEVC){.picture_id = ids[1], .pic_order_cnt = 8,
        .flags = VA_PICTURE_HEVC_RPS_ST_CURR_AFTER};
    pic->ReferenceFrames[5] = (VAPictureHEVC){.picture_id = ids[0], .pic_order_cnt = 0,
        .flags = VA_PICTURE_HEVC_RPS_ST_CURR_BEFORE};
    codec.reference_order[0] = pic->ReferenceFrames[5];
    codec.reference_order[1] = pic->ReferenceFrames[1];
    codec.reference_order[2] = pic->ReferenceFrames[0];
    codec.nb_reference_order = 3;
    assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
    assert(codec.dpb_index_of_va[5] == 0 && codec.dpb_index_of_va[1] == 1 &&
           codec.dpb_index_of_va[0] == 2);
    assert(codec.decode_params.poc_st_curr_before[0] == 2 &&
           codec.decode_params.poc_st_curr_before[1] == 0);
    assert(codec.decode_params.poc_st_curr_after[0] == 1);
    VASliceParameterBufferHEVC slice = {0};
    slice.LongSliceFlags.fields.slice_type = V4L2_HEVC_SLICE_TYPE_B;
    slice.RefPicList[0][0] = 0; slice.RefPicList[0][1] = 5;
    slice.RefPicList[1][0] = 1;
    slice.num_ref_idx_l0_active_minus1 = 1;
    assert(hevc_slice_refs_valid(&codec, &slice));
    struct hevc_slice_info info = {0};
    struct v4l2_ctrl_hevc_slice_params params;
    hevc_fill_slice_params(&ctx, &params, &slice, &info);
    assert(params.ref_idx_l0[0] == 2 && params.ref_idx_l0[1] == 0 && params.ref_idx_l1[0] == 1);
    slice.RefPicList[0][0] = 15;
    assert(!hevc_slice_refs_valid(&codec, &slice));
    slice.RefPicList[0][0] = 2; /* Hole in VA reference array. */
    assert(!hevc_slice_refs_valid(&codec, &slice));
    slice.RefPicList[0][0] = 0;
    slice.num_ref_idx_l0_active_minus1 = 15;
    assert(!hevc_slice_refs_valid(&codec, &slice));
    slice.num_ref_idx_l0_active_minus1 = 1;
    slice.LongSliceFlags.fields.slice_temporal_mvp_enabled_flag = 1;
    slice.collocated_ref_idx = 1; /* L1 has only one entry. */
    assert(!hevc_slice_refs_valid(&codec, &slice));
    slice.LongSliceFlags.fields.collocated_from_l0_flag = 1;
    assert(hevc_slice_refs_valid(&codec, &slice));
    codec.decode_params.dpb[2].timestamp = 0;
    assert(!hevc_slice_refs_valid(&codec, &slice));
    assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
    hevc_remember_reference_order(&codec);
    assert(codec.nb_reference_order == 4 && codec.reference_order[3].picture_id == ids[3]);

    /* Retained long-term refs survive arbitrarily many pictures; reusing a
     * surface for a different POC must append a new picture, not reuse its age. */
    for (unsigned int n = 0; n < 200; n++) {
        pic->ReferenceFrames[5].flags = VA_PICTURE_HEVC_LONG_TERM_REFERENCE | VA_PICTURE_HEVC_RPS_LT_CURR;
        pic->ReferenceFrames[0].pic_order_cnt = 10 + n;
        assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
        assert(codec.dpb_index_of_va[5] == 0 && codec.dpb_index_of_va[0] == 2);
        assert(codec.decode_params.poc_lt_curr[0] == 0);
        hevc_remember_reference_order(&codec);
        assert(hevc_begin_picture(&ctx) == VA_STATUS_SUCCESS);
    }
    pic->slice_parsing_fields.bits.long_term_ref_pics_present_flag = 1;
    assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
    assert(codec.dpb_index_of_va[0] == 0 && codec.dpb_index_of_va[5] == 2);
    hevc_remember_reference_order(&codec);
    pic->slice_parsing_fields.bits.long_term_ref_pics_present_flag = 0;
    assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
    assert(codec.dpb_index_of_va[5] == 0 && codec.dpb_index_of_va[0] == 2);
    ctx.is_avd = false;
    assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
    assert(codec.dpb_index_of_va[0] == 0 && codec.dpb_index_of_va[5] == 2);
    V4L2R_SURFACE(&drv, ids[0])->capture_index = -1;
    assert(hevc_fill_decode_params(&ctx, pic) == VA_STATUS_SUCCESS);
    assert(codec.dpb_index_of_va[5] == 0xff);
    assert(codec.decode_params.num_active_dpb_entries == 2);
    assert(!hevc_slice_refs_valid(&codec, &slice));
    v4l2r_handles_destroy(&drv.surfaces);
    pthread_mutex_destroy(&drv.mutex);
}

static void failed_picture(void)
{
    struct hevc_context codec = {.have_pic = true, .num_slices = 1,
        .decode_mode = V4L2_STATELESS_HEVC_DECODE_MODE_SLICE_BASED,
        .max_slice_params = 1, .num_slice_params = 1};
    struct v4l2r_context ctx = {.codec_priv = &codec};
    VASliceParameterBufferHEVC slice = {.slice_data_size = 2};
    slice.LongSliceFlags.fields.slice_type = V4L2_HEVC_SLICE_TYPE_I;
    uint8_t bad[] = {0xff, 0xff};
    codec.va_slices = &slice; codec.nb_va_slices = 1;
    struct v4l2r_buffer buffer = {.type = VASliceDataBufferType,
        .data = bad, .nb_elements = 1, .element_size = sizeof(bad)};
    /* This must fail before a full batch is sent to the (absent) device. */
    assert(hevc_render_buffer(&ctx, &buffer) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(codec.failed && codec.num_slice_params == 1);
    assert(hevc_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(hevc_render_buffer(&ctx, &buffer) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(hevc_begin_picture(&ctx) == VA_STATUS_SUCCESS && !codec.failed);
    codec.have_pic = true;
    slice.slice_data_offset = UINT32_MAX;
    assert(hevc_process_slice(&ctx, &slice, bad, sizeof(bad)) == VA_STATUS_ERROR_INVALID_BUFFER);
    slice.slice_data_offset = 0; slice.slice_data_size = UINT32_MAX;
    assert(hevc_process_slice(&ctx, &slice, bad, SIZE_MAX) == VA_STATUS_ERROR_INVALID_BUFFER);
    slice.slice_data_size = 2; slice.slice_data_byte_offset = 3;
    assert(hevc_process_slice(&ctx, &slice, bad, sizeof(bad)) == VA_STATUS_ERROR_INVALID_BUFFER);
    slice.slice_data_byte_offset = 0;
    assert(hevc_process_slice(&ctx, &slice, NULL, sizeof(bad)) == VA_STATUS_ERROR_INVALID_BUFFER);
    codec.num_slices = codec.nb_va_slices = 1;
    assert(hevc_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER); /* Missing slice data. */
}

int main(void)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    reference_order();
    failed_picture();
    uint32_t offsets[2] = {0};
    struct hevc_context codec = {
        .max_entry_point_offsets = 2, .entry_point_offsets = offsets,
    };
    codec.va_pic.pic_fields.bits.tiles_enabled_flag = 1;
    struct hevc_slice_info info;
    size_t size = idr_header(2, 2);
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(info.valid && info.num_entry_point_offsets == 2);
    assert(codec.num_entry_point_offsets == 2 && offsets[0] == 1 && offsets[1] == 2);

    /* A dry parse validates a next batch without touching queued offsets. */
    codec.entry_point_offsets = NULL;
    codec.num_entry_point_offsets = 0;
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(info.valid && codec.num_entry_point_offsets == 2);
    codec.entry_point_offsets = offsets;
    for (unsigned int n = 0; n < size; n++) {
        codec.num_entry_point_offsets = 0;
        hevc_parse_slice_header(&codec, data, n, &info);
        assert(!info.valid);
    }
    codec.num_entry_point_offsets = 0;
    data[0] |= 0x80;
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(!info.valid);
    data[0] = 32 << 1; /* VPS is not slice data. */
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(!info.valid);
    data[0] = 19 << 1; data[1] = 0;
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(!info.valid);

    codec.num_entry_point_offsets = 0;
    size = idr_header(3, 2);
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(!info.valid && codec.num_entry_point_offsets == 0);

    codec.num_entry_point_offsets = 0;
    size = idr_header(1, 33);
    hevc_parse_slice_header(&codec, data, size, &info);
    assert(!info.valid && codec.num_entry_point_offsets == 0);

    /* Repro for the previous unchecked stack VLA in pred_weight_table. */
    struct v4l2r_bits b;
    v4l2r_bits_init(&b, data, size, true);
    pred_weight_table(&b, 0x7fffffff, 1);
    assert(b.error);
    memset(data, 0, sizeof(data));
    data[4] = 0x80; /* Exp-Golomb with 32 leading zero bits. */
    v4l2r_bits_init(&b, data, 9, false);
    v4l2r_bits_ue(&b);
    assert(b.error);

    uint32_t seed = 0x615a7du;
    for (unsigned int n = 0; n < 24000; n++) {
        for (unsigned int i = 0; i < sizeof(data); i++) {
            seed = seed * 1664525u + 1013904223u;
            data[i] = seed >> 24;
        }
        memset(&codec.va_pic, 0, sizeof(codec.va_pic));
        codec.va_pic.pic_width_in_luma_samples = 128;
        codec.va_pic.pic_height_in_luma_samples = 128;
        codec.va_pic.log2_min_luma_coding_block_size_minus3 = data[0];
        codec.va_pic.log2_diff_max_min_luma_coding_block_size = data[1];
        codec.va_pic.pic_fields.value = data[2] | data[3] << 8;
        codec.va_pic.slice_parsing_fields.value = data[4] | data[5] << 8;
        codec.va_pic.num_short_term_ref_pic_sets = data[6];
        codec.va_pic.num_long_term_ref_pic_sps = data[7];
        codec.va_pic.num_ref_idx_l0_default_active_minus1 = data[8];
        codec.va_pic.num_ref_idx_l1_default_active_minus1 = data[9];
        codec.va_pic.st_rps_bits = (uint32_t)data[10] * data[11];
        codec.num_entry_point_offsets = 0;
        hevc_parse_slice_header(&codec, data, n % sizeof(data), &info);
        assert(codec.num_entry_point_offsets <= 2);
    }
    return 0;
}
