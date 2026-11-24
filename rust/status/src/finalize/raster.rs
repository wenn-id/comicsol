use std::io::Cursor;

use image::{
    DynamicImage, GrayImage, ImageBuffer, ImageDecoder, ImageFormat, ImageReader, Limits, Pixel,
    RgbImage,
};

use crate::error::{Error, Result};

use super::native;

const MAX_PIXELS: u64 = 1600 * 2400 * 16;
const PRECISION: i64 = 1 << 22;

pub(super) fn decode(bytes: &[u8]) -> Option<(DynamicImage, ImageFormat)> {
    let (image, format, _) = raster(bytes, false)?;
    Some((image, format))
}

pub(super) fn lettering_source(bytes: &[u8]) -> Option<(RgbImage, (u32, u32))> {
    let (image, _, dimensions) = raster(bytes, true)?;
    Some((image.into_rgb8(), dimensions))
}

fn raster(bytes: &[u8], orient: bool) -> Option<(DynamicImage, ImageFormat, (u32, u32))> {
    let format = image::guess_format(bytes).ok()?;
    if !matches!(
        format,
        ImageFormat::Png | ImageFormat::Jpeg | ImageFormat::WebP
    ) {
        return None;
    }
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    let mut limits = Limits::default();
    limits.max_image_width = Some(MAX_PIXELS as u32);
    limits.max_image_height = Some(MAX_PIXELS as u32);
    reader.limits(limits);
    let mut decoder = reader.into_decoder().ok()?;
    let (width, height) = decoder.dimensions();
    if u64::from(width) * u64::from(height) > MAX_PIXELS {
        return None;
    }
    let orientation = orient.then(|| decoder.orientation().ok()).flatten();
    let mut image = DynamicImage::from_decoder(decoder).ok()?;
    if let Some(orientation) = orientation {
        image.apply_orientation(orientation);
    }
    Some((image, format, (width, height)))
}

pub(super) fn png(image: &RgbImage) -> Result<Vec<u8>> {
    encode_png(image.as_raw(), image.width(), image.height(), 8, 2, None, 9)
}

pub(crate) fn encode_png(
    pixels: &[u8],
    width: u32,
    height: u32,
    depth: u8,
    color: u8,
    palette: Option<&[u8]>,
    compression: i32,
) -> Result<Vec<u8>> {
    let bits = usize::from(depth) * if color == 2 { 3 } else { 1 };
    let bpp = bits.div_ceil(8);
    let stride = (width as usize * bits).div_ceil(8);
    if stride == 0 || height == 0 || pixels.len() != stride * height as usize {
        return Err(Error::invalid("cannot encode an empty PNG raster"));
    }
    let mut rows = Vec::with_capacity((stride + 1) * height as usize);
    let zero = vec![0; stride];
    let mut previous = zero.as_slice();
    let mut candidate = vec![0; stride];
    for raw in pixels.chunks_exact(stride) {
        let start = rows.len();
        rows.push(0);
        rows.extend_from_slice(raw);
        let mut best = filter_cost(raw);
        for filter in [2, 1, 4] {
            for (i, out) in candidate.iter_mut().enumerate() {
                let left = i.checked_sub(bpp).map_or(0, |j| raw[j]);
                let above = previous[i];
                let above_left = i.checked_sub(bpp).map_or(0, |j| previous[j]);
                let prediction = match filter {
                    2 => above,
                    1 => left,
                    _ => paeth(left, above, above_left),
                };
                *out = raw[i].wrapping_sub(prediction);
            }
            let cost = filter_cost(&candidate);
            if cost < best {
                best = cost;
                rows[start] = filter;
                rows[start + 1..].copy_from_slice(&candidate);
            }
        }
        previous = raw;
    }
    let compressed = native::deflate(&rows, (stride + 1) as u32, height, compression)
        .ok_or_else(|| Error::invalid("cannot encode a PNG raster"))?;
    let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut header = [0, 0, 0, 0, 0, 0, 0, 0, depth, color, 0, 0, 0];
    header[..4].copy_from_slice(&width.to_be_bytes());
    header[4..8].copy_from_slice(&height.to_be_bytes());
    png_chunk(&mut bytes, b"IHDR", &header);
    if let Some(palette) = palette {
        png_chunk(&mut bytes, b"PLTE", palette);
    }
    for chunk in compressed.chunks(65_536.max(width as usize * 4)) {
        png_chunk(&mut bytes, b"IDAT", chunk);
    }
    png_chunk(&mut bytes, b"IEND", &[]);
    Ok(bytes)
}

fn filter_cost(bytes: &[u8]) -> u64 {
    bytes
        .iter()
        .map(|&byte| u64::from((byte as i8).unsigned_abs()))
        .sum()
}

fn paeth(left: u8, above: u8, diagonal: u8) -> u8 {
    let a = i16::from(left) - i16::from(diagonal);
    let b = i16::from(above) - i16::from(diagonal);
    let (pa, pb, pc) = (b.abs(), a.abs(), (a + b).abs());
    if pa <= pb && pa <= pc {
        left
    } else if pb <= pc {
        above
    } else {
        diagonal
    }
}

fn png_chunk(bytes: &mut Vec<u8>, tag: &[u8; 4], data: &[u8]) {
    bytes.extend_from_slice(&(data.len() as u32).to_be_bytes());
    let start = bytes.len();
    bytes.extend_from_slice(tag);
    bytes.extend_from_slice(data);
    bytes.extend_from_slice(&native::crc(&bytes[start..]).to_be_bytes());
}

struct Weights {
    first: usize,
    values: Vec<i64>,
}

fn sinc(x: f64) -> f64 {
    if x == 0.0 {
        return 1.0;
    }
    let phase = x * std::f64::consts::PI;
    phase.sin() / phase
}

fn weights(input: u32, start: f32, end: f32, output: u32) -> Vec<Weights> {
    let start = f64::from(start);
    let scale = (f64::from(end) - start) / f64::from(output);
    let filter = scale.max(1.0);
    let support = filter * 3.0;
    (0..output)
        .map(|index| {
            let center = start + (f64::from(index) + 0.5) * scale;
            let first = ((center - support + 0.5) as usize).min(input as usize);
            let last = ((center + support + 0.5) as usize).min(input as usize);
            let values: Vec<_> = (first..last)
                .map(|x| {
                    let radius = (x as f64 - center + 0.5) / filter;
                    if (-3.0..3.0).contains(&radius) {
                        sinc(radius) * sinc(radius / 3.0)
                    } else {
                        0.0
                    }
                })
                .collect();
            let sum: f64 = values.iter().sum();
            let values = values
                .into_iter()
                .map(|value| (value / sum * PRECISION as f64).round() as i64)
                .collect();
            Weights { first, values }
        })
        .collect()
}

fn sample<P: Pixel<Subpixel = u8>>(weights: &Weights, mut pixel: impl FnMut(usize) -> P) -> P {
    let mut sums = [PRECISION / 2; 4];
    for (offset, &weight) in weights.values.iter().enumerate() {
        let p = pixel(weights.first + offset);
        for (sum, &c) in sums.iter_mut().zip(p.channels()) {
            *sum += weight * i64::from(c);
        }
    }
    let channels = sums.map(|sum| (sum >> 22).clamp(0, 255) as u8);
    *P::from_slice(&channels[..usize::from(P::CHANNEL_COUNT)])
}

fn resize<P: Pixel<Subpixel = u8> + 'static>(
    image: &ImageBuffer<P, Vec<u8>>,
    width: u32,
    height: u32,
    [left, top, right, bottom]: [f32; 4],
) -> ImageBuffer<P, Vec<u8>> {
    let vertical = weights(image.height(), top, bottom, height);
    let begin = vertical.first().map_or(0, |row| row.first);
    let end = vertical
        .last()
        .map_or(0, |row| row.first + row.values.len());
    let horizontal = if width != image.width() || left != 0.0 || right != image.width() as f32 {
        Some(weights(image.width(), left, right, width))
    } else {
        None
    };
    let temp = horizontal.as_ref().map(|horizontal| {
        ImageBuffer::from_fn(width, (end - begin) as u32, |x, y| {
            sample(&horizontal[x as usize], |sx| {
                *image.get_pixel(sx as u32, y + begin as u32)
            })
        })
    });
    if height == image.height() && top == 0.0 && bottom == image.height() as f32 {
        return temp.unwrap_or_else(|| image.clone());
    }
    ImageBuffer::from_fn(width, height, |x, y| {
        sample(&vertical[y as usize], |sy| {
            if let Some(temp) = &temp {
                *temp.get_pixel(x, (sy - begin) as u32)
            } else {
                *image.get_pixel(x, sy as u32)
            }
        })
    })
}

pub(super) fn fit(image: &RgbImage, width: u32, height: u32) -> RgbImage {
    let (w, h) = (f64::from(image.width()), f64::from(image.height()));
    let ratio = f64::from(width) / f64::from(height);
    let (cw, ch) = if w / h == ratio {
        (w, h)
    } else if w / h >= ratio {
        (ratio * h, h)
    } else {
        (w, w / ratio)
    };
    let (left, top) = ((w - cw) * 0.5, (h - ch) * 0.5);
    resize(
        image,
        width,
        height,
        [
            left as f32,
            top as f32,
            (left + cw) as f32,
            (top + ch) as f32,
        ],
    )
}

pub(super) fn shrink(mask: &GrayImage, width: u32, height: u32) -> GrayImage {
    resize(
        mask,
        width,
        height,
        [0.0, 0.0, mask.width() as f32, mask.height() as f32],
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::validation::bytes_hash;

    #[test]
    fn png_matches_pillow_across_idat_chunks() {
        let mut state = 0x1234_abcdu32;
        let pixels: Vec<_> = (0..257 * 96 * 3)
            .map(|_| {
                state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                (state >> 24) as u8
            })
            .collect();
        let image = RgbImage::from_raw(257, 96, pixels).unwrap();
        assert_eq!(
            bytes_hash(&png(&image).unwrap()),
            "06e04949d0c0136457a48fd175a66be31055dde93a8c6a0c542a308bbde1c01e"
        );
    }
}
