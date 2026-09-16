/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Legal/illegal VA-API sequences with intercepted device operations. No
 * decoder is opened: /dev paths return ENODEV, ioctls fail closed. */
#define _GNU_SOURCE
#include "fuzz-common.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <sys/mman.h>

#include "v4l2_request.h"

#define FUZZ_MAX_OBJECTS 8

enum {
	OP_NOP = 0,
	OP_BEGIN = 1,
	OP_RENDER = 2,
	OP_END = 3,
	OP_SYNC = 4,
	OP_DESTROY_SURFACE = 5,
	OP_CREATE_SURFACE = 6,
	OP_DESTROY_BUFFER = 7,
	OP_CREATE_BUFFER = 8,
	OP_DESTROY_CONTEXT = 9,
	OP_CREATE_CONTEXT = 10,
	OP_BEGIN_INVALID = 11,
	OP_RENDER_NEGATIVE = 12,
	OP_DOUBLE_BEGIN = 13,
	OP_DESTROY_DURING = 14,
	OP_CREATE_CONFIG = 15,
};

static __thread int alloc_guard;
static __thread size_t alloc_used;
static unsigned device_opens;
static struct v4l2r_driver drv;
static struct VADriverContext va;
static struct v4l2r_context *ctx;
static VAContextID cid;
static VASurfaceID sid;
static VABufferID bid;
static VAStatus begin_status, render_status;
static FILE *diag_sink;

static uint32_t first_live_id(struct v4l2r_handles *handles)
{
	unsigned int iter = 0;
	uint32_t id = 0;

	pthread_mutex_lock(&drv.mutex);
	if (!v4l2r_handles_next(handles, &iter, &id))
		id = 0;
	pthread_mutex_unlock(&drv.mutex);
	return id;
}

static VAStatus begin_codec(struct v4l2r_context *c)
{
	(void)c;
	return begin_status;
}

static VAStatus render_codec(struct v4l2r_context *c, struct v4l2r_buffer *b)
{
	(void)c;
	(void)b;
	return render_status;
}

static VAStatus end_codec(struct v4l2r_context *c)
{
	(void)c;
	return VA_STATUS_SUCCESS;
}

static const struct v4l2r_codec fuzz_codec = {
	.name = "fuzz",
	.begin_picture = begin_codec,
	.render_buffer = render_codec,
	.end_picture = end_codec,
};

void *__real_malloc(size_t size);
void *__real_calloc(size_t n, size_t size);
void *__real_realloc(void *ptr, size_t size);
void __real_free(void *ptr);
int __real_open(const char *path, int flags, ...);
int __real_open64(const char *path, int flags, ...);

void *__wrap_malloc(size_t size)
{
	void *p;

	/* Only the in-target picture sequence is charged. libFuzzer, libc++
	 * and process startup must not see NULL from this wrap. */
	if (alloc_guard &&
	    (size > FUZZ_ALLOC_BUDGET || alloc_used + size > FUZZ_ALLOC_BUDGET))
		return NULL;
	p = __real_malloc(size);
	if (p && alloc_guard)
		alloc_used += size;
	return p;
}

void *__wrap_calloc(size_t n, size_t size)
{
	void *p;
	size_t bytes;

	if (!n || !size)
		return __real_calloc(n, size);
	bytes = n * size;
	if (alloc_guard) {
		if (size && n > FUZZ_ALLOC_BUDGET / size)
			return NULL;
		if (alloc_used + bytes > FUZZ_ALLOC_BUDGET)
			return NULL;
	}
	p = __real_calloc(n, size);
	if (p && alloc_guard)
		alloc_used += bytes;
	return p;
}

void *__wrap_realloc(void *ptr, size_t size)
{
	void *p;

	if (alloc_guard &&
	    (size > FUZZ_ALLOC_BUDGET || alloc_used + size > FUZZ_ALLOC_BUDGET))
		return NULL;
	p = __real_realloc(ptr, size);
	if (p && alloc_guard)
		alloc_used += size;
	return p;
}

void __wrap_free(void *ptr)
{
	__real_free(ptr);
}

static int deny_dev(const char *path)
{
	if (path && strncmp(path, "/dev/", 5) == 0 && strcmp(path, "/dev/null") != 0) {
		device_opens++;
		fprintf(stderr, "fuzz-harness: device-open %s\n", path);
		errno = ENODEV;
		return -1;
	}
	return 0;
}

int __wrap_open(const char *path, int flags, ...)
{
	mode_t mode = 0;
	va_list ap;

	if (deny_dev(path))
		return -1;
	va_start(ap, flags);
	if (flags & O_CREAT)
		mode = (mode_t)va_arg(ap, int);
	va_end(ap);
	if (flags & O_CREAT)
		return __real_open(path, flags, mode);
	return __real_open(path, flags);
}

int __wrap_open64(const char *path, int flags, ...)
{
	mode_t mode = 0;
	va_list ap;

	if (deny_dev(path))
		return -1;
	va_start(ap, flags);
	if (flags & O_CREAT)
		mode = (mode_t)va_arg(ap, int);
	va_end(ap);
	if (flags & O_CREAT)
		return __real_open64(path, flags, mode);
	return __real_open64(path, flags);
}

int __wrap___open_2(const char *path, int flags)
{
	return __wrap_open(path, flags);
}

int __wrap___open64_2(const char *path, int flags)
{
	return __wrap_open64(path, flags);
}

int __wrap_ioctl(int fd, unsigned long request, ...)
{
	(void)fd;
	(void)request;
	errno = ENODEV;
	return -1;
}

int __wrap_poll(struct pollfd *fds, nfds_t count, int timeout)
{
	(void)timeout;
	for (nfds_t i = 0; i < count; i++)
		fds[i].revents = 0;
	errno = ENODEV;
	return -1;
}

int __wrap___poll_chk(struct pollfd *fds, nfds_t count, int timeout, size_t size)
{
	(void)size;
	return __wrap_poll(fds, count, timeout);
}

static void teardown(void)
{
	uint32_t id;

	if (ctx && ctx->in_picture)
		(void)v4l2r_EndPicture(&va, cid);
	while ((id = first_live_id(&drv.buffers))) {
		if (v4l2r_DestroyBuffer(&va, id) != VA_STATUS_SUCCESS)
			break;
	}
	while ((id = first_live_id(&drv.surfaces))) {
		VASurfaceID surface = id;

		if (v4l2r_DestroySurfaces(&va, &surface, 1) != VA_STATUS_SUCCESS)
			break;
	}
	while ((id = first_live_id(&drv.contexts))) {
		if (v4l2r_DestroyContext(&va, id) != VA_STATUS_SUCCESS)
			break;
		if (id == cid)
			ctx = NULL;
	}
	while ((id = first_live_id(&drv.configs))) {
		if (v4l2r_DestroyConfig(&va, id) != VA_STATUS_SUCCESS)
			break;
	}
	if (ctx) {
		pthread_mutex_destroy(&ctx->mutex);
		ctx = NULL;
	}
	v4l2r_handles_destroy(&drv.configs);
	v4l2r_handles_destroy(&drv.contexts);
	v4l2r_handles_destroy(&drv.surfaces);
	v4l2r_handles_destroy(&drv.buffers);
	v4l2r_handles_destroy(&drv.images);
	pthread_mutex_destroy(&drv.mutex);
	pthread_mutex_destroy(&drv.api_mutex);
	memset(&drv, 0, sizeof(drv));
	memset(&va, 0, sizeof(va));
	cid = VA_INVALID_ID;
	sid = VA_INVALID_SURFACE;
	bid = VA_INVALID_ID;
	if (diag_sink) {
		fclose(diag_sink);
		diag_sink = NULL;
		v4l2r_diag_configure(NULL);
	}
}

static int setup(void)
{
	begin_status = render_status = VA_STATUS_SUCCESS;
	memset(&drv, 0, sizeof(drv));
	memset(&va, 0, sizeof(va));
	if (pthread_mutex_init(&drv.mutex, NULL))
		return -1;
	if (pthread_mutex_init(&drv.api_mutex, NULL)) {
		pthread_mutex_destroy(&drv.mutex);
		return -1;
	}
	if (v4l2r_handles_init(&drv.configs, V4L2R_ID_OFFSET_CONFIG) ||
	    v4l2r_handles_init(&drv.contexts, V4L2R_ID_OFFSET_CONTEXT) ||
	    v4l2r_handles_init(&drv.surfaces, V4L2R_ID_OFFSET_SURFACE) ||
	    v4l2r_handles_init(&drv.buffers, V4L2R_ID_OFFSET_BUFFER) ||
	    v4l2r_handles_init(&drv.images, V4L2R_ID_OFFSET_IMAGE)) {
		teardown();
		return -1;
	}
	va.pDriverData = &drv;
	cid = v4l2r_handles_alloc(&drv.contexts, sizeof(*ctx));
	ctx = V4L2R_CONTEXT(&drv, cid);
	if (!ctx || pthread_mutex_init(&ctx->mutex, NULL)) {
		teardown();
		return -1;
	}
	ctx->drv = &drv;
	ctx->codec = &fuzz_codec;
	ctx->video_fd = -1;
	ctx->media_fd = -1;
	diag_sink = fopen("/dev/null", "w");
	if (diag_sink) {
		struct v4l2r_diag_options options = {
			.mode = V4L2R_DIAG_MODE_TEXT,
			.sink = diag_sink,
		};

		v4l2r_diag_configure(&options);
	}
	if (v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, 64, 64, &sid, 1, NULL, 0) !=
	    VA_STATUS_SUCCESS) {
		teardown();
		return -1;
	}
	if (v4l2r_CreateBuffer(&va, cid, VAPictureParameterBufferType, 1, 1, NULL, &bid) !=
	    VA_STATUS_SUCCESS) {
		teardown();
		return -1;
	}
	return 0;
}

static uint8_t take(const uint8_t **data, size_t *size)
{
	uint8_t v;

	if (!*size)
		return 0;
	v = **data;
	(*data)++;
	(*size)--;
	return v;
}

static VAStatus observe_status(VAStatus status)
{
    fuzz_oracle_mix(status);
    return status;
}

static void run_opcodes(const uint8_t *data, size_t size)
{
	unsigned extra_surfaces = 0, extra_buffers = 0;
	VASurfaceID extras[FUZZ_MAX_OBJECTS];
	VABufferID extra_bufs[FUZZ_MAX_OBJECTS];
	unsigned steps = 0;

	while (size && steps++ < 64) {
		uint8_t op = take(&data, &size);

		switch (op) {
		case OP_BEGIN:
			(void)observe_status(v4l2r_BeginPicture(&va, cid, sid));
			break;
		case OP_RENDER:
			(void)observe_status(v4l2r_RenderPicture(&va, cid, &bid, 1));
			break;
		case OP_END:
			(void)observe_status(v4l2r_EndPicture(&va, cid));
			break;
		case OP_SYNC:
			(void)observe_status(v4l2r_SyncSurface(&va, sid));
			break;
		case OP_DESTROY_SURFACE: {
			VASurfaceID victim = sid;
			uint8_t idx = take(&data, &size);

			if (extra_surfaces && idx < extra_surfaces)
				victim = extras[idx % extra_surfaces];
			(void)observe_status(v4l2r_DestroySurfaces(&va, &victim, 1));
			break;
		}
		case OP_CREATE_SURFACE:
			if (extra_surfaces < FUZZ_MAX_OBJECTS) {
				unsigned w = 8u + (take(&data, &size) & 63u);
				unsigned h = 8u + (take(&data, &size) & 63u);

				if (observe_status(v4l2r_CreateSurfaces2(&va, VA_RT_FORMAT_YUV420, w, h,
							  &extras[extra_surfaces], 1, NULL, 0)) ==
				    VA_STATUS_SUCCESS)
					extra_surfaces++;
			}
			break;
		case OP_DESTROY_BUFFER: {
			VABufferID victim = bid;
			uint8_t idx = take(&data, &size);

			if (extra_buffers && idx < extra_buffers)
				victim = extra_bufs[idx % extra_buffers];
			(void)observe_status(v4l2r_DestroyBuffer(&va, victim));
			break;
		}
		case OP_CREATE_BUFFER:
			if (extra_buffers < FUZZ_MAX_OBJECTS) {
				unsigned n = 1u + (take(&data, &size) & 7u);
				unsigned sz = 1u + (take(&data, &size) & 63u);

				if (observe_status(v4l2r_CreateBuffer(&va, cid, VASliceDataBufferType, sz, n,
						       NULL, &extra_bufs[extra_buffers])) ==
				    VA_STATUS_SUCCESS)
					extra_buffers++;
			}
			break;
		case OP_DESTROY_CONTEXT:
			(void)observe_status(v4l2r_DestroyContext(&va, cid));
			ctx = NULL;
			break;
		case OP_CREATE_CONTEXT: {
			VAContextID created = VA_INVALID_ID;
			VAConfigID cfg = (VAConfigID)((unsigned)take(&data, &size) << 24);
			int w = (int)take(&data, &size);
			int h = (int)take(&data, &size);
			int n = (int)(take(&data, &size) % 4u) - 1;

			(void)observe_status(v4l2r_CreateContext(&va, cfg, w, h, 0, n > 0 ? &sid : NULL, n,
						  &created));
			break;
		}
		case OP_BEGIN_INVALID:
			(void)observe_status(v4l2r_BeginPicture(&va, 0xdeadbeefu, sid));
			(void)observe_status(v4l2r_BeginPicture(&va, cid, VA_INVALID_SURFACE));
			break;
		case OP_RENDER_NEGATIVE:
			(void)observe_status(v4l2r_RenderPicture(&va, cid, NULL, -1));
			(void)observe_status(v4l2r_RenderPicture(&va, cid, NULL, 1));
			break;
		case OP_DOUBLE_BEGIN:
			(void)observe_status(v4l2r_BeginPicture(&va, cid, sid));
			(void)observe_status(v4l2r_BeginPicture(&va, cid, sid));
			break;
		case OP_DESTROY_DURING:
			(void)observe_status(v4l2r_BeginPicture(&va, cid, sid));
			(void)observe_status(v4l2r_DestroySurfaces(&va, &sid, 1));
			(void)observe_status(v4l2r_EndPicture(&va, cid));
			break;
		case OP_CREATE_CONFIG: {
			VAConfigID cfg = VA_INVALID_ID;

			(void)observe_status(v4l2r_CreateConfig(&va, VAProfileH264Main, VAEntrypointVLD,
						 NULL, 0, &cfg));
			if (cfg != VA_INVALID_ID)
				(void)observe_status(v4l2r_DestroyConfig(&va, cfg));
			break;
		}
		default:
			break;
		}
	}
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	fuzz_oracle_reset();
	fuzz_begin_budget();
	alloc_used = 0;
	device_opens = 0;
	alloc_guard = 1;
	size = fuzz_cap_size(size);
	if (setup() == 0) {
		run_opcodes(data, size);
        fuzz_oracle_mix(ctx ? ctx->in_picture : 0);
        struct v4l2r_handles *tables[] = {
            &drv.configs, &drv.contexts, &drv.surfaces, &drv.buffers, &drv.images
        };
        for (unsigned i = 0; i < sizeof(tables) / sizeof(tables[0]); i++) {
            unsigned iter = 0, count = 0;
            uint32_t id;
            while (v4l2r_handles_next(tables[i], &iter, &id)) {
                fuzz_oracle_mix(id);
                count++;
            }
            fuzz_oracle_mix(count);
        }
		if (device_opens)
			fuzz_oracle_last.flags |= FUZZ_ORACLE_ERROR;
		fuzz_oracle_last.extra = device_opens;
		fuzz_oracle_mix(device_opens);
		fuzz_oracle_mix(cid);
		fuzz_oracle_mix(sid);
		fuzz_oracle_mix(bid);
		fuzz_oracle_mix(size ? data[0] : 0);
		teardown();
	}
	alloc_guard = 0;
	fuzz_end_budget();
	return 0;
}
