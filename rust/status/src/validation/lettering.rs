use std::collections::HashSet;
use std::fs::File;

use serde_json::Value;

use super::{Issue, Report, bytes_hash, contract, file_hash, items, keys, read_value, sfx, text};
use crate::manifest;

pub(super) fn inspect(root: &File, id: &str, issues: &mut Vec<Issue>) {
    let geometry_path = format!("panels/{id}/lettering.json");
    let mut report = Report {
        path: &geometry_path,
        issues,
    };
    let mut stale =
        |field: &str, detail: &str| report.add(field, format!("lettering-record-stale: {detail}"));
    let Ok(Some(geometry)) = read_value(root, &geometry_path) else {
        stale(
            "geometry.path",
            "lettering geometry is missing or unreadable",
        );
        return;
    };
    if !geometry.is_object() {
        stale("geometry.path", "lettering geometry must be an object");
        return;
    }
    if geometry["panel_id"] != id {
        stale("panel_id", "geometry panel ID does not match its path");
    }
    if geometry["schema_version"] != "1.2" {
        stale(
            "schema_version",
            "geometry predates speaker attribution or SFX provenance and must be lettered again",
        );
    }
    let mut semantic = geometry.clone();
    semantic
        .as_object_mut()
        .expect("geometry object")
        .remove("geometry_sha256");
    let mut bytes = manifest::compact(&semantic);
    bytes.push(b'\n');
    if geometry["geometry_sha256"] != bytes_hash(&bytes) {
        stale("geometry_sha256", "canonical geometry hash does not match");
    }
    let board = read_value(root, "plan/storyboard.json").ok().flatten();
    let authored = board.as_ref().and_then(|board| {
        items(&board["pages"])
            .iter()
            .flat_map(|page| items(&page["panels"]))
            .find(|panel| panel["id"] == id)
    });
    if let Some(panel) = authored {
        if geometry["sfx"] != sfx::provenance(panel, &geometry["items"]) {
            stale(
                "sfx",
                "recorded SFX provenance does not match the authored storyboard; letter the panel again",
            );
        }
    } else {
        stale(
            "sfx",
            "storyboard panel is unavailable to verify SFX provenance",
        );
    }
    let typography = match read_value(root, &format!("panels/{id}/typography.json")) {
        Ok(Some(value)) => Some(value),
        _ => {
            stale(
                "typography.path",
                "typography preflight is missing or unreadable",
            );
            None
        }
    };
    if let Some(typography) = typography.as_ref().filter(|value| value.is_object()) {
        inspect_typography(typography, &mut stale);
    }
    let bindings = &geometry["bindings"];
    if !bindings.is_object() {
        stale("bindings", "geometry bindings are missing");
    }
    for kind in ["clean", "storyboard"] {
        let path = format!("{kind}_path");
        let hash = format!("{kind}_sha256");
        match observed_hash(root, &bindings[&path]) {
            None => stale(&format!("bindings.{path}"), "bound artifact is missing"),
            Some(value) if bindings[&hash] != value => stale(
                &format!("bindings.{hash}"),
                "bound artifact hash does not match",
            ),
            _ => {}
        }
    }
    if let Some(typography) = typography.as_ref().filter(|value| value.is_object()) {
        if bindings["typography_sha256"] != bytes_hash(&manifest::canonical(typography)) {
            stale("typography.path", "typography record hash does not match");
        }
        if bindings["font_policy_sha256"] != typography["font_policy_sha256"] {
            stale(
                "bindings.font_policy_sha256",
                "font policy hash does not match preflight",
            );
        }
    }
    let lettered = &geometry["lettered"];
    if !lettered.is_object() {
        stale("lettered", "lettered artifact descriptor is missing");
    } else {
        match observed_hash(root, &lettered["path"]) {
            None => stale("lettered.path", "lettered image is missing"),
            Some(value) if lettered["sha256"] != value => {
                stale("lettered.sha256", "lettered image hash does not match")
            }
            _ => {}
        }
    }
    placements(&geometry["items"], &mut stale);
}

fn observed_hash(root: &File, path: &Value) -> Option<String> {
    file_hash(root, path.as_str()?).ok().flatten()
}

fn inspect_typography(typography: &Value, stale: &mut impl FnMut(&str, &str)) {
    if typography["status"] != "pass" || !typography["issues"].as_array().is_some_and(Vec::is_empty)
    {
        stale(
            "typography.status",
            "typography preflight is not a clean pass",
        );
    }
    if typography["schema_version"] != "1.1" {
        stale(
            "typography.schema_version",
            "typography preflight predates the script coverage policy and must be lettered again",
        );
    }
    let checks = &typography["checks"];
    let mut ids: Vec<_> = items(checks)
        .iter()
        .filter_map(|check| check["id"].as_str())
        .collect();
    if !checks.is_array() || ids.len() != items(checks).len() {
        stale(
            "typography.checks",
            "typography preflight checks are missing",
        );
    } else {
        ids.sort_unstable();
        if ids != ["typography-glyph-coverage", "typography-shaping-policy"] {
            stale(
                "typography.checks",
                "typography preflight check set does not match the current policy",
            );
        } else if items(checks).iter().any(|check| {
            check["result"] != "pass"
                || check["severity"] != "error"
                || check["reviewer"] != "comic-sol"
                || check["method"] != "font-cmap-policy-v1"
                || text(&check["evidence"]).is_none()
        }) {
            stale(
                "typography.checks",
                "typography preflight check is incomplete or did not pass",
            );
        }
    }
    if let Some(glyphs) = typography["glyphs"].as_array() {
        if let Some((field, message)) = glyphs.iter().find_map(glyph_issue) {
            stale(field, message);
        }
    } else {
        stale("glyphs", "typography glyph list is missing");
    }
}

fn glyph_issue(value: &Value) -> Option<(&'static str, &'static str)> {
    if !value.is_object() {
        return Some(("glyphs", "typography glyph entry is invalid"));
    }
    if !value["font_id"]
        .as_str()
        .is_some_and(|font| !font.is_empty() && font != ".notdef" && !font.contains(['/', '\\']))
    {
        return Some((
            "glyphs.font_id",
            "glyph resolves to .notdef or a private path",
        ));
    }
    if value["coverage"] != "supported" || value["shaping"] != "supported" {
        return Some(("glyphs", "glyph coverage or shaping is unsupported"));
    }
    let code = value["codepoint"]
        .as_str()
        .and_then(|code| code.strip_prefix("U+"))
        .filter(|hex| {
            (4..=6).contains(&hex.len())
                && hex
                    .bytes()
                    .all(|byte| matches!(byte,b'0'..=b'9'|b'A'..=b'F'))
        })
        .and_then(|hex| u32::from_str_radix(hex, 16).ok());
    let Some(code) = code else {
        return Some((
            "glyphs.codepoint",
            "glyph codepoint is missing or malformed",
        ));
    };
    if !value["character"].as_str().is_some_and(|text| {
        let mut chars = text.chars();
        chars.next().map(u32::from) == Some(code) && chars.next().is_none()
    }) {
        return Some((
            "glyphs.character",
            "glyph character does not match its recorded codepoint",
        ));
    }
    let block = contract::UNICODE_BLOCKS
        .iter()
        .find(|(start, end, ..)| (*start..=*end).contains(&code));
    let (script, linear) = block.map_or(("unassigned", false), |(_, _, script, linear, ..)| {
        (*script, *linear)
    });
    if value["script"] != script {
        return Some((
            "glyphs.script",
            "glyph script does not match its recorded codepoint",
        ));
    }
    if !linear || code > 0xFFFF {
        return Some((
            "glyphs.shaping",
            "glyph codepoint is not letterable under the current policy",
        ));
    }
    None
}

fn placements(items: &Value, stale: &mut impl FnMut(&str, &str)) {
    let Some(items) = items.as_array() else {
        stale("items", "geometry items must be an array");
        return;
    };
    let mut seen = HashSet::new();
    let mut duplicate = false;
    for entry in items {
        if !entry.is_object() {
            stale("items", "geometry item must be an object");
            continue;
        }
        match entry["reading_order"].as_i64().filter(|order| *order > 0) {
            Some(order) => duplicate |= !seen.insert(order),
            None => stale(
                "items.reading_order",
                "reading order must be a positive integer",
            ),
        }
        let bounds = &entry["box"];
        if !["x", "y", "width", "height"]
            .iter()
            .all(|&key| bounds[key].as_i64().is_some())
            || bounds["width"].as_i64().unwrap_or_default() <= 0
            || bounds["height"].as_i64().unwrap_or_default() <= 0
        {
            stale("items.box", "item box is missing or non-positive");
        }
        let attribution = &entry["attribution"];
        let tail = &entry["tail"];
        if entry["kind"] == "dialogue" {
            if !keys(
                attribution,
                &[
                    "authored_speaker",
                    "resolution",
                    "speaker",
                    "speaker_anchor",
                ],
            ) {
                stale(
                    "items.attribution",
                    "dialogue attribution record is missing or malformed",
                );
            } else {
                if !["authored_speaker", "speaker"].iter().all(|&key| {
                    attribution[key]
                        .as_str()
                        .is_some_and(|text| !text.is_empty())
                }) {
                    stale(
                        "items.attribution.speaker",
                        "attribution must name a stable speaker identity",
                    );
                }
                if !matches!(
                    attribution["resolution"].as_str(),
                    Some("declared" | "inferred")
                ) {
                    stale(
                        "items.attribution.resolution",
                        "attribution resolution must be declared or inferred",
                    );
                }
                if normalized(&attribution["speaker_anchor"]).is_none() {
                    stale(
                        "items.attribution.speaker_anchor",
                        "attribution must bind a normalized speaker anchor",
                    );
                }
                if tail.is_object() && tail["speaker_anchor"] != attribution["speaker_anchor"] {
                    stale(
                        "items.attribution.speaker_anchor",
                        "attribution anchor does not match the tail that was drawn",
                    );
                }
            }
        } else if !attribution.is_null() {
            stale(
                "items.attribution",
                "only dialogue carries speaker attribution",
            );
        }
        if !tail.is_null() && !valid_tail(tail) {
            stale(
                "items.tail",
                "tail must be a finite organic-cubic-v1 semantic geometry record",
            );
        }
    }
    if duplicate {
        stale("items.reading_order", "reading order values must be unique");
    }
}

pub(super) fn point(value: &Value) -> Option<[f64; 2]> {
    let [x, y] = items(value) else {
        return None;
    };
    let (x, y) = (x.as_f64()?, y.as_f64()?);
    (x.is_finite() && y.is_finite()).then_some([x, y])
}

pub(super) fn normalized(value: &Value) -> Option<[f64; 2]> {
    point(value).filter(|coords| coords.iter().all(|coord| (0.0..=1.0).contains(coord)))
}

fn point_pair(value: &Value) -> bool {
    matches!(items(value), [a,b] if point(a).is_some() && point(b).is_some())
}

fn valid_tail(tail: &Value) -> bool {
    keys(
        tail,
        &[
            "attachment",
            "base",
            "control",
            "length",
            "policy_version",
            "source_gap",
            "speaker_anchor",
            "tip",
            "voice_source",
            "width",
        ],
    ) && tail["policy_version"] == "organic-cubic-v1"
        && matches!(tail["voice_source"].as_str(), Some("human" | "device"))
        && normalized(&tail["speaker_anchor"]).is_some()
        && point(&tail["attachment"]).is_some()
        && point(&tail["tip"]).is_some()
        && point_pair(&tail["base"])
        && matches!(items(&tail["control"]), [a,b] if point_pair(a) && point_pair(b))
        && ["length", "source_gap", "width"].iter().all(|&field| {
            tail[field]
                .as_f64()
                .is_some_and(|n| n.is_finite() && n > 0.0)
        })
}
