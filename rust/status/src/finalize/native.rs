use std::ffi::c_void;
use std::ptr::{self, NonNull};
use std::slice;

use image::{GrayImage, RgbImage};

#[repr(C)]
struct Buffer {
    data: *mut u8,
    len: usize,
}

impl Buffer {
    // The callback must return the pointer and element count of a C allocation of T.
    unsafe fn receive<T: Copy>(call: impl FnOnce(*mut Self) -> i32) -> Option<Vec<T>> {
        let mut buffer = Self {
            data: ptr::null_mut(),
            len: 0,
        };
        if call(&mut buffer) == 0 || buffer.data.is_null() {
            return None;
        }
        let items = unsafe { slice::from_raw_parts(buffer.data.cast(), buffer.len) }.to_vec();
        unsafe { cs_release(buffer.data) };
        Some(items)
    }
}

unsafe extern "C" {
    fn cs_release(data: *mut u8);
    fn cs_png_crc(data: *const u8, len: usize) -> u32;
    fn cs_png_deflate(data: *const u8, row: u32, rows: u32, level: i32, out: *mut Buffer) -> i32;
    fn cs_zip_deflate(data: *const u8, len: u32, out: *mut Buffer) -> i32;
    fn cs_jpeg_encode(data: *const u8, width: u32, height: u32, out: *mut Buffer) -> i32;
    fn cs_jpeg_decode(
        data: *const u8,
        len: usize,
        width: u32,
        height: u32,
        out: *mut Buffer,
    ) -> i32;
    fn cs_font_open(data: *const u8, len: usize) -> *mut c_void;
    fn cs_font_close(font: *mut c_void);
    fn cs_font_size(font: *mut c_void, size: u32) -> i32;
    fn cs_font_index(font: *mut c_void, character: u32) -> u32;
    #[cfg(target_os = "linux")]
    fn cs_font_init_shaping() -> i32;
    #[cfg(target_os = "linux")]
    fn cs_font_shape(font: *mut c_void, text: *const u32, len: usize, out: *mut Buffer) -> i32;
    fn cs_font_kerning(font: *mut c_void, previous: u32, glyph: u32) -> i32;
    fn cs_font_metrics(font: *mut c_void, glyph: u32, out: *mut GlyphMetrics) -> i32;
    fn cs_font_bitmap(
        font: *mut c_void,
        glyph: u32,
        stroke: u32,
        dimensions: *mut GlyphBitmap,
        out: *mut Buffer,
    ) -> i32;
    fn cs_draw_balloon(
        pixels: *mut u8,
        width: i32,
        height: i32,
        polygon: *mut [i32; 2],
        count: i32,
        ellipse: *const i32,
    ) -> i32;
}

pub(crate) fn crc(bytes: &[u8]) -> u32 {
    unsafe { cs_png_crc(bytes.as_ptr(), bytes.len()) }
}

pub(crate) fn zip_deflate(bytes: &[u8]) -> Option<Vec<u8>> {
    let len = u32::try_from(bytes.len()).ok()?;
    unsafe { Buffer::receive(|out| cs_zip_deflate(bytes.as_ptr(), len, out)) }
}

pub(super) fn deflate(bytes: &[u8], row: u32, rows: u32, level: i32) -> Option<Vec<u8>> {
    (row as usize)
        .checked_mul(rows as usize)
        .filter(|&n| n == bytes.len())?;
    unsafe { Buffer::receive(|out| cs_png_deflate(bytes.as_ptr(), row, rows, level, out)) }
}

pub(super) fn jpeg(image: &RgbImage) -> Option<Vec<u8>> {
    unsafe {
        Buffer::receive(|out| {
            cs_jpeg_encode(image.as_raw().as_ptr(), image.width(), image.height(), out)
        })
    }
}

pub(super) fn decode_jpeg(bytes: &[u8], width: u32, height: u32) -> Option<RgbImage> {
    let pixels = unsafe {
        Buffer::receive(|out| cs_jpeg_decode(bytes.as_ptr(), bytes.len(), width, height, out))
    }?;
    RgbImage::from_raw(width, height, pixels)
}

pub(super) fn draw_balloon(
    mask: &mut GrayImage,
    polygon: &mut [[i32; 2]],
    ellipse: [i32; 4],
) -> bool {
    let (width, height) = mask.dimensions();
    unsafe {
        cs_draw_balloon(
            mask.as_mut_ptr(),
            width as i32,
            height as i32,
            polygon.as_mut_ptr(),
            polygon.len() as i32,
            ellipse.as_ptr(),
        ) != 0
    }
}

#[repr(C)]
#[derive(Default)]
struct GlyphMetrics {
    advance: i32,
    left: i32,
    bottom: i32,
    right: i32,
    top: i32,
}

#[repr(C)]
#[derive(Default)]
struct GlyphBitmap {
    left: i32,
    top: i32,
    width: u32,
    height: u32,
}

#[cfg(target_os = "linux")]
#[derive(Clone, Copy)]
#[repr(C)]
struct ShapedGlyph {
    index: u32,
    x_offset: i32,
    y_offset: i32,
    advance: i32,
}

struct GlyphPosition {
    index: u32,
    x: i32,
    y: i32,
}

#[cfg(target_os = "linux")]
fn raqm_available() -> bool {
    static AVAILABLE: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *AVAILABLE.get_or_init(|| unsafe { cs_font_init_shaping() != 0 })
}

pub(super) struct Font(NonNull<c_void>, Option<Vec<u8>>);

impl Drop for Font {
    fn drop(&mut self) {
        unsafe { cs_font_close(self.0.as_ptr()) };
        drop(self.1.take());
    }
}

impl Font {
    pub(super) fn new(bytes: &'static [u8]) -> Option<Self> {
        NonNull::new(unsafe { cs_font_open(bytes.as_ptr(), bytes.len()) })
            .map(|face| Self(face, None))
    }

    pub(super) fn from_bytes(bytes: Vec<u8>) -> Option<Self> {
        NonNull::new(unsafe { cs_font_open(bytes.as_ptr(), bytes.len()) })
            .map(|face| Self(face, Some(bytes)))
    }

    pub(super) fn glyph_index(&self, character: char) -> u32 {
        unsafe { cs_font_index(self.0.as_ptr(), character as u32) }
    }

    pub(super) fn layout(
        &self,
        characters: impl Iterator<Item = char>,
        size: i32,
    ) -> Option<TextRun> {
        #[cfg(target_os = "linux")]
        if raqm_available() {
            return self.layout_shaped(characters, size);
        }
        self.layout_basic(characters, size)
    }

    #[cfg(target_os = "linux")]
    fn layout_shaped(&self, characters: impl Iterator<Item = char>, size: i32) -> Option<TextRun> {
        let font = self.0.as_ptr();
        let text: Vec<u32> = characters.map(u32::from).collect();
        let mut run = TextRun::default();
        if text.is_empty() {
            return Some(run);
        }
        if unsafe { cs_font_size(font, size as u32) } == 0 {
            return None;
        }
        let glyphs: Vec<ShapedGlyph> =
            unsafe { Buffer::receive(|out| cs_font_shape(font, text.as_ptr(), text.len(), out)) }?;
        for glyph in glyphs {
            run.push(
                GlyphPosition {
                    index: glyph.index,
                    x: run.advance + glyph.x_offset,
                    y: glyph.y_offset,
                },
                self.metrics(glyph.index)?,
                glyph.advance,
            );
        }
        Some(run)
    }

    fn layout_basic(&self, characters: impl Iterator<Item = char>, size: i32) -> Option<TextRun> {
        let font = self.0.as_ptr();
        if unsafe { cs_font_size(font, size as u32) } == 0 {
            return None;
        }
        let mut run = TextRun::default();
        let mut previous = 0;
        for character in characters {
            let index = self.glyph_index(character);
            // BASIC layout adds kerning rounded to pixels to the 26.6 advance.
            run.advance += pixel(unsafe { cs_font_kerning(font, previous, index) });
            let metrics = self.metrics(index)?;
            let advance = metrics.advance;
            run.push(
                GlyphPosition {
                    index,
                    x: run.advance,
                    y: 0,
                },
                metrics,
                advance,
            );
            previous = index;
        }
        Some(run)
    }

    fn metrics(&self, index: u32) -> Option<GlyphMetrics> {
        let mut metrics = GlyphMetrics::default();
        (unsafe { cs_font_metrics(self.0.as_ptr(), index, &mut metrics) } != 0).then_some(metrics)
    }

    fn bitmap(&self, index: u32, stroke: u32) -> Option<(GlyphBitmap, Vec<u8>)> {
        let mut dimensions = GlyphBitmap::default();
        let bytes = unsafe {
            Buffer::receive(|out| {
                cs_font_bitmap(self.0.as_ptr(), index, stroke, &mut dimensions, out)
            })
        }?;
        Some((dimensions, bytes))
    }

    pub(super) fn mask(
        &self,
        run: &TextRun,
        size: i32,
        x: f64,
        y: f64,
        stroke: i32,
    ) -> Option<(GrayImage, i32, i32)> {
        if unsafe { cs_font_size(self.0.as_ptr(), size as u32) } == 0 {
            return None;
        }
        let (fx, fy) = (x.fract() as f32, y.fract() as f32);
        let (w, h) = (
            run.x_max - run.x_min + (2.0 * stroke as f32 + fx).ceil() as i32,
            run.y_max - run.y_min + (2.0 * stroke as f32 + fy).ceil() as i32,
        );
        let unfilled = run
            .glyphs
            .iter()
            .map(|position| self.bitmap(position.index, 0))
            .collect::<Option<Vec<_>>>()?;
        let (mut x_min, mut y_max) = (0, 0);
        for (position, (glyph, _)) in run.glyphs.iter().zip(&unfilled) {
            x_min = x_min.min(pixel(position.x) + glyph.left);
            y_max = y_max.max(pixel(position.y) + glyph.top);
        }
        let px = ((-x_min as f32 + stroke as f32 + fx) * 64.0).round() as i32;
        let py = ((-y_max as f32 - stroke as f32 - fy) * 64.0).round() as i32;
        let mut mask = GrayImage::new(w as u32, h as u32);
        for (position, normal) in run.glyphs.iter().zip(unfilled) {
            let (glyph, data) = if stroke == 0 {
                normal
            } else {
                self.bitmap(position.index, stroke as u32)?
            };
            let left = pixel(px + position.x) + glyph.left;
            let top = -(pixel(py + position.y) + glyph.top);
            for row in 0..glyph.height as i32 {
                for col in 0..glyph.width as i32 {
                    let (mx, my) = (left + col, top + row);
                    if (0..w).contains(&mx) && (0..h).contains(&my) {
                        let alpha =
                            u32::from(data[row as usize * glyph.width as usize + col as usize]);
                        let pixel = &mut mask.get_pixel_mut(mx as u32, my as u32)[0];
                        *pixel = (alpha + (u32::from(*pixel) * (255 - alpha) + 127) / 255) as u8;
                    }
                }
            }
        }
        Some((
            mask,
            x as i32 + run.x_min - stroke,
            y as i32 - run.y_max - stroke,
        ))
    }
}

fn pixel(fixed: i32) -> i32 {
    (fixed + 32) >> 6
}

#[derive(Default)]
pub(super) struct TextRun {
    glyphs: Vec<GlyphPosition>,
    advance: i32,
    x_min: i32,
    x_max: i32,
    y_min: i32,
    y_max: i32,
}

impl TextRun {
    fn push(&mut self, position: GlyphPosition, metrics: GlyphMetrics, advance: i32) {
        let (x, y) = (pixel(position.x), pixel(position.y));
        self.glyphs.push(position);
        self.advance += advance;
        self.x_min = self.x_min.min(x + metrics.left);
        self.x_max = self.x_max.max(pixel(self.advance)).max(x + metrics.right);
        self.y_min = self.y_min.min(y + metrics.bottom);
        self.y_max = self.y_max.max(y + metrics.top);
    }

    pub(super) fn length(&self) -> f64 {
        f64::from(self.advance) / 64.0
    }

    pub(super) fn vertical_bounds(&self) -> (i32, i32) {
        (-self.y_max, -self.y_min)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::validation::bytes_hash;

    #[test]
    fn glyph_masks_match_pillow_at_fractional_baseline() {
        let font = Font::new(include_bytes!(
            "../../../../assets/fonts/ComicNeue-Regular.ttf"
        ))
        .unwrap();
        #[cfg(target_os = "linux")]
        let shaped = raqm_available();
        #[cfg(not(target_os = "linux"))]
        let shaped = false;
        let (advance, masks) = if shaped {
            (
                265.6875,
                [
                    (
                        0,
                        (267, 38),
                        (1, -2),
                        "d9b945592712b8a330d5969ca1b487f1d14612ba4afe5ad87d8efee819906eff",
                    ),
                    (
                        1,
                        (269, 40),
                        (0, -3),
                        "10dee179eb58751080b67698cdbd9abbafd7eac99fe0b5426a69cb6ffd1ca56e",
                    ),
                ],
            )
        } else {
            (
                267.0,
                [
                    (
                        0,
                        (268, 38),
                        (1, -2),
                        "7f443bf0a500db97ddff4a2e01b57495aec219bbd6e6821424f54cb53c94700d",
                    ),
                    (
                        1,
                        (270, 40),
                        (0, -3),
                        "55f762c12d205f3860a258c3c629d0a318999b34a2a4ab265adaaba393124773",
                    ),
                ],
            )
        };
        let run = font.layout("AV office – Ag!".chars(), 42).unwrap();
        assert_eq!(run.length(), advance);
        assert_eq!(run.vertical_bounds(), (-29, 8));
        for (stroke, dimensions, position, hash) in masks {
            let (mask, x, y) = font.mask(&run, 42, 1.5, 27.5, stroke).unwrap();
            assert_eq!(mask.dimensions(), dimensions);
            assert_eq!((x, y), position);
            assert_eq!(bytes_hash(mask.as_raw()), hash);
        }
    }
}
