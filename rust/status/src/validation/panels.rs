use std::collections::{BTreeSet, HashMap};
use std::fs::File;
use std::io::{Cursor, Read};
use std::path::Path;

use image::{ImageDecoder, ImageError, ImageFormat, ImageReader, Limits};
use serde_json::Value;
use unicode_normalization::UnicodeNormalization;

use super::planning::timestamp;
use super::terminal::{check_binding, sha256};
use super::{
    Issue, PROJECT, Report, Stage, artifact, is_identifier, is_panel, is_sha256, items, load,
    number_equal, qualified, repr,
};
use crate::error::Result;
use crate::{manifest, project_fs};

pub(crate) const IDS: &[&str] = &[
    "character-identity",
    "anatomy",
    "action",
    "composition",
    "continuity",
    "text-free",
    "technical",
];
const BINDINGS: &[&str] = &[
    "raw_path",
    "raw_sha256",
    "raw_width",
    "raw_height",
    "clean_path",
    "clean_sha256",
    "clean_width",
    "clean_height",
    "normalization_path",
    "normalization_sha256",
];
const MAX_PIXELS: u64 = 1600 * 2400 * 16;
const MAX_ENCODED: u64 = 128 * 1024 * 1024;
const MIN_DIMENSION: u32 = 512;

pub(super) fn inspect(
    root: &File,
    board: &Value,
    project: Option<&Value>,
    stage: Stage,
    issues: &mut Vec<Issue>,
) -> Result<()> {
    let repair = super::repair::PATH;
    if artifact(root, repair)?.is_some()
        && let Some(plan) = load(root, repair, issues)
    {
        for category in super::repair::inspect(root, board, &plan) {
            Report {
                path: repair,
                issues,
            }
            .add(category, category);
        }
    }
    let mut ordered = Vec::new();
    let mut panels = HashMap::new();
    for panel in items(&board["pages"])
        .iter()
        .flat_map(|page| items(&page["panels"]))
    {
        if let Some(id) = panel["id"].as_str() {
            if panels.insert(id, panel).is_none() {
                ordered.push(id);
            }
        }
    }
    let mut errors = false;
    let mut warnings = false;
    for id in ordered {
        let path = format!("qa/panels/{id}.json");
        let Some(record) = load(root, &path, issues) else {
            continue;
        };
        let v2 = record["schema_version"] == "2.0";
        let subject_field = if v2 { "subject_id" } else { "panel_id" };
        let name = record[subject_field]
            .as_str()
            .map_or_else(|| repr(&record[subject_field]), str::to_owned);
        let schema_path = format!("qa/panels/{name}.json");
        let mut schema = Report {
            path: &schema_path,
            issues,
        };
        if v2 {
            modern(&record, &mut schema);
        } else {
            legacy(&record, &mut schema);
        }
        let mut report = Report {
            path: &path,
            issues,
        };
        if v2 {
            let provenance_name = record["subject_id"].as_str().unwrap_or("unknown");
            let provenance_path = format!("qa/panels/{provenance_name}.json");
            provenance(
                root,
                &record,
                &mut Report {
                    path: &provenance_path,
                    issues: report.issues,
                },
            )?;
            for detail in super::character::provenance(root, &record) {
                report.add(
                    "character-consistency-provenance",
                    format!("quality-record-stale: {detail}"),
                );
            }
        } else {
            report.add(
                "quality-migration-required",
                "schema 1.0 quality record remains readable but must be reviewed as schema 2.0",
            );
        }
        report.ensure(
            record[subject_field] == id,
            subject_field,
            "must match its storyboard panel",
        );
        raster_bindings(root, &record, panels[id], v2, &mut report)?;
        if stage.terminal()
            && let Some(manifest) = project
        {
            let (error, warning) = outcomes(&record, manifest, &mut report);
            errors |= error;
            warnings |= warning;
        }
    }
    if let Some(project) = project {
        let input = &project["input"];
        let mut report = Report {
            path: PROJECT,
            issues,
        };
        check_binding(
            root,
            &input["source_path"],
            &input["source_sha256"],
            "input.source_path",
            "input.source_sha256",
            &mut report,
            "source",
        )?;
        let status = project["status"].as_str();
        if errors && matches!(status, Some("COMPLETE" | "COMPLETE_WITH_WARNINGS")) {
            report.add(
                "status",
                "cannot be terminal success while unresolved panel errors remain",
            );
        }
        if warnings && status == Some("COMPLETE") {
            report.add(
                "status",
                "must be COMPLETE_WITH_WARNINGS while panel warnings remain",
            );
        }
    }
    Ok(())
}

pub(crate) fn reusable(root: &File, record: &Value, id: &str) -> Result<bool> {
    let v2 = record["schema_version"] == "2.0";
    let path = format!("qa/panels/{id}.json");
    let mut issues = Vec::new();
    let mut report = Report {
        path: &path,
        issues: &mut issues,
    };
    if v2 {
        modern(record, &mut report);
    } else {
        legacy(record, &mut report);
    }
    if !report.issues.is_empty() {
        return Ok(false);
    }
    if v2 {
        provenance(root, record, &mut report)?;
        if !report.issues.is_empty() || !super::character::provenance(root, record).is_empty() {
            return Ok(false);
        }
    } else if matches!(
        record["failure_category"].as_str(),
        Some("corrupt" | "corrupt_image" | "safety" | "safety_refusal")
    ) {
        return Ok(false);
    }
    let data = if v2 { &record["bindings"] } else { record };
    let clean = if v2 {
        format!("panels/{id}/clean.png")
    } else {
        format!("panels/clean/{id}.png")
    };
    if data["raw_path"] != format!("panels/raw/{id}.png")
        || data["clean_path"] != clean
        || (v2 && data["normalization_path"] != format!("panels/{id}/normalization.json"))
        || (!v2 && record["source_prompt_path"] != format!("prompts/panels/{id}.txt"))
    {
        return Ok(false);
    }
    let mut sizes = Vec::new();
    for keys in [
        ["raw_path", "raw_sha256", "raw_width", "raw_height"],
        ["clean_path", "clean_sha256", "clean_width", "clean_height"],
    ] {
        let path = data[keys[0]].as_str().expect("canonical panel path");
        let Ok(Some((dir, name))) = artifact(root, path) else {
            return Ok(false);
        };
        let Ok(file) = project_fs::open_file(&dir, &name, false, false) else {
            return Ok(false);
        };
        if (v2 || keys[0] == "raw_path")
            && !data[keys[1]].is_null()
            && data[keys[1]] != sha256(file.try_clone()?)?
        {
            return Ok(false);
        }
        // A cloned handle shares its cursor. Open a new bounded decoder input.
        let Ok(info) = project_fs::open_file(&dir, &name, false, false)
            .map_err(|_| "OSError")
            .and_then(decode)
        else {
            return Ok(false);
        };
        if info.width < MIN_DIMENSION || info.height < MIN_DIMENSION {
            return Ok(false);
        }
        let dimensions = &record["dimensions"];
        if (v2
            && (!number_equal(&data[keys[2]], info.width as usize)
                || !number_equal(&data[keys[3]], info.height as usize)))
            || (!v2
                && dimensions.is_object()
                && (!number_equal(&dimensions["width"], info.width as usize)
                    || !number_equal(&dimensions["height"], info.height as usize)))
        {
            return Ok(false);
        }
        sizes.push((info.width, info.height));
    }
    Ok(v2 || sizes[0] == sizes[1])
}

pub(crate) fn record_issue(record: &Value) -> Option<String> {
    let mut issues = Vec::new();
    let mut report = Report {
        path: "",
        issues: &mut issues,
    };
    if record["schema_version"] == "2.0" {
        modern(record, &mut report);
    } else {
        legacy(record, &mut report);
    }
    issues.sort_unstable();
    issues
        .first()
        .map(|issue| format!("{}: {}", issue.field, issue.message))
}

fn legacy(record: &Value, r: &mut Report) {
    r.object(
        record,
        "",
        &[
            "schema_version",
            "panel_id",
            "source_prompt_path",
            "raw_path",
            "clean_path",
            "raw_sha256",
            "dimensions",
            "attempts",
            "generation",
            "checks",
            "decision",
            "retry_reason",
            "unresolved_warnings",
        ],
        &["failure_category", "override_reason"],
    );
    r.ensure(
        record["schema_version"] == "1.0",
        "schema_version",
        "must equal 1.0",
    );
    r.ensure(
        record["panel_id"].as_str().is_some_and(is_panel),
        "panel_id",
        "must match pNN-NN",
    );
    let paths = ["source_prompt_path", "raw_path", "clean_path"];
    for field in paths {
        r.relative(&record[field], field, true);
    }
    r.sha(&record["raw_sha256"], "raw_sha256", true);
    let attempts = &record["attempts"];
    r.integer(attempts, "attempts", 0, 3);
    let generated = attempts.as_i64().is_some_and(|n| n > 0);
    let dimensions = &record["dimensions"];
    if r.object(dimensions, "dimensions", &["width", "height"], &[])
        .is_some()
    {
        for axis in ["width", "height"] {
            let field = format!("dimensions.{axis}");
            r.integer(&dimensions[axis], &field, 0, 100_000);
            if generated {
                r.ensure(
                    dimensions[axis].as_i64().is_some_and(|n| n > 0) || dimensions[axis] == true,
                    &field,
                    "must be positive after generation",
                );
            }
        }
    }
    let generation = &record["generation"];
    if r.object(
        generation,
        "generation",
        &["capability_name", "reference_paths", "completed_at"],
        &[],
    )
    .is_some()
    {
        for field in ["capability_name", "completed_at"] {
            let prefix = format!("generation.{field}");
            let value = &generation[field];
            if value.is_null() && generated {
                r.add(prefix, "is required after generation");
            } else if !value.is_null() {
                if field == "completed_at" {
                    timestamp(value, &prefix, r);
                } else {
                    r.string(value, &prefix);
                }
            }
        }
        if let Some(references) =
            r.array(&generation["reference_paths"], "generation.reference_paths")
        {
            for (index, path) in references.iter().enumerate() {
                r.relative(path, &format!("generation.reference_paths[{index}]"), false);
            }
        }
    }
    if generated {
        for field in paths.into_iter().chain(["raw_sha256"]) {
            r.ensure(
                !record[field].is_null(),
                field,
                "is required after generation",
            );
        }
    }
    if let Some(checks) = r.array(&record["checks"], "checks") {
        r.ensure(
            ordered(checks),
            "checks",
            "must contain the seven required checks in normative order",
        );
        for (index, check) in checks.iter().enumerate() {
            let prefix = format!("checks[{index}]");
            if r.object(
                check,
                &prefix,
                &["id", "result", "severity", "evidence"],
                &[],
            )
            .is_some()
            {
                verify_check(check, &prefix, false, r);
            }
        }
    }
    decisions(record, false, r);
    if matches!(
        record["decision"].as_str(),
        Some("accept" | "accept_with_warnings")
    ) {
        r.ensure(
            !number_equal(attempts, 0),
            "attempts",
            "accepted panels require a generation attempt",
        );
    }
}

fn modern(record: &Value, r: &mut Report) {
    r.object(
        record,
        "",
        &[
            "schema_version",
            "kind",
            "subject_id",
            "bindings",
            "checks",
            "review",
            "decision",
            "unresolved_warnings",
        ],
        &["override_reason"],
    );
    r.ensure(record["kind"] == "panel-qa", "kind", "must equal panel-qa");
    let id = record["subject_id"].as_str();
    r.ensure(id.is_some_and(is_panel), "subject_id", "must match pNN-NN");
    let bindings = &record["bindings"];
    if r.object(bindings, "bindings", BINDINGS, &[]).is_some() {
        for field in ["raw_path", "clean_path", "normalization_path"] {
            let prefix = format!("bindings.{field}");
            r.relative(&bindings[field], &prefix, false);
            if let Some(id) = id {
                r.ensure(
                    bindings[field] == panel_path(field, id),
                    &prefix,
                    "must match the canonical panel path",
                );
            }
        }
        for field in ["raw_sha256", "clean_sha256", "normalization_sha256"] {
            r.sha(&bindings[field], &format!("bindings.{field}"), false);
        }
        for field in ["raw_width", "clean_width", "raw_height", "clean_height"] {
            r.integer(&bindings[field], &format!("bindings.{field}"), 1, 100_000);
        }
    }
    checks_v2(record, r);
    let review = &record["review"];
    if r.object(
        review,
        "review",
        &["method", "reviewer", "reviewed_at"],
        &[],
    )
    .is_some()
    {
        r.string(&review["method"], "review.method");
        r.string(&review["reviewer"], "review.reviewer");
        timestamp(&review["reviewed_at"], "review.reviewed_at", r);
    }
    decisions(record, true, r);
}

fn panel_path(field: &str, id: &str) -> String {
    match field {
        "raw_path" => format!("panels/raw/{id}.png"),
        "clean_path" => format!("panels/{id}/clean.png"),
        _ => format!("panels/{id}/normalization.json"),
    }
}

fn ordered(checks: &[Value]) -> bool {
    checks
        .iter()
        .map(|check| check["id"].as_str())
        .eq(IDS.iter().copied().map(Some))
}

fn verify_check(check: &Value, prefix: &str, v2: bool, r: &mut Report) {
    r.ensure(
        matches!(check["result"].as_str(), Some("pass" | "fail" | "warning")),
        &qualified(prefix, "result"),
        "unknown check result",
    );
    r.ensure(
        matches!(check["severity"].as_str(), Some("warning" | "error"))
            || v2 && check["severity"] == "info",
        &qualified(prefix, "severity"),
        "unknown check severity",
    );
    r.string(&check["evidence"], &qualified(prefix, "evidence"));
}

pub(super) fn check_categories(value: &Value, ids: &[&str]) -> BTreeSet<&'static str> {
    let mut issues = BTreeSet::new();
    let mut evidence = Vec::new();
    let checks = items(value);
    if !value.is_array()
        || !checks
            .iter()
            .map(|check| check["id"].as_str())
            .eq(ids.iter().copied().map(Some))
    {
        issues.insert("quality-check-ids");
    }
    for check in checks {
        if !check.is_object() {
            issues.insert("quality-check-structure");
            continue;
        }
        if !matches!(check["result"].as_str(), Some("pass" | "fail" | "warning")) {
            issues.insert("quality-check-result");
        }
        if !matches!(
            check["severity"].as_str(),
            Some("info" | "warning" | "error")
        ) {
            issues.insert("quality-check-severity");
        }
        for (field, category) in [
            ("method", "quality-check-method"),
            ("reviewer", "quality-check-reviewer"),
        ] {
            if check[field]
                .as_str()
                .is_none_or(|text| text.trim().is_empty())
            {
                issues.insert(category);
            }
        }
        if !check["regions"].is_array() {
            issues.insert("quality-check-regions");
        }
        let normalized = check["evidence"]
            .as_str()
            .unwrap_or_default()
            .nfc()
            .collect::<String>()
            .split(super::python_whitespace)
            .filter(|word| !word.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        let normalized = caseless::default_case_fold_str(&normalized);
        if matches!(
            normalized.as_str(),
            "" | "verified" | "looks good" | "ok" | "pass"
        ) {
            issues.insert("quality-evidence-generic");
        }
        evidence.push(normalized);
    }
    if evidence.len() > 1 && evidence.iter().all(|text| text == &evidence[0]) {
        issues.insert("quality-evidence-generic");
    }
    issues
}

fn checks_v2(record: &Value, r: &mut Report) {
    let checks = items(&record["checks"]);
    for check in checks.iter().filter(|check| check.is_object()) {
        let id = check["id"]
            .as_str()
            .map_or_else(|| repr(&check["id"]), str::to_owned);
        for issue in super::repair::region_issues(check) {
            r.add(format!("checks.{id}.{issue}"), issue);
        }
    }
    if let Some(identity) = checks
        .iter()
        .find(|check| check["id"] == "character-identity")
    {
        for issue in super::character::identity_issues(identity, record) {
            r.add(format!("checks.character-identity.{issue}"), issue);
        }
    }
    for issue in check_categories(&record["checks"], IDS) {
        r.add(format!("checks.{issue}"), issue);
    }
}

fn decisions(record: &Value, v2: bool, r: &mut Report) {
    let decision = record["decision"].as_str().unwrap_or_default();
    let accept_warning = if v2 {
        "accept-warning"
    } else {
        "accept_with_warnings"
    };
    r.ensure(
        matches!(decision, "accept" | "regenerate") || decision == accept_warning,
        "decision",
        if v2 {
            "unknown quality decision"
        } else {
            "unknown panel decision"
        },
    );
    let checks = items(&record["checks"]);
    let hard_fail = checks
        .iter()
        .any(|check| check["result"] == "fail" && check["severity"] == "error");
    let failed_warning = checks
        .iter()
        .any(|check| check["result"] == "fail" && check["severity"] == "warning");
    let warning = checks
        .iter()
        .any(|check| check["result"] == "warning" || (v2 && check["severity"] == "warning"))
        || failed_warning;
    r.ensure(
        !hard_fail || decision == "regenerate",
        "decision",
        if v2 {
            "error-level failures require regenerate"
        } else {
            "error-level failed checks require regeneration"
        },
    );
    r.ensure(
        !warning || decision == accept_warning || decision == "regenerate",
        "decision",
        if v2 {
            "warnings require accept-warning or regenerate"
        } else {
            "warning checks require accept_with_warnings or regeneration"
        },
    );
    let warnings = r.strings(
        &record["unresolved_warnings"],
        "unresolved_warnings",
        0,
        None,
        Some(500),
    );
    if decision == accept_warning
        && (warnings.as_ref().is_none_or(Vec::is_empty) || !v2 && !warning)
    {
        r.add(
            "unresolved_warnings",
            if v2 {
                "accepted warnings must be recorded"
            } else {
                "accepted warnings require check evidence and user-visible impact"
            },
        );
    }
    if v2 && decision == "accept" && warnings.as_ref().is_some_and(|items| !items.is_empty()) {
        r.add(
            "unresolved_warnings",
            "accepted record cannot have warnings",
        );
    }
    if !v2 {
        if decision == "regenerate" {
            r.string(&record["retry_reason"], "retry_reason");
        } else {
            r.ensure(
                record["retry_reason"].is_null(),
                "retry_reason",
                "must be null unless regenerating",
            );
        }
        r.ensure(
            record["failure_category"].is_null()
                || record["failure_category"]
                    .as_str()
                    .is_some_and(|text| is_identifier(text, 64, true)),
            "failure_category",
            "must be a sanitized category string or null",
        );
    }
    if let Some(reason) = record.get("override_reason") {
        r.narrative(reason, "override_reason", 1000);
        if !v2 {
            r.ensure(
                record["failure_category"] == "visual_qa",
                "failure_category",
                "an override must record visual_qa",
            );
        }
        r.ensure(
            decision == accept_warning,
            "override_reason",
            format!("is allowed only for {accept_warning}"),
        );
        r.ensure(
            failed_warning,
            "override_reason",
            "requires a failed check downgraded to warning severity",
        );
        if let Some(warnings) = warnings {
            r.ensure(
                reason.as_str().is_some_and(|text| warnings.contains(&text)),
                "override_reason",
                "must also appear in unresolved_warnings",
            );
        }
    }
}

fn outcomes(record: &Value, manifest: &Value, r: &mut Report) -> (bool, bool) {
    let has_error = record["decision"] == "regenerate"
        || items(&record["checks"])
            .iter()
            .any(|check| check["result"] == "fail" && check["severity"] == "error")
        || matches!(
            record["failure_category"].as_str(),
            Some("corrupt" | "corrupt_image" | "safety" | "safety_refusal")
        );
    if has_error {
        let reason = record["retry_reason"]
            .as_str()
            .filter(|reason| !reason.trim().is_empty())
            .unwrap_or("panel has an unresolved error");
        r.add(
            "decision",
            format!("unresolved panel error blocks final validation: {reason}"),
        );
    }
    let mut warning = false;
    for detail in items(&record["unresolved_warnings"])
        .iter()
        .filter_map(Value::as_str)
        .filter(|text| !text.trim().is_empty())
    {
        warning = true;
        if !items(&manifest["warnings"])
            .iter()
            .any(|item| item == detail)
        {
            Report {
                path: PROJECT,
                issues: r.issues,
            }
            .add(
                "warnings",
                format!("must include unresolved panel warning: {detail}"),
            );
        }
    }
    (has_error, warning)
}

fn provenance(root: &File, record: &Value, r: &mut Report) -> Result<()> {
    let mut stale = |field: &str, detail: &str| {
        r.add(
            format!("bindings.{field}"),
            format!("quality-record-stale: {detail}"),
        );
    };
    let bindings = &record["bindings"];
    let Some(object) = bindings.as_object() else {
        stale("bindings", "bindings object is missing");
        return Ok(());
    };
    for field in BINDINGS {
        if !object.contains_key(*field) {
            stale(field, "required provenance binding is missing");
        }
    }
    for kind in ["raw", "clean", "normalization"] {
        let path_field = format!("{kind}_path");
        if let Some(id) = record["subject_id"].as_str()
            && bindings[&path_field] != panel_path(&path_field, id)
        {
            stale(
                &path_field,
                "binding does not match the canonical panel path",
            );
        }
        let Some(path) = bindings[&path_field].as_str() else {
            continue;
        };
        let Ok(Some((dir, name))) = artifact(root, path) else {
            stale(
                &path_field,
                "bound artifact is missing or outside the project",
            );
            continue;
        };
        let file = project_fs::open_file(&dir, &name, false, false)?;
        let sha = format!("{kind}_sha256");
        if let Some(expected) = bindings[&sha].as_str().filter(|text| is_sha256(text)) {
            if sha256(file)? != expected {
                stale(&sha, "bound SHA-256 does not match current bytes");
            }
        } else {
            stale(&sha, "bound SHA-256 is invalid");
        }
        if kind == "normalization" {
            check_normalization(&dir, &name, bindings, &mut stale);
        } else {
            let file = project_fs::open_file(&dir, &name, false, false)?;
            match decode(file) {
                Ok(info) => {
                    for (axis, value) in [("width", info.width), ("height", info.height)] {
                        let field = format!("{kind}_{axis}");
                        if let Some(number) = bindings.get(&field)
                            && !number_equal(number, value as usize)
                        {
                            stale(
                                &field,
                                &format!("recorded {axis} does not match current raster"),
                            );
                        }
                    }
                }
                Err(_) => stale(
                    &path_field,
                    "bound raster is unreadable or exceeds the decode limit",
                ),
            }
        }
    }
    Ok(())
}

fn check_normalization(
    dir: &File,
    name: &Path,
    bindings: &Value,
    stale: &mut impl FnMut(&str, &str),
) {
    let Ok((record, _)) = manifest::read_document(dir, name, "normalization record") else {
        stale("normalization_path", "normalization record is unreadable");
        return;
    };
    if !record.is_object() {
        stale(
            "normalization_path",
            "normalization record must be an object",
        );
        return;
    }
    if !record["source"].is_object() || !record["clean"].is_object() {
        stale(
            "normalization_path",
            "normalization record structure is invalid",
        );
        return;
    }
    for (kind, part) in [("raw", "source"), ("clean", "clean")] {
        for key in ["path", "sha256"] {
            let field = format!("{kind}_{key}");
            if bindings[&field] != record[part][key] {
                stale(&field, "binding disagrees with normalization record");
            }
        }
        if let [a, b] = items(&record[part]["size"]) {
            for (axis, value) in [("width", a), ("height", b)] {
                let field = format!("{kind}_{axis}");
                if let Some(recorded) = bindings.get(&field)
                    && recorded != value
                {
                    stale(&field, "binding disagrees with normalization record");
                }
            }
        } else {
            stale(
                "normalization_path",
                &format!("normalization {kind} size is invalid"),
            );
        }
    }
}

fn raster_bindings(
    root: &File,
    record: &Value,
    panel: &Value,
    v2: bool,
    r: &mut Report,
) -> Result<()> {
    let bindings = if v2 { &record["bindings"] } else { record };
    let ratio = match (
        panel["rect"]["width"].as_i64(),
        panel["rect"]["height"].as_i64(),
    ) {
        (Some(width), Some(height)) if width > 0 && height > 0 => {
            Some(width as f64 / height as f64)
        }
        _ => None,
    };
    let prefix = if v2 { "bindings" } else { "" };
    let raw = raster(
        root,
        &bindings["raw_path"],
        &qualified(prefix, "raw_path"),
        ratio,
        None,
        r,
    )?;
    raster(
        root,
        &bindings["clean_path"],
        &qualified(prefix, "clean_path"),
        ratio,
        None,
        r,
    )?;
    if let Some(raw) = raw
        && !v2
        && record["dimensions"].is_object()
    {
        r.ensure(
            number_equal(&record["dimensions"]["width"], raw.width as usize)
                && number_equal(&record["dimensions"]["height"], raw.height as usize),
            "dimensions",
            "must match the raw image",
        );
    }
    for kind in ["raw", "clean"] {
        if kind == "clean" && !v2 {
            continue;
        }
        let hash = format!("{kind}_sha256");
        let path = format!("{kind}_path");
        if let (Some(path), Some(expected)) = (bindings[&path].as_str(), bindings[&hash].as_str())
            && is_sha256(expected)
            && let Ok(Some((dir, name))) = artifact(root, path)
        {
            let observed = sha256(project_fs::open_file(&dir, &name, false, false)?)?;
            r.ensure(
                observed == expected,
                &qualified(prefix, &hash),
                format!("hash does not match the {kind} image"),
            );
        }
    }
    if !v2 {
        if let Some(path) = record["source_prompt_path"].as_str() {
            match artifact(root, path) {
                Ok(Some(_)) => {}
                Ok(None) => r.add("source_prompt_path", "referenced prompt is missing"),
                Err(_) => r.add(
                    "source_prompt_path",
                    "referenced prompt path escapes the project boundary",
                ),
            }
        }
        for (index, path) in items(&record["generation"]["reference_paths"])
            .iter()
            .enumerate()
        {
            raster(
                root,
                path,
                &format!("generation.reference_paths[{index}]"),
                None,
                None,
                r,
            )?;
        }
    }
    Ok(())
}

pub(super) fn raster(
    root: &File,
    value: &Value,
    field: &str,
    ratio: Option<f64>,
    expected_format: Option<ImageFormat>,
    r: &mut Report,
) -> Result<Option<Raster>> {
    let before = r.issues.len();
    r.relative(value, field, false);
    if r.issues.len() != before {
        return Ok(None);
    }
    let path = value.as_str().expect("validated path");
    let located = match artifact(root, path) {
        Ok(location) => location,
        Err(_) => {
            r.add(field, "referenced image path escapes the project boundary");
            return Ok(None);
        }
    };
    let extension = Path::new(path)
        .extension()
        .and_then(|text| text.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(extension.as_str(), "png" | "jpg" | "jpeg" | "webp") {
        r.add(field, "must be PNG, JPEG, or WebP");
        return Ok(None);
    }
    let Some((dir, name)) = located else {
        r.add(field, "referenced image is missing");
        return Ok(None);
    };
    let info = match project_fs::open_file(&dir, &name, false, false) {
        Ok(file) => decode(file),
        Err(_) => Err("OSError"),
    };
    match info {
        Ok(info) => {
            if let Some(expected) = expected_format {
                r.ensure(info.format == expected, field, "must contain PNG data");
            }
            r.ensure(
                info.width >= MIN_DIMENSION && info.height >= MIN_DIMENSION,
                field,
                format!("image dimensions must both be at least {MIN_DIMENSION}px"),
            );
            r.ensure(
                !info.alpha,
                field,
                "image has unintended alpha transparency",
            );
            if let Some(expected) = ratio {
                let actual = f64::from(info.width) / f64::from(info.height);
                r.ensure(
                    (actual - expected).abs() / expected <= 0.02,
                    field,
                    "image aspect ratio differs from storyboard by more than 2%",
                );
            }
            Ok(Some(info))
        }
        Err(error) => {
            r.add(field, format!("image is unreadable: {error}"));
            Ok(None)
        }
    }
}

pub(crate) struct Raster {
    format: ImageFormat,
    pub(crate) width: u32,
    pub(crate) height: u32,
    alpha: bool,
}

pub(crate) fn decode(file: File) -> std::result::Result<Raster, &'static str> {
    let mut bytes = Vec::new();
    if file.metadata().map_err(|_| "OSError")?.len() > MAX_ENCODED {
        return Err("InputResourceLimitError");
    }
    file.take(MAX_ENCODED + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "OSError")?;
    if bytes.len() as u64 > MAX_ENCODED {
        return Err("InputResourceLimitError");
    }
    decode_bytes(&bytes)
}

pub(crate) fn decode_bytes(bytes: &[u8]) -> std::result::Result<Raster, &'static str> {
    let format = image::guess_format(bytes).map_err(|_| "UnidentifiedImageError")?;
    if !matches!(
        format,
        ImageFormat::Png | ImageFormat::Jpeg | ImageFormat::WebP
    ) {
        return Err("UnidentifiedImageError");
    }
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    let mut limits = Limits::default();
    limits.max_image_width = Some(MAX_PIXELS as u32);
    limits.max_image_height = Some(MAX_PIXELS as u32);
    reader.limits(limits);
    let decoder = reader.into_decoder().map_err(|error| match error {
        ImageError::Limits(_) => "DecompressionBombError",
        _ => "UnidentifiedImageError",
    })?;
    let (width, height) = decoder.dimensions();
    if u64::from(width) * u64::from(height) > MAX_PIXELS {
        return Err("DecompressionBombError");
    }
    let alpha = decoder.color_type().has_alpha();
    let size = usize::try_from(decoder.total_bytes()).map_err(|_| "DecompressionBombError")?;
    let mut pixels = Vec::new();
    pixels
        .try_reserve_exact(size)
        .map_err(|_| "DecompressionBombError")?;
    pixels.resize(size, 0);
    decoder.read_image(&mut pixels).map_err(|_| "OSError")?;
    Ok(Raster {
        format,
        width,
        height,
        alpha,
    })
}
