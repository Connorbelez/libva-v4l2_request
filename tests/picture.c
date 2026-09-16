/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Public picture lifecycle with an intercepted codec; no device is opened. */
#include <assert.h>
#include <string.h>
#include <sys/resource.h>
#include "v4l2_request.h"

static struct v4l2r_driver drv;
static struct VADriverContext va;
static struct v4l2r_context *ctx;
static VAContextID cid;
static VASurfaceID sid;
static VABufferID bid;
static unsigned int renders, submissions;
static VAStatus begin_status, render_status;

static VAStatus begin_codec(struct v4l2r_context *c)
{
    (void)c;
    return begin_status;
}
static VAStatus render_codec(struct v4l2r_context *c, struct v4l2r_buffer *b)
{
    (void)c; (void)b;
    renders++;
    return render_status;
}
static VAStatus end_codec(struct v4l2r_context *c)
{
    assert(c->pic.target && c->pic.output);
    assert(c->pic.target->id == sid);
    submissions++;
    return VA_STATUS_SUCCESS;
}
static const struct v4l2r_codec codec = {
    .begin_picture = begin_codec, .render_buffer = render_codec,
    .end_picture = end_codec,
};

static void begin(void)
{
    renders = submissions = 0;
    render_status = begin_status = VA_STATUS_SUCCESS;
    assert(v4l2r_BeginPicture(&va, cid, sid) == VA_STATUS_SUCCESS);
}
static void render_errors(void)
{
    VABufferID bad = VA_INVALID_ID;
    /* A bad handle following a valid buffer must poison the whole picture. */
    begin();
    VABufferID list[] = {bid, bad};
    assert(v4l2r_RenderPicture(&va, cid, list, 2) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(renders == 1);
    assert(v4l2r_RenderPicture(&va, cid, &bid, 1) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(renders == 1);
    assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(!submissions && !ctx->in_picture && !ctx->pic.target && !ctx->pic.output);
    assert(v4l2r_SyncSurface(&va, sid) == VA_STATUS_ERROR_INVALID_BUFFER);
    /* Errors returned by a codec also stop generic EndPicture submission. */
    begin(); render_status = VA_STATUS_ERROR_UNSUPPORTED_BUFFERTYPE;
    assert(v4l2r_RenderPicture(&va, cid, &bid, 1) == render_status);
    assert(v4l2r_EndPicture(&va, cid) == render_status && !submissions);
    for (unsigned int n = 0; n < 2; n++) {
        begin();
        assert(v4l2r_RenderPicture(&va, cid, n ? &bid : NULL, n ? -1 : 1) ==
               VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_ERROR_INVALID_PARAMETER);
        assert(!submissions);
    }
    begin();
    assert(v4l2r_RenderPicture(&va, cid, NULL, 0) == VA_STATUS_SUCCESS);
    assert(v4l2r_RenderPicture(&va, cid, &bid, 1) == VA_STATUS_SUCCESS);
    assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_SUCCESS && submissions == 1);
}
static void begin_errors(void)
{
    begin();
    struct v4l2r_output_buffer *out = ctx->pic.output;
    assert(v4l2r_BeginPicture(&va, cid, sid) == VA_STATUS_ERROR_OPERATION_FAILED);
    assert(ctx->pic.output == out && ctx->in_picture);
    assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_SUCCESS);
    begin_status = VA_STATUS_ERROR_INVALID_PARAMETER;
    assert(v4l2r_BeginPicture(&va, cid, sid) == begin_status);
    assert(!ctx->in_picture && !ctx->pic.output && !ctx->pic.target);
    assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_ERROR_OPERATION_FAILED);
    begin();
    assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_SUCCESS);
}
static void target_lifetime(void)
{
    struct v4l2r_surface *surface = V4L2R_SURFACE(&drv, sid);
    assert(!surface->ctx);
    begin(); /* A fresh surface must become owned before its first decode. */
    assert(surface->ctx == ctx);
    VAContextID other_id = v4l2r_handles_alloc(&drv.contexts, sizeof(*ctx));
    struct v4l2r_context *other = V4L2R_CONTEXT(&drv, other_id);
    other->drv = &drv; other->codec = &codec;
    assert(v4l2r_BeginPicture(&va, other_id, sid) == VA_STATUS_ERROR_SURFACE_BUSY);
    VASurfaceID spare;
    assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, &spare, 1, NULL, 0) == VA_STATUS_SUCCESS);
    VASurfaceID list[] = {spare, sid};
    assert(v4l2r_DestroySurfaces(&va, list, 2) == VA_STATUS_ERROR_SURFACE_BUSY);
    assert(V4L2R_SURFACE(&drv, spare)); /* No partial destruction of the list. */
    assert(v4l2r_DestroySurfaces(&va, &sid, 1) == VA_STATUS_ERROR_SURFACE_BUSY);
    assert(V4L2R_SURFACE(&drv, sid) == surface);
    assert(v4l2r_EndPicture(&va, cid) == VA_STATUS_SUCCESS);
    assert(v4l2r_DestroySurfaces(&va, &sid, 1) == VA_STATUS_SUCCESS);
    assert(!V4L2R_SURFACE(&drv, sid));
}
static void reference_owner(void)
{
    struct v4l2r_surface *surface = V4L2R_SURFACE(&drv, sid);
    struct v4l2r_context other = {.in_picture = true};
    ctx->in_picture = true;
    ctx->nb_captures = 1;
    ctx->captures[0].surface = surface;
    surface->capture_index = 0;
    surface->ctx = &other;
    /* Matching buffer indices in different contexts must not alias. */
    assert(v4l2r_surface_timestamp(ctx, sid) == 0);
    assert(!other.pic.ref_mask && !ctx->pic.ref_mask);
    surface->ctx = ctx;
    assert(v4l2r_surface_timestamp(ctx, sid) == 1000);
    assert(ctx->pic.ref_mask == 1);
    ctx->pic.ref_mask = 0;
    surface->decode_status = VA_STATUS_ERROR_DECODING_ERROR;
    assert(v4l2r_surface_timestamp(ctx, sid) == 0 && !ctx->pic.ref_mask);
    surface->decode_status = VA_STATUS_SUCCESS;
    surface->capture_index = 64;
    assert(v4l2r_surface_timestamp(ctx, sid) == 0 && !ctx->pic.ref_mask);
    surface->capture_index = 0;
    ctx->captures[0].surface = NULL;
    assert(v4l2r_surface_timestamp(ctx, sid) == 0 && !ctx->pic.ref_mask);
}
static void context_parameters(void)
{
    VAConfigID config_id = v4l2r_handles_alloc(&drv.configs, sizeof(struct v4l2r_config));
    struct v4l2r_config *config = V4L2R_CONFIG(&drv, config_id);
    config->codec = &codec;
    VAContextID created = VA_INVALID_ID;
    VASurfaceID invalid = VA_INVALID_SURFACE;
    assert(v4l2r_CreateContext(&va, config_id, 64, 64, 0, NULL, 1, &created) == VA_STATUS_ERROR_INVALID_PARAMETER);
    assert(v4l2r_CreateContext(&va, config_id, 64, 64, 0, &sid, -1, &created) == VA_STATUS_ERROR_INVALID_PARAMETER);
    assert(v4l2r_CreateContext(&va, config_id, 0, 64, 0, NULL, 0, &created) == VA_STATUS_ERROR_INVALID_PARAMETER);
    assert(v4l2r_CreateContext(&va, config_id, 64, -1, 0, NULL, 0, &created) == VA_STATUS_ERROR_INVALID_PARAMETER);
    assert(v4l2r_CreateContext(&va, config_id, 64, 64, 0, NULL, 0, NULL) == VA_STATUS_ERROR_INVALID_PARAMETER);
    assert(v4l2r_CreateContext(&va, config_id, 64, 64, 0, &invalid, 1, &created) == VA_STATUS_ERROR_INVALID_SURFACE);
    assert(created == VA_INVALID_ID);
    unsigned int iter = 0, count = 0;
    while (v4l2r_handles_next(&drv.contexts, &iter, NULL)) count++;
    assert(count == 1); /* Invalid arguments allocated no extra context. */
}
int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    assert(argc == 2);
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!v4l2r_handles_init(&drv.configs, V4L2R_ID_OFFSET_CONFIG));
    assert(!v4l2r_handles_init(&drv.contexts, V4L2R_ID_OFFSET_CONTEXT));
    assert(!v4l2r_handles_init(&drv.surfaces, V4L2R_ID_OFFSET_SURFACE));
    assert(!v4l2r_handles_init(&drv.buffers, V4L2R_ID_OFFSET_BUFFER));
    va.pDriverData = &drv;
    cid = v4l2r_handles_alloc(&drv.contexts, sizeof(*ctx));
    ctx = V4L2R_CONTEXT(&drv, cid);
    assert(ctx && !pthread_mutex_init(&ctx->mutex, NULL));
    ctx->drv = &drv; ctx->codec = &codec;
    assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, &sid, 1, NULL, 0) == VA_STATUS_SUCCESS);
    assert(v4l2r_CreateBuffer(&va, cid, VAPictureParameterBufferType, 1, 1, NULL, &bid) == VA_STATUS_SUCCESS);
    if (!strcmp(argv[1], "render-errors")) render_errors();
    else if (!strcmp(argv[1], "begin-errors")) begin_errors();
    else if (!strcmp(argv[1], "target-lifetime")) target_lifetime();
    else if (!strcmp(argv[1], "reference-owner")) reference_owner();
    else if (!strcmp(argv[1], "context-parameters")) context_parameters();
    else assert(!"unknown case");
    assert(v4l2r_DestroyBuffer(&va, bid) == VA_STATUS_SUCCESS);
    pthread_mutex_destroy(&ctx->mutex);
    v4l2r_handles_destroy(&drv.configs);
    v4l2r_handles_destroy(&drv.contexts);
    v4l2r_handles_destroy(&drv.surfaces);
    v4l2r_handles_destroy(&drv.buffers);
    pthread_mutex_destroy(&drv.mutex);
    return 0;
}
