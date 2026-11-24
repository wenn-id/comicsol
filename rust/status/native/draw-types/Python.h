#ifndef COMIC_SOL_DRAW_TYPES_H
#define COMIC_SOL_DRAW_TYPES_H

/* The Pillow drawing header declares opaque capsule pointers and sizes.
   The standalone polygon and ellipse routines use no interpreter symbols. */
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct _object PyObject;
typedef ptrdiff_t Py_ssize_t;

#define HAVE_PROTOTYPES 1
#define STDC_HEADERS 1

#endif
