use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::LockedProject;
use crate::recovery::{Change, publish};
use crate::validation::{artifact, bytes_hash, storyboard};

use super::{COMPOSITION, MAX_RASTER, descriptor, publish_manifest, raster, read};

pub(super) const WIDTH: u32 = 1600;
pub(super) const HEIGHT: u32 = 2400;

pub(super) fn run(locked: &LockedProject, project: &mut Value, board: &Value) -> Result<()> {
    compose(locked, project, board, None)
}

pub(super) fn one(
    locked: &LockedProject,
    project: &mut Value,
    board: &Value,
    page: u64,
) -> Result<()> {
    compose(locked, project, board, Some(page))
}

fn compose(
    locked: &LockedProject,
    project: &mut Value,
    board: &Value,
    only: Option<u64>,
) -> Result<()> {
    let root = locked.root();
    let settings = &project["settings"];
    if !settings.is_object() || !project.get("artifacts").is_none_or(Value::is_object) {
        return Err(Error::invalid(
            "manifest settings and artifacts must be objects",
        ));
    }
    let pages = board["pages"]
        .as_array()
        .ok_or_else(|| Error::invalid("storyboard pages must be an array"))?;
    let mut numbered: Vec<_> = pages
        .iter()
        .filter_map(|page| Some((page["number"].as_u64()?, page)))
        .collect();
    numbered.sort_unstable_by_key(|&(number, _)| number);
    if let Some(number) = only {
        numbered.retain(|&(n, _)| n == number);
        if numbered.is_empty() {
            return Err(Error::invalid(format!(
                "storyboard does not define page {number}"
            )));
        }
        numbered.truncate(1);
    } else if numbered.len() != pages.len()
        || !numbered.iter().map(|&(n, _)| n).eq(1..=pages.len() as u64)
    {
        return Err(Error::invalid(
            "storyboard pages must be numbered contiguously from 1",
        ));
    }
    let settings_hash = bytes_hash(&manifest::canonical(settings));
    let mut changes = Vec::new();
    let mut records = Vec::new();
    for (number, page) in numbered {
        let id = format!("page-{number:03}");
        let relative = format!("pages/{id}.png");
        let panels = page["panels"]
            .as_array()
            .filter(|list| !list.is_empty())
            .ok_or_else(|| {
                Error::invalid(format!("page {number} panels must be a non-empty array"))
            })?;
        let matched = storyboard::match_layout(panels).ok_or_else(|| {
            Error::invalid("layout rectangles overlap or exceed the canonical page")
        })?;
        let declared = page["layout"]
            .as_str()
            .filter(|layout| !layout.is_empty())
            .unwrap_or(matched);
        if declared != matched {
            return Err(Error::invalid(format!(
                "declared layout {declared} does not match storyboard rectangles"
            )));
        }
        let mut ordered = Vec::new();
        let mut panel_ids = Vec::new();
        let mut rectangles = Vec::new();
        let mut canvas = image::RgbImage::from_pixel(WIDTH, HEIGHT, image::Rgb([255, 255, 255]));
        for panel in panels {
            let panel_id = panel["id"].as_str().ok_or_else(|| {
                Error::invalid(format!("page {number} contains an invalid panel"))
            })?;
            let rect = &panel["rect"];
            let [x, y, w, h] = ["x", "y", "width", "height"]
                .map(|name| rect[name].as_u64().expect("validated rectangle") as u32);
            let configured = &project["artifacts"][panel_id];
            let candidates = [
                configured
                    .as_str()
                    .or_else(|| configured["path"].as_str())
                    .map(str::to_owned),
                Some(format!("panels/{panel_id}/lettered.png")),
                Some(format!("pages/{panel_id}.png")),
                Some(format!("panels/lettered/{panel_id}.png")),
            ];
            let mut source = None;
            for candidate in candidates.into_iter().flatten() {
                if artifact(root, &candidate)?.is_some() {
                    source = Some(read(root, &candidate, MAX_RASTER)?);
                    break;
                }
            }
            let source =
                source.ok_or_else(|| std::io::Error::from(std::io::ErrorKind::NotFound))?;
            let (source_image, _) = raster::decode(&source).ok_or_else(|| {
                Error::invalid(format!("panel {panel_id} is not a readable image"))
            })?;
            let fit = raster::fit(&source_image.into_rgb8(), w, h);
            for (dx, dy, pixel) in fit.enumerate_pixels() {
                let border = dx < 6 || dy < 6 || dx + 6 >= w || dy + 6 >= h;
                canvas.put_pixel(
                    x + dx,
                    y + dy,
                    if border {
                        image::Rgb([0, 0, 0])
                    } else {
                        *pixel
                    },
                );
            }
            ordered.push(format!("{panel_id}:{}", bytes_hash(&source)));
            panel_ids.push(panel_id);
            rectangles.push([x, y, w, h]);
        }
        let png = raster::png(&canvas)?;
        records.push(json!({
            "layout":{"name":declared,"rectangles":rectangles,"version":"1"},
            "ordered_lettered_sha256s":ordered,
            "output":{"dimensions":[WIDTH,HEIGHT],"path":relative,"sha256":bytes_hash(&png)},
            "page_id":id,"panel_ids":panel_ids,"settings_sha256":settings_hash,
            "storyboard_page_sha256":bytes_hash(&manifest::canonical(page)),
        }));
        changes.push(Change::Replace(relative, png));
    }
    if only.is_some() {
        return publish(locked, "composition-page", changes);
    }
    let bytes = manifest::canonical(
        &json!({"kind":"composition-cache","pages":records,"schema_version":"2.0"}),
    );
    descriptor(project, "composition_cache", COMPOSITION, &bytes);
    changes.push(Change::Replace(COMPOSITION.into(), bytes));
    publish_manifest(locked, "composition", project, changes)
}
