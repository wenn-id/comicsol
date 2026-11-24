use std::collections::BTreeSet;
use std::fs::File;
use std::sync::LazyLock;

use regex::bytes::Regex;
use serde_json::Value;

use super::{
    EVENTS, MAX_RASTER, REPORT, descriptor, names, object, page_number, publish_manifest, raster,
    read,
};
use crate::error::{Error, Result};
use crate::project_fs::LockedProject;
use crate::recovery::Change;
use crate::validation::{file_hash, items, panels::IDS};

const TEMPLATE: &str = include_str!("../../../../templates/qa-report.md.tmpl");
const MAX_EVENTS: u64 = 8 * 1024 * 1024;

pub(super) fn run(locked: &LockedProject, project: &mut Value, final_status: &str) -> Result<()> {
    let bytes = render(locked.root(), project, final_status)?;
    descriptor(project, "qa_report", REPORT, &bytes);
    publish_manifest(
        locked,
        "report-publish",
        project,
        vec![Change::Replace(REPORT.into(), bytes)],
    )
}

pub(super) fn render(root: &File, project: &Value, final_status: &str) -> Result<Vec<u8>> {
    let mut panels = Vec::new();
    for entry in names(root, "qa/panels")?
        .into_iter()
        .filter(|name| name.ends_with(".json"))
    {
        let record = object(root, &format!("qa/panels/{entry}"))?;
        if panel_id(&record).is_none() {
            return Err(Error::invalid("ValueError"));
        }
        panels.push(record);
    }
    panels.sort_by(|a, b| panel_id(a).cmp(&panel_id(b)));
    let mut pages = Vec::new();
    for entry in names(root, "qa/pages")?
        .into_iter()
        .filter(|name| name.starts_with("page-") && name.ends_with(".json"))
    {
        pages.push(object(root, &format!("qa/pages/{entry}"))?);
    }
    pages.sort_by_key(|record| {
        if let Some(id) = record["subject_id"].as_str() {
            (1, id.to_owned())
        } else if let Some(number) = record["page"].as_i64() {
            (0, format!("page-{number:03}"))
        } else {
            (0, format!("page-{}", string(&record["page"])))
        }
    });
    let status = if project["status"] == "EXPORTED" {
        final_status.to_owned()
    } else {
        field(project, "status", "unknown")
    };
    let project_summary = format!(
        "- Project: {} (`{}`)\n- Final status: **{status}**",
        escape(&field(project, "title", "Untitled")),
        field(project, "project_id", "unknown")
    );
    let fields = [
        ("{{PROJECT_SUMMARY}}", project_summary),
        ("{{CAPABILITY}}", capability(project)),
        ("{{COUNTS}}", counts(project, &panels)),
        ("{{EVIDENCE_PROVENANCE}}", evidence(root)?),
        ("{{PANEL_TABLE}}", panel_table(&panels)),
        ("{{NORMALIZATION_TABLE}}", normalization(root, &panels)),
        ("{{PAGE_QA_TABLE}}", page_table(&pages)),
        ("{{WARNINGS}}", warnings(project, &panels)),
        ("{{INTEGRITY}}", integrity(root, project, &panels)?),
        ("{{RESUME}}", resume(root)),
    ];
    let rendered = fields
        .into_iter()
        .fold(TEMPLATE.to_owned(), |report, (token, text)| {
            report.replace(token, &text)
        });
    Ok(format!("{}\n", rendered.trim_end()).into_bytes())
}

fn string(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(value) => value.clone(),
        _ => value.to_string(),
    }
}

fn field(value: &Value, name: &str, default: &str) -> String {
    value.get(name).map_or_else(|| default.into(), string)
}

fn escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('|', "\\|")
        .replace("\r\n", "<br>")
        .replace(['\r', '\n'], "<br>")
}

fn yes(condition: bool) -> &'static str {
    if condition { "yes" } else { "no" }
}

fn panel_id(record: &Value) -> Option<&str> {
    record[if record["schema_version"] == "2.0" {
        "subject_id"
    } else {
        "panel_id"
    }]
    .as_str()
    .filter(|id| !id.is_empty())
}

fn attempts(record: &Value) -> u64 {
    let source = if record["schema_version"] == "2.0" && record["bindings"].is_object() {
        &record["bindings"]
    } else {
        record
    };
    source["attempts"].as_u64().unwrap_or_default()
}

fn capability(project: &Value) -> String {
    let cap = &project["capability"];
    let support = cap["supports_reference_images"] == true;
    let name = cap["name"]
        .as_str()
        .filter(|name| !name.is_empty())
        .unwrap_or("none");
    let mut text = format!(
        "- Status: {}\n- Capability: {name}\n- Reference images supported: {}\n- Explicit dimensions supported: {}",
        field(cap, "status", "unknown"),
        yes(support),
        yes(cap["supports_dimensions"] == true)
    );
    if !support {
        text.push_str("\n- Consistency: degraded consistency mode because reference images are unsupported; canonical text anchors were used instead.");
    }
    text.push_str("\n- Privacy: external provider policies govern transmitted prompts and references; Comic Sol stores no provider credentials.");
    text
}

fn counts(project: &Value, panels: &[Value]) -> String {
    let summary = [
        (
            "page",
            project["settings"]["page_count"]
                .as_u64()
                .unwrap_or_default(),
        ),
        ("panel", panels.len() as u64),
        ("generation attempt", panels.iter().map(attempts).sum()),
        (
            "regenerated panel",
            panels.iter().filter(|panel| attempts(panel) > 1).count() as u64,
        ),
        (
            "accepted warning",
            panels
                .iter()
                .filter(|panel| {
                    matches!(
                        panel["decision"].as_str(),
                        Some("accept-warning" | "accept_with_warnings")
                    )
                })
                .count() as u64,
        ),
        (
            "hard failure",
            panels
                .iter()
                .filter(|panel| {
                    matches!(
                        panel["failure_category"].as_str(),
                        Some("corrupt" | "corrupt_image" | "safety" | "safety_refusal")
                    ) || attempts(panel)
                        > project["settings"]["max_panel_retries"]
                            .as_u64()
                            .unwrap_or(2)
                        && items(&panel["checks"])
                            .iter()
                            .any(|check| check["result"] == "fail" && check["severity"] == "error")
                })
                .count() as u64,
        ),
    ];
    summary
        .into_iter()
        .map(|(name, n)| format!("- {n} {name}{}", if n == 1 { "" } else { "s" }))
        .collect::<Vec<_>>()
        .join("\n")
}

fn evidence(root: &File) -> Result<String> {
    if !super::exists(root, "qa/evidence.json") {
        return Ok(
            "- Mode: unavailable\n- Scope: no explicit evidence provenance record was supplied."
                .into(),
        );
    }
    let data = object(root, "qa/evidence.json")?;
    if data["mode"] == "deterministic" {
        return Ok(format!(
            "- Mode: deterministic\n- Scope: {}\n- Claim boundary: deterministic evidence proves mechanics only and does not prove live visual quality.",
            field(&data, "scope", "mechanics-only")
        ));
    }
    if data["mode"] != "live-visual" {
        return Err(Error::invalid("ValueError"));
    }
    if [
        "retained_attempt",
        "attempt_sha256",
        "provider",
        "model",
        "reviewer_method",
    ]
    .iter()
    .any(|field| data[*field].as_str().is_none_or(str::is_empty))
    {
        return Err(Error::invalid("ValueError"));
    }
    let val = |field| escape(&string(&data[field]));
    let joined = |field| {
        let text = items(&data[field])
            .iter()
            .map(|item| escape(&string(item)))
            .collect::<Vec<_>>()
            .join(", ");
        if text.is_empty() { "none".into() } else { text }
    };
    Ok(format!(
        "- Mode: live-visual\n- Scope: {}\n- Provider/model: {} / {}\n- Retained attempt: `{}`\n- Attempt SHA-256: `{}`\n- References: {}\n- Reviewer method: {}\n- Known limitations: {}",
        escape(&field(&data, "scope", "retained-attempt-visual-review")),
        val("provider"),
        val("model"),
        val("retained_attempt"),
        val("attempt_sha256"),
        joined("references"),
        val("reviewer_method"),
        joined("limitations")
    ))
}

fn table(headings: &[&str]) -> Vec<String> {
    vec![
        format!("| {} |", headings.join(" | ")),
        format!("| {} |", vec!["---"; headings.len()].join(" | ")),
    ]
}

fn row<'a>(cells: impl IntoIterator<Item = &'a str>) -> String {
    format!(
        "| {} |",
        cells
            .into_iter()
            .map(escape)
            .collect::<Vec<_>>()
            .join(" | ")
    )
}

fn panel_table(records: &[Value]) -> String {
    let mut headers = vec!["Panel", "Attempts", "Decision"];
    headers.extend(IDS);
    headers.push("Evidence");
    let mut lines = table(&headers);
    for record in records {
        let mut decision = field(record, "decision", "unknown");
        if let Some(reason) = record["override_reason"]
            .as_str()
            .filter(|reason| !reason.is_empty())
        {
            decision.push_str(&format!(" (override: {reason})"));
        }
        let mut cells = vec![
            panel_id(record).unwrap_or("unknown").into(),
            attempts(record).to_string(),
            decision,
        ];
        let mut evidence = Vec::new();
        for id in IDS {
            if let Some(check) = items(&record["checks"])
                .iter()
                .rev()
                .find(|check| check["id"] == *id)
            {
                let mut result = field(check, "result", "missing");
                if result == "fail" && check["severity"] == "warning" {
                    result.push_str(" (warning)");
                }
                cells.push(result);
                evidence.push(format!("{id}: {}", field(check, "evidence", "")));
            } else {
                cells.push("missing".into());
            }
        }
        cells.push(evidence.join("; "));
        lines.push(row(cells.iter().map(String::as_str)));
    }
    lines.join("\n")
}

fn dimensions(value: &Value) -> String {
    if let [w, h] = items(value)
        && w.as_i64().is_some()
        && h.as_i64().is_some()
    {
        format!("{w}×{h}")
    } else {
        "unavailable".into()
    }
}

fn normalization(root: &File, panels: &[Value]) -> String {
    let mut lines = table(&["Panel", "Mode", "Source", "Target"]);
    for panel in panels {
        let data = panel["bindings"]["normalization_path"]
            .as_str()
            .and_then(|path| object(root, path).ok())
            .unwrap_or_default();
        let mode = data["operation"]["mode"].as_str().unwrap_or("unavailable");
        lines.push(row([
            panel_id(panel).unwrap_or("unknown"),
            mode,
            &dimensions(&data["source"]["size"]),
            &dimensions(&data["target_size"]),
        ]));
    }
    lines.join("\n")
}

fn page_table(records: &[Value]) -> String {
    let mut lines = table(&["Page", "Layout", "Check", "Result", "Method", "Reviewer"]);
    for data in records {
        if data["schema_version"] != "2.1"
            || data["kind"] != "page-qa"
            || data["quality-migration-required"] == true
        {
            let page = data["subject_id"]
                .as_str()
                .filter(|id| !id.is_empty())
                .map_or_else(
                    || format!("page-{}", field(data, "page", "unknown")),
                    str::to_owned,
                );
            lines.push(row([
                &page,
                "quality-migration-required",
                "migration-required",
                "missing",
                "migration-required",
                "migration-required",
            ]));
            continue;
        }
        let page = field(data, "subject_id", "unknown");
        let layout = format!(
            "{} v{}",
            field(&data["bindings"], "layout_name", "unknown"),
            field(&data["bindings"], "layout_version", "unknown")
        );
        for check in items(&data["checks"])
            .iter()
            .filter(|item| item.is_object())
        {
            lines.push(row([
                page.as_str(),
                layout.as_str(),
                &field(check, "id", "unknown"),
                &field(check, "result", "missing"),
                &field(check, "method", "missing"),
                &field(check, "reviewer", "missing"),
            ]));
        }
    }
    if lines.len() == 2 {
        lines.push(row([
            "none",
            "unavailable",
            "unavailable",
            "unavailable",
            "unavailable",
            "unavailable",
        ]));
    }
    lines.join("\n")
}

fn warnings(project: &Value, records: &[Value]) -> String {
    let iter = records
        .iter()
        .map(|record| {
            (
                panel_id(record).unwrap_or("unknown"),
                &record["unresolved_warnings"],
            )
        })
        .chain(std::iter::once(("project", &project["warnings"])));
    let mut warnings: Vec<(&str, Vec<&str>)> = Vec::new();
    for (source, values) in iter {
        for value in items(values)
            .iter()
            .filter_map(Value::as_str)
            .filter(|text| !text.is_empty())
        {
            if let Some((_, sources)) = warnings.iter_mut().find(|(warning, _)| *warning == value) {
                if !sources.contains(&source) {
                    sources.push(source);
                }
            } else {
                warnings.push((value, vec![source]));
            }
        }
    }
    if warnings.is_empty() {
        return "No unresolved warnings.".into();
    }
    warnings
        .into_iter()
        .map(|(warning, sources)| format!("- `{}`: {warning}", sources.join(", ")))
        .collect::<Vec<_>>()
        .join("\n")
}

fn integrity(root: &File, project: &Value, records: &[Value]) -> Result<String> {
    static PDF_PAGES: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"/Type\s*/Page\b").expect("PDF page marker"));
    let artifacts = &project["artifacts"];
    let mut lines = Vec::new();
    if let Some(artifacts) = artifacts.as_object() {
        for (name, desc) in artifacts {
            if !desc.is_object() {
                lines.push(format!("- `{name}`: invalid descriptor"));
                continue;
            }
            let relative = field(desc, "path", "None");
            let expected = field(desc, "sha256", "None");
            if let Some(hash) = desc["path"]
                .as_str()
                .and_then(|path| file_hash(root, path).ok())
            {
                lines.push(format!(
                    "- `{relative}` — exists: {}; hash: `{expected}`; hash matches: {}",
                    yes(hash.is_some()),
                    yes(desc["sha256"].as_str().is_some()
                        && hash.as_deref() == desc["sha256"].as_str())
                ));
            } else {
                lines.push(format!("- `{relative}` — outside the project boundary"));
            }
        }
    }
    let pages: Vec<_> = names(root, "pages")?
        .into_iter()
        .filter(|name| page_number(name, ".png").is_some())
        .collect();
    for page in &pages {
        let path = format!("pages/{page}");
        let (dimensions, valid, sha) = match read(root, &path, MAX_RASTER) {
            Ok(bytes) => {
                let result = raster::decode(&bytes)
                    .map(|(image, format)| {
                        (
                            format!("{}×{}", image.width(), image.height()),
                            format == image::ImageFormat::Png
                                && image.as_rgb8().is_some()
                                && (image.width(), image.height()) == (1600, 2400),
                        )
                    })
                    .unwrap_or_else(|| ("unreadable".into(), false));
                (result.0, result.1, crate::validation::bytes_hash(&bytes))
            }
            Err(_) => ("unreadable".into(), false, "missing".into()),
        };
        lines.push(format!(
            "- `{path}` — dimensions: {dimensions}; valid page: {}; sha256: `{sha}`",
            yes(valid)
        ));
    }
    let references: BTreeSet<_> = records
        .iter()
        .flat_map(|record| items(&record["generation"]["reference_paths"]))
        .filter_map(Value::as_str)
        .collect();
    for reference in references {
        lines.push(format!(
            "- Reference `{reference}` — valid: {}",
            yes(super::exists(root, reference))
        ));
    }
    let pdf = artifacts["pdf"]["path"].as_str().map_or_else(
        || format!("exports/{}.pdf", field(project, "project_id", "")),
        str::to_owned,
    );
    let readable = read(root, &pdf, MAX_RASTER).ok().is_some_and(|bytes| {
        bytes.starts_with(b"%PDF-")
            && bytes.trim_ascii_end().ends_with(b"%%EOF")
            && PDF_PAGES.find_iter(&bytes).count() == pages.len()
    });
    lines.push(format!(
        "- `{pdf}` — PDF readable: {}; pages: {}",
        yes(readable),
        pages.len()
    ));
    Ok(lines.join("\n"))
}

fn resume(root: &File) -> String {
    let mut reused = BTreeSet::new();
    let mut regenerated = BTreeSet::new();
    if let Ok(bytes) = read(root, EVENTS, MAX_EVENTS) {
        for line in String::from_utf8_lossy(&bytes).lines() {
            if let Ok(event) = crate::manifest::parse_document(line, "events.jsonl")
                && let Some(artifact) = event["details"]["artifact_path"].as_str()
            {
                match event["event"].as_str() {
                    Some("artifact.reused") => {
                        reused.insert(artifact.to_owned());
                    }
                    Some("artifact.regenerated") => {
                        regenerated.insert(artifact.to_owned());
                    }
                    _ => {}
                }
            }
        }
    }
    let joined = |values: BTreeSet<String>| {
        if values.is_empty() {
            "none".into()
        } else {
            values.into_iter().collect::<Vec<_>>().join(", ")
        }
    };
    format!(
        "- Reused: {}\n- Regenerated: {}",
        joined(reused),
        joined(regenerated)
    )
}
