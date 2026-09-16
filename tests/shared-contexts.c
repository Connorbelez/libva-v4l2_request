/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Reuse the strict pixel checker with several live decoders on one VA display. */
#define main single_stream_main
#include "frame-check.c"
#undef main

struct stream {
    AVFormatContext *input;
    AVCodecContext *decoder;
    struct check check;
    int index, done;
};

int main(int argc, char **argv)
{
    if (argc < 6 || argc % 2 ||
        (strcmp(argv[1], "software") && strcmp(argv[1], "vaapi"))) {
        fprintf(stderr, "Usage: %s software|vaapi INPUT PIX_FMT INPUT PIX_FMT [...]\n", argv[0]);
        return 2;
    }
    int count = (argc - 2) / 2, ret = AVERROR(ENOMEM), live = count;
    int hardware = !strcmp(argv[1], "vaapi");
    struct stream *streams = av_calloc(count, sizeof(*streams));
    AVBufferRef *device = NULL;
    AVPacket *packet = av_packet_alloc();
    av_log_set_level(AV_LOG_WARNING);
    if (!streams || !packet) goto done;
    /* Every codec holds a reference to this one device/display. */
    if (hardware && (ret = av_hwdevice_ctx_create(&device, AV_HWDEVICE_TYPE_VAAPI,
                                                 NULL, NULL, 0)) < 0) goto done;
    for (int i = 0; i < count; i++) {
        struct stream *s = &streams[i];
        /* Same libavcodec 59 const gate as the included frame-check.c. */
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(59, 0, 100)
        const AVCodec *codec;
#else
        AVCodec *codec;
#endif
        s->check.hardware = hardware;
        s->check.format = av_get_pix_fmt(argv[3 + i * 2]);
        if (s->check.format == AV_PIX_FMT_NONE) { ret = AVERROR(EINVAL); goto done; }
        s->check.md5 = av_md5_alloc();
        if (!s->check.md5) { ret = AVERROR(ENOMEM); goto done; }
        av_md5_init(s->check.md5);
        if ((ret = avformat_open_input(&s->input, argv[2 + i * 2], NULL, NULL)) < 0 ||
            (ret = avformat_find_stream_info(s->input, NULL)) < 0) goto done;
        s->index = av_find_best_stream(s->input, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
        if (s->index < 0) { ret = s->index; goto done; }
        s->decoder = avcodec_alloc_context3(codec);
        if (!s->decoder) { ret = AVERROR(ENOMEM); goto done; }
        if ((ret = avcodec_parameters_to_context(s->decoder,
                           s->input->streams[s->index]->codecpar)) < 0) goto done;
        s->decoder->thread_count = 1;
        s->decoder->apply_cropping = 0;
        if (hardware) {
            s->decoder->hw_device_ctx = av_buffer_ref(device);
            if (!s->decoder->hw_device_ctx) { ret = AVERROR(ENOMEM); goto done; }
            s->decoder->get_format = vaapi_format;
        }
        if ((ret = avcodec_open2(s->decoder, codec, NULL)) < 0) goto done;
    }
    while (live) {
        for (int i = 0; i < count; i++) {
            struct stream *s = &streams[i];
            if (s->done) continue;
            do {
                av_packet_unref(packet);
                ret = av_read_frame(s->input, packet);
            } while (ret >= 0 && packet->stream_index != s->index);
            if (ret < 0 && ret != AVERROR_EOF) goto done;
            int eof = ret == AVERROR_EOF;
            if ((ret = decode(&s->check, s->decoder, eof ? NULL : packet)) < 0) goto done;
            if (eof) {
                if (!s->check.frames) { ret = AVERROR_INVALIDDATA; goto done; }
                uint8_t digest[16];
                av_md5_final(s->check.md5, digest);
                printf("stream %d frames=%u MD5=", i, s->check.frames);
                for (int j = 0; j < 16; j++) printf("%02x", digest[j]);
                putchar('\n'); fflush(stdout);
                /* Tear down shorter streams while the remaining contexts
                 * continue decoding on the same display. */
                avcodec_free_context(&s->decoder);
                avformat_close_input(&s->input);
                s->done = 1; live--;
            }
        }
    }
    ret = 0;
done:
    if (ret < 0) fprintf(stderr, "Shared-context decode failed: %s\n", av_err2str(ret));
    av_packet_free(&packet);
    if (streams) for (int i = 0; i < count; i++) {
        avcodec_free_context(&streams[i].decoder);
        avformat_close_input(&streams[i].input);
        av_free(streams[i].check.md5);
        sws_freeContext(streams[i].check.sws);
    }
    av_free(streams);
    av_buffer_unref(&device);
    return ret < 0;
}
