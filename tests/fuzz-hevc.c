/* SPDX-License-Identifier: GPL-3.0-or-later */
/* No-device HEVC slice-header fuzz target, plus direct entry points for the
 * pred_weight_table VLA guard and the 32-leading-zero exp-Golomb check.
 * Those two regressions are distinct: slice-header parsing never reaches
 * pred_weight_table with an out-of-range count because the header rejects
 * num_ref_idx_* > 14 first. tests/hevc-parser.c keeps the original asserts. */
#include "fuzz-common.h"

#define v4l2r_codec_hevc v4l2r_test_codec_hevc
#include "../src/codec_hevc.c"

#define FUZZ_HEVC_MAGIC0	0xF5
#define FUZZ_HEVC_MAGIC1	0xE1
#define FUZZ_HEVC_MODE_PRED_WEIGHT	1
#define FUZZ_HEVC_MODE_UE		2

static uint32_t be32(const uint8_t *p)
{
	return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
	       ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	struct hevc_context codec;
	struct hevc_slice_info info;
	const uint8_t *nal;
	size_t nal_size;

	fuzz_oracle_reset();
	fuzz_begin_budget();
	size = fuzz_cap_size(size);

	if (size >= 3 && data[0] == FUZZ_HEVC_MAGIC0 && data[1] == FUZZ_HEVC_MAGIC1) {
		uint8_t mode = data[2];
		struct v4l2r_bits b;

		if (mode == FUZZ_HEVC_MODE_PRED_WEIGHT && size >= 8) {
			uint32_t count = be32(data + 4);
			uint32_t chroma = data[3];

			v4l2r_bits_init(&b, data + 8, size - 8, false);
			fuzz_oracle_last.flags |= FUZZ_ORACLE_PRED_WEIGHT;
			pred_weight_table(&b, count, chroma);
			if (b.error)
				fuzz_oracle_last.flags |= FUZZ_ORACLE_ERROR;
			fuzz_oracle_last.extra = count;
			fuzz_oracle_mix(count);
			fuzz_oracle_mix(chroma);
			fuzz_oracle_mix(b.error);
			/* Fails if the >14 guard is removed and the parse
			 * still claims success (ASan also traps the OOB). */
			if (count > 14 && !b.error)
				fuzz_harness_abort("pred-weight-guard-missing");
		} else if (mode == FUZZ_HEVC_MODE_UE && size > 3) {
			v4l2r_bits_init(&b, data + 3, size - 3, false);
			fuzz_oracle_last.flags |= FUZZ_ORACLE_UE;
			(void)v4l2r_bits_ue(&b);
			if (b.error)
				fuzz_oracle_last.flags |= FUZZ_ORACLE_ERROR;
			fuzz_oracle_mix(b.error);
			fuzz_oracle_mix(b.pos);
			/* Pinned 32-leading-zero fixture from hevc-parser.c. */
			if (size >= 12 && data[3] == 0 && data[4] == 0 &&
			    data[5] == 0 && data[6] == 0 && data[7] == 0x80 &&
			    !b.error)
				fuzz_harness_abort("ue-overflow-guard-missing");
		}
		fuzz_end_budget();
		return 0;
	}

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
	if (info.valid)
		fuzz_oracle_last.flags |= FUZZ_ORACLE_VALID;
	fuzz_oracle_last.nal_unit_type = info.nal_unit_type;
	fuzz_oracle_last.extra = info.temporal_id_plus1;
	fuzz_oracle_mix(info.valid);
	fuzz_oracle_mix(info.nal_unit_type);
	fuzz_oracle_mix(info.temporal_id_plus1);
	fuzz_oracle_mix(info.short_term_ref_pic_set_size);
	fuzz_oracle_mix(info.long_term_ref_pic_set_size);
	fuzz_oracle_mix(info.num_entry_point_offsets);
	fuzz_oracle_mix(codec.num_entry_point_offsets);
	fuzz_end_budget();
	return 0;
}
