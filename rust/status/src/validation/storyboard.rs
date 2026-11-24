use std::collections::{BTreeSet, HashMap, HashSet};

use serde_json::Value;
use unicode_normalization::is_nfc;

use super::{
    Report, check_legacy_version, has_duplicates, is_panel, items, number_equal, qualified,
};

const PAGE_FIELDS: &[&str] = &["number", "layout", "panels"];
const PANEL_TEXT: &[&str] = &[
    "beat",
    "shot",
    "composition",
    "action",
    "expression",
    "lighting",
];
const PANEL_FIELDS: &[&str] = &[
    "id",
    "order",
    "scene_id",
    "rect",
    "beat",
    "characters",
    "shot",
    "composition",
    "action",
    "expression",
    "lighting",
    "continuity",
    "negative",
    "text",
];
const RECT_FIELDS: [&str; 4] = ["x", "y", "width", "height"];
const TEXT_FIELDS: &[&str] = &["id", "kind", "speaker", "content", "anchor", "priority"];
const VOICE_FIELDS: &[&str] = &["tail_target", "voice_source", "speaker_anchor"];
const WIDTH: i128 = 1600;
const HEIGHT: i128 = 2400;
const MARGIN: i128 = 64;
const GUTTER: i128 = 32;
type Rect = [i128; 4];

struct References<'a> {
    scenes: HashMap<&'a str, Option<&'a str>>,
    characters: HashMap<&'a str, &'a [Value]>,
}

pub(super) fn validate(data: &Value, story: &Value, characters: &Value, report: &mut Report) {
    report.object(data, "", &["schema_version", "pages"], &[]);
    check_legacy_version(data, report);
    let Some(pages) = report.array(&data["pages"], "pages") else {
        return;
    };
    report.ensure(
        (1..=4).contains(&pages.len()),
        "pages",
        "must contain 1 to 4 pages",
    );
    let references = References {
        scenes: items(&story["scenes"])
            .iter()
            .filter_map(|scene| {
                Some((
                    scene["id"].as_str()?,
                    scene.get("continuity_anchor").unwrap_or(&EMPTY).as_str(),
                ))
            })
            .collect(),
        characters: items(&characters["characters"])
            .iter()
            .filter_map(|character| {
                Some((
                    character["id"].as_str()?,
                    items(&character["visual_fingerprint"]["invariants"]),
                ))
            })
            .collect(),
    };
    let mut panel_ids = Vec::new();
    let mut text_ids = Vec::new();
    let mut total = 0;
    for (page_index, page) in pages.iter().enumerate() {
        let field = format!("pages[{page_index}]");
        if report.object(page, &field, PAGE_FIELDS, &[]).is_none() {
            continue;
        }
        let number = qualified(&field, "number");
        report.integer(&page["number"], &number, 1, 4);
        report.ensure(
            number_equal(&page["number"], page_index + 1),
            &number,
            "pages must be numbered contiguously from 1",
        );
        let layout = layout_rects(page["layout"].as_str().unwrap_or_default());
        report.ensure(
            layout.is_some(),
            &qualified(&field, "layout"),
            "unknown layout",
        );
        let panels_field = qualified(&field, "panels");
        let Some(panels) = report.array(&page["panels"], &panels_field) else {
            continue;
        };
        total += panels.len();
        report.ensure(
            (1..=4).contains(&panels.len()),
            &panels_field,
            "must contain 1 to 4 panels",
        );
        let layout = layout.unwrap_or_default();
        report.ensure(
            layout.is_empty() || panels.len() == layout.len(),
            &panels_field,
            "panel count must match the fixed layout",
        );
        let mut rectangles = Vec::new();
        for (panel_index, panel) in panels.iter().enumerate() {
            let prefix = format!("{panels_field}[{panel_index}]");
            if report.object(panel, &prefix, PANEL_FIELDS, &[]).is_none() {
                continue;
            }
            panel_identity(
                panel,
                page_index,
                panel_index,
                &prefix,
                &references,
                report,
                &mut panel_ids,
            );
            let panel_characters = panel_references(panel, &prefix, &references, report);
            if let Some(rect) = geometry(&panel["rect"], layout.get(panel_index), &prefix, report) {
                rectangles.push((prefix.clone(), rect));
            }
            board_text(
                &panel["text"],
                &prefix,
                &panel_characters,
                &references,
                report,
                &mut text_ids,
            );
        }
        if rectangles.len() <= 128 {
            for (idx, (prefix, first)) in rectangles.iter().enumerate() {
                for (other, second) in &rectangles[idx + 1..] {
                    if overlaps(first, second) {
                        report.add(qualified(prefix, "rect"), format!("overlaps {other}.rect"));
                    }
                }
            }
        }
    }
    report.ensure(
        total <= 12,
        "pages.panels",
        "project must contain at most 12 panels",
    );
    report.ensure(
        !has_duplicates(&panel_ids),
        "pages.panels",
        "panel IDs must be unique",
    );
    report.ensure(
        !has_duplicates(&text_ids),
        "pages.panels.text",
        "text IDs must be unique",
    );
}

static EMPTY: Value = Value::Null;

fn panel_identity<'a>(
    panel: &'a Value,
    page_index: usize,
    panel_index: usize,
    prefix: &str,
    references: &References,
    report: &mut Report,
    ids: &mut Vec<&'a str>,
) {
    let field = qualified(prefix, "id");
    if let Some(id) = panel["id"].as_str().filter(|id| is_panel(id)) {
        ids.push(id);
        let expected = format!("p{:02}-{:02}", page_index + 1, panel_index + 1);
        report.ensure(id == expected, &field, format!("must equal {expected}"));
    } else {
        report.add(field, "must match pNN-NN");
    }
    report.ensure(
        number_equal(&panel["order"], panel_index + 1),
        &qualified(prefix, "order"),
        "must match reading order",
    );
    report.ensure(
        panel["scene_id"]
            .as_str()
            .is_some_and(|id| references.scenes.contains_key(id)),
        &qualified(prefix, "scene_id"),
        "must reference a story scene",
    );
    for &field in PANEL_TEXT {
        report.string(&panel[field], &qualified(prefix, field));
    }
}

fn panel_references<'a>(
    panel: &'a Value,
    prefix: &str,
    references: &References,
    report: &mut Report,
) -> Vec<&'a str> {
    let field = qualified(prefix, "characters");
    let characters = report
        .strings(&panel["characters"], &field, 0, None, None)
        .unwrap_or_default();
    for (index, id) in characters.iter().enumerate() {
        report.ensure(
            references.characters.contains_key(id),
            &format!("{field}[{index}]"),
            "must reference the character bible",
        );
    }
    report.ensure(
        !has_duplicates(&characters),
        &field,
        "character IDs must be unique",
    );
    let field = qualified(prefix, "continuity");
    let continuity = report
        .strings(&panel["continuity"], &field, 0, None, None)
        .unwrap_or_default();
    report.ensure(
        !has_duplicates(&continuity),
        &field,
        "continuity anchors must be unique",
    );
    for (index, anchor) in continuity.iter().enumerate() {
        let field = format!("{field}[{index}]");
        let reference = anchor.split_once(':').filter(|(owner, fact)| {
            !fact.trim().is_empty()
                && (references.characters.contains_key(owner)
                    || references.scenes.contains_key(owner))
        });
        let Some((owner, fact)) = reference else {
            report.add(field, "must reference a known character or scene anchor");
            continue;
        };
        if references
            .characters
            .get(owner)
            .is_some_and(|invariants| !invariants.iter().any(|item| item == fact.trim()))
        {
            report.add(field, "must reuse an exact character invariant");
        } else if references
            .scenes
            .get(owner)
            .is_some_and(|expected| expected.is_some_and(|text| text.trim() != fact.trim()))
        {
            report.add(field, "must reuse the exact scene continuity anchor");
        }
    }
    report.strings(
        &panel["negative"],
        &qualified(prefix, "negative"),
        1,
        None,
        None,
    );
    characters
}

fn geometry(
    value: &Value,
    expected: Option<&Rect>,
    prefix: &str,
    report: &mut Report,
) -> Option<Rect> {
    let field = qualified(prefix, "rect");
    report.object(value, &field, &RECT_FIELDS, &[])?;
    let numbers = RECT_FIELDS.map(|key| value[key].as_i64().map(i128::from));
    let [Some(x), Some(y), Some(width), Some(height)] = numbers else {
        report.add(field, "rectangle values must be integers");
        return None;
    };
    report.ensure(
        width > 0
            && height > 0
            && x >= MARGIN
            && y >= MARGIN
            && x + width <= WIDTH - MARGIN
            && y + height <= HEIGHT - MARGIN,
        &field,
        "must stay inside the page margin with positive area",
    );
    let rect = [x, y, width, height];
    report.ensure(
        expected.is_none_or(|other| *other == rect),
        &field,
        "must equal the fixed layout rectangle",
    );
    Some(rect)
}

fn layout_rects(name: &str) -> Option<Vec<Rect>> {
    let inner_width = WIDTH - 2 * MARGIN;
    let inner_height = HEIGHT - 2 * MARGIN;
    let half_width = (inner_width - GUTTER) / 2;
    let half_height = (inner_height - GUTTER) / 2;
    let third = (inner_height - 2 * GUTTER) / 3;
    let hero = 1176;
    let support = inner_height - GUTTER - hero;
    Some(match name {
        "full-page" => vec![[MARGIN, MARGIN, inner_width, inner_height]],
        "two-horizontal" => (0..2)
            .map(|i| {
                [
                    MARGIN,
                    MARGIN + i * (half_height + GUTTER),
                    inner_width,
                    half_height,
                ]
            })
            .collect(),
        "three-horizontal" => (0..3)
            .map(|i| [MARGIN, MARGIN + i * (third + GUTTER), inner_width, third])
            .collect(),
        "hero-top-two-bottom" => vec![
            [MARGIN, MARGIN, inner_width, hero],
            [MARGIN, MARGIN + hero + GUTTER, half_width, support],
            [
                MARGIN + half_width + GUTTER,
                MARGIN + hero + GUTTER,
                half_width,
                support,
            ],
        ],
        "two-top-hero-bottom" => vec![
            [MARGIN, MARGIN, half_width, support],
            [MARGIN + half_width + GUTTER, MARGIN, half_width, support],
            [MARGIN, MARGIN + support + GUTTER, inner_width, hero],
        ],
        "four-grid" => vec![
            [64, 64, 720, 1120],
            [816, 64, 720, 1120],
            [64, 1216, 720, 1120],
            [816, 1216, 720, 1120],
        ],
        _ => return None,
    })
}

pub(crate) fn match_layout(panels: &[Value]) -> Option<&'static str> {
    let mut rectangles = Vec::with_capacity(panels.len());
    for panel in panels {
        let numbers = RECT_FIELDS.map(|key| panel["rect"][key].as_i64().map(i128::from));
        let [Some(x), Some(y), Some(w), Some(h)] = numbers else {
            return None;
        };
        let rect = [x, y, w, h];
        if x < 0
            || y < 0
            || w <= 0
            || h <= 0
            || x + w > WIDTH
            || y + h > HEIGHT
            || rectangles.iter().any(|prior| overlaps(prior, &rect))
        {
            return None;
        }
        rectangles.push(rect);
    }
    Some(
        [
            "full-page",
            "two-horizontal",
            "three-horizontal",
            "hero-top-two-bottom",
            "two-top-hero-bottom",
            "four-grid",
        ]
        .into_iter()
        .find(|name| layout_rects(name).is_some_and(|layout| layout == rectangles))
        .unwrap_or("custom"),
    )
}

fn overlaps([x, y, w, h]: &Rect, [xx, yy, ww, hh]: &Rect) -> bool {
    *x < xx + ww && *xx < x + w && *y < yy + hh && *yy < y + h
}

fn board_text<'a>(
    value: &'a Value,
    prefix: &str,
    panel_characters: &[&str],
    references: &References,
    report: &mut Report,
    ids: &mut Vec<&'a str>,
) {
    let field = qualified(prefix, "text");
    let Some(texts) = report.array(value, &field) else {
        return;
    };
    report.ensure(texts.len() <= 3, &field, "must contain 0 to 3 text items");
    let mut words = 0;
    let mut attribution = Vec::new();
    for (index, item) in texts.iter().enumerate() {
        let prefix = format!("{field}[{index}]");
        words += text_item(item, &prefix, panel_characters, references, report);
        if let Some(id) = item["id"].as_str() {
            ids.push(id);
            if item["kind"] == "dialogue"
                && let Some(speaker) = item["speaker"].as_str()
                && let Some(anchor) = normalized_point(&item["speaker_anchor"])
            {
                attribution.push((id, speaker, anchor));
            }
        }
    }
    report.ensure(words <= 45, &field, "panel text exceeds 45 total words");
    let mut conflicts = BTreeSet::new();
    if attribution.len() <= 128 {
        for (index, &(id, speaker, [x, y])) in attribution.iter().enumerate() {
            for &(other, second, [xx, yy]) in &attribution[index + 1..] {
                let distance = (x - xx).hypot(y - yy);
                let message = if speaker != second && distance < 0.04 {
                    "attribute different speakers to one speaker anchor"
                } else if speaker == second && distance > 0.25 {
                    "attribute one speaker to two distant speaker anchors"
                } else {
                    continue;
                };
                conflicts.insert((message, id.min(other), id.max(other)));
            }
        }
    }
    for (message, first, second) in conflicts {
        report.add(
            &field,
            format!("dialogue-attribution-ambiguous: {first} and {second} {message}"),
        );
    }
}

fn text_item(
    item: &Value,
    prefix: &str,
    panel: &[&str],
    references: &References,
    report: &mut Report,
) -> usize {
    if report
        .object(
            item,
            prefix,
            TEXT_FIELDS,
            &[
                "tail_target",
                "voice_source",
                "speaker_anchor",
                "render_mode",
            ],
        )
        .is_none()
    {
        return 0;
    }
    report.identifier(&item["id"], &qualified(prefix, "id"));
    let kind = item["kind"].as_str().unwrap_or_default();
    let max = match kind {
        "dialogue" => 32,
        "caption" => 45,
        "sfx" => 3,
        _ => {
            report.add(
                qualified(prefix, "kind"),
                "must be dialogue, caption, or sfx",
            );
            usize::MAX
        }
    };
    let field = qualified(prefix, "content");
    let words = report
        .string(&item["content"], &field)
        .map_or(0, |content| {
            let words = content.split_whitespace().count();
            report.ensure(
                words <= max,
                &field,
                format!("exceeds {max}-word {kind} limit"),
            );
            report.ensure(is_nfc(content), &field, "must be Unicode NFC-normalized");
            report.ensure(
                !content.chars().any(|ch| ch.is_control() && ch != '\n'),
                &field,
                "contains a disallowed control character",
            );
            words
        });
    let speaker = qualified(prefix, "speaker");
    if kind == "dialogue" {
        if let Some(id) = report.identifier(&item["speaker"], &speaker) {
            report.ensure(
                references.characters.contains_key(id) && panel.contains(&id),
                &speaker,
                "must reference a character present in the panel",
            );
        }
        if item.get("tail_target").is_some() {
            report.add(qualified(prefix, "tail_target"),
                "balloon-tail-migration-required: replace tail_target with explicit voice_source and speaker_anchor");
        } else {
            report.ensure(
                matches!(item["voice_source"].as_str(), Some("human" | "device")),
                &qualified(prefix, "voice_source"),
                "must be human or device",
            );
            report.ensure(
                normalized_point(&item["speaker_anchor"]).is_some(),
                &qualified(prefix, "speaker_anchor"),
                "must be finite normalized [x, y] coordinates",
            );
        }
    } else {
        report.ensure(
            item["speaker"].is_null(),
            &speaker,
            "must be null for caption and sfx",
        );
        for field in VOICE_FIELDS {
            report.ensure(
                item.get(field).is_none(),
                &qualified(prefix, field),
                "must be omitted for caption and sfx",
            );
        }
    }
    report.ensure(
        matches!(
            item["anchor"].as_str(),
            Some(
                "top-left"
                    | "top-center"
                    | "top-right"
                    | "middle-left"
                    | "middle-right"
                    | "bottom-left"
                    | "bottom-center"
                    | "bottom-right"
            )
        ),
        &qualified(prefix, "anchor"),
        "unknown text anchor",
    );
    if let Some(mode) = item.get("render_mode") {
        let field = qualified(prefix, "render_mode");
        if kind != "sfx" {
            report.add(field, "must be omitted for dialogue and caption");
        } else {
            report.ensure(
                matches!(
                    mode.as_str(),
                    Some("generated-visual" | "deterministic-lettering")
                ),
                &field,
                "must be one of generated-visual, deterministic-lettering",
            );
        }
    }
    report.integer(
        &item["priority"],
        &qualified(prefix, "priority"),
        1,
        1_000_000,
    );
    words
}

fn normalized_point(value: &Value) -> Option<[f64; 2]> {
    let [x, y] = items(value) else {
        return None;
    };
    let (x, y) = (x.as_f64()?, y.as_f64()?);
    ((0.0..=1.0).contains(&x) && (0.0..=1.0).contains(&y)).then_some([x, y])
}

pub(super) fn consistency(board: &Value, manifest: &Value, report: &mut Report) {
    let pages = items(&board["pages"]);
    if board["pages"].is_array() && manifest["settings"].is_object() {
        let settings = &manifest["settings"];
        let panels: usize = pages.iter().map(|page| items(&page["panels"]).len()).sum();
        report.ensure(
            number_equal(&settings["page_count"], pages.len()),
            "settings.page_count",
            "must match the storyboard page count",
        );
        report.ensure(
            number_equal(&settings["panel_count"], panels),
            "settings.panel_count",
            "must match the storyboard panel count",
        );
    }
    let mut known = HashSet::new();
    let order: Vec<_> = pages
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .filter_map(|panel| panel["id"].as_str())
        .filter(|id| known.insert(*id))
        .map(Value::from)
        .collect();
    match manifest.get("panels") {
        Some(Value::Array(panels)) => report.ensure(
            panels == &order,
            "panels",
            "must match storyboard panel order",
        ),
        None => report.ensure(
            order.is_empty(),
            "panels",
            "must match storyboard panel order",
        ),
        _ => {}
    }
}
