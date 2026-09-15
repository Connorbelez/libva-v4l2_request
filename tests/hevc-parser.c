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

int main(void)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
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
