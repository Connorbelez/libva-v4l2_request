/* SPDX-License-Identifier: GPL-3.0-or-later */
/* No-device HEVC slice-header fuzz target. */
#include "fuzz-common.h"

#define v4l2r_codec_hevc v4l2r_test_codec_hevc
#include "../src/codec_hevc.c"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	struct hevc_context codec;
	struct hevc_slice_info info;
	const uint8_t *nal;
	size_t nal_size;

	fuzz_begin_budget();
	size = fuzz_cap_size(size);
	memset(&codec, 0, sizeof(codec));
	codec.va_pic.pic_width_in_luma_samples = 128;
	codec.va_pic.pic_height_in_luma_samples = 128;
	nal = data;
	nal_size = size;
	if (size >= 16) {
		codec.va_pic.log2_min_luma_coding_block_size_minus3 = data[0];
		codec.va_pic.log2_diff_max_min_luma_coding_block_size = data[1];
		codec.va_pic.pic_fields.value = (unsigned)data[2] | (unsigned)data[3] << 8;
		codec.va_pic.slice_parsing_fields.value =
			(unsigned)data[4] | (unsigned)data[5] << 8;
		codec.va_pic.num_short_term_ref_pic_sets = data[6];
		codec.va_pic.num_long_term_ref_pic_sps = data[7];
		codec.va_pic.num_ref_idx_l0_default_active_minus1 = data[8];
		codec.va_pic.num_ref_idx_l1_default_active_minus1 = data[9];
		codec.va_pic.st_rps_bits = (uint32_t)data[10] * data[11];
		nal = data + 12;
		nal_size = size - 12;
	}
	codec.num_entry_point_offsets = 0;
	hevc_parse_slice_header(&codec, nal, nal_size, &info);
	(void)info;
	fuzz_end_budget();
	return 0;
}
