use std::io::Cursor;

use png::{BitDepth, ColorType};
use serde_json::Value;

use super::contract::aspect_matches;
use crate::error::{Error, Result};
use crate::finalize::raster::encode_png;

const MAX_PIXELS: u64 = 1600 * 2400 * 16;
const MIN_DIMENSION: u32 = 512;
const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";

pub(super) fn sanitize(bytes: &[u8], job: &Value) -> Result<Vec<u8>> {
    let invalid = |detail| super::snapshot::result_error(detail);
    if bytes.is_empty() {
        return Err(invalid("result raster: must not be empty"));
    }
    if !job["target_path"]
        .as_str()
        .is_some_and(|path| path.ends_with(".png"))
    {
        return Err(invalid("result raster: job target format is unsupported"));
    }
    if !bytes.starts_with(PNG_SIGNATURE) {
        let detail = if image::guess_format(bytes).is_ok() {
            "result raster: format must match PNG target"
        } else {
            "result raster: must be a readable PNG"
        };
        return Err(invalid(detail));
    }
    let unreadable = |_| invalid("result raster: must be a readable PNG");
    let mut decoder = png::Decoder::new(Cursor::new(bytes));
    decoder.set_limits(png::Limits {
        bytes: 256 * 1024 * 1024,
    });
    let info = decoder.read_header_info().map_err(unreadable)?;
    if u64::from(info.width) * u64::from(info.height) > MAX_PIXELS {
        return Err(Error::limit(format!(
            "the decoded raster pixel limit of {MAX_PIXELS} pixels"
        )));
    }
    let mut reader = decoder.read_info().map_err(unreadable)?;
    let info = reader.info();
    if matches!(info.color_type, ColorType::GrayscaleAlpha | ColorType::Rgba) || info.trns.is_some()
    {
        return Err(invalid(
            "result raster: must not contain alpha transparency",
        ));
    }
    let mut palette = info.palette.as_deref().map(<[u8]>::to_vec);
    let size = reader
        .output_buffer_size()
        .ok_or_else(|| invalid("result raster: must be a readable PNG"))?;
    let mut pixels = vec![0; size];
    let frame = reader.next_frame(&mut pixels).map_err(unreadable)?;
    pixels.truncate(frame.buffer_size());
    if frame.width < MIN_DIMENSION || frame.height < MIN_DIMENSION {
        return Err(invalid(
            "result raster: dimensions must both be at least 512px",
        ));
    }
    let dimensions = &job["requested_dimensions"];
    if dimensions.is_object()
        && (dimensions["width"] != frame.width || dimensions["height"] != frame.height)
    {
        return Err(invalid("result raster: dimensions do not match the job"));
    }
    if let Some(ratio) = job["requested_aspect_ratio"].as_str()
        && aspect_matches(frame.width.into(), frame.height.into(), ratio) != Some(true)
    {
        return Err(invalid(
            "result raster: aspect ratio does not match the job",
        ));
    }
    let mut depth = frame.bit_depth as u8;
    match (frame.color_type, frame.bit_depth) {
        (ColorType::Rgb, BitDepth::Sixteen) => {
            pixels = pixels.iter().step_by(2).copied().collect();
            depth = 8;
        }
        (ColorType::Grayscale, BitDepth::Two | BitDepth::Four) => {
            let max = (1u8 << depth) - 1;
            pixels = repack(&pixels, frame.width as usize, depth, 8, |value| {
                value * (255 / max)
            });
            depth = 8;
        }
        (ColorType::Indexed, _) => {
            let palette = palette
                .as_mut()
                .ok_or_else(|| invalid("result raster: must be a readable PNG"))?;
            let new_depth = match palette.len() / 3 {
                0..=2 => 1,
                3..=4 => 2,
                5..=16 => 4,
                _ => 8,
            };
            pixels = repack(&pixels, frame.width as usize, depth, new_depth, |value| {
                value
            });
            depth = new_depth;
            palette.resize(3 * (1 << depth), 0);
        }
        _ => {}
    }
    let palette = (frame.color_type == ColorType::Indexed)
        .then(|| palette.as_deref())
        .flatten();
    let encoded = encode_png(
        &pixels,
        frame.width,
        frame.height,
        depth,
        frame.color_type as u8,
        palette,
        6,
    )
    .map_err(|_| invalid("result raster: could not be sanitized safely"))?;
    if encoded.len() as u64 > super::MAX_READ {
        return Err(Error::limit(format!(
            "the encoded raster size limit of {} bytes",
            super::MAX_READ
        )));
    }
    Ok(encoded)
}

fn repack(pixels: &[u8], width: usize, before: u8, after: u8, map: impl Fn(u8) -> u8) -> Vec<u8> {
    let before = usize::from(before);
    let after = usize::from(after);
    let mask = ((1u16 << before) - 1) as u8;
    let stride = (width * before).div_ceil(8);
    let output_stride = (width * after).div_ceil(8);
    let mut output = vec![0; output_stride * (pixels.len() / stride)];
    for (input, output) in pixels
        .chunks_exact(stride)
        .zip(output.chunks_exact_mut(output_stride))
    {
        for pixel in 0..width {
            let src = pixel * before;
            let dst = pixel * after;
            let value = (input[src / 8] >> (8 - before - src % 8)) & mask;
            output[dst / 8] |= map(value) << (8 - after - dst % 8);
        }
    }
    output
}
