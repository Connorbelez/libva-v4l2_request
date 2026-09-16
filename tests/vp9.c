/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Private parser and submission checks; no decoder device is opened. */
#include <assert.h>
#include <sys/resource.h>
#define v4l2r_codec_vp9 v4l2r_test_codec_vp9
#define v4l2r_decode test_decode
#define v4l2r_append_output test_append
#include "../src/codec_vp9.c"

static unsigned int submitted, appended;
static VAStatus decode_status, append_status;
static struct v4l2_ctrl_vp9_frame submitted_frame;

VAStatus test_decode(struct v4l2r_context *ctx, struct v4l2_ext_control *controls,
                     unsigned int count, bool first, bool last)
{
    (void)ctx; (void)count; (void)first; (void)last;
    submitted++;
    submitted_frame = *(struct v4l2_ctrl_vp9_frame *)controls[0].ptr;
    return decode_status;
}
VAStatus test_append(struct v4l2r_context *ctx, const void *data, size_t size)
{
    (void)ctx; (void)data; (void)size;
    appended++;
    return append_status;
}
static struct v4l2r_driver drv;
static struct vp9_context codec;
static struct v4l2r_context ctx = {.drv = &drv, .codec_priv = &codec};
static VASurfaceID ref;
static VADecPictureParameterBufferVP9 pic;
static uint8_t data[256];
static unsigned int pos;
static size_t size;

static void bits(unsigned int v, unsigned int n)
{
    while (n--) {
        assert(pos < sizeof(data) * 8);
        data[pos / 8] |= ((v >> n) & 1) << (7 - pos % 8);
        pos++;
    }
}

static void header(bool inter, bool full_range, bool update, unsigned int profile)
{
    memset(data, 0, sizeof(data)); memset(&pic, 0, sizeof(pic)); pos = 0;
    pic.profile = profile; pic.bit_depth = profile ? 10 : 8;
    pic.frame_width = pic.frame_height = 64;
    pic.pic_fields.bits.frame_type = inter;
    pic.pic_fields.bits.show_frame = 1;
    pic.pic_fields.bits.subsampling_x = pic.pic_fields.bits.subsampling_y = 1;
    pic.pic_fields.bits.frame_parallel_decoding_mode = 1;
    pic.pic_fields.bits.lossless_flag = 1;
    pic.pic_fields.bits.mcomp_filter_type = 4;
    for (unsigned int i = 0; i < 8; i++) pic.reference_frames[i] = ref;
    bits(2, 2); bits(profile & 1, 1); bits(profile >> 1, 1);
    bits(0, 1); bits(inter, 1); bits(1, 1); bits(0, 1);
    if (!inter) {
        bits(0x498342, 24);
        if (profile == 2) bits(0, 1); /* 10 bit */
        bits(1, 3); bits(full_range, 1);
        bits(63, 16); bits(63, 16); bits(0, 1);
    } else {
        bits(0, 2); bits(1, 8); /* reset context, refresh frame flags */
        bits(0, 12); /* three reference indices/sign biases */
        bits(1, 1); bits(0, 1); /* size from ref, render size unchanged */
        bits(0, 1); bits(1, 1); /* MV precision, switchable filter */
    }
    bits(0, 1); bits(1, 1); bits(0, 2); /* refresh, parallel, context */
    bits(0, 6); bits(0, 3); /* loop-filter level, sharpness */
    bits(1, 1); bits(update, 1);
    if (update) {
        bits(1, 1); bits(25, 6); bits(0, 1); /* ref delta[0] = +25 */
        bits(0, 5); /* remaining ref/mode deltas unchanged */
    }
    bits(0, 8); bits(0, 3); /* lossless quantization */
    bits(update, 1); /* segmentation */
    if (update) {
        bits(0, 1); bits(1, 1); bits(0, 1); /* no map update, feature data, deltas */
        for (unsigned int segment = 0; segment < 8; segment++) {
            for (unsigned int feature = 0; feature < 4; feature++) {
                bool enabled = !segment && !feature;
                bits(enabled, 1);
                if (enabled) { bits(17, 8); bits(0, 1); } /* ALT_Q +17 */
            }
        }
    }
    bits(0, 1); /* one tile row; 64px width has no tile-column bits */
    bits(2, 16); /* compressed header byte count */
    pic.frame_header_length_in_bytes = (pos + 7) / 8;
    pic.first_partition_size = 2;
    size = pic.frame_header_length_in_bytes + 3; /* two zero header bytes + tile */
}

static void begin(void)
{
    assert(vp9_begin_picture(&ctx) == VA_STATUS_SUCCESS);
    submitted = appended = 0;
    decode_status = append_status = VA_STATUS_SUCCESS;
}

static VAStatus picture(void)
{
    struct v4l2r_buffer buf = {.type = VAPictureParameterBufferType,
        .data = &pic, .element_size = sizeof(pic), .nb_elements = 1};
    return vp9_render_buffer(&ctx, &buf);
}

static VAStatus render(void)
{
    struct v4l2r_buffer buf = {.type = VASliceDataBufferType,
        .data = data, .element_size = size, .nb_elements = 1};
    return vp9_render_buffer(&ctx, &buf);
}

static void malformed(void)
{
    begin(); header(false, false, false, 0);
    assert(picture() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) != VA_STATUS_SUCCESS && !submitted);
    for (unsigned int n = 0; n < 10; n++) {
        begin(); header(false, false, false, 0);
        if (n == 0) data[0] = 0; /* bad frame marker */
        if (n == 1) data[1] ^= 1; /* bad sync code */
        if (n == 2) size = 1; /* truncated uncompressed header */
        if (n == 3) pic.frame_header_length_in_bytes = size;
        if (n == 4) pic.first_partition_size = UINT16_MAX;
        if (n == 5) data[pic.frame_header_length_in_bytes] = 0xff; /* bool marker */
        if (n == 6) pic.first_partition_size = 0;
        if (n == 7) pic.frame_header_length_in_bytes--; /* VA boundary disagrees */
        if (n == 8) data[0] |= 8; /* show_existing_frame belongs to the client */
        if (n == 9) size--; /* No tile payload after the compressed header. */
        assert(picture() == VA_STATUS_SUCCESS);
        assert(render() == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(!appended && !submitted);
        assert(vp9_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    }
    begin(); header(false, false, false, 2);
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 1);
    assert(submitted_frame.profile == 2 && submitted_frame.bit_depth == 10);
}

static void persistent_state(void)
{
    begin(); header(false, true, false, 0);
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS);
    assert(submitted_frame.flags & V4L2_VP9_FRAME_FLAG_COLOR_RANGE_FULL_SWING);
    assert(submitted_frame.lf.ref_deltas[0] == 1);

    begin(); header(true, false, false, 0); /* Inter header does not code range. */
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS);
    assert(submitted_frame.flags & V4L2_VP9_FRAME_FLAG_COLOR_RANGE_FULL_SWING);

    for (unsigned int fail = 0; fail < 3; fail++) {
        begin(); header(false, false, true, 0); /* Would change range and filter state. */
        assert(picture() == VA_STATUS_SUCCESS);
        if (fail == 0) {
            data[pic.frame_header_length_in_bytes] = 0xff;
            assert(render() == VA_STATUS_ERROR_INVALID_BUFFER);
        } else if (fail == 1) {
            append_status = VA_STATUS_ERROR_ALLOCATION_FAILED;
            assert(render() == append_status);
        } else {
            assert(render() == VA_STATUS_SUCCESS);
            decode_status = VA_STATUS_ERROR_OPERATION_FAILED;
            assert(vp9_end_picture(&ctx) == decode_status);
        }
        begin(); header(true, false, false, 0);
        assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
        assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS);
        assert(submitted_frame.flags & V4L2_VP9_FRAME_FLAG_COLOR_RANGE_FULL_SWING);
        assert(submitted_frame.lf.ref_deltas[0] == 1);
        assert(!submitted_frame.seg.feature_enabled[0]);
    }
    begin(); header(false, false, true, 0);
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS);
    begin(); header(true, false, false, 0);
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS);
    assert(!(submitted_frame.flags & V4L2_VP9_FRAME_FLAG_COLOR_RANGE_FULL_SWING));
    assert(submitted_frame.lf.ref_deltas[0] == 25);
    assert(submitted_frame.seg.feature_enabled[0] &
           V4L2_VP9_SEGMENT_FEATURE_ENABLED(V4L2_VP9_SEG_LVL_ALT_Q));
    assert(submitted_frame.seg.feature_data[0][V4L2_VP9_SEG_LVL_ALT_Q] == 17);
}

static void references(void)
{
    for (unsigned int missing = 0; missing < 3; missing++) {
        begin(); header(true, false, false, 0);
        pic.pic_fields.bits.last_ref_frame = 0;
        pic.pic_fields.bits.golden_ref_frame = 1;
        pic.pic_fields.bits.alt_ref_frame = 2;
        pic.reference_frames[missing] = VA_INVALID_SURFACE;
        assert(picture() == VA_STATUS_SUCCESS);
        assert(render() == VA_STATUS_ERROR_INVALID_SURFACE);
        assert(!appended && !submitted);
        assert(vp9_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    }
    struct v4l2r_context other = {0};
    struct v4l2r_surface *surface = V4L2R_SURFACE(&drv, ref);
    for (unsigned int missing = 0; missing < 3; missing++) {
        begin(); header(true, false, false, 0);
        surface->ctx = missing == 0 ? NULL : missing == 1 ? &other : &ctx;
        surface->capture_index = missing == 2 ? -1 : 0;
        assert(picture() == VA_STATUS_SUCCESS);
        assert(render() == VA_STATUS_ERROR_INVALID_SURFACE);
        assert(!appended && !submitted);
        assert(vp9_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    }
    surface->ctx = &ctx; surface->capture_index = 0;
    begin(); header(true, false, false, 0);
    pic.reference_frames[7] = VA_INVALID_SURFACE; /* Unused slot is allowed. */
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 1);
    begin(); header(false, false, false, 0);
    for (unsigned int i = 0; i < 8; i++) pic.reference_frames[i] = VA_INVALID_SURFACE;
    assert(picture() == VA_STATUS_SUCCESS && render() == VA_STATUS_SUCCESS);
    assert(vp9_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 1);
}

static void parser_inputs(void)
{
    uint8_t zeros[9] = {0};
    struct vp9_bool b;
    vp9_bool_init(&b, zeros, sizeof(zeros));
    for (unsigned int i = 0; i < 58; i++)
        vp9_bool_bit(&b, 128);
    assert(b.count < 0 && b.buffer < b.end);
    assert(vp9_bool_valid(&b)); /* A final symbol may leave a refill pending. */
    for (unsigned int i = 0; i < 8; i++)
        vp9_bool_bit(&b, 128);
    assert(!vp9_bool_valid(&b)); /* Actual end-of-input exhaustion. */
    uint32_t seed = 0x9a010;
    for (unsigned int n = 0; n < 24000; n++) {
        begin(); header(false, false, false, 0);
        codec.va_pic = pic;
        /* Retain a plausible picture while fuzzing both parser entry points. */
        for (unsigned int i = 0; i < sizeof(data); i++) {
            seed = seed * 1664525u + 1013904223u;
            data[i] = seed >> 24;
        }
        vp9_parse_uncompressed_header(&codec, data, n % sizeof(data));
        vp9_parse_compressed_header(&codec, data, n % sizeof(data));
    }
}

int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    assert(argc == 2);
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!v4l2r_handles_init(&drv.surfaces, 0));
    ref = v4l2r_handles_alloc(&drv.surfaces, sizeof(struct v4l2r_surface));
    assert(ref != VA_INVALID_SURFACE);
    V4L2R_SURFACE(&drv, ref)->ctx = &ctx;
    V4L2R_SURFACE(&drv, ref)->capture_index = 0;
    ctx.nb_captures = 1;
    ctx.captures[0].surface = V4L2R_SURFACE(&drv, ref);
    if (!strcmp(argv[1], "malformed")) malformed();
    else if (!strcmp(argv[1], "state")) persistent_state();
    else if (!strcmp(argv[1], "parser")) parser_inputs();
    else if (!strcmp(argv[1], "references")) references();
    else assert(!"unknown case");
    v4l2r_handles_destroy(&drv.surfaces);
    pthread_mutex_destroy(&drv.mutex);
    return 0;
}
