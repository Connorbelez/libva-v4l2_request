/* SPDX-License-Identifier: GPL-3.0-or-later */
/* One VA display survives every decoder lifecycle. Checkpoints block before
 * the next allocation so the parent can sample this process, not its wrapper. */
#define _GNU_SOURCE
#define main frame_check_main
#include "frame-check.c"
#undef main
#include <errno.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
#include <libavutil/hwcontext_vaapi.h>
#include <va/va_drmcommon.h>

static unsigned destroyed_contexts;

VAStatus vaDestroyContext(VADisplay display, VAContextID context)
{
    VAStatus (*real_destroy)(VADisplay, VAContextID) = dlsym(RTLD_NEXT, "vaDestroyContext");
    if (!real_destroy) return VA_STATUS_ERROR_OPERATION_FAILED;
    VAStatus status = real_destroy(display, context);
    if (status == VA_STATUS_SUCCESS) destroyed_contexts++;
    return status;
}

struct held_image {
    VADisplay display;
    VAImage image;
    AVBufferRef *pool;
    int valid;
};

static int image_digest(struct held_image *held, uint8_t result[16])
{
    void *data = NULL;
    if (vaMapBuffer(held->display, held->image.buf, &data) != VA_STATUS_SUCCESS)
        return AVERROR_EXTERNAL;
    av_md5_sum(result, data, held->image.data_size);
    return vaUnmapBuffer(held->display, held->image.buf) == VA_STATUS_SUCCESS
        ? 0 : AVERROR_EXTERNAL;
}

static int receive(struct check *check, AVCodecContext *decoder, AVPacket *packet,
                   struct held_image *held)
{
    int ret = avcodec_send_packet(decoder, packet);
    if (ret < 0) return ret;
    AVFrame *frame = av_frame_alloc();
    if (!frame) return AVERROR(ENOMEM);
    while ((ret = avcodec_receive_frame(decoder, frame)) >= 0) {
        if (check->hardware) {
            if (frame->format != AV_PIX_FMT_VAAPI) { ret = AVERROR_INVALIDDATA; break; }
            if (!held->pool) {
                held->pool = av_buffer_ref(frame->hw_frames_ctx);
                if (!held->pool) { ret = AVERROR(ENOMEM); break; }
            }
            VASurfaceID surface = (VASurfaceID)(uintptr_t)frame->data[3];
            VADRMPRIMESurfaceDescriptor exported;
            if (vaSyncSurface(held->display, surface) != VA_STATUS_SUCCESS ||
                vaExportSurfaceHandle(held->display, surface,
                    VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2,
                    VA_EXPORT_SURFACE_READ_ONLY | VA_EXPORT_SURFACE_SEPARATE_LAYERS,
                    &exported) != VA_STATUS_SUCCESS) { ret = AVERROR_EXTERNAL; break; }
            if (!exported.num_objects || exported.num_objects > 4) {
                ret = AVERROR_INVALIDDATA; break;
            }
            for (unsigned i = 0; i < exported.num_objects; i++) close(exported.objects[i].fd);
            if (held->valid && vaDestroyImage(held->display, held->image.image_id) != VA_STATUS_SUCCESS) {
                ret = AVERROR_EXTERNAL; break;
            }
            held->valid = 0;
            if (vaDeriveImage(held->display, surface, &held->image) != VA_STATUS_SUCCESS) {
                ret = AVERROR_EXTERNAL; break;
            }
            held->valid = 1;
        }
        ret = hash_frame(check, frame);
        av_frame_unref(frame);
        if (ret < 0) break;
    }
    av_frame_free(&frame);
    return ret == AVERROR(EAGAIN) || ret == AVERROR_EOF ? 0 : ret;
}

static int lifecycle(AVBufferRef *device, const char *path, const char *format,
                     unsigned cycle, unsigned stream_index)
{
    struct check check = {.hardware = device != NULL, .format = av_get_pix_fmt(format)};
    struct held_image held = {0};
    AVFormatContext *input = NULL;
    AVCodecContext *decoder = NULL;
    AVPacket *packet = av_packet_alloc();
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(59, 0, 100)
    const AVCodec *codec = NULL;
#else
    AVCodec *codec = NULL;
#endif
    int ret = AVERROR(ENOMEM);
    uint8_t before[16], after[16];
    check.md5 = av_md5_alloc();
    if (!packet || !check.md5 || check.format == AV_PIX_FMT_NONE) goto done;
    if (device) {
        AVHWDeviceContext *context = (AVHWDeviceContext *)device->data;
        held.display = ((AVVAAPIDeviceContext *)context->hwctx)->display;
    }
    if ((ret = avformat_open_input(&input, path, NULL, NULL)) < 0 ||
        (ret = avformat_find_stream_info(input, NULL)) < 0) goto done;
    int stream = av_find_best_stream(input, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
    if (stream < 0) { ret = stream; goto done; }
    decoder = avcodec_alloc_context3(codec);
    if (!decoder) { ret = AVERROR(ENOMEM); goto done; }
    if ((ret = avcodec_parameters_to_context(decoder, input->streams[stream]->codecpar)) < 0) goto done;
    decoder->thread_count = 1;
    decoder->apply_cropping = 0;
    if (device) {
        decoder->hw_device_ctx = av_buffer_ref(device);
        if (!decoder->hw_device_ctx) { ret = AVERROR(ENOMEM); goto done; }
        decoder->get_format = vaapi_format;
    }
    if ((ret = avcodec_open2(decoder, codec, NULL)) < 0) goto done;
    for (unsigned pass = 0; pass < 2; pass++) {
        if (pass) {
            int64_t start = input->streams[stream]->start_time;
            if (start == AV_NOPTS_VALUE) start = 0;
            if ((ret = avformat_seek_file(input, stream, INT64_MIN, start, start, AVSEEK_FLAG_BACKWARD)) < 0) goto done;
            avcodec_flush_buffers(decoder);
        }
        av_md5_init(check.md5);
        check.frames = 0;
        while ((ret = av_read_frame(input, packet)) >= 0) {
            if (packet->stream_index == stream) ret = receive(&check, decoder, packet, &held);
            av_packet_unref(packet);
            if (ret < 0) goto done;
        }
        if (ret != AVERROR_EOF || (ret = receive(&check, decoder, NULL, &held)) < 0) goto done;
        if (!check.frames) { ret = AVERROR_INVALIDDATA; goto done; }
        uint8_t digest[16];
        av_md5_final(check.md5, digest);
        printf("RESULT cycle=%u stream=%u pass=%u frames=%u MD5=", cycle, stream_index, pass, check.frames);
        for (int i = 0; i < 16; i++) printf("%02x", digest[i]);
        putchar('\n');
    }
    if (device && !held.valid) { ret = AVERROR_INVALIDDATA; goto done; }
    if (device && (ret = image_digest(&held, before)) < 0) goto done;
    unsigned before_destroy = destroyed_contexts;
    avcodec_free_context(&decoder);
    /* FFmpeg destroys its VA decode context here. Keep the frame pool (surface
     * owner) alive until the derived image is destroyed: vaDestroySurfaces is
     * correctly rejected while an image is derived from a surface. */
    if (device) {
        if (destroyed_contexts != before_destroy + 1) { ret = AVERROR_INVALIDDATA; goto done; }
        if ((ret = image_digest(&held, after)) < 0) goto done;
        if (memcmp(before, after, sizeof(before))) { ret = AVERROR_INVALIDDATA; goto done; }
        printf("HELD_IMAGE_PASS cycle=%u\n", cycle);
    }
    ret = 0;
done:
    av_packet_free(&packet);
    avcodec_free_context(&decoder);
    avformat_close_input(&input);
    if (held.valid && vaDestroyImage(held.display, held.image.image_id) != VA_STATUS_SUCCESS)
        ret = AVERROR_EXTERNAL;
    av_buffer_unref(&held.pool);
    av_free(check.md5);
    sws_freeContext(check.sws);
    return ret;
}

static int positive(const char *value)
{
    char *end;
    errno = 0;
    long n = strtol(value, &end, 10);
    return !errno && *value && !*end && n > 0 && n <= 1000000 ? (int)n : -1;
}

int main(int argc, char **argv)
{
    if (argc < 6 || argc % 2 || (strcmp(argv[1], "software") && strcmp(argv[1], "vaapi"))) {
        fprintf(stderr, "Usage: %s software|vaapi CYCLES WARMUP INPUT FORMAT [INPUT FORMAT ...]\n", argv[0]);
        return 2;
    }
    int cycles = positive(argv[2]), warmup = positive(argv[3]);
    if (cycles < 0 || warmup < 0) return 2;
    int hardware = !strcmp(argv[1], "vaapi"), count = (argc - 4) / 2;
    if (hardware && !getenv("LIBVA_HW_GUARD_LEASE")) {
        fprintf(stderr, "hardware resource workload requires hwguard\n"); return 2;
    }
    AVBufferRef *device = NULL;
    av_log_set_level(AV_LOG_WARNING);
    int ret = 0;
    if (hardware && (ret = av_hwdevice_ctx_create(&device, AV_HWDEVICE_TYPE_VAAPI, NULL, NULL, 0)) < 0)
        goto done;
    printf("INITIAL\n");
    fflush(stdout);
    char initial[16];
    if (!fgets(initial, sizeof(initial), stdin) || strcmp(initial, "go\n")) {
        ret = AVERROR(EIO); goto done;
    }
    for (int i = 0; i < warmup + cycles; i++) {
        unsigned stream = i % count;
        if ((ret = lifecycle(device, argv[4 + 2 * stream], argv[5 + 2 * stream], i + 1, stream)) < 0) break;
        if (i + 1 >= warmup) {
            printf("CHECKPOINT %d\n", i + 1 - warmup);
            fflush(stdout);
            char command[16];
            if (!fgets(command, sizeof(command), stdin)) { ret = AVERROR(EIO); break; }
            if (!strcmp(command, "stop\n")) break;
            if (strcmp(command, "go\n")) { ret = AVERROR(EINVAL); break; }
        }
    }
done:
    if (ret < 0) fprintf(stderr, "resource workload failed: %s\n", av_err2str(ret));
    av_buffer_unref(&device);
    return ret < 0;
}
