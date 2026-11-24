use serde_json::Value;

use super::{
    DECIMAL, Report, check_legacy_version, contract, has_duplicates, is_identifier, is_panel,
    items, number_equal, qualified, repr,
};
use crate::manifest;

pub(super) const LINEAR: &[&str] = &[
    "INIT",
    "PLANNED",
    "SCRIPTED",
    "STORYBOARDED",
    "REFERENCES_READY",
    "PANELS_READY",
    "QA_READY",
    "LETTERED",
    "COMPOSED",
    "EXPORTED",
    "COMPLETE",
];
const STAGES: &[&str] = &[
    "planning",
    "storyboard",
    "generation",
    "lettering",
    "composition",
    "export",
];
const ARTIFACTS: &[&str] = &[
    "story_plan",
    "character_bible",
    "storyboard",
    "composition_cache",
    "qa_report",
    "pdf",
    "pdf_verification",
];

pub(super) fn project(data: &Value, report: &mut Report) {
    let mut fields = vec![
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "status",
        "input",
        "settings",
        "capability",
        "artifacts",
        "stage_versions",
        "panels",
        "warnings",
    ];
    let legacy = Value::from(contract::LEGACY_PROJECT_SCHEMA_VERSION);
    let version = data.get("schema_version").unwrap_or(&legacy);
    let current_shape = version != &legacy;
    if current_shape {
        fields.push("handoff");
    }
    report.object(
        data,
        "",
        &fields,
        &["schema_version", "blocked_from", "blocked_reason"],
    );
    report.ensure(
        manifest::supported_version(version),
        "schema_version",
        format!(
            "unsupported project schema version {}; reader supports {} through {}",
            repr(version),
            contract::LEGACY_PROJECT_SCHEMA_VERSION,
            contract::CURRENT_PROJECT_SCHEMA_VERSION,
        ),
    );
    if current_shape {
        handoff(&data["handoff"], report);
    }
    report.identifier(&data["project_id"], "project_id");
    report.narrative(&data["title"], "title", 200);
    for field in ["created_at", "updated_at"] {
        timestamp(&data[field], field, report);
    }
    let status = data["status"].as_str().unwrap_or_default();
    report.ensure(
        LINEAR.contains(&status) || matches!(status, "BLOCKED" | "COMPLETE_WITH_WARNINGS"),
        "status",
        "unknown manifest status",
    );
    if status == "BLOCKED" {
        report.ensure(
            data["blocked_from"]
                .as_str()
                .is_some_and(|status| LINEAR.contains(&status)),
            "blocked_from",
            "must be a normal pipeline status",
        );
        report.ensure(
            data["blocked_reason"]
                .as_str()
                .is_some_and(|text| is_identifier(text, 64, true)),
            "blocked_reason",
            "must be a stable category",
        );
    } else {
        report.ensure(
            data["blocked_from"].is_null() && data["blocked_reason"].is_null(),
            "status",
            "blocked fields must be null when not BLOCKED",
        );
    }
    input(&data["input"], report);
    settings(&data["settings"], report);
    capability(&data["capability"], report);
    if let Some(artifacts) = report.object(&data["artifacts"], "artifacts", &[], ARTIFACTS) {
        for (name, value) in artifacts {
            let field = format!("artifacts.{name}");
            if report
                .object(value, &field, &["path", "sha256"], &[])
                .is_some()
            {
                report.relative(&value["path"], &qualified(&field, "path"), false);
                report.sha(&value["sha256"], &qualified(&field, "sha256"), false);
            }
        }
    }
    if let Some(versions) = report.object(&data["stage_versions"], "stage_versions", STAGES, &[]) {
        for (name, version) in versions {
            report.ensure(
                version.as_str().is_some_and(|text| DECIMAL.is_match(text)),
                &format!("stage_versions.{name}"),
                "must be a decimal version string",
            );
        }
    }
    if let Some(panels) = report.strings(&data["panels"], "panels", 0, Some(12), None) {
        for (index, id) in panels.iter().enumerate() {
            report.ensure(
                is_panel(id),
                &format!("panels[{index}]"),
                "must match pNN-NN",
            );
        }
        report.ensure(
            !has_duplicates(&panels),
            "panels",
            "panel IDs must be unique",
        );
    }
    if let Some(warnings) = report.strings(&data["warnings"], "warnings", 0, None, Some(500)) {
        if status == "COMPLETE" && !warnings.is_empty() {
            report.add(
                "status",
                "must be COMPLETE_WITH_WARNINGS while warnings remain",
            );
        }
        if status == "COMPLETE_WITH_WARNINGS" && warnings.is_empty() {
            report.add(
                "warnings",
                "COMPLETE_WITH_WARNINGS requires an unresolved warning",
            );
        }
    }
}

fn handoff(value: &Value, report: &mut Report) {
    if report
        .object(
            value,
            "handoff",
            &["contract_version", "locked_scope_sha256", "manifest_path"],
            &[],
        )
        .is_none()
    {
        return;
    }
    report.ensure(
        value["contract_version"] == "1.0",
        "handoff.contract_version",
        "must equal 1.0",
    );
    report.sha(
        &value["locked_scope_sha256"],
        "handoff.locked_scope_sha256",
        true,
    );
    report.relative(&value["manifest_path"], "handoff.manifest_path", true);
    report.ensure(
        value["locked_scope_sha256"].is_null() == value["manifest_path"].is_null(),
        "handoff",
        "locked_scope_sha256 and manifest_path must both be null or both populated",
    );
    report.ensure(
        value["manifest_path"].is_null() || value["manifest_path"] == "handoff/manifest.json",
        "handoff.manifest_path",
        "must equal handoff/manifest.json when populated",
    );
}

fn input(value: &Value, report: &mut Report) {
    if report
        .object(
            value,
            "input",
            &[
                "mode",
                "source_path",
                "source_sha256",
                "request_path",
                "language",
            ],
            &[],
        )
        .is_none()
    {
        return;
    }
    report.ensure(
        matches!(
            value["mode"].as_str(),
            Some("short_prompt" | "pasted_story" | "source_file" | "resume")
        ),
        "input.mode",
        "unknown input mode",
    );
    for (field, path) in [
        ("source_path", "source/input.txt"),
        ("request_path", "source/request.json"),
    ] {
        let name = format!("input.{field}");
        report.relative(&value[field], &name, false);
        report.ensure(value[field] == path, &name, format!("must equal {path}"));
    }
    report.sha(&value["source_sha256"], "input.source_sha256", false);
    report.string(&value["language"], "input.language");
}

fn settings(value: &Value, report: &mut Report) {
    if report
        .object(
            value,
            "settings",
            &[
                "page_width",
                "page_height",
                "reading_direction",
                "page_count",
                "panel_count",
                "style_anchor",
                "max_panel_retries",
            ],
            &[],
        )
        .is_none()
    {
        return;
    }
    for (field, n) in [
        ("page_width", 1600),
        ("page_height", 2400),
        ("max_panel_retries", 2),
    ] {
        report.ensure(
            number_equal(&value[field], n),
            &format!("settings.{field}"),
            format!("must equal {n}"),
        );
    }
    report.ensure(
        value["reading_direction"] == "ltr",
        "settings.reading_direction",
        "must equal ltr",
    );
    report.integer(&value["page_count"], "settings.page_count", 1, 4);
    report.integer(&value["panel_count"], "settings.panel_count", 0, 12);
    report.string(&value["style_anchor"], "settings.style_anchor");
}

fn capability(value: &Value, report: &mut Report) {
    if report
        .object(
            value,
            "capability",
            &[
                "status",
                "name",
                "supports_reference_images",
                "supports_dimensions",
                "detected_at",
            ],
            &[],
        )
        .is_none()
    {
        return;
    }
    let status = value["status"].as_str();
    report.ensure(
        matches!(status, Some("not_checked" | "available" | "unavailable")),
        "capability.status",
        "unknown capability status",
    );
    if !value["name"].is_null() {
        report.string(&value["name"], "capability.name");
    } else if status == Some("available") {
        report.add(
            "capability.name",
            "is required when capability is available",
        );
    }
    for field in ["supports_reference_images", "supports_dimensions"] {
        report.ensure(
            value[field].is_boolean(),
            &format!("capability.{field}"),
            "must be boolean",
        );
    }
    if !value["detected_at"].is_null() {
        timestamp(&value["detected_at"], "capability.detected_at", report);
    } else if matches!(status, Some("available" | "unavailable")) {
        report.add(
            "capability.detected_at",
            "is required after capability detection",
        );
    }
}

pub(super) fn timestamp(value: &Value, field: &str, report: &mut Report) {
    report.ensure(
        valid_timestamp(value),
        field,
        "must be an ISO 8601 UTC timestamp",
    );
}

pub(super) fn valid_timestamp(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        let [
            a,
            b,
            c,
            d,
            b'-',
            e,
            f,
            b'-',
            g,
            h,
            b'T',
            i,
            j,
            b':',
            k,
            l,
            b':',
            m,
            n,
            b'Z',
        ] = text.as_bytes()
        else {
            return false;
        };
        let digits = [a, b, c, d, e, f, g, h, i, j, k, l, m, n];
        if !digits.into_iter().all(u8::is_ascii_digit) {
            return false;
        }
        let number = |a: &u8, b: &u8| u32::from(a - b'0') * 10 + u32::from(b - b'0');
        let year = number(a, b) * 100 + number(c, d);
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
        let days = match number(e, f) {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 => {
                if leap {
                    29
                } else {
                    28
                }
            }
            _ => 0,
        };
        year > 0
            && (1..=days).contains(&number(g, h))
            && number(i, j) < 24
            && number(k, l) < 60
            && number(m, n) < 60
    })
}

pub(super) fn story(data: &Value, report: &mut Report) {
    let text = [
        "title",
        "logline",
        "theme",
        "setting",
        "beginning",
        "turn",
        "climax",
        "ending",
    ];
    let required: Vec<_> = text
        .into_iter()
        .chain(["schema_version", "tone", "rating", "scenes"])
        .collect();
    report.object(data, "", &required, &[]);
    check_legacy_version(data, report);
    for field in text {
        report.string(&data[field], field);
    }
    report.strings(&data["tone"], "tone", 1, None, None);
    report.ensure(data["rating"] == "teen", "rating", "must equal teen");
    let Some(scenes) = report.array(&data["scenes"], "scenes") else {
        return;
    };
    report.ensure(
        (2..=5).contains(&scenes.len()),
        "scenes",
        "must contain 2 to 5 scenes",
    );
    let mut ids = Vec::new();
    let text = ["purpose", "location", "time", "continuity_anchor"];
    let required: Vec<_> = text.into_iter().chain(["id", "characters"]).collect();
    for (index, scene) in scenes.iter().enumerate() {
        let prefix = format!("scenes[{index}]");
        if report.object(scene, &prefix, &required, &[]).is_none() {
            continue;
        }
        ids.extend(report.identifier(&scene["id"], &qualified(&prefix, "id")));
        for name in text {
            report.string(&scene[name], &qualified(&prefix, name));
        }
        let field = qualified(&prefix, "characters");
        if let Some(ids) = report.strings(&scene["characters"], &field, 0, None, None) {
            for (index, id) in ids.iter().enumerate() {
                report.ensure(
                    is_identifier(id, 48, false),
                    &format!("{field}[{index}]"),
                    "must match ^[a-z][a-z0-9-]{0,47}$",
                );
            }
            report.ensure(
                !has_duplicates(&ids),
                &field,
                "character IDs must be unique",
            );
        }
    }
    report.ensure(!has_duplicates(&ids), "scenes", "scene IDs must be unique");
}

pub(super) fn characters(data: &Value, report: &mut Report) {
    report.object(data, "", &["schema_version", "characters"], &[]);
    check_legacy_version(data, report);
    let Some(characters) = report.array(&data["characters"], "characters") else {
        return;
    };
    let text = [
        "name",
        "role",
        "age_band",
        "pronouns",
        "motivation",
        "speech",
    ];
    let required: Vec<_> = text
        .into_iter()
        .chain(["id", "personality", "reference_path", "visual_fingerprint"])
        .collect();
    let mut ids = Vec::new();
    for (index, character) in characters.iter().enumerate() {
        let prefix = format!("characters[{index}]");
        if report.object(character, &prefix, &required, &[]).is_none() {
            continue;
        }
        ids.extend(report.identifier(&character["id"], &qualified(&prefix, "id")));
        for name in text {
            report.string(&character[name], &qualified(&prefix, name));
        }
        report.strings(
            &character["personality"],
            &qualified(&prefix, "personality"),
            1,
            None,
            None,
        );
        let reference = qualified(&prefix, "reference_path");
        report.relative(&character["reference_path"], &reference, false);
        if let Some(id) = character["id"].as_str() {
            let path = format!("references/characters/{id}.png");
            report.ensure(
                character["reference_path"].as_str() == Some(&path),
                &reference,
                "must match the character ID",
            );
        }
        let fingerprint = &character["visual_fingerprint"];
        let prefix = qualified(&prefix, "visual_fingerprint");
        let text = ["silhouette", "face", "hair", "wardrobe"];
        let required: Vec<_> = text
            .into_iter()
            .chain(["palette", "signature_props", "invariants", "avoid"])
            .collect();
        if report
            .object(fingerprint, &prefix, &required, &[])
            .is_some()
        {
            for field in text {
                report.string(&fingerprint[field], &qualified(&prefix, field));
            }
            for field in ["palette", "signature_props", "avoid"] {
                report.strings(
                    &fingerprint[field],
                    &qualified(&prefix, field),
                    1,
                    None,
                    None,
                );
            }
            report.strings(
                &fingerprint["invariants"],
                &qualified(&prefix, "invariants"),
                2,
                Some(5),
                None,
            );
        }
    }
    report.ensure(
        !has_duplicates(&ids),
        "characters",
        "character IDs must be unique",
    );
}

pub(super) fn reference_characters(story: &Value, characters: &Value, report: &mut Report) {
    let known: Vec<_> = items(&characters["characters"])
        .iter()
        .filter(|item| item.is_object())
        .map(|c| &c["id"])
        .collect();
    for (index, scene) in items(&story["scenes"]).iter().enumerate() {
        for (group, id) in items(&scene["characters"]).iter().enumerate() {
            report.ensure(
                known.contains(&id),
                &format!("scenes[{index}].characters[{group}]"),
                "must reference the character bible",
            );
        }
    }
}
