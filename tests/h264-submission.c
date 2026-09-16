/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Exercise codec submission without opening a device. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#define v4l2r_codec_h264 v4l2r_test_codec_h264
#define v4l2r_decode test_decode
#define v4l2r_append_output test_append_output
#define v4l2r_picture_next_output test_next_output
#include "../src/codec_h264.c"

static unsigned int submitted, appended, advanced;
static struct v4l2_ctrl_h264_slice_params submitted_slice;

VAStatus test_decode(struct v4l2r_context *ctx,
                     struct v4l2_ext_control *controls, unsigned int count,
                     bool first, bool last)
{
    (void)controls; (void)count; (void)first; (void)last;
    submitted++;
    submitted_slice = ((struct h264_context *)ctx->codec_priv)->slice_params;
    return VA_STATUS_SUCCESS;
}

VAStatus test_append_output(struct v4l2r_context *ctx, const void *data, size_t size)
{
    (void)ctx;
    assert(data && size);
    appended++;
    return VA_STATUS_SUCCESS;
}

VAStatus test_next_output(struct v4l2r_context *ctx)
{
    (void)ctx;
    advanced++;
    return VA_STATUS_SUCCESS;
}

static struct v4l2r_driver drv;
static struct h264_context codec;
static struct v4l2r_context ctx = {.drv = &drv, .codec_priv = &codec,
                                  .profile = VAProfileH264Main};
static VASurfaceID ids[3];
static uint8_t data[8];
static unsigned int pos;

static void bits(unsigned int value, unsigned int n)
{
    while (n--) {
        data[pos / 8] |= ((value >> n) & 1) << (7 - pos % 8);
        pos++;
    }
}

static size_t header(unsigned int type)
{
    memset(data, 0, sizeof(data)); pos = 0;
    bits(0x41, 8); /* Non-IDR reference picture. */
    bits(1, 1); /* first_mb_in_slice = 0 */
    if (type == SLICE_P) bits(1, 1);
    else bits(type + 1, 3); /* ue(1) = 010, ue(2) = 011 */
    bits(1, 1); /* PPS = 0 */
    bits(0, 4); bits(0, 4); /* frame_num, POC */
    if (type == SLICE_B) bits(1, 1); /* spatial prediction */
    if (type != SLICE_I) {
        bits(0, 1); /* default ref counts */
        bits(0, 1); /* L0 not modified */
        if (type == SLICE_B) bits(0, 1);
    }
    bits(0, 1); /* no adaptive reference marking */
    bits(1, 1); /* QP delta = 0 */
    bits(1, 1); /* payload / RBSP trailing bit */
    return (pos + 7) / 8;
}

static void begin(void)
{
    assert(h264_begin_picture(&ctx) == VA_STATUS_SUCCESS);
    submitted = appended = advanced = 0;
    VAPictureParameterBufferH264 pic = {0};
    pic.seq_fields.bits.frame_mbs_only_flag = 1;
    for (unsigned int i = 0; i < 16; i++)
        pic.ReferenceFrames[i].flags = VA_PICTURE_H264_INVALID;
    /* One usable reference and one unavailable reference. */
    pic.ReferenceFrames[0] = (VAPictureH264){.picture_id = ids[0],
        .flags = VA_PICTURE_H264_SHORT_TERM_REFERENCE};
    pic.ReferenceFrames[1] = (VAPictureH264){.picture_id = ids[1],
        .flags = VA_PICTURE_H264_SHORT_TERM_REFERENCE};
    h264_fill_picture(&ctx, &pic);
}

static VAStatus parameters(VASliceParameterBufferH264 *slice, unsigned int count)
{
    struct v4l2r_buffer buf = {.type = VASliceParameterBufferType,
        .data = slice, .element_size = sizeof(*slice), .nb_elements = count};
    return h264_render_buffer(&ctx, &buf);
}

static VAStatus render(VASliceParameterBufferH264 *slice)
{
    assert(parameters(slice, 1) == VA_STATUS_SUCCESS);
    struct v4l2r_buffer buf = {.type = VASliceDataBufferType,
        .data = data, .element_size = slice->slice_data_size, .nb_elements = 1};
    return h264_render_buffer(&ctx, &buf);
}

static void incomplete(void)
{
    for (unsigned int mode = 0; mode < 2; mode++) {
        codec.decode_mode = mode ? V4L2_STATELESS_H264_DECODE_MODE_FRAME_BASED :
                                  V4L2_STATELESS_H264_DECODE_MODE_SLICE_BASED;
        begin();
        VASliceParameterBufferH264 slice = {.slice_type = SLICE_I,
            .slice_data_size = header(SLICE_I)};
        assert(render(&slice) == VA_STATUS_SUCCESS);
        assert(parameters(&slice, 1) == VA_STATUS_SUCCESS);
        assert(h264_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(!submitted && !advanced);
        begin(); /* A complete next picture remains usable. */
        assert(render(&slice) == VA_STATUS_SUCCESS);
        assert(h264_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 1);
    }
}

/* A NAL unit whose header byte declares the given type, with no payload: the
 * parser rejects it before reading further, which is what these cases measure. */
static size_t nal_only(unsigned int unit_type)
{
    memset(data, 0, sizeof(data));
    pos = 0;
    bits(0, 1);   /* forbidden_zero_bit */
    bits(2, 2);   /* nal_ref_idc */
    bits(unit_type, 5);
    return 1;
}

static void unsupported_partition(void)
{
    char *log = NULL;
    size_t log_size = 0;
    FILE *sink = open_memstream(&log, &log_size);
    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = sink });
    codec.decode_mode = V4L2_STATELESS_H264_DECODE_MODE_SLICE_BASED;
    begin();
    VASliceParameterBufferH264 slice = {.slice_type = SLICE_I,
        .slice_data_size = nal_only(2)};  /* data partition A */
    /* Unsupported, not malformed: the client is told to fall back. */
    assert(render(&slice) == VA_STATUS_ERROR_UNIMPLEMENTED);
    assert(codec.failed && !submitted && !appended);
    /* The rejected picture cannot be completed by a later valid slice. */
    assert(h264_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(!submitted);
    assert(fclose(sink) == 0);
    assert(strstr(log, "h264-unsupported-nal"));
    free(log);
    /* Rejection is per picture: the next picture stays usable. */
    begin();
    VASliceParameterBufferH264 valid = {.slice_type = SLICE_I,
        .slice_data_size = header(SLICE_I)};
    assert(render(&valid) == VA_STATUS_SUCCESS);
    assert(h264_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 1);
}

static void unsupported_extension(void)
{
    codec.decode_mode = V4L2_STATELESS_H264_DECODE_MODE_SLICE_BASED;
    begin();
    VASliceParameterBufferH264 slice = {.slice_type = SLICE_I,
        .slice_data_size = nal_only(20)}; /* MVC extension */
    assert(render(&slice) == VA_STATUS_ERROR_UNIMPLEMENTED);
    assert(codec.failed && !submitted && !appended);
    assert(h264_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    begin();
    VASliceParameterBufferH264 valid = {.slice_type = SLICE_I,
        .slice_data_size = header(SLICE_I)};
    assert(render(&valid) == VA_STATUS_SUCCESS);
    assert(h264_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 1);
}

static void unsupported_fmo(void)
{
    VAPictureParameterBufferH264 pic = {0};
    pic.seq_fields.bits.frame_mbs_only_flag = 1;
    pic.num_slice_groups_minus1 = 1; /* slice groups declared by the client */
    struct v4l2r_buffer buf = {.type = VAPictureParameterBufferType,
        .data = &pic, .element_size = sizeof(pic), .nb_elements = 1};
    assert(h264_render_buffer(&ctx, &buf) == VA_STATUS_ERROR_UNIMPLEMENTED);
    assert(codec.failed && !submitted && !appended);
    assert(h264_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
}

static void slice_count(void)
{
    begin();
    VASliceParameterBufferH264 slice = {0};
    codec.nb_va_slices = UINT32_MAX;
    assert(parameters(&slice, 1) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(codec.failed && codec.nb_va_slices == UINT32_MAX);
    assert(!submitted && !appended);
}

static void references(void)
{
    char *log = NULL;
    size_t log_size = 0;
    FILE *sink = open_memstream(&log, &log_size);
    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = sink });
    codec.decode_mode = V4L2_STATELESS_H264_DECODE_MODE_SLICE_BASED;
    for (unsigned int which = 0; which < 4; which++) {
        begin();
        VASliceParameterBufferH264 slice = {.slice_type = SLICE_P,
            .slice_data_size = header(SLICE_P)};
        slice.RefPicList0[0].picture_id = which == 0 ? VA_INVALID_SURFACE :
                                        which == 1 ? ids[1] : ids[2];
        if (which == 3) slice.RefPicList0[0].flags = VA_PICTURE_H264_INVALID;
        assert(render(&slice) == VA_STATUS_ERROR_INVALID_BUFFER);
        assert(codec.failed && !submitted && !appended);
        assert(h264_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    }

    begin();
    VASliceParameterBufferH264 p = {.slice_type = SLICE_P,
        .slice_data_size = header(SLICE_P)};
    p.RefPicList0[0] = (VAPictureH264){.picture_id = ids[0],
        .flags = VA_PICTURE_H264_TOP_FIELD};
    assert(render(&p) == VA_STATUS_SUCCESS);
    VASliceParameterBufferH264 b = {.slice_type = SLICE_B,
        .slice_data_size = header(SLICE_B)};
    b.RefPicList0[0].picture_id = ids[0];
    b.RefPicList1[0].picture_id = ids[2]; /* Exists, but absent from DPB. */
    assert(render(&b) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(!submitted && !advanced && appended == 1); /* Keep prior slice pending. */

    begin();
    p.slice_data_size = header(SLICE_P);
    assert(render(&p) == VA_STATUS_SUCCESS);
    b.slice_data_size = header(SLICE_B);
    b.RefPicList1[0] = (VAPictureH264){.picture_id = ids[0],
        .flags = VA_PICTURE_H264_BOTTOM_FIELD};
    assert(render(&b) == VA_STATUS_SUCCESS && submitted == 1);
    assert(submitted_slice.slice_type == SLICE_P);
    assert(submitted_slice.ref_pic_list0[0].fields == V4L2_H264_TOP_FIELD_REF);
    assert(h264_end_picture(&ctx) == VA_STATUS_SUCCESS && submitted == 2);
    assert(submitted_slice.slice_type == SLICE_B);
    assert(submitted_slice.ref_pic_list1[0].index == 0);
    assert(submitted_slice.ref_pic_list1[0].fields == V4L2_H264_BOTTOM_FIELD_REF);

    begin();
    p.slice_data_size = header(SLICE_I); /* Metadata contradicts the NAL. */
    assert(render(&p) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(!submitted && !appended);
    /* The absent reference and the contradictory header are told apart. */
    v4l2r_diag_configure(NULL);
    fclose(sink);
    unsigned int references = 0;
    const char *last = log;
    for (const char *p = log; (p = strstr(p, "\"category\":\"reference\",\"op\":\"h264-references\"")); p++, references++)
        last = p;
    const char *header = strstr(log, "\"category\":\"bitstream\",\"op\":\"h264-slice-header\"");
    assert(references == 5 && header && last < header);
    assert(!strstr(header, "\"category\":\"reference\""));
    free(log);
}

int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    assert(argc == 2);
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!v4l2r_handles_init(&drv.surfaces, 0));
    for (unsigned int i = 0; i < 3; i++) {
        ids[i] = v4l2r_handles_alloc(&drv.surfaces, sizeof(struct v4l2r_surface));
        V4L2R_SURFACE(&drv, ids[i])->capture_index = i == 1 ? -1 : (int)i;
        V4L2R_SURFACE(&drv, ids[i])->ctx = &ctx;
        ctx.captures[i].surface = V4L2R_SURFACE(&drv, ids[i]);
    }
    ctx.nb_captures = 3;
    if (!strcmp(argv[1], "incomplete")) incomplete();
    else if (!strcmp(argv[1], "slice-count")) slice_count();
    else if (!strcmp(argv[1], "references")) references();
    else if (!strcmp(argv[1], "unsupported-partition")) unsupported_partition();
    else if (!strcmp(argv[1], "unsupported-extension")) unsupported_extension();
    else if (!strcmp(argv[1], "unsupported-fmo")) unsupported_fmo();
    else assert(!"unknown case");
    h264_uninit(&ctx);
    v4l2r_handles_destroy(&drv.surfaces);
    pthread_mutex_destroy(&drv.mutex);
    return 0;
}
