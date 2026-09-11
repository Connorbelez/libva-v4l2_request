/* Regression check: a VA surface exported before decoding must retain its
 * dma-buf identity and layout. Pair with early-export.sh for pixel comparison. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <va/va.h>
#include <va/va_drmcommon.h>

static _Thread_local VADRMPRIMESurfaceDescriptor before;
static _Thread_local VASurfaceID current;
static unsigned checked;

static void require(int condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "EARLY_EXPORT_FAIL: %s\n", message);
        exit(70);
    }
}

static void export_surface(VADisplay display, VASurfaceID surface,
                           VADRMPRIMESurfaceDescriptor *descriptor)
{
    require(vaExportSurfaceHandle(display, surface,
                VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2,
                VA_EXPORT_SURFACE_READ_ONLY | VA_EXPORT_SURFACE_SEPARATE_LAYERS,
                descriptor) == VA_STATUS_SUCCESS, "export failed");
    require(descriptor->num_objects > 0 && descriptor->num_objects <= 4,
            "invalid object count");
}

VAStatus vaBeginPicture(VADisplay display, VAContextID context, VASurfaceID surface)
{
    VAStatus (*real_begin)(VADisplay, VAContextID, VASurfaceID) =
        dlsym(RTLD_NEXT, "vaBeginPicture");
    require(real_begin != NULL, "missing vaBeginPicture");
    current = surface;
    export_surface(display, surface, &before);
    return real_begin(display, context, surface);
}

VAStatus vaEndPicture(VADisplay display, VAContextID context)
{
    VAStatus (*real_end)(VADisplay, VAContextID) = dlsym(RTLD_NEXT, "vaEndPicture");
    VADRMPRIMESurfaceDescriptor after;
    require(real_end != NULL, "missing vaEndPicture");
    VAStatus status = real_end(display, context);
    require(status == VA_STATUS_SUCCESS, "decode submission failed");
    require(vaSyncSurface(display, current) == VA_STATUS_SUCCESS, "decode sync failed");
    export_surface(display, current, &after);
    require(before.num_objects == after.num_objects, "object count changed");
    require(before.width == after.width && before.height == after.height,
            "surface dimensions changed");
    require(before.num_layers == after.num_layers &&
            memcmp(before.layers, after.layers, sizeof(before.layers)) == 0,
            "exported layer layout changed");
    for (unsigned i = 0; i < before.num_objects; i++) {
        struct stat old_stat, new_stat;
        require(fstat(before.objects[i].fd, &old_stat) == 0 &&
                fstat(after.objects[i].fd, &new_stat) == 0, "fstat failed");
        require(old_stat.st_dev == new_stat.st_dev && old_stat.st_ino == new_stat.st_ino,
                "decoder replaced the exported dma-buf; client sees stale green pixels");
        close(after.objects[i].fd);
    }
    for (unsigned i = 0; i < before.num_objects; i++) close(before.objects[i].fd);
    fprintf(stderr, "EARLY_EXPORT_PASS: frame %u, stable buffer and layout\n", ++checked);
    return status;
}
