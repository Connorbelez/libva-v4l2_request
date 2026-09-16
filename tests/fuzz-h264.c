/* SPDX-License-Identifier: GPL-3.0-or-later */
/* No-device H.264 slice-header fuzz target. */
#include "fuzz-common.h"

#define v4l2r_codec_h264 v4l2r_test_codec_h264
#include "../src/codec_h264.c"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	struct h264_context codec;
	VASliceParameterBufferH264 slice;
	struct h264_slice_info info;
	const uint8_t *nal;
	size_t nal_size;

	fuzz_oracle_reset();
	fuzz_begin_budget();
	size = fuzz_cap_size(size);
	memset(&codec, 0, sizeof(codec));
	memset(&slice, 0, sizeof(slice));
	codec.va_pic.seq_fields.bits.frame_mbs_only_flag = 1;
	nal = data;
	nal_size = size;
	if (size >= 12) {
		codec.va_pic.seq_fields.value = (unsigned)data[0] | (unsigned)data[1] << 8;
		codec.va_pic.pic_fields.value = (unsigned)data[2] | (unsigned)data[3] << 8;
		slice.num_ref_idx_l0_active_minus1 = data[4];
		slice.num_ref_idx_l1_active_minus1 = data[5];
		nal = data + 6;
		nal_size = size - 6;
	}
	slice.slice_data_size = (uint32_t)nal_size;
	h264_parse_slice_header(&codec, &slice, nal, nal_size, &info);
	if (info.valid)
		fuzz_oracle_last.flags |= FUZZ_ORACLE_VALID;
	fuzz_oracle_last.nal_unit_type = info.nal_unit_type;
	fuzz_oracle_mix(info.valid);
	fuzz_oracle_mix(info.nal_unit_type);
	fuzz_oracle_mix(info.nal_ref_idc);
	fuzz_oracle_mix(info.slice_type);
	fuzz_oracle_mix(info.header_bit_size);
	fuzz_oracle_mix(info.idr);
	fuzz_end_budget();
	return 0;
}
