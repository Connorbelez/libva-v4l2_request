/* SPDX-License-Identifier: GPL-3.0-or-later */
/* No-device VP9 uncompressed/compressed-header fuzz target. */
#include "fuzz-common.h"

#define v4l2r_codec_vp9 v4l2r_test_codec_vp9
#include "../src/codec_vp9.c"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	struct vp9_context codec;

	fuzz_begin_budget();
	size = fuzz_cap_size(size);
	memset(&codec, 0, sizeof(codec));
	codec.va_pic.profile = 0;
	codec.va_pic.frame_width = codec.va_pic.frame_height = 64;
	codec.va_pic.pic_fields.bits.subsampling_x = 1;
	codec.va_pic.pic_fields.bits.subsampling_y = 1;
	if (size) {
		unsigned profile = data[0] & 3;

		if (profile == 0 || profile == 2)
			codec.va_pic.profile = profile;
	}
	vp9_parse_uncompressed_header(&codec, data, size);
	vp9_parse_compressed_header(&codec, data, size);
	fuzz_end_budget();
	return 0;
}
