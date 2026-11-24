#include "Imaging.h"

#ifdef _MSC_VER
static __declspec(thread) int allocation_failed;
#else
static _Thread_local int allocation_failed;
#endif

void *ImagingError_MemoryError(void) {
    allocation_failed = 1;
    return NULL;
}

/* Draw.c also exports a bitmap-paste entry point outside this bridge. */
int ImagingFill2(Imaging into, const void *ink, Imaging mask,
                 int x0, int y0, int x1, int y1) {
    (void)into;
    (void)ink;
    (void)mask;
    (void)x0;
    (void)y0;
    (void)x1;
    (void)y1;
    return -1;
}

int cs_draw_balloon(unsigned char *pixels, int width, int height,
                    int *polygon, int count, const int *ellipse) {
    UINT8 **rows = malloc((size_t)height * sizeof *rows);
    if (!rows) {
        return 0;
    }
    for (int y = 0; y < height; y++) {
        rows[y] = pixels + (size_t)y * width;
    }
    struct ImagingMemoryInstance image = {0};
    image.mode = IMAGING_MODE_L;
    image.type = IMAGING_TYPE_UINT8;
    image.bands = 1;
    image.xsize = width;
    image.ysize = height;
    image.image8 = rows;
    image.pixelsize = 1;
    image.linesize = width;
    const UINT8 ink = 255;
    allocation_failed = 0;
    int result = ImagingDrawPolygon(&image, count, polygon, &ink, 1, 1, 0, NULL);
    if (result == 0 && !allocation_failed) {
        result = ImagingDrawEllipse(
            &image, ellipse[0], ellipse[1], ellipse[2], ellipse[3], &ink, 1, 1, 0);
    }
    free(rows);
    return result == 0 && !allocation_failed;
}
