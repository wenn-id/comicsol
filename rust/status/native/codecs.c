#include <limits.h>
#include <setjmp.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <jpeglib.h>
#include <zlib.h>

#include "buffer.h"

void cs_release(void *p) {
    free(p);
}

uint32_t cs_png_crc(const unsigned char *data, size_t len) {
    return (uint32_t)crc32_z(0, data, len);
}

int cs_png_deflate(const unsigned char *data, unsigned row, unsigned rows, int level, Buffer *out) {
    z_stream stream = {0};
    if (deflateInit2(&stream, level, Z_DEFLATED, 15, 9, Z_FILTERED) != Z_OK) {
        return 0;
    }
    size_t input_size = (size_t)row * rows;
    if (input_size > ULONG_MAX) {
        deflateEnd(&stream);
        return 0;
    }
    unsigned long bound = deflateBound(&stream, (unsigned long)input_size);
    unsigned char *buffer = bound <= UINT_MAX ? malloc(bound) : NULL;
    if (!buffer) {
        deflateEnd(&stream);
        return 0;
    }
    stream.next_out = buffer;
    stream.avail_out = (unsigned)bound;
    int status = Z_OK;
    for (unsigned y = 0; y < rows && status == Z_OK; y++) {
        stream.next_in = (z_const unsigned char *)data + (size_t)y * row;
        stream.avail_in = row;
        status = deflate(&stream, Z_NO_FLUSH);
    }
    if (status == Z_OK) {
        status = deflate(&stream, Z_FINISH);
    }
    size_t len = stream.total_out;
    deflateEnd(&stream);
    if (status != Z_STREAM_END) {
        free(buffer);
        return 0;
    }
    out->data = buffer;
    out->len = len;
    return 1;
}

typedef struct {
    struct jpeg_error_mgr manager;
    jmp_buf recovery;
} JpegError;

static void fail_jpeg(j_common_ptr jpeg) {
    JpegError *error = (JpegError *)jpeg->err;
    longjmp(error->recovery, 1);
}

static void quiet_jpeg(j_common_ptr jpeg) {
    (void)jpeg;
}

static void jpeg_errors(j_common_ptr jpeg, JpegError *error) {
    jpeg->err = jpeg_std_error(&error->manager);
    error->manager.error_exit = fail_jpeg;
    error->manager.output_message = quiet_jpeg;
}

typedef struct {
    struct jpeg_compress_struct jpeg;
    unsigned char *buffer;
    unsigned long len;
} JpegEncode;

int cs_jpeg_encode(const unsigned char *rgb, unsigned width, unsigned height, Buffer *out) {
    JpegEncode *job = calloc(1, sizeof *job);
    if (!job) {
        return 0;
    }
    JpegError error;
    jpeg_errors((j_common_ptr)&job->jpeg, &error);
    if (setjmp(error.recovery)) {
        jpeg_destroy_compress(&job->jpeg);
        free(job->buffer);
        free(job);
        return 0;
    }
    jpeg_create_compress(&job->jpeg);
    jpeg_mem_dest(&job->jpeg, &job->buffer, &job->len);
    job->jpeg.image_width = width;
    job->jpeg.image_height = height;
    job->jpeg.input_components = 3;
    job->jpeg.in_color_space = JCS_RGB;
    jpeg_set_defaults(&job->jpeg);
    jpeg_set_quality(&job->jpeg, 95, TRUE);
    jpeg_start_compress(&job->jpeg, TRUE);
    while (job->jpeg.next_scanline < height) {
        JSAMPROW row = (JSAMPROW)(rgb + (size_t)job->jpeg.next_scanline * width * 3);
        jpeg_write_scanlines(&job->jpeg, &row, 1);
    }
    jpeg_finish_compress(&job->jpeg);
    jpeg_destroy_compress(&job->jpeg);
    out->data = job->buffer;
    out->len = job->len;
    free(job);
    return 1;
}

typedef struct {
    struct jpeg_decompress_struct jpeg;
    unsigned char *buffer;
} JpegDecode;

int cs_jpeg_decode(const unsigned char *data, size_t len, unsigned width, unsigned height, Buffer *out) {
    if (!len || len > ULONG_MAX) {
        return 0;
    }
    JpegDecode *job = calloc(1, sizeof *job);
    if (!job) {
        return 0;
    }
    JpegError error;
    jpeg_errors((j_common_ptr)&job->jpeg, &error);
    if (setjmp(error.recovery)) {
        jpeg_destroy_decompress(&job->jpeg);
        free(job->buffer);
        free(job);
        return 0;
    }
    jpeg_create_decompress(&job->jpeg);
    jpeg_mem_src(&job->jpeg, data, (unsigned long)len);
    jpeg_read_header(&job->jpeg, TRUE);
    if (job->jpeg.image_width != width || job->jpeg.image_height != height) {
        longjmp(error.recovery, 1);
    }
    job->jpeg.out_color_space = JCS_RGB;
    jpeg_start_decompress(&job->jpeg);
    size_t stride = (size_t)width * 3;
    job->buffer = malloc(stride * height);
    if (!job->buffer) {
        longjmp(error.recovery, 1);
    }
    while (job->jpeg.output_scanline < height) {
        unsigned char *row = job->buffer + (size_t)job->jpeg.output_scanline * stride;
        jpeg_read_scanlines(&job->jpeg, &row, 1);
    }
    jpeg_finish_decompress(&job->jpeg);
    jpeg_destroy_decompress(&job->jpeg);
    out->data = job->buffer;
    out->len = stride * height;
    free(job);
    return 1;
}
