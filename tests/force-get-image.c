/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Force FFmpeg's copying download path and prove that it was exercised. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <va/va.h>

VAStatus vaDeriveImage(VADisplay dpy, VASurfaceID surface, VAImage *image)
{
    (void)dpy; (void)surface; (void)image;
    return VA_STATUS_ERROR_OPERATION_FAILED;
}

VAStatus vaGetImage(VADisplay dpy, VASurfaceID surface, int x, int y,
                    unsigned int width, unsigned int height, VAImageID image)
{
    VAStatus (*get)(VADisplay, VASurfaceID, int, int, unsigned int,
                   unsigned int, VAImageID) = dlsym(RTLD_NEXT, "vaGetImage");
    if (!get)
        return VA_STATUS_ERROR_OPERATION_FAILED;
    VAStatus status = get(dpy, surface, x, y, width, height, image);
    if (status == VA_STATUS_SUCCESS)
        fprintf(stderr, "GET_IMAGE_PASS\n");
    return status;
}
