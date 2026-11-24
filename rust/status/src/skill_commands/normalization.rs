use std::io::Read;

use image::{ImageFormat, Rgb, RgbImage, imageops};
use serde_json::{Value, json};

use super::{panel_id, relative_arg, required};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::finalize::raster::{self, MAX_PIXELS};
use crate::manifest;
use crate::project_fs::{self, LockedProject};
use crate::recovery::{Change, publish};
use crate::validation::{artifact, bytes_hash};

const MAX_RASTER: u64 = 128 * 1024 * 1024;

pub(super) fn run(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    let id = panel_id(required(args, "panel")?)?;
    let dimension = |name| {
        required(args, name)?
            .parse::<u32>()
            .ok()
            .filter(|&n| n > 0)
            .ok_or_else(|| Error::invalid("target size dimensions must be positive integers"))
    };
    let (width, height) = (dimension("width")?, dimension("height")?);
    if u64::from(width) * u64::from(height) > MAX_PIXELS {
        return Err(Error::invalid(
            "target size exceeds the decoded pixel limit",
        ));
    }
    let mode = required(args, "mode")?;
    if !matches!(mode, "exact" | "fit" | "crop") {
        return Err(Error::invalid(
            "normalization mode must be one of ['crop', 'exact', 'fit']",
        ));
    }
    let source = args
        .path("source")
        .ok_or_else(|| Error::invalid("required argument: source"))?;
    let source = relative_arg(args.paths("project_dir"), source)?;
    let (parent, name) = artifact(locked.root(), &source)?
        .ok_or_else(|| Error::invalid(format!("source is missing: {source}")))?;
    let file = project_fs::open_file(&parent, &name, false, false)?;
    let mut bytes = Vec::new();
    file.take(MAX_RASTER + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_RASTER {
        return Err(Error::limit("the raster byte limit"));
    }
    let (decoded, format, encoded_size, orientation) = raster::normalization_source(&bytes)
        .ok_or_else(|| Error::invalid(format!("source is not a readable image: {source}")))?;
    let format = match format {
        ImageFormat::Png => "PNG",
        ImageFormat::Jpeg => "JPEG",
        ImageFormat::WebP => "WEBP",
        _ => {
            return Err(Error::invalid(
                "source image format must be PNG, JPEG, or WEBP",
            ));
        }
    };
    let oriented = match decoded {
        image::DynamicImage::ImageLuma16(image) => {
            RgbImage::from_fn(image.width(), image.height(), |x, y| {
                Rgb([image.get_pixel(x, y).0[0].min(255) as u8; 3])
            })
        }
        image::DynamicImage::ImageRgb16(image) => {
            RgbImage::from_fn(image.width(), image.height(), |x, y| {
                Rgb(image.get_pixel(x, y).0.map(|sample| (sample >> 8) as u8))
            })
        }
        image::DynamicImage::ImageRgba16(image) => {
            RgbImage::from_fn(image.width(), image.height(), |x, y| {
                let [r, g, b, _] = image.get_pixel(x, y).0;
                Rgb([r, g, b].map(|sample| (sample >> 8) as u8))
            })
        }
        image => image.into_rgb8(),
    };
    let (w, h) = oriented.dimensions();
    let across = u64::from(w) * u64::from(height);
    let down = u64::from(h) * u64::from(width);
    let ratio = |lhs, rhs, divisor| (u64::from(lhs) * u64::from(rhs) / u64::from(divisor)) as u32;
    let (mut origin, mut resized, mut crop) = ([0, 0], [width, height], None);
    let image = match mode {
        "exact" if across != down => {
            return Err(Error::invalid(
                "exact normalization requires matching aspect ratios",
            ));
        }
        "fit" => {
            resized = if across >= down {
                [width, ratio(h, width, w).max(1)]
            } else {
                [ratio(w, height, h).max(1), height]
            };
            origin = [(width - resized[0]) / 2, (height - resized[1]) / 2];
            let fit = resize(&oriented, resized);
            let mut canvas = RgbImage::from_pixel(width, height, Rgb([255; 3]));
            imageops::replace(
                &mut canvas,
                &fit,
                i64::from(origin[0]),
                i64::from(origin[1]),
            );
            canvas
        }
        "crop" => {
            let (cw, ch) = if across > down {
                (ratio(h, width, height), h)
            } else {
                (w, ratio(w, height, width))
            };
            if cw == 0 || ch == 0 {
                return Err(Error::invalid("normalization crop is empty"));
            }
            let (x, y) = ((w - cw) / 2, (h - ch) / 2);
            crop = Some([x, y, x + cw, y + ch]);
            let cropped = imageops::crop_imm(&oriented, x, y, cw, ch).to_image();
            resize(&cropped, resized)
        }
        _ => resize(&oriented, resized),
    };
    let clean = raster::png(&image)?;
    let destination = format!("panels/{id}/clean.png");
    let record = json!({
        "clean":{"mode":"RGB", "path":destination, "sha256":bytes_hash(&clean), "size":[width,height]},
        "implementation_version":"1",
        "operation":{"crop_box":crop, "mode":mode, "paste_origin":origin, "resized_size":resized},
        "panel_id":id, "schema_version":"1.0",
        "source":{"encoded_size":encoded_size, "exif_orientation":orientation, "format":format,
            "path":source, "sha256":bytes_hash(&bytes), "size":[w,h]},
        "target_size":[width,height],
    });
    publish(
        locked,
        "panel-normalization",
        vec![
            Change::Replace(destination.clone(), clean),
            Change::Replace(
                format!("panels/{id}/normalization.json"),
                manifest::canonical(&record),
            ),
        ],
    )?;
    Ok(destination.into())
}

fn resize(image: &RgbImage, [width, height]: [u32; 2]) -> RgbImage {
    raster::resize(
        image,
        width,
        height,
        [0.0, 0.0, image.width() as f32, image.height() as f32],
    )
}
