#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <ft2build.h>
#include FT_FREETYPE_H
#include FT_GLYPH_H
#include FT_STROKER_H

#include "buffer.h"

#ifdef COMIC_SOL_RAQM
#include "fribidi-shim/fribidi.h"
#include "raqm/raqm.h"
#endif

typedef struct {
    FT_Library library;
    FT_Face face;
} Font;

Font *cs_font_open(const uint8_t *data, size_t len) {
    if (len > LONG_MAX) {
        return NULL;
    }
    Font *font = calloc(1, sizeof *font);
    if (!font) {
        return NULL;
    }
    if (FT_Init_FreeType(&font->library)) {
        free(font);
        return NULL;
    }
    if (FT_New_Memory_Face(font->library, data, (long)len, 0, &font->face)) {
        FT_Done_FreeType(font->library);
        free(font);
        return NULL;
    }
    return font;
}

void cs_font_close(Font *font) {
    FT_Done_Face(font->face);
    FT_Done_FreeType(font->library);
    free(font);
}

int cs_font_size(Font *font, unsigned size) {
    return FT_Set_Pixel_Sizes(font->face, 0, size) == 0;
}

unsigned cs_font_index(Font *font, uint32_t character) {
    return FT_Get_Char_Index(font->face, character);
}

#ifdef COMIC_SOL_RAQM
int cs_font_init_shaping(void) {
    return load_fribidi() == 0;
}

typedef struct {
    unsigned index;
    int x_offset;
    int y_offset;
    int advance;
} ShapedGlyph;

int cs_font_shape(Font *font, const uint32_t *text, size_t len, Buffer *out) {
    raqm_t *layout = raqm_create();
    if (!layout) {
        return 0;
    }
    size_t count = 0;
    raqm_glyph_t *glyphs = NULL;
    if (raqm_set_text(layout, text, len) && raqm_set_freetype_face(layout, font->face)
        && raqm_layout(layout)) {
        glyphs = raqm_get_glyphs(layout, &count);
    }
    ShapedGlyph *result = glyphs ? calloc(count ? count : 1, sizeof *result) : NULL;
    if (result) {
        for (size_t i = 0; i < count; i++) {
            result[i] = (ShapedGlyph){
                glyphs[i].index, glyphs[i].x_offset, glyphs[i].y_offset, glyphs[i].x_advance
            };
        }
        out->data = (unsigned char *)result;
        out->len = count;
    }
    raqm_destroy(layout);
    return result != NULL;
}
#endif

int cs_font_kerning(Font *font, unsigned previous, unsigned glyph) {
    FT_Vector delta;
    if (!previous || !glyph || !FT_HAS_KERNING(font->face)
        || FT_Get_Kerning(font->face, previous, glyph, FT_KERNING_DEFAULT, &delta)) {
        return 0;
    }
    return (int)delta.x;
}

typedef struct {
    int advance;
    int left;
    int bottom;
    int right;
    int top;
} GlyphMetrics;

int cs_font_metrics(Font *font, unsigned index, GlyphMetrics *out) {
    FT_Glyph glyph;
    if (FT_Load_Glyph(font->face, index, FT_LOAD_NO_BITMAP) || FT_Get_Glyph(font->face->glyph, &glyph)) {
        return 0;
    }
    FT_BBox box;
    FT_Glyph_Get_CBox(glyph, FT_GLYPH_BBOX_PIXELS, &box);
    FT_Done_Glyph(glyph);
    out->advance = (int)font->face->glyph->metrics.horiAdvance;
    out->left = (int)box.xMin;
    out->bottom = (int)box.yMin;
    out->right = (int)box.xMax;
    out->top = (int)box.yMax;
    return 1;
}

typedef struct {
    int left;
    int top;
    unsigned width;
    unsigned height;
} GlyphBitmap;

int cs_font_bitmap(Font *font, unsigned index, unsigned stroke, GlyphBitmap *out, Buffer *buffer) {
    FT_Glyph glyph = NULL;
    FT_Bitmap bitmap;
    if (FT_Load_Glyph(font->face, index, stroke ? FT_LOAD_NO_BITMAP : FT_LOAD_RENDER)) {
        return 0;
    }
    FT_GlyphSlot slot = font->face->glyph;
    if (stroke) {
        FT_Stroker stroker;
        if (FT_Stroker_New(font->library, &stroker)) {
            return 0;
        }
        FT_Stroker_Set(stroker, (FT_Fixed)stroke * 64, FT_STROKER_LINECAP_ROUND,
                       FT_STROKER_LINEJOIN_ROUND, 0);
        FT_Error error = FT_Get_Glyph(slot, &glyph);
        if (!error) {
            error = FT_Glyph_StrokeBorder(&glyph, stroker, 0, 1);
        }
        if (!error) {
            FT_Vector origin = {0, 0};
            error = FT_Glyph_To_Bitmap(&glyph, FT_RENDER_MODE_NORMAL, &origin, 1);
        }
        FT_Stroker_Done(stroker);
        if (error) {
            if (glyph) {
                FT_Done_Glyph(glyph);
            }
            return 0;
        }
        FT_BitmapGlyph rendered = (FT_BitmapGlyph)glyph;
        out->left = rendered->left;
        out->top = rendered->top;
        bitmap = rendered->bitmap;
    } else {
        out->left = slot->bitmap_left;
        out->top = slot->bitmap_top;
        bitmap = slot->bitmap;
    }
    out->width = bitmap.width;
    out->height = bitmap.rows;
    size_t len = (size_t)bitmap.width * bitmap.rows;
    unsigned char *bytes = NULL;
    if (!len || (bitmap.pixel_mode == FT_PIXEL_MODE_GRAY && bitmap.pitch >= (int)bitmap.width)) {
        bytes = malloc(len ? len : 1);
    }
    if (bytes && len) {
        for (unsigned row = 0; row < bitmap.rows; row++) {
            memcpy(bytes + (size_t)row * bitmap.width,
                   bitmap.buffer + (size_t)row * bitmap.pitch, bitmap.width);
        }
    }
    if (glyph) {
        FT_Done_Glyph(glyph);
    }
    buffer->data = bytes;
    buffer->len = len;
    return bytes != NULL;
}
