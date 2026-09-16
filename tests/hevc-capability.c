/* SPDX-License-Identifier: GPL-3.0-or-later */
/* HEVC profile, RT-format and picture-format boundaries without a device.
 * Companion to docs/HEVC_RANGE_EXTENSIONS.md: every range-extension, 12-bit,
 * non-4:2:0 or unequal-depth request must be refused in userspace before a
 * V4L2 control is built, and the Main/Main10 4:2:0 paths must be unchanged. */
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <string.h>
#include <sys/resource.h>
#define v4l2r_codec_hevc v4l2r_test_codec_hevc
#include "../src/codec_hevc.c"

static unsigned int ioctls;

/* Nothing in these cases may reach the (absent) device. */
int __wrap_ioctl(int fd, unsigned long request, ...)
{
    (void)fd; (void)request;
    ioctls++;
    errno = ENODEV;
    return -1;
}

static struct v4l2r_driver drv;
static struct VADriverContext va;

static void driver_init(bool hevc_10bit)
{
    memset(&drv, 0, sizeof(drv));
    assert(!pthread_mutex_init(&drv.mutex, NULL));
    assert(!v4l2r_handles_init(&drv.configs, V4L2R_ID_OFFSET_CONFIG));
    assert(!v4l2r_handles_init(&drv.surfaces, V4L2R_ID_OFFSET_SURFACE));
    drv.nb_decoders = 1;
    drv.converter_probed = true;
    drv.decoders[0].pixelformats[0] = V4L2_PIX_FMT_HEVC_SLICE;
    drv.decoders[0].nb_pixelformats = 1;
    drv.decoders[0].hevc_10bit = hevc_10bit;
    va.pDriverData = &drv;
}

static void driver_destroy(void)
{
    v4l2r_handles_destroy(&drv.configs);
    v4l2r_handles_destroy(&drv.surfaces);
    pthread_mutex_destroy(&drv.mutex);
}

static int entrypoints(VAProfile profile)
{
    VAEntrypoint entry[4];
    int count = -1;
    assert(v4l2r_QueryConfigEntrypoints(&va, profile, entry, &count) == VA_STATUS_SUCCESS);
    if (count == 1)
        assert(entry[0] == VAEntrypointVLD);
    return count;
}

static VAStatus create_config(VAProfile profile, unsigned int rt_format, VAConfigID *id)
{
    VAConfigAttrib attrib = {.type = VAConfigAttribRTFormat, .value = rt_format};
    *id = VA_INVALID_ID;
    return v4l2r_CreateConfig(&va, profile, VAEntrypointVLD, rt_format ? &attrib : NULL,
                              rt_format ? 1 : 0, id);
}

static unsigned int rt_format_of(VAProfile profile)
{
    VAConfigAttrib attrib = {.type = VAConfigAttribRTFormat};
    assert(v4l2r_GetConfigAttributes(&va, profile, VAEntrypointVLD, &attrib, 1) ==
           VA_STATUS_SUCCESS);
    return attrib.value;
}

/* Every HEVC profile libva defines beyond Main/Main10. Range extensions and
 * SCC arrived together in libva 2.2 (VA API 1.2); SccMain444_10 later. */
static const VAProfile unadvertised[] = {
#if VA_CHECK_VERSION(1, 2, 0)
    VAProfileHEVCMain12, VAProfileHEVCMain422_10, VAProfileHEVCMain422_12,
    VAProfileHEVCMain444, VAProfileHEVCMain444_10, VAProfileHEVCMain444_12,
    VAProfileHEVCSccMain, VAProfileHEVCSccMain10, VAProfileHEVCSccMain444,
#endif
#if VA_CHECK_VERSION(1, 8, 0)
    VAProfileHEVCSccMain444_10,
#endif
};
#define NB_UNADVERTISED (sizeof(unadvertised) / sizeof(unadvertised[0]))

static void profiles(void)
{
    VAProfile list[V4L2R_MAX_PROFILES];
    int count = 0;
    VAConfigID id;

    driver_init(true);
    assert(v4l2r_QueryConfigProfiles(&va, list, &count) == VA_STATUS_SUCCESS);
    bool main = false, main10 = false;
    for (int i = 0; i < count; i++) {
        /* Only the two 4:2:0 profiles and the VPP placeholder may appear. */
        assert(list[i] == VAProfileHEVCMain || list[i] == VAProfileHEVCMain10 ||
               list[i] == VAProfileNone);
        main |= list[i] == VAProfileHEVCMain;
        main10 |= list[i] == VAProfileHEVCMain10;
    }
    assert(main && main10);
    assert(entrypoints(VAProfileHEVCMain) == 1 && entrypoints(VAProfileHEVCMain10) == 1);
    assert(rt_format_of(VAProfileHEVCMain) == VA_RT_FORMAT_YUV420);
    assert(rt_format_of(VAProfileHEVCMain10) == VA_RT_FORMAT_YUV420_10);

    for (unsigned int i = 0; i < NB_UNADVERTISED; i++) {
        assert(entrypoints(unadvertised[i]) == 0);
        assert(create_config(unadvertised[i], 0, &id) == VA_STATUS_ERROR_UNSUPPORTED_PROFILE);
        assert(id == VA_INVALID_ID);
    }

    /* A Main or Main10 config only accepts its own 4:2:0 RT format. */
    static const unsigned int refused[] = {
        VA_RT_FORMAT_YUV422, VA_RT_FORMAT_YUV444, VA_RT_FORMAT_YUV400,
        VA_RT_FORMAT_YUV422_10, VA_RT_FORMAT_YUV444_10,
        VA_RT_FORMAT_YUV420_12, VA_RT_FORMAT_YUV422_12, VA_RT_FORMAT_YUV444_12,
    };
    for (unsigned int i = 0; i < sizeof(refused) / sizeof(refused[0]); i++) {
        assert(create_config(VAProfileHEVCMain, refused[i], &id) ==
               VA_STATUS_ERROR_UNSUPPORTED_RT_FORMAT);
        assert(create_config(VAProfileHEVCMain10, refused[i], &id) ==
               VA_STATUS_ERROR_UNSUPPORTED_RT_FORMAT);
    }
    assert(create_config(VAProfileHEVCMain, VA_RT_FORMAT_YUV420_10, &id) ==
           VA_STATUS_ERROR_UNSUPPORTED_RT_FORMAT);
    assert(create_config(VAProfileHEVCMain10, VA_RT_FORMAT_YUV420, &id) ==
           VA_STATUS_ERROR_UNSUPPORTED_RT_FORMAT);
    assert(create_config(VAProfileHEVCMain, VA_RT_FORMAT_YUV420, &id) == VA_STATUS_SUCCESS);
    assert(V4L2R_CONFIG(&drv, id)->rt_format == VA_RT_FORMAT_YUV420);
    assert(v4l2r_DestroyConfig(&va, id) == VA_STATUS_SUCCESS);
    assert(create_config(VAProfileHEVCMain10, VA_RT_FORMAT_YUV420_10, &id) ==
           VA_STATUS_SUCCESS);
    assert(V4L2R_CONFIG(&drv, id)->rt_format == VA_RT_FORMAT_YUV420_10);
    assert(v4l2r_DestroyConfig(&va, id) == VA_STATUS_SUCCESS);
    /* No refused config leaked a handle. */
    assert(create_config(VAProfileHEVCMain, 0, &id) == VA_STATUS_SUCCESS &&
           id == V4L2R_ID_OFFSET_CONFIG);
    assert(v4l2r_DestroyConfig(&va, id) == VA_STATUS_SUCCESS);
    driver_destroy();

    /* An 8-bit-only decoder keeps Main and drops Main10; nothing else appears. */
    driver_init(false);
    assert(entrypoints(VAProfileHEVCMain) == 1 && entrypoints(VAProfileHEVCMain10) == 0);
    assert(create_config(VAProfileHEVCMain10, 0, &id) == VA_STATUS_ERROR_UNSUPPORTED_PROFILE);
    for (unsigned int i = 0; i < NB_UNADVERTISED; i++)
        assert(entrypoints(unadvertised[i]) == 0);
    driver_destroy();
    assert(!ioctls);
}

static struct hevc_context codec;
static struct v4l2r_context ctx;
static VAPictureParameterBufferHEVC pic;
static struct v4l2r_buffer pic_buffer;

/* The boundary is a driver contract, not an AVD quirk: every case below runs
 * once as an AVD context and once as a generic V4L2 context. */
static bool generic_backend;

static void context_init(VAProfile profile)
{
    memset(&codec, 0, sizeof(codec));
    memset(&ctx, 0, sizeof(ctx));
    ctx.drv = &drv;
    ctx.codec_priv = &codec;
    ctx.profile = profile;
    ctx.bit_depth = v4l2r_profile_bit_depth(profile);
    ctx.is_avd = !generic_backend;
    assert(hevc_begin_picture(&ctx) == VA_STATUS_SUCCESS);
}

static void picture_init(unsigned int chroma_format_idc, unsigned int luma, unsigned int chroma)
{
    memset(&pic, 0, sizeof(pic));
    pic.pic_width_in_luma_samples = 64;
    pic.pic_height_in_luma_samples = 64;
    pic.pic_fields.bits.chroma_format_idc = chroma_format_idc;
    pic.bit_depth_luma_minus8 = luma - 8;
    pic.bit_depth_chroma_minus8 = chroma - 8;
    pic.CurrPic.picture_id = VA_INVALID_SURFACE;
    for (unsigned int i = 0; i < 15; i++) {
        pic.ReferenceFrames[i].picture_id = VA_INVALID_SURFACE;
        pic.ReferenceFrames[i].flags = VA_PICTURE_HEVC_INVALID;
    }
    pic_buffer = (struct v4l2r_buffer){.type = VAPictureParameterBufferType,
        .data = &pic, .nb_elements = 1, .element_size = sizeof(pic)};
}

static void accepted(VAProfile profile, unsigned int chroma_format_idc,
                     unsigned int luma, unsigned int chroma)
{
    context_init(profile);
    picture_init(chroma_format_idc, luma, chroma);
    assert(hevc_render_buffer(&ctx, &pic_buffer) == VA_STATUS_SUCCESS);
    assert(codec.have_pic && !codec.failed);
    assert(codec.sps.bit_depth_luma_minus8 == luma - 8);
    assert(codec.sps.bit_depth_chroma_minus8 == chroma - 8);
    assert(codec.sps.chroma_format_idc == chroma_format_idc);
}

static void rejected(VAProfile profile, unsigned int chroma_format_idc,
                     unsigned int luma, unsigned int chroma, bool separate_planes)
{
    context_init(profile);
    picture_init(chroma_format_idc, luma, chroma);
    pic.pic_fields.bits.separate_colour_plane_flag = separate_planes;
    assert(hevc_render_buffer(&ctx, &pic_buffer) == VA_STATUS_ERROR_UNSUPPORTED_RT_FORMAT);
    /* Nothing of the refused picture is staged, and the picture is poisoned
     * so a later slice or EndPicture cannot submit it. */
    assert(!codec.have_pic && codec.failed);
    assert(!codec.sps.bit_depth_luma_minus8 && !codec.sps.bit_depth_chroma_minus8);
    assert(!codec.sps.chroma_format_idc && !codec.sps.pic_width_in_luma_samples);
    assert(hevc_render_buffer(&ctx, &pic_buffer) == VA_STATUS_ERROR_INVALID_BUFFER);
    assert(hevc_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    /* The next picture starts clean. */
    assert(hevc_begin_picture(&ctx) == VA_STATUS_SUCCESS && !codec.failed);
}

static void picture_cases(void)
{
    /* Supported: 4:2:0 at the negotiated depth. A Main10 decoder also takes
     * 8-bit pictures, as the HEVC profile constraints permit. */
    accepted(VAProfileHEVCMain, 1, 8, 8);
    accepted(VAProfileHEVCMain10, 1, 10, 10);
    accepted(VAProfileHEVCMain10, 1, 8, 8);

    /* The known firmware-reset stream: 10-bit luma with 9-bit chroma
     * (TSUNEQBD_A_MAIN10_Technicolor_2), plus every other unequal pairing. */
    rejected(VAProfileHEVCMain10, 1, 10, 9, false);
    rejected(VAProfileHEVCMain10, 1, 8, 10, false);
    rejected(VAProfileHEVCMain10, 1, 10, 8, false);
    rejected(VAProfileHEVCMain, 1, 8, 9, false);
    rejected(VAProfileHEVCMain, 1, 9, 8, false);

    /* Equal depths with no CAPTURE layout: 9-bit fits under the Main10 maximum
     * and 11-bit does not, and neither has an 8- or 10-bit buffer format. The
     * first review found 9/9 slipping through an equality-plus-maximum check. */
    rejected(VAProfileHEVCMain, 1, 9, 9, false);
    rejected(VAProfileHEVCMain10, 1, 9, 9, false);
    rejected(VAProfileHEVCMain10, 1, 11, 11, false);

    /* Depth above the negotiated profile: Main12/Main16 pictures, and a 10-bit
     * picture inside an 8-bit context whose CAPTURE buffers are NV12. */
    rejected(VAProfileHEVCMain, 1, 10, 10, false);
    rejected(VAProfileHEVCMain, 1, 12, 12, false);
    rejected(VAProfileHEVCMain10, 1, 12, 12, false);
    rejected(VAProfileHEVCMain10, 1, 16, 16, false);

    /* Chroma formats without a negotiated output layout: monochrome, 4:2:2
     * and 4:4:4 at every depth, and 4:4:4 coded as separate colour planes. */
    for (unsigned int chroma_format_idc = 0; chroma_format_idc <= 3; chroma_format_idc++) {
        if (chroma_format_idc == 1)
            continue;
        rejected(VAProfileHEVCMain, chroma_format_idc, 8, 8, false);
        rejected(VAProfileHEVCMain10, chroma_format_idc, 8, 8, false);
        rejected(VAProfileHEVCMain10, chroma_format_idc, 10, 10, false);
        rejected(VAProfileHEVCMain10, chroma_format_idc, 12, 12, false);
    }
    rejected(VAProfileHEVCMain, 3, 8, 8, true);
    rejected(VAProfileHEVCMain, 1, 8, 8, true);

    /* A mid-stream change cannot bypass the check: an accepted 4:2:0 picture
     * followed by an unequal-depth picture in the same context is refused. */
    accepted(VAProfileHEVCMain10, 1, 10, 10);
    assert(hevc_begin_picture(&ctx) == VA_STATUS_SUCCESS);
    picture_init(1, 10, 9);
    assert(hevc_render_buffer(&ctx, &pic_buffer) == VA_STATUS_ERROR_UNSUPPORTED_RT_FORMAT);
    assert(!codec.have_pic && codec.failed);
    assert(hevc_end_picture(&ctx) == VA_STATUS_ERROR_INVALID_BUFFER);
    /* The stale SPS from the accepted picture was not replaced. */
    assert(codec.sps.bit_depth_chroma_minus8 == 2);

    /* Short picture parameter buffers are still an invalid-buffer error,
     * checked before the format. */
    context_init(VAProfileHEVCMain10);
    picture_init(1, 10, 9);
    pic_buffer.element_size = sizeof(pic) - 1;
    assert(hevc_render_buffer(&ctx, &pic_buffer) == VA_STATUS_ERROR_INVALID_BUFFER);
}

static void picture(void)
{
    driver_init(true);
    generic_backend = false;
    picture_cases();
    generic_backend = true;
    picture_cases();
    driver_destroy();
    assert(!ioctls);
}

int main(int argc, char **argv)
{
    struct rlimit core = {0, 0};
    assert(!setrlimit(RLIMIT_CORE, &core) && argc == 2);
    if (!strcmp(argv[1], "profiles"))
        profiles();
    else if (!strcmp(argv[1], "picture"))
        picture();
    else
        return 1;
    return 0;
}
