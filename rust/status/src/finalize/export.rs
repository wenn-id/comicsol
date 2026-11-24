use std::io::Write;
use std::ops::Range;

use image::{ImageFormat, RgbImage};
use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::{self, LockedProject};
use crate::recovery::Change;
use crate::resume::timestamp;
use crate::validation::{Stage, bytes_hash};

use super::composition::{HEIGHT, WIDTH};
use super::{
    MAX_RASTER, descriptor, exists, names, native, page_number, publish_manifest, raster, read,
    require_valid,
};

const VERIFICATION: &str = "exports/pdf-verification.json";

pub(super) fn run(locked: &LockedProject, project: &mut Value, count: usize) -> Result<String> {
    let root = locked.root();
    let id = project["project_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .ok_or_else(|| Error::export("manifest project_id is invalid"))?;
    let destination = format!("exports/{id}.pdf");
    project_fs::relative_path(&destination)
        .map_err(|_| Error::export("guarded export destination must remain inside the project"))?;
    require_valid(root, Stage::ExportReady)?;
    let pages: Vec<_> = names(root, "pages")?
        .into_iter()
        .filter_map(|name| {
            let number = page_number(&name, ".png")?;
            exists(root, &format!("pages/{name}")).then_some(number)
        })
        .collect();
    if pages.is_empty() {
        return Err(Error::export("no composed page PNGs exist"));
    }
    if !pages.iter().copied().eq(1..=count) {
        let missing: Vec<_> = (1..=count)
            .filter(|n| !pages.contains(n))
            .map(|n| format!("page-{n:03}.png"))
            .collect();
        let detail = if missing.is_empty() {
            String::new()
        } else {
            format!("; missing {}", missing.join(", "))
        };
        return Err(Error::export(format!(
            "page filenames must be contiguous from page-001.png through page-{count:03}.png{detail}"
        )));
    }

    let mut sources = Vec::with_capacity(count);
    let mut frames = Vec::with_capacity(count);
    let mut images = Vec::with_capacity(count);
    for number in 1..=count {
        let page = format!("pages/page-{number:03}.png");
        let qa = format!("qa/pages/page-{number:03}.json");
        let source = read(root, &page, MAX_RASTER)
            .map_err(|_| Error::export(format!("page-{number:03}.png is not a readable PNG")))?;
        let (image, format) = raster::decode(&source)
            .ok_or_else(|| Error::export(format!("page-{number:03}.png is not a readable PNG")))?;
        if format != ImageFormat::Png {
            return Err(Error::export(format!(
                "page-{number:03}.png must contain PNG data"
            )));
        }
        if (image.width(), image.height()) != (WIDTH, HEIGHT) {
            return Err(Error::export(format!(
                "page-{number:03}.png must be exactly {WIDTH}x{HEIGHT}"
            )));
        }
        let review = read(root, &qa, crate::error::contract::MAX_JSON_BYTES as u64)
            .map_err(|_| Error::export("PdfExportError"))?;
        let hash = bytes_hash(&source);
        let record = std::str::from_utf8(&review)
            .ok()
            .and_then(|text| manifest::parse_document(text, &qa).ok())
            .unwrap_or_default();
        let issues = crate::validation::page_quality_issues(
            root,
            number,
            &record,
            Some((&hash, [WIDTH, HEIGHT])),
        );
        if !issues.is_empty() {
            let detail = issues
                .into_iter()
                .map(|(_, message)| message)
                .collect::<Vec<_>>()
                .join("; ");
            return Err(Error::export(format!(
                "page QA is stale for the consumed input snapshot: {detail}"
            )));
        }
        sources.push(
            json!({"dimensions":[WIDTH,HEIGHT],"path":page,"page_qa_path":qa,
            "sha256":hash,"page_qa_sha256":bytes_hash(&review)}),
        );
        let image = image.into_rgb8();
        let jpeg = native::jpeg(&image)
            .ok_or_else(|| Error::export("cannot encode the PDF raster frame"))?;
        frames.push(jpeg);
        images.push(image);
    }
    let (pdf, streams) = pdf_bytes(&frames);
    let metrics: Vec<_> = streams
        .iter()
        .zip(&images)
        .enumerate()
        .map(|(index, (stream, image))| {
            let decoded = native::decode_jpeg(&pdf[stream.clone()], WIDTH, HEIGHT)
                .ok_or_else(|| Error::export("PDF raster frame could not be decoded"))?;
            metrics(image, &decoded, index + 1)
        })
        .collect::<Result<_>>()?;
    let verification = json!({
        "exporter_version":"comic-sol-pillow-raster-v1", "page_count":count, "pages":metrics,
        "tolerance":{"grid_region_max_size_px":64,"high_error_channel_threshold":24,
            "max_grid_region_error":16.0,"max_high_error_pixel_ratio":0.021,
            "max_mean_absolute_channel_error":3.1,"version":"2"},
        "kind":"pdf-verification", "pdf_path":destination, "pdf_sha256":bytes_hash(&pdf),
        "schema_version":"1.0", "source_pages":sources, "verified_at":timestamp(),
    });
    let evidence = manifest::canonical(&verification);
    descriptor(project, "pdf", &destination, &pdf);
    descriptor(project, "pdf_verification", VERIFICATION, &evidence);
    publish_manifest(
        locked,
        "pdf-export",
        project,
        vec![
            Change::Replace(destination.clone(), pdf),
            Change::Replace(VERIFICATION.into(), evidence),
        ],
    )?;
    Ok(destination)
}

fn pdf_bytes(jpegs: &[Vec<u8>]) -> (Vec<u8>, Vec<Range<usize>>) {
    let count = jpegs.len();
    let mut pdf = b"%PDF-1.4\n% created by Pillow PDF driver\n".to_vec();
    let mut offsets = vec![0; count * 3 + 4];
    let mut ranges = Vec::new();
    let catalog = count * 2 + 1;
    let pages = catalog + 1;
    let kids = (0..count)
        .map(|i| format!("{} 0 R", i * 2 + 1))
        .collect::<Vec<_>>()
        .join(" ");
    object(
        &mut pdf,
        &mut offsets,
        catalog,
        format!("<<\n/Type /Catalog\n/Pages {pages} 0 R\n>>").as_bytes(),
    );
    object(
        &mut pdf,
        &mut offsets,
        pages,
        format!("<<\n/Type /Pages\n/Count {count}\n/Kids [ {kids} ]\n>>").as_bytes(),
    );
    for (index, jpeg) in jpegs.iter().enumerate() {
        let page = index * 2 + 1;
        let draw = page + 1;
        let image = pages + 1 + index;
        let entry = format!(
            "/Type /XObject\n/Subtype /Image\n/Width {WIDTH}\n/Height {HEIGHT}\n/Filter /DCTDecode\n/BitsPerComponent 8\n/ColorSpace /DeviceRGB\n"
        );
        ranges.push(stream(&mut pdf, &mut offsets, image, &entry, jpeg));
        object(&mut pdf,&mut offsets,page,
            format!("<<\n/Resources <<\n/ProcSet [ /PDF /ImageC ]\n/XObject <<\n/image {image} 0 R\n>>\n>>\n/MediaBox [ 0 0 768.0 1152.0 ]\n/Contents {draw} 0 R\n/Type /Page\n/Parent {pages} 0 R\n>>").as_bytes());
        let content = b"q 768.000000 0 0 1152.000000 0 0 cm /image Do Q\n";
        stream(&mut pdf, &mut offsets, draw, "", content);
    }
    let info = offsets.len() - 1;
    let mut producer = b"<<\n/Producer (\xfe\xff".to_vec();
    for code in "Comic Sol 1.0".encode_utf16() {
        producer.extend_from_slice(&code.to_be_bytes());
    }
    producer.extend_from_slice(b")\n>>");
    object(&mut pdf, &mut offsets, info, &producer);
    let start = pdf.len();
    write!(pdf, "xref\n0 {}\n0000000000 65536 f \n", offsets.len()).expect("PDF memory");
    for offset in offsets.iter().skip(1) {
        writeln!(pdf, "{offset:010} 00000 n ").expect("PDF memory");
    }
    write!(
        pdf,
        "trailer\n<<\n/Root {catalog} 0 R\n/Size {}\n/Info {info} 0 R\n>>\nstartxref\n{start}\n%%EOF",
        offsets.len()
    )
    .expect("PDF memory");
    (pdf, ranges)
}

fn object(pdf: &mut Vec<u8>, offsets: &mut [usize], id: usize, body: &[u8]) {
    offsets[id] = pdf.len();
    write!(pdf, "{id} 0 obj").expect("PDF memory");
    pdf.extend_from_slice(body);
    pdf.extend_from_slice(b"endobj\n");
}

fn stream(
    pdf: &mut Vec<u8>,
    offsets: &mut [usize],
    id: usize,
    attributes: &str,
    body: &[u8],
) -> Range<usize> {
    offsets[id] = pdf.len();
    write!(
        pdf,
        "{id} 0 obj<<\n{attributes}/Length {}\n>>stream\n",
        body.len()
    )
    .expect("PDF memory");
    let start = pdf.len();
    pdf.extend_from_slice(body);
    let end = pdf.len();
    pdf.extend_from_slice(b"\nendstream\nendobj\n");
    start..end
}

fn metrics(source: &RgbImage, decoded: &RgbImage, number: usize) -> Result<Value> {
    let mut total = 0;
    let mut high = 0;
    let mut max_error: f64 = 0.0;
    let cols = WIDTH.div_ceil(64);
    let rows = HEIGHT.div_ceil(64);
    let ratio =
        |n: u64, d: u64| (n as f64 * 1_000_000.0 / d as f64).round_ties_even() / 1_000_000.0;
    for row in 0..rows {
        let top = row * HEIGHT / rows;
        let bottom = (row + 1) * HEIGHT / rows;
        for col in 0..cols {
            let left = col * WIDTH / cols;
            let right = (col + 1) * WIDTH / cols;
            let mut error = 0;
            for y in top..bottom {
                for x in left..right {
                    let a = source.get_pixel(x, y);
                    let b = decoded.get_pixel(x, y);
                    let delta = [
                        a[0].abs_diff(b[0]),
                        a[1].abs_diff(b[1]),
                        a[2].abs_diff(b[2]),
                    ];
                    high += u64::from(delta.iter().any(|&d| d > 24));
                    error += delta.into_iter().map(u64::from).sum::<u64>();
                }
            }
            max_error = max_error.max(ratio(error, u64::from((right - left) * (bottom - top) * 3)));
            total += error;
        }
    }
    let pixels = u64::from(WIDTH) * u64::from(HEIGHT);
    let average = ratio(total, pixels * 3);
    let high = ratio(high, pixels);
    if average > 3.1 || high > 0.021 || max_error > 16.0 {
        return Err(Error::export(format!(
            "written PDF content mismatch at page {number}"
        )));
    }
    Ok(
        json!({"page_number":number,"dimensions":[WIDTH,HEIGHT],"mode":"RGB","compared_pixels":pixels,
        "mean_absolute_channel_error":average,"high_error_pixel_ratio":high,"maximum_grid_region_error":max_error}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::Rgb;

    #[test]
    fn multipage_pdf_matches_pillow() {
        let frames = [[53, 87, 173], [216, 80, 10]]
            .map(|color| native::jpeg(&RgbImage::from_pixel(WIDTH, HEIGHT, Rgb(color))).unwrap());
        let (pdf, _) = pdf_bytes(&frames);
        assert_eq!(
            bytes_hash(&pdf),
            "753ef471ba448dd232ed886e66971131f2981ad5813d5f6c6ce885a30c08a974"
        );
    }
}
