/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Diagnostic categories, context identity, redaction and rate limits, driven
 * through the real VA entrypoints with an in-memory V4L2 device. No hardware. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#include <linux/media.h>
#include "v4l2_request.h"

static struct v4l2r_driver drv;
static struct v4l2r_context *ctx;
static VAContextID cid;
static struct VADriverContext va;
static struct v4l2r_surface *surface;
static VASurfaceID sid;
static int capture_available, capture_flags, poll_ready = 1;
static short poll_extra;
static unsigned long fail_request;
static int fail_errno;
static unsigned char pixels[64 * 6];

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (fail_request && request == fail_request) {
        errno = fail_errno;
        return -1;
    }
    if (request == MEDIA_REQUEST_IOC_REINIT || request == MEDIA_REQUEST_IOC_QUEUE ||
        request == VIDIOC_STREAMOFF || request == VIDIOC_QBUF ||
        request == VIDIOC_S_EXT_CTRLS)
        return 0;
    if (request == VIDIOC_DQBUF) {
        struct v4l2_buffer *b = arg;
        if (V4L2_TYPE_IS_OUTPUT(b->type) || !capture_available) {
            errno = EAGAIN;
            return -1;
        }
        capture_available = 0;
        b->index = 0;
        b->flags = capture_flags;
        return 0;
    }
    fprintf(stderr, "unexpected ioctl %#lx\n", request);
    abort();
}

int __wrap_poll(struct pollfd *fds, nfds_t count, int timeout)
{
    (void)timeout;
    for (nfds_t i = 0; i < count; i++)
        fds[i].revents = poll_ready ? fds[i].events | poll_extra : 0;
    return poll_ready ? (int)count : 0;
}

int __wrap___poll_chk(struct pollfd *fds, nfds_t count, int timeout, size_t size)
{
    assert(count <= size / sizeof(*fds));
    return __wrap_poll(fds, count, timeout);
}

static void setup(void)
{
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!pthread_mutex_init(&drv.api_mutex, NULL));
    assert(!v4l2r_handles_init(&drv.configs, V4L2R_ID_OFFSET_CONFIG));
    assert(!v4l2r_handles_init(&drv.surfaces, V4L2R_ID_OFFSET_SURFACE));
    assert(!v4l2r_handles_init(&drv.images, V4L2R_ID_OFFSET_IMAGE));
    assert(!v4l2r_handles_init(&drv.buffers, V4L2R_ID_OFFSET_BUFFER));
    assert(!v4l2r_handles_init(&drv.contexts, V4L2R_ID_OFFSET_CONTEXT));
    va.pDriverData = &drv;
    assert(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 3, 3,
                                 &sid, 1, NULL, 0) == VA_STATUS_SUCCESS);
    surface = V4L2R_SURFACE(&drv, sid);

    /* A registered, streaming decode context, like a CreateContext result. */
    cid = v4l2r_handles_alloc(&drv.contexts, sizeof(*ctx));
    ctx = V4L2R_CONTEXT(&drv, cid);
    assert(ctx && !pthread_mutex_init(&ctx->mutex, NULL));
    ctx->drv = &drv;
    ctx->id = cid;
    ctx->diag_serial = v4l2r_diag_context_serial();
    ctx->profile = VAProfileH264Main;
    ctx->video_fd = 100;
    ctx->media_fd = -1;
    ctx->streaming = true;
    ctx->nb_captures = 1;
    ctx->capture_memory = V4L2_MEMORY_MMAP;
    ctx->capture_format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    ctx->capture_format.fmt.pix_mp = (struct v4l2_pix_format_mplane) {
        .width = 4, .height = 4, .pixelformat = V4L2_PIX_FMT_NV12,
        .num_planes = 1, .plane_fmt = {{ .bytesperline = 64, .sizeimage = sizeof(pixels) }},
    };
    ctx->output_format.type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
    for (unsigned int i = 0; i < V4L2R_OUTPUT_BUFFERS; i++)
        ctx->output[i].request_fd = -1;
    ctx->captures[0].surface = surface;
    ctx->captures[0].nb_planes = 1;
    ctx->captures[0].plane_size[0] = sizeof(pixels);
    for (unsigned int i = 0; i < VIDEO_MAX_PLANES; i++)
        ctx->captures[0].dmabuf_fd[i] = -1;
    surface->ctx = ctx;
    surface->capture_index = 0;
}

/* --- category scenarios; each returns the VA status the client sees --- */

static VAStatus scenario_unsupported(void)
{
    VAConfigID config;

    /* No decoder was enumerated, so no codec profile is drivable. */
    return v4l2r_CreateConfig(&va, VAProfileH264Main, VAEntrypointVLD,
                              NULL, 0, &config);
}

static VAStatus scenario_client(void)
{
    return v4l2r_EndPicture(&va, cid);
}

static VAStatus scenario_bitstream(void)
{
    ctx->pic.output = &ctx->output[0];
    return v4l2r_append_output(ctx, pixels, SIZE_MAX);
}

static VAStatus scenario_allocation(void)
{
    /* First decode into a fresh surface grows the CAPTURE pool. */
    ctx->captures[0].surface = NULL;
    surface->capture_index = -1;
    surface->ctx = NULL;
    fail_request = VIDIOC_CREATE_BUFS;
    fail_errno = ENOMEM;
    return v4l2r_context_bind_surface(ctx, surface);
}

static VAStatus scenario_timeout(void)
{
    unsigned char bitstream[128] = {0};

    ctx->queued_request = 1;
    poll_ready = 0;
    ctx->output[0].addr = bitstream;
    ctx->pic.output = &ctx->output[0];
    ctx->pic.target = surface;
    return v4l2r_decode(ctx, NULL, 0, false, true);
}

static VAStatus scenario_kernel(void)
{
    unsigned char bitstream[128] = {0};
    struct v4l2_ext_control control = { .id = 1 };

    fail_request = VIDIOC_S_EXT_CTRLS;
    fail_errno = EINVAL;
    ctx->output[0].addr = bitstream;
    ctx->pic.output = &ctx->output[0];
    ctx->pic.target = surface;
    return v4l2r_decode(ctx, &control, 1, false, true);
}

static VAStatus scenario_decoder(void)
{
    ctx->queued_capture = 1;
    ctx->submitted = 1;
    surface->status = VASurfaceRendering;
    capture_available = 1;
    capture_flags = V4L2_BUF_FLAG_ERROR;
    return v4l2r_SyncSurface(&va, sid);
}

static VAStatus scenario_device(void)
{
    poll_extra = POLLNVAL;
    ctx->queued_capture = 1;
    return v4l2r_SyncSurface(&va, sid);
}

static const struct scenario {
    const char *category;
    const char *op;
    const char *errno_name;
    bool has_context;
    VAStatus (*run)(void);
} scenarios[] = {
    { "unsupported", "create-config", NULL, false, scenario_unsupported },
    { "client", "end-picture", NULL, true, scenario_client },
    { "bitstream", "bitstream-append", "EOVERFLOW", true, scenario_bitstream },
    { "allocation", "capture-alloc", "ENOMEM", true, scenario_allocation },
    { "timeout", "request-wait", "ETIMEDOUT", true, scenario_timeout },
    { "kernel", "set-controls", "EINVAL", true, scenario_kernel },
    { "decoder", "capture-dequeue", NULL, true, scenario_decoder },
    { "device", "capture-wait", "ENODEV", true, scenario_device },
};

/* Run one scenario in a child with a fresh driver, collecting its output. */
static VAStatus run_isolated(const struct scenario *s, enum v4l2r_diag_mode mode,
                             char *out, size_t size)
{
    FILE *sink = tmpfile();
    int status_pipe[2];
    VAStatus status;
    int wstatus;

    assert(sink && !pipe(status_pipe));
    fflush(NULL);
    pid_t pid = fork();
    assert(pid >= 0);
    if (!pid) {
        close(status_pipe[0]);
        v4l2r_diag_configure(&(struct v4l2r_diag_options) {
            .mode = mode, .sink = sink });
        setup();
        status = s->run();
        assert(write(status_pipe[1], &status, sizeof(status)) == sizeof(status));
        _exit(0);
    }
    close(status_pipe[1]);
    assert(read(status_pipe[0], &status, sizeof(status)) == sizeof(status));
    close(status_pipe[0]);
    assert(waitpid(pid, &wstatus, 0) == pid && WIFEXITED(wstatus) &&
           !WEXITSTATUS(wstatus));

    rewind(sink);
    size_t n = fread(out, 1, size - 1, sink);
    out[n] = '\0';
    fclose(sink);
    return status;
}

static bool line_has(const char *line, size_t len, const char *needle)
{
    return memmem(line, len, needle, strlen(needle)) != NULL;
}

/* Find the JSON line carrying the scenario's category and operation. */
static const char *find_record(const char *output, const char *category,
                               const char *op, size_t *len)
{
    char cat[64], opfield[64];

    snprintf(cat, sizeof(cat), "\"category\":\"%s\"", category);
    snprintf(opfield, sizeof(opfield), "\"op\":\"%s\"", op);
    for (const char *line = output; *line; ) {
        const char *end = strchr(line, '\n');
        size_t l = end ? (size_t)(end - line) : strlen(line);

        if (line_has(line, l, cat) && line_has(line, l, opfield)) {
            *len = l;
            return line;
        }
        line += l + (end ? 1 : 0);
    }
    return NULL;
}

static void test_categories(void)
{
    static char text[16384], json[16384];

    for (size_t i = 0; i < sizeof(scenarios) / sizeof(scenarios[0]); i++) {
        const struct scenario *s = &scenarios[i];
        VAStatus text_status = run_isolated(s, V4L2R_DIAG_MODE_TEXT, text, sizeof(text));
        VAStatus json_status = run_isolated(s, V4L2R_DIAG_MODE_JSON, json, sizeof(json));
        size_t len;
        const char *record = find_record(json, s->category, s->op, &len);

        printf("%-12s %-18s status %s\n", s->category, s->op, vaErrorStr(json_status));
        /* Diagnostic mode must never change what the client gets back. */
        assert(text_status == json_status);
        assert(json_status != VA_STATUS_SUCCESS);
        if (!record) {
            fprintf(stderr, "no %s/%s record in:\n%s", s->category, s->op, json);
            abort();
        }
        if (s->errno_name) {
            char field[64];
            snprintf(field, sizeof(field), "\"errno\":\"%s\"", s->errno_name);
            assert(line_has(record, len, field));
        }
        if (s->has_context) {
            assert(line_has(record, len, "\"ctx\":"));
            assert(line_has(record, len, "\"va_context\":\"0x02000000\""));
        }
        /* Text mode is the historic plain prefix, never JSON. */
        assert(!strstr(text, "\"category\""));
        for (const char *line = text; *line; line = strchr(line, '\n') + 1) {
            assert(!strncmp(line, "libva-v4l2request: ", 19));
            if (!strchr(line, '\n'))
                break;
        }
        /* Every category is distinct from the other scenarios' records. */
        for (size_t j = 0; j < sizeof(scenarios) / sizeof(scenarios[0]); j++) {
            char other[64];
            if (!strcmp(scenarios[j].category, s->category))
                continue;
            snprintf(other, sizeof(other), "\"category\":\"%s\"", scenarios[j].category);
            assert(!strstr(json, other));
        }
    }
}

/* VAContextIDs repeat across contexts and displays; serials must not. */
static void test_context_identity(void)
{
    char *buf = NULL;
    size_t size = 0;
    FILE *sink = open_memstream(&buf, &size);
    struct v4l2r_context first = { .id = V4L2R_ID_OFFSET_CONTEXT };
    struct v4l2r_context second = { .id = V4L2R_ID_OFFSET_CONTEXT };

    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = sink });
    first.diag_serial = v4l2r_diag_context_serial();
    second.diag_serial = v4l2r_diag_context_serial();
    assert(first.diag_serial && second.diag_serial != first.diag_serial);
    v4l2r_diag(&first, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_TIMEOUT, "capture-wait",
               -ETIMEDOUT, "first");
    v4l2r_diag(&second, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_TIMEOUT, "capture-wait",
               -ETIMEDOUT, "second");
    fclose(sink);

    char a[32], b[32];
    snprintf(a, sizeof(a), "\"ctx\":%u,", first.diag_serial);
    snprintf(b, sizeof(b), "\"ctx\":%u,", second.diag_serial);
    const char *second_line = strchr(buf, '\n') + 1;
    assert(strstr(buf, a) && strstr(buf, a) < second_line);
    assert(strstr(second_line, b) && !strstr(second_line, a));
    /* Both share a VA id and the run id, so only ctx tells them apart. */
    assert(strstr(buf, "\"va_context\":\"0x02000000\"") &&
           strstr(second_line, "\"va_context\":\"0x02000000\""));
    assert(!strncmp(strstr(buf, "\"run\":"), strstr(second_line, "\"run\":"), 25));
    free(buf);

    /* Real CreateContext failures get fresh serials for a reused VA id. */
    setup();
    struct v4l2r_config *config;
    VAConfigID config_id = v4l2r_handles_alloc(&drv.configs, sizeof(*config));
    config = V4L2R_CONFIG(&drv, config_id);
    config->profile = VAProfileMPEG2Main;
    config->codec = v4l2r_codec_for_profile(VAProfileMPEG2Main);
    assert(config->codec);
    buf = NULL;
    sink = open_memstream(&buf, &size);
    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = sink });
    VAContextID ids[2];
    for (int i = 0; i < 2; i++)
        assert(v4l2r_CreateContext(&va, config_id, 64, 64, 0, NULL, 0, &ids[i]) ==
               VA_STATUS_ERROR_OPERATION_FAILED);
    fclose(sink);
    unsigned int serials[2] = {0};
    const char *line = buf;
    for (int i = 0; i < 2; i++) {
        line = strstr(line, "\"category\":\"unsupported\"");
        assert(line);
        const char *ctxfield = strstr(line, "\"ctx\":");
        assert(ctxfield && sscanf(ctxfield, "\"ctx\":%u", &serials[i]) == 1);
        assert(strstr(line, "\"va_context\":\"0x02000001\"") &&
               strstr(line, "\"codec\":\"mpeg2\""));
        line = strchr(line, '\n');
    }
    assert(serials[0] && serials[1] && serials[0] != serials[1]);
    free(buf);
}

static void test_redaction(void)
{
    char big[2048];
    char *buf = NULL;
    size_t size = 0;

    memset(big, 'A', sizeof(big) - 1);
    big[sizeof(big) - 1] = '\0';

    for (int mode = V4L2R_DIAG_MODE_TEXT; mode <= V4L2R_DIAG_MODE_JSON; mode++) {
        FILE *sink = open_memstream(&buf, &size);
        v4l2r_diag_configure(&(struct v4l2r_diag_options) { .mode = mode, .sink = sink });
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_KERNEL, "redact", -EINVAL,
                   "open /home/alice/Videos/holiday.mkv (from https://cdn.example/v?token=s3cret) "
                   "file:///tmp/clip.webm via /dev/video0 [avd] \"q\\u\" \x01\x7f\xff tab\tend");
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_KERNEL, "redact", 0,
                   "bypass /dev/../home/bob/a open:/home/carol/b <a>/tmp/dave "
                   "384x288/2 Input/output error media /dev/media0");
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_KERNEL, "redact", 0, "%s", big);
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_DEBUG, V4L2R_DIAG_CLIENT, "debug-only", 0,
                   "debug record");
        fclose(sink);

        assert(!strstr(buf, "alice") && !strstr(buf, "holiday") && !strstr(buf, "s3cret") &&
               !strstr(buf, "cdn.example") && !strstr(buf, "clip.webm"));
        assert(strstr(buf, "open <path> (from <url>) <url> via /dev/video0 [avd]"));
        assert(!strstr(buf, "bob") && !strstr(buf, "carol") && !strstr(buf, "dave"));
        assert(strstr(buf, "bypass <path> open:<path> <a><path> 384x288/2 "
                           "Input/output error media /dev/media0"));
        assert(!memchr(buf, '\x01', size) && !memchr(buf, '\x7f', size) &&
               !memchr(buf, '\t', size));
        /* Text keeps high bytes (localized strerror); JSON stays ASCII. */
        assert(!memchr(buf, '\xff', size) == (mode == V4L2R_DIAG_MODE_JSON));
        for (const char *line = buf; *line; ) {
            const char *end = strchr(line, '\n');
            assert(end && (size_t)(end - line) < 1024);
            line = end + 1;
        }
        const char *second = strchr(strchr(buf, '\n') + 1, '\n') + 1;
        const char *as = strchr(second, 'A');
        size_t run = strspn(as, "A");
        assert(run <= V4L2R_DIAG_MSG_MAX - 3 && !strncmp(as + run, "...", 3));
        if (mode == V4L2R_DIAG_MODE_TEXT) {
            assert(!strstr(buf, "debug record"));
        } else {
            assert(strstr(buf, "\"category\":\"client\",\"op\":\"debug-only\""));
            assert(strstr(buf, "\\\"q\\\\u\\\""));
        }
        free(buf);
        buf = NULL;
    }
}

static uint64_t fake_now;
static uint64_t fake_clock(void)
{
    return fake_now;
}

static unsigned int count_lines(const char *buf, const char *needle)
{
    unsigned int n = 0;
    for (const char *p = buf; (p = strstr(p, needle)); p++)
        n++;
    return n;
}

static void test_rate_limit(void)
{
    char *buf = NULL;
    size_t size = 0;
    FILE *sink = open_memstream(&buf, &size);

    fake_now = 1000;
    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = sink, .clock_ns = fake_clock });
    for (int i = 0; i < 1000; i++)
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_BITSTREAM,
                   "h264-slice-header", 0, "malformed %d", i);
    /* Another category keeps its own budget. */
    v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_TIMEOUT, "capture-wait",
               -ETIMEDOUT, "late");
    fflush(sink);
    assert(count_lines(buf, "\"op\":\"h264-slice-header\"") == 20);
    assert(count_lines(buf, "\"category\":\"timeout\"") == 1);
    assert(!strstr(buf, "\"op\":\"suppressed\""));

    fake_now += 11ull * 1000000000ull;
    v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_BITSTREAM,
               "h264-slice-header", 0, "after window");
    fflush(sink);
    assert(strstr(buf, "\"category\":\"bitstream\",\"op\":\"suppressed\",\"suppressed\":980"));
    assert(strstr(buf, "after window"));

    for (int i = 0; i < 25; i++)
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_BITSTREAM,
                   "h264-slice-header", 0, "burst %d", i);
    /* Debug records have their own budget and cannot hide warnings. */
    for (int i = 0; i < 50; i++)
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_DEBUG, V4L2R_DIAG_CLIENT, "create-config", 0,
                   "debug %d", i);
    v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_CLIENT, "end-picture", 0,
               "warning after debug burst");
    /* Lifecycle info lines are never limited. */
    for (int i = 0; i < 100; i++)
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_INFO, V4L2R_DIAG_INFO, "create-context", 0,
                   "decoding h264 via /dev/video0 [avd] (media /dev/media0)");
    v4l2r_diag_flush();
    fclose(sink);
    assert(strstr(buf, "\"suppressed\":6"));
    assert(strstr(buf, "\"category\":\"client\",\"op\":\"suppressed\",\"suppressed\":30"));
    assert(strstr(buf, "warning after debug burst"));
    assert(count_lines(buf, "decoding h264 via") == 100);
    assert(!strstr(buf, "\"category\":\"info\",\"op\":\"suppressed\""));
    assert(size < 64 * 1024);
    free(buf);

    /* The same holds for the default text lines scripts grep for. */
    buf = NULL;
    sink = open_memstream(&buf, &size);
    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_TEXT, .sink = sink, .clock_ns = fake_clock });
    for (int i = 0; i < 100; i++)
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_INFO, V4L2R_DIAG_INFO, "create-context", 0,
                   "decoding h264 via /dev/video0 [avd] (media /dev/media0)");
    v4l2r_diag_flush();
    fclose(sink);
    assert(count_lines(buf, "libva-v4l2request: decoding h264 via /dev/video0 [avd] "
                            "(media /dev/media0)\n") == 100);
    assert(!strstr(buf, "suppressed"));
    free(buf);
}

/* Bounded synthetic overhead measurement. Reported, with a loose ceiling so a
 * sanitizer build on a slow runner does not flake. */
static void test_overhead(void)
{
    FILE *sink = fopen("/dev/null", "w");
    struct v4l2r_context c = { .id = V4L2R_ID_OFFSET_CONTEXT, .diag_serial = 7,
                               .profile = VAProfileH264High };
    const unsigned int n = 20000;
    uint64_t t0, t1;

    assert(sink);
    fake_now = 1;
    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = sink, .clock_ns = fake_clock });
    t0 = v4l2r_now_ns();
    for (unsigned int i = 0; i < n; i++) {
        fake_now += 11ull * 1000000000ull; /* every record opens a new window */
        v4l2r_diag(&c, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_TIMEOUT, "capture-wait",
                   -ETIMEDOUT, "failed waiting on CAPTURE buffer %u", i);
    }
    t1 = v4l2r_now_ns();
    double emitted = (double)(t1 - t0) / n;

    t0 = v4l2r_now_ns();
    for (unsigned int i = 0; i < n; i++)
        v4l2r_diag(&c, V4L2R_DIAG_LEVEL_ERROR, V4L2R_DIAG_TIMEOUT, "capture-wait",
                   -ETIMEDOUT, "failed waiting on CAPTURE buffer %u", i);
    t1 = v4l2r_now_ns();
    double suppressed = (double)(t1 - t0) / n;

    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_TEXT, .sink = sink, .clock_ns = fake_clock });
    t0 = v4l2r_now_ns();
    for (unsigned int i = 0; i < n; i++)
        v4l2r_diag(&c, V4L2R_DIAG_LEVEL_DEBUG, V4L2R_DIAG_CLIENT, "debug", 0,
                   "debug %u", i);
    t1 = v4l2r_now_ns();
    double debug_off = (double)(t1 - t0) / n;

    printf("overhead: %u records each; json emitted %.0f ns, rate-limited %.0f ns, "
           "text-mode debug %.0f ns per call\n", n, emitted, suppressed, debug_off);
    assert(emitted < 1e6 && suppressed < 1e6 && debug_off < 1e6);
    fclose(sink);
}

/* Print one record of every category (and the driver summary) for the
 * schema validator in diag-schema.py. */
static void emit_samples(void)
{
    struct v4l2r_context c = { .id = V4L2R_ID_OFFSET_CONTEXT, .profile = VAProfileHEVCMain10,
                               .codec = v4l2r_codec_for_profile(VAProfileHEVCMain10) };
    struct v4l2r_driver d = { .nb_decoders = 1, .h264_high10 = V4L2R_H264_HIGH10_FFMPEG };

    c.diag_serial = v4l2r_diag_context_serial();
    snprintf(d.decoders[0].card, sizeof(d.decoders[0].card), "avd \"quoted\"");
    d.decoders[0].pixelformats[0] = V4L2_PIX_FMT_H264_SLICE;
    d.decoders[0].pixelformats[1] = 0x01020304;
    d.decoders[0].nb_pixelformats = 2;
    d.decoders[0].hevc_10bit = true;

    v4l2r_diag_configure(&(struct v4l2r_diag_options) {
        .mode = V4L2R_DIAG_MODE_JSON, .sink = stdout });
    v4l2r_diag_driver(&d);
    for (int cat = 0; cat < V4L2R_DIAG_NB_CATEGORIES; cat++)
        v4l2r_diag(cat % 2 ? &c : NULL, cat % 4, cat, "sample", cat ? -EIO : 0,
                   "sample %s record /home/user/private.mkv",
                   v4l2r_diag_category_name(cat));
    for (int i = 0; i < 25; i++)
        v4l2r_diag(NULL, V4L2R_DIAG_LEVEL_WARNING, V4L2R_DIAG_BITSTREAM, "sample", 0,
                   "burst");
    v4l2r_diag_flush();
    for (int cat = 0; cat < V4L2R_DIAG_NB_CATEGORIES; cat++)
        fprintf(stderr, "category %s\n", v4l2r_diag_category_name(cat));
}

int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core));
    assert(argc == 2);
    if (!strcmp(argv[1], "categories"))
        test_categories();
    else if (!strcmp(argv[1], "context-identity"))
        test_context_identity();
    else if (!strcmp(argv[1], "redaction"))
        test_redaction();
    else if (!strcmp(argv[1], "rate-limit"))
        test_rate_limit();
    else if (!strcmp(argv[1], "overhead"))
        test_overhead();
    else if (!strcmp(argv[1], "emit-samples"))
        emit_samples();
    else
        abort();
    return 0;
}
