mod shapes;
pub(super) mod typography;

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use image::RgbImage;
use serde_json::{Value, json};
use unicode_segmentation::UnicodeSegmentation;

use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::LockedProject;
use crate::recovery::{Change, publish};
use crate::validation::{artifact, bytes_hash, is_panel, items, python_whitespace, sfx};

use super::native::TextRun;
use super::{BOARD, MAX_RASTER, object, raster, read};
use shapes::{Rect, Tail};
use typography::{BOLD, Fonts, Glyph, digest, display};

const ANCHORS: [&str; 8] = [
    "top-left",
    "top-center",
    "top-right",
    "middle-right",
    "bottom-right",
    "bottom-center",
    "bottom-left",
    "middle-left",
];
const GENERATED: &str = "generated-visual";
const LETTERED: &str = "deterministic-lettering";

pub(super) fn run(locked: &LockedProject, board: &Value) -> Result<()> {
    run_with_fonts(locked, board, &Fonts::new()?, Path::new("")).map(|_| ())
}

pub(super) fn run_with_fonts(
    locked: &LockedProject,
    board: &Value,
    fonts: &Fonts,
    project_path: &Path,
) -> Result<Value> {
    let root = locked.root();
    let board_bytes = read(root, BOARD, crate::error::contract::MAX_JSON_BYTES as u64)?;
    let board_hash = bytes_hash(&board_bytes);
    let bible = object(root, "plan/character-bible.json")?;
    let cast = bible["characters"]
        .as_array()
        .filter(|cast| cast.iter().all(Value::is_object))
        .ok_or_else(|| Error::invalid("character bible characters must be an array of objects"))?;
    let pages = board["pages"]
        .as_array()
        .filter(|pages| !pages.is_empty())
        .ok_or_else(|| Error::invalid("storyboard pages must be a non-empty array"))?;
    let mut panels = Vec::new();
    for (page_index, page) in pages.iter().enumerate() {
        if !page.is_object() {
            return Err(Error::invalid(format!(
                "storyboard page {} must be an object",
                page_index + 1
            )));
        }
        let page_panels = page["panels"]
            .as_array()
            .filter(|panels| !panels.is_empty())
            .ok_or_else(|| {
                Error::invalid(format!(
                    "storyboard page {} panels must be a non-empty array",
                    page_index + 1
                ))
            })?;
        for (panel_index, panel) in page_panels.iter().enumerate() {
            if !panel.is_object() {
                return Err(Error::invalid(format!(
                    "storyboard page {} panel {} must be an object",
                    page_index + 1,
                    panel_index + 1
                )));
            }
            let id = panel["id"]
                .as_str()
                .filter(|id| is_panel(id))
                .ok_or_else(|| {
                    Error::invalid(format!(
                        "storyboard page {} panel {} has an invalid ID",
                        page_index + 1,
                        panel_index + 1
                    ))
                })?;
            let text = panel["text"].as_array().ok_or_else(|| {
                Error::invalid(format!("storyboard panel {id} text must be an array"))
            })?;
            panels.push((id, panel, text, fonts.preflight(text)?));
        }
    }
    let mut changes = Vec::new();
    let mut summaries = Vec::new();
    for (id, panel, text, preflight) in panels {
        let canonical = format!("panels/{id}/clean.png");
        let clean_path = if artifact(root, &canonical)?.is_some() {
            canonical
        } else {
            format!("panels/clean/{id}.png")
        };
        let source = read(root, &clean_path, MAX_RASTER)
            .map_err(|_| Error::invalid(format!("panel {id} is not a readable image")))?;
        let (mut canvas, size) = raster::lettering_source(&source)
            .ok_or_else(|| Error::invalid(format!("panel {id} is not a readable image")))?;
        if canvas.dimensions() != size {
            canvas = raster::fit(&canvas, size.0, size.1);
        }
        let placements = draw_panel(&mut canvas, text, cast, fonts)?;
        let image = raster::png(&canvas)?;
        let path = format!("panels/{id}/lettered.png");
        let typography = manifest::canonical(&preflight);
        let mut geometry = json!({
            "bindings":{"clean_path":clean_path,"clean_sha256":bytes_hash(&source),
                "font_policy_sha256":preflight["font_policy_sha256"],"storyboard_path":BOARD,
                "storyboard_sha256":board_hash,"typography_sha256":bytes_hash(&typography)},
            "items":placements,"kind":"lettering-geometry","lettered":{"path":path,"sha256":bytes_hash(&image)},
            "panel_id":id,"schema_version":"1.2","sfx":sfx::provenance(panel,&placements),
        });
        geometry["geometry_sha256"] = digest(&geometry).into();
        if !project_path.as_os_str().is_empty() {
            let effects = text.iter().filter(|item| item["kind"] == "sfx").count();
            let word_count: usize = text
                .iter()
                .filter_map(|item| item["content"].as_str())
                .map(|content| {
                    content
                        .split(python_whitespace)
                        .filter(|word| !word.is_empty())
                        .count()
                })
                .sum();
            let output = std::fs::canonicalize(project_path)?.join(&path);
            summaries.push(json!({
                "font_used":fonts.regular_path(), "lettered_path":output, "lettered_sfx_count":items(&placements).iter().filter(|item| item["kind"] == "sfx").count(),
                "rendered_text_count":items(&placements).len(), "sfx_count":effects, "text_count":text.len(), "word_count":word_count,
                "placements":placements, "sfx_flags":geometry["sfx"]["flags"],
            }));
        }
        changes.push(Change::Replace(path, image));
        changes.push(Change::Replace(
            format!("panels/{id}/typography.json"),
            typography,
        ));
        changes.push(Change::Replace(
            format!("panels/{id}/lettering.json"),
            manifest::canonical(&geometry),
        ));
    }
    publish(locked, "lettering", changes)?;
    Ok(json!(summaries))
}

fn priority(item: &Value) -> f64 {
    item["priority"].as_f64().unwrap_or(0.0)
}

fn attributions(text: &[&Value], cast: &[Value]) -> Result<BTreeMap<String, Value>> {
    let mut result = BTreeMap::new();
    let mut prior: Vec<(&str, String, [f64; 2])> = Vec::new();
    let mut conflicts = Vec::new();
    for item in text.iter().filter(|item| item["kind"] == "dialogue") {
        let id = item["id"].as_str().expect("preflight item ID");
        let speaker = item["speaker"]
            .as_str()
            .filter(|name| !name.is_empty())
            .ok_or_else(|| Error::invalid("unknown dialogue character: None"))?;
        let (identity, resolution) = if cast.iter().any(|entry| entry["id"] == speaker) {
            (speaker, "declared")
        } else {
            let names: BTreeSet<_> = cast
                .iter()
                .filter(|entry| entry["name"] == speaker)
                .filter_map(|entry| entry["id"].as_str())
                .filter(|id| !id.is_empty())
                .collect();
            if names.len() > 1 {
                return Err(Error::invalid(format!(
                    "dialogue-attribution-ambiguous: display name '{speaker}' resolves to characters {}",
                    names.into_iter().collect::<Vec<_>>().join(", ")
                )));
            }
            (
                names.into_iter().next().ok_or_else(|| {
                    Error::invalid(format!("unknown dialogue character: {speaker}"))
                })?,
                "inferred",
            )
        };
        if result.contains_key(id) {
            return Err(Error::invalid(format!(
                "dialogue-attribution-ambiguous: text ID {id} is used by two spoken balloons"
            )));
        }
        result.insert(
            id.to_owned(),
            json!({"authored_speaker":speaker,"resolution":resolution,"speaker":identity,
            "speaker_anchor":item["speaker_anchor"]}),
        );
        if let Some(point) = anchor(item) {
            for (other, other_identity, other_point) in &prior {
                let distance = (point[0] - other_point[0]).hypot(point[1] - other_point[1]);
                let reason = if other_identity == identity && distance > 0.25 {
                    Some("split-anchor")
                } else if other_identity != identity && distance < 0.04 {
                    Some("shared-anchor")
                } else {
                    None
                };
                if let Some(reason) = reason {
                    conflicts.push(format!("{reason} between {other} and {id}"));
                }
            }
            prior.push((id, identity.to_owned(), point));
        }
    }
    if !conflicts.is_empty() {
        return Err(Error::invalid(format!(
            "dialogue-attribution-ambiguous: {}",
            conflicts.join("; ")
        )));
    }
    Ok(result)
}

fn anchor(item: &Value) -> Option<[f64; 2]> {
    let [x, y] = item["speaker_anchor"].as_array()?.as_slice() else {
        return None;
    };
    let point = [x.as_f64()?, y.as_f64()?];
    point
        .iter()
        .all(|value| (0.0..=1.0).contains(value))
        .then_some(point)
}

struct Placed {
    rect: Rect,
    tail: Option<Tail>,
}

fn draw_panel(
    canvas: &mut RgbImage,
    text: &[Value],
    cast: &[Value],
    fonts: &Fonts,
) -> Result<Value> {
    let mut ordered: Vec<_> = text.iter().collect();
    ordered.sort_by(|a, b| {
        priority(a)
            .total_cmp(&priority(b))
            .then_with(|| a["id"].as_str().cmp(&b["id"].as_str()))
    });
    let attribution = attributions(&ordered, cast)?;
    let mut content = Vec::new();
    for item in ordered {
        let id = item["id"].as_str().expect("preflight item ID");
        let kind = item["kind"].as_str().unwrap_or_default();
        let mode = item.get("render_mode");
        if kind == "dialogue" {
            if item.get("tail_target").is_some() {
                return Err(Error::invalid(
                    "balloon-tail-migration-required: replace tail_target with explicit voice_source and speaker_anchor",
                ));
            }
            if !matches!(item["voice_source"].as_str(), Some("human" | "device")) {
                return Err(Error::invalid(format!(
                    "text item {id} voice_source must be human or device"
                )));
            }
            if anchor(item).is_none() {
                return Err(Error::invalid(format!(
                    "text item {id} speaker_anchor must be finite normalized coordinates"
                )));
            }
        }
        if let Some(mode) = mode {
            if kind != "sfx" {
                return Err(Error::invalid(format!(
                    "text item {id} render_mode must be omitted for dialogue and caption"
                )));
            }
            if !matches!(mode.as_str(), Some(GENERATED | LETTERED)) {
                return Err(Error::invalid(format!(
                    "text item {id} render_mode must be one of {GENERATED}, {LETTERED}"
                )));
            }
        }
        let limit = match kind {
            "dialogue" => 32,
            "caption" => 45,
            "sfx" => 3,
            _ => return Err(Error::invalid(format!("text item {id} has unknown kind"))),
        };
        let display = display(kind, item["content"].as_str().unwrap_or_default());
        let words = display
            .split(python_whitespace)
            .filter(|word| !word.is_empty())
            .count();
        if words == 0 || words > limit {
            return Err(Error::invalid(format!(
                "text item {id} exceeds its content limit"
            )));
        }
        let choice = item.get("anchor").map_or(Some("top-left"), Value::as_str);
        let position = ANCHORS
            .iter()
            .position(|&anchor| Some(anchor) == choice)
            .ok_or_else(|| Error::invalid(format!("text item {id} has unknown anchor")))?;
        if kind != "sfx" || mode.and_then(Value::as_str) == Some(LETTERED) {
            content.push((item, kind, display, position));
        }
    }
    let mut geometry = Vec::new();
    let mut placed = Vec::<Placed>::new();
    let bounds = [canvas.width() as i32, canvas.height() as i32];
    for (index, (item, kind, text, anchor_index)) in content.iter().enumerate() {
        let id = item["id"].as_str().expect("preflight item ID");
        let primary = if *kind == "sfx" { BOLD } else { 0 };
        let tokens = fonts.tokens(text, primary, *kind == "dialogue");
        let mut selected = None;
        for offset in 0..ANCHORS.len() {
            let name = ANCHORS[(anchor_index + offset) % ANCHORS.len()];
            let maximum = placement(name, bounds);
            let (size, layout) = fit(fonts, &tokens, kind, maximum).ok_or_else(|| {
                Error::invalid(format!("text item {id} does not fit inside the panel"))
            })?;
            let (width, height) = shape_size(&layout, kind, size);
            let rect = fitted(maximum, name, width, height);
            if placed.iter().any(|placed| rect.overlaps(placed.rect)) {
                continue;
            }
            let tail = if *kind == "dialogue" {
                let Some(tail) = shapes::tail(
                    rect,
                    anchor(item).expect("validated anchor"),
                    bounds,
                    item["voice_source"].as_str().expect("validated voice"),
                ) else {
                    continue;
                };
                if placed
                    .iter()
                    .filter_map(|placed| placed.tail.as_ref().map(|prior| (placed.rect, prior)))
                    .any(|(other, prior)| {
                        shapes::ellipse_collision(&tail.polygon, other)
                            || shapes::ellipse_collision(&prior.polygon, rect)
                            || shapes::polygon_collision(&tail.polygon, &prior.polygon)
                    })
                {
                    continue;
                }
                Some(tail)
            } else {
                None
            };
            selected = Some((name, rect, size, tail));
            break;
        }
        let Some((name, rect, size, tail)) = selected else {
            return Err(Error::invalid(format!(
                "text item {id} has no non-overlapping placement"
            )));
        };
        let visible = Rect {
            w: rect.w.min(bounds[0] - 1 - rect.x),
            h: rect.h.min(bounds[1] - 1 - rect.y),
            ..rect
        };
        let layout = wrap(
            fonts,
            &tokens,
            size,
            wrap_width(kind, visible.w, size),
            *kind == "sfx",
        )
        .ok_or_else(|| Error::invalid(format!("text item {id} cannot be wrapped")))?;
        if let Some(tail) = &tail {
            shapes::balloon(canvas, visible, tail)?;
        }
        if *kind == "caption" {
            shapes::caption(canvas, visible);
        }
        let top = visible.y as f64
            + ((visible.h - layout.height) as f64 / 2.0).max(if *kind == "caption" {
                0.0
            } else {
                padding(kind) as f64
            });
        draw_text(
            canvas,
            fonts,
            &layout,
            size,
            visible.x as f64 + visible.w as f64 / 2.0,
            top,
            kind,
        )?;
        let runs = fonts.runs(&fonts.tokens(text, primary, *kind != "sfx"));
        geometry.push(json!({"anchor":name,"attribution":attribution.get(id),"box":rect.value(),
            "font_runs":runs,"id":id,"kind":kind,"reading_order":index+1,"tail":tail.as_ref().map(|tail| &tail.data)}));
        placed.push(Placed { rect, tail });
    }
    Ok(Value::Array(geometry))
}

fn placement(anchor: &str, [w, h]: [i32; 2]) -> Rect {
    let ix = (w as f64 * 0.04).round_ties_even() as i32;
    let iy = (h as f64 * 0.04).round_ties_even() as i32;
    let ix = ix.max(4);
    let iy = iy.max(4);
    let width = (w - 2 * ix).min(((w as f64 * 0.42).round_ties_even() as i32).max(1));
    let height = (h - 2 * iy).min(((h as f64 * 0.30).round_ties_even() as i32).max(1));
    fitted(Rect { x: 0, y: 0, w, h }, anchor, width, height).with_inset(anchor, ix, iy)
}

impl Rect {
    fn with_inset(self, anchor: &str, ix: i32, iy: i32) -> Self {
        let (vertical, horizontal) = anchor.split_once('-').expect("registered anchor");
        Self {
            x: self.x
                + match horizontal {
                    "left" => ix,
                    "right" => -ix,
                    _ => 0,
                },
            y: self.y
                + match vertical {
                    "top" => iy,
                    "bottom" => -iy,
                    _ => 0,
                },
            ..self
        }
    }
}

fn fitted(maximum: Rect, anchor: &str, width: i32, height: i32) -> Rect {
    let w = maximum.w.min(width);
    let h = maximum.h.min(height);
    let (vertical, horizontal) = anchor.split_once('-').expect("registered anchor");
    Rect {
        x: maximum.x
            + match horizontal {
                "left" => 0,
                "center" => (maximum.w - w) / 2,
                _ => maximum.w - w,
            },
        y: maximum.y
            + match vertical {
                "top" => 0,
                "middle" => (maximum.h - h) / 2,
                _ => maximum.h - h,
            },
        w,
        h,
    }
}

fn padding(kind: &str) -> i32 {
    match kind {
        "dialogue" => 19,
        "sfx" => 10,
        _ => 20,
    }
}
fn stroke(size: i32) -> i32 {
    (size as f64 * 0.11).round_ties_even().max(3.0) as i32
}

fn wrap_width(kind: &str, w: i32, size: i32) -> f64 {
    let w =
        (w - 2 * padding(kind) - if kind == "sfx" { 2 * stroke(size) } else { 0 }).max(1) as f64;
    if kind == "dialogue" {
        w / std::f64::consts::SQRT_2
    } else {
        w
    }
}

fn shape_size(layout: &Layout, kind: &str, size: i32) -> (i32, i32) {
    if kind == "dialogue" {
        (
            (layout.width * std::f64::consts::SQRT_2).ceil() as i32 + 38,
            (layout.height as f64 * std::f64::consts::SQRT_2).ceil() as i32 + 38,
        )
    } else if kind == "sfx" {
        let inset = padding(kind) + stroke(size);
        (
            layout.width.ceil() as i32 + 2 * inset,
            layout.height + 2 * inset,
        )
    } else {
        (layout.width.ceil() as i32 + 40, layout.height + 20)
    }
}

fn fit(fonts: &Fonts, tokens: &[Glyph], kind: &str, rect: Rect) -> Option<(i32, Layout)> {
    let (max, min, step) = if kind == "sfx" {
        (120, 44, 4)
    } else {
        (42, 24, 2)
    };
    (min..=max).rev().step_by(step).find_map(|size| {
        let layout = wrap(
            fonts,
            tokens,
            size,
            wrap_width(kind, rect.w, size),
            kind == "sfx",
        )?;
        let (w, h) = shape_size(&layout, kind, size);
        let fits = if kind == "caption" {
            layout.height <= rect.h - 40
        } else {
            w <= rect.w && h <= rect.h
        };
        fits.then_some((size, layout))
    })
}

struct Line {
    runs: Vec<(usize, TextRun)>,
    width: f64,
    top: i32,
    bottom: i32,
}
struct Layout {
    lines: Vec<Line>,
    width: f64,
    height: i32,
}

fn measure(fonts: &Fonts, glyphs: &[Glyph], size: i32, bold: bool) -> Option<Line> {
    let mut width = 0.0;
    let mut top = 0;
    let mut bottom = 0;
    let mut runs = Vec::new();
    if glyphs.is_empty() {
        let face = if bold { BOLD } else { 0 };
        (top, bottom) = fonts.faces[face]
            .layout("Ag".chars(), size)?
            .vertical_bounds();
    }
    for glyphs in glyphs.chunk_by(|a, b| a.font == b.font) {
        let face = glyphs[0].font;
        let run = fonts.faces[face].layout(glyphs.iter().map(|g| g.character), size)?;
        width += run.length();
        let (y0, y1) = run.vertical_bounds();
        top = top.min(y0);
        bottom = bottom.max(y1);
        runs.push((face, run));
    }
    Some(Line {
        runs,
        width,
        top,
        bottom,
    })
}

fn wrap(fonts: &Fonts, tokens: &[Glyph], size: i32, maximum: f64, bold: bool) -> Option<Layout> {
    if maximum <= 0.0 {
        return None;
    }
    let mut lines = Vec::new();
    for paragraph in tokens.split(|glyph| glyph.character == '\n') {
        let mut current = Vec::new();
        let mut cursor = 0;
        while cursor < paragraph.len() {
            let separator = cursor;
            while cursor < paragraph.len() && python_whitespace(paragraph[cursor].character) {
                cursor += 1;
            }
            let begin = cursor;
            while cursor < paragraph.len() && !python_whitespace(paragraph[cursor].character) {
                cursor += 1;
            }
            if begin == cursor {
                break;
            }
            let token = &paragraph[begin..cursor];
            let units = word_units(fonts, token, size, maximum, bold)?;
            for (index, unit) in units.into_iter().enumerate() {
                let components = if index == 0 {
                    &paragraph[separator..begin]
                } else {
                    &[]
                };
                let mut candidate = current.clone();
                candidate.extend_from_slice(components);
                candidate.extend_from_slice(unit);
                if measure(fonts, &candidate, size, bold)?.width <= maximum {
                    current = candidate;
                } else if !current.is_empty() {
                    lines.push(measure(fonts, &current, size, bold)?);
                    current = unit.to_vec();
                } else {
                    return None;
                }
            }
        }
        lines.push(measure(fonts, &current, size, bold)?);
    }
    let width = lines.iter().map(|line| line.width).fold(0.0, f64::max);
    let height = lines.iter().map(|line| line.bottom - line.top).sum::<i32>()
        + (lines.len().saturating_sub(1) as i32) * 6;
    Some(Layout {
        lines,
        width,
        height,
    })
}

fn word_units<'a>(
    fonts: &Fonts,
    word: &'a [Glyph],
    size: i32,
    maximum: f64,
    bold: bool,
) -> Option<Vec<&'a [Glyph]>> {
    if measure(fonts, word, size, bold)?.width <= maximum {
        return Some(vec![word]);
    }
    let text: String = word.iter().map(|glyph| glyph.character).collect();
    let mut start = 0;
    let mut cursor = 0;
    let mut parts = Vec::new();
    for cluster in text.graphemes(true) {
        let end = cursor + cluster.chars().count();
        if measure(fonts, &word[start..end], size, bold)?.width > maximum {
            if cursor == start || measure(fonts, &word[cursor..end], size, bold)?.width > maximum {
                return None;
            }
            parts.push(&word[start..cursor]);
            start = cursor;
        }
        cursor = end;
    }
    parts.push(&word[start..]);
    Some(parts)
}

fn draw_text(
    canvas: &mut RgbImage,
    fonts: &Fonts,
    layout: &Layout,
    size: i32,
    center: f64,
    mut y: f64,
    kind: &str,
) -> Result<()> {
    let radius = if kind == "sfx" { stroke(size) } else { 1 };
    for line in &layout.lines {
        let mut x = center - line.width / 2.0;
        let baseline = y - f64::from(line.top);
        for (face, run) in &line.runs {
            let font = &fonts.faces[*face];
            let glyph_error = || Error::typography("could not render a lettering glyph");
            let (mask, left, top) = font
                .mask(run, size, x, baseline, radius)
                .ok_or_else(glyph_error)?;
            shapes::blend_mask(
                canvas,
                &mask,
                left,
                top,
                if kind == "dialogue" { 10 } else { 15 },
            );
            if kind == "sfx" {
                let (mask, left, top) = font
                    .mask(run, size, x, baseline, 0)
                    .ok_or_else(glyph_error)?;
                shapes::blend_mask(canvas, &mask, left, top, 255);
            }
            x += run.length();
        }
        y += f64::from(line.bottom - line.top + 6);
    }
    Ok(())
}
