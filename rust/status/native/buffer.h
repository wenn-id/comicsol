#ifndef COMIC_SOL_BUFFER_H
#define COMIC_SOL_BUFFER_H

#include <stddef.h>

typedef struct {
    unsigned char *data;
    size_t len;
} Buffer;

#endif
