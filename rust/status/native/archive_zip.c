#include <limits.h>
#include <stddef.h>
#include <stdlib.h>
#include <archive-zlib/zlib.h>

#include "buffer.h"

int cs_zip_deflate(const unsigned char *data, unsigned len, Buffer *out) {
    z_stream stream = {0};
    if (deflateInit2(&stream, 0, Z_DEFLATED, -15, 8, Z_DEFAULT_STRATEGY) != Z_OK) {
        return 0;
    }
    unsigned long bound = deflateBound(&stream, len);
    unsigned char *buffer = bound <= UINT_MAX ? malloc(bound) : NULL;
    if (!buffer) {
        deflateEnd(&stream);
        return 0;
    }
    size_t written = 0;
    int status = Z_OK;
    while (written < len && status == Z_OK) {
        unsigned chunk = len - (unsigned)written;
        if (chunk > 1024 * 1024) {
            chunk = 1024 * 1024;
        }
        /* Match the reference ZipFile compressor's output buffer growth. */
        static const unsigned capacities[] = {32768, 65536, 262144, 1048576, 4194304};
        unsigned index = 0;
        stream.next_in = (z_const unsigned char *)data + written;
        stream.avail_in = chunk;
        do {
            unsigned capacity = capacities[index];
            unsigned long available = bound - stream.total_out;
            stream.next_out = buffer + stream.total_out;
            stream.avail_out = capacity < available ? capacity : (unsigned)available;
            if (!stream.avail_out) {
                status = Z_BUF_ERROR;
                break;
            }
            status = deflate(&stream, Z_NO_FLUSH);
            if (index + 1 < sizeof(capacities) / sizeof(*capacities)) {
                index++;
            }
        } while (status == Z_OK && stream.avail_out == 0);
        written += chunk;
    }
    if (status == Z_OK) {
        stream.next_out = buffer + stream.total_out;
        stream.avail_out = (unsigned)(bound - stream.total_out);
        status = deflate(&stream, Z_FINISH);
    }
    size_t size = stream.total_out;
    deflateEnd(&stream);
    if (status != Z_STREAM_END) {
        free(buffer);
        return 0;
    }
    out->data = buffer;
    out->len = size;
    return 1;
}
