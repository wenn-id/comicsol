use std::fs::File;
use std::io::{self, Read};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{
    Issue, PROJECT, Report, Stage, artifact, is_identifier, is_sha256, items, load, number_equal,
    repr,
};
use crate::error::Result;
use crate::{manifest, project_fs};

const EXPORT_READY: &[(&str, &str)] = &[
    ("character_bible", "plan/character-bible.json"),
    ("story_plan", "plan/story-plan.json"),
    ("storyboard", "plan/storyboard.json"),
    ("composition_cache", "cache/composition.json"),
];
const FINAL: &[(&str, &str)] = &[
    ("qa_report", "qa/report.md"),
    ("pdf_verification", "exports/pdf-verification.json"),
];

pub(super) fn inspect(
    root: &File,
    project: &Value,
    stage: Stage,
    issues: &mut Vec<Issue>,
) -> Result<()> {
    let page_count = project["settings"]["page_count"]
        .as_i64()
        .unwrap_or_default()
        .clamp(0, 4);
    for page in 1..=page_count {
        inspect_page(root, page as usize, issues)?;
    }
    for panel in items(&project["panels"]).iter().take(12) {
        let id = panel.as_str().map_or_else(|| repr(panel), str::to_owned);
        let lettered = format!("panels/{id}/lettered.png");
        match artifact(root, &lettered) {
            Ok(Some(_)) => {}
            Ok(None) => Report {
                path: &lettered,
                issues,
            }
            .add("", "lettered panel is missing"),
            Err(_) => {
                Report {
                    path: &lettered,
                    issues,
                }
                .add("", "lettered panel path escapes the project boundary");
                continue;
            }
        }
        super::lettering::inspect(root, &id, issues);
    }
    if stage != Stage::ExportReady {
        inspect_pdf(root, project, page_count as usize, issues)?;
    }
    let prefix = "cache/composition.json";
    if artifact(root, prefix)?.is_none() {
        Report {
            path: prefix,
            issues,
        }
        .add("", "composition stage cache is missing");
    }
    inspect_descriptors(
        root,
        project,
        stage,
        &mut Report {
            path: PROJECT,
            issues,
        },
    )?;
    Ok(())
}

fn inspect_page(root: &File, page: usize, issues: &mut Vec<Issue>) -> Result<()> {
    let qa = format!("qa/pages/page-{page:03}.json");
    let image = format!("pages/page-{page:03}.png");
    let record = load(root, &qa, issues);
    let legacy = match record
        .as_ref()
        .map(|record| record["schema_version"].as_str())
    {
        Some(Some("2.1")) => {
            super::page_quality::inspect(
                root,
                page,
                record.as_ref().expect("read QA record"),
                &mut Report { path: &qa, issues },
            );
            None
        }
        Some(Some("2.0")) => {
            Report { path: &qa, issues }.add(
                "schema_version",
                "quality-migration-required: schema 2.0 page QA must be migrated to 2.1",
            );
            None
        }
        Some(Some("1.0")) => record.as_ref(),
        Some(_) => {
            Report {path: &qa, issues}.add("schema_version", "quality-migration-required: unsupported page QA schema; no migration path to 2.1 is registered");
            None
        }
        None => None,
    };
    if let Some(legacy) = legacy {
        let mut report = Report {
            path: "qa/pages/",
            issues,
        };
        report.object(
            legacy,
            "",
            &[
                "page",
                "page_path",
                "page_sha256",
                "schema_version",
                "status",
            ],
            &[],
        );
        report.ensure(
            legacy["page"].as_i64().is_some_and(|number| number >= 1) || legacy["page"] == true,
            "page",
            "must be a positive integer",
        );
        report.relative(&legacy["page_path"], "page_path", false);
        report.sha(&legacy["page_sha256"], "page_sha256", false);
        report.ensure(
            legacy["status"] == "reviewed",
            "status",
            "must be \"reviewed\"",
        );
        let mut report = Report { path: &qa, issues };
        report.add(
            "schema_version",
            "quality-migration-required: schema 1.0 page QA must be migrated",
        );
        report.ensure(
            number_equal(&legacy["page"], page),
            "page",
            "must match the canonical page number",
        );
        report.ensure(
            legacy["page_path"] == image,
            "page_path",
            "must match the canonical page path",
        );
    }
    match artifact(root, &image)? {
        None => Report {
            path: &image,
            issues,
        }
        .add("", "composed page is missing"),
        Some((dir, name)) => {
            if let Some(expected) = legacy.and_then(|record| record["page_sha256"].as_str()) {
                let actual = sha256(project_fs::open_file(&dir, &name, false, false)?)?;
                Report { path: &qa, issues }.ensure(
                    actual == expected,
                    "page_sha256",
                    "hash does not match the page image",
                );
            }
        }
    }
    Ok(())
}

static TIMESTAMP_SHAPE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
        .expect("verification timestamp format")
});

fn inspect_pdf(
    root: &File,
    project: &Value,
    page_count: usize,
    issues: &mut Vec<Issue>,
) -> Result<()> {
    if artifact(root, "qa/report.md")?.is_none() {
        Report {
            path: "qa/report.md",
            issues,
        }
        .add("", "QA report is missing");
    }
    inspect_pdf_verification(root, project, page_count, issues)
}

pub(crate) fn pdf_verification_valid(root: &File, project: &Value, page_count: usize) -> bool {
    let mut issues = Vec::new();
    inspect_pdf_verification(root, project, page_count, &mut issues).is_ok() && issues.is_empty()
}

fn inspect_pdf_verification(
    root: &File,
    project: &Value,
    page_count: usize,
    issues: &mut Vec<Issue>,
) -> Result<()> {
    let Some(id) = project["project_id"].as_str().filter(|id| !id.is_empty()) else {
        return Ok(());
    };
    let path = format!("exports/{id}.pdf");
    match artifact(root, &path) {
        Ok(Some(_)) => {}
        Ok(None) => Report {
            path: &path,
            issues,
        }
        .add("", "exported PDF is missing"),
        Err(_) => {
            Report {
                path: PROJECT,
                issues,
            }
            .add(
                "project_id",
                "exported PDF path escapes the project boundary",
            );
            return Ok(());
        }
    }
    let verification = "exports/pdf-verification.json";
    let mut report = Report {
        path: verification,
        issues,
    };
    let (dir, name) = match artifact(root, verification) {
        Ok(Some(file)) => file,
        Ok(None) => {
            report.add(
                "pdf-verification-stale",
                "verification record is missing or unreadable",
            );
            return Ok(());
        }
        Err(_) => {
            report.add(
                "pdf-verification-stale",
                "verification path escapes the project boundary",
            );
            return Ok(());
        }
    };
    let Ok((record, _)) = manifest::read_document(&dir, &name, verification) else {
        report.add(
            "pdf-verification-stale",
            "verification record is missing or unreadable",
        );
        return Ok(());
    };
    if !record.is_object() {
        report.add(
            "pdf-verification-stale",
            "verification record must be an object",
        );
        return Ok(());
    }
    let mut stale = Vec::new();
    for (valid, detail) in [
        (
            record["schema_version"] == "1.0",
            "schema version is unsupported",
        ),
        (
            record["kind"] == "pdf-verification",
            "record kind is invalid",
        ),
        (
            record["verified_at"]
                .as_str()
                .is_some_and(|text| TIMESTAMP_SHAPE.is_match(text)),
            "verification timestamp is not ISO 8601 UTC",
        ),
        (record["pdf_path"] == path, "PDF path binding is stale"),
        (
            hash_matches(root, &path, &record["pdf_sha256"])?,
            "PDF hash binding is stale",
        ),
        (
            number_equal(&record["page_count"], page_count),
            "page count binding is stale",
        ),
    ] {
        if !valid {
            stale.push(detail.to_owned());
        }
    }
    if let Some(sources) = record["source_pages"]
        .as_array()
        .filter(|items| items.len() == page_count)
    {
        for (index, source) in sources.iter().enumerate() {
            let number = index + 1;
            if !source.is_object() {
                stale.push(format!("page {number} binding is invalid"));
                continue;
            }
            let expected_page = format!("pages/page-{number:03}.png");
            let expected_qa = format!("qa/pages/page-{number:03}.json");
            let dimensions = matches!(items(&source["dimensions"]), [w, h] if number_equal(w, 1600) && number_equal(h, 2400));
            for (valid, description) in [
                (source["path"] == expected_page, "path binding is stale"),
                (
                    source["page_qa_path"] == expected_qa,
                    "QA path binding is stale",
                ),
                (dimensions, "dimensions are stale"),
                (
                    hash_matches(root, &expected_page, &source["sha256"])?,
                    "sha256 binding is stale",
                ),
                (
                    hash_matches(root, &expected_qa, &source["page_qa_sha256"])?,
                    "page_qa_sha256 binding is stale",
                ),
            ] {
                if !valid {
                    stale.push(format!("page {number} {description}"));
                }
            }
        }
    } else {
        stale.push("ordered source page bindings are incomplete".into());
    }
    if !stale.is_empty() {
        report.add("pdf-verification-stale", stale.join("; "));
    }
    Ok(())
}

fn hash_matches(root: &File, path: &str, expected: &Value) -> Result<bool> {
    let Some(expected) = expected.as_str().filter(|text| is_sha256(text)) else {
        return Ok(false);
    };
    let Ok(Some((dir, name))) = artifact(root, path) else {
        return Ok(false);
    };
    Ok(sha256(project_fs::open_file(&dir, &name, false, false)?)? == expected)
}
fn inspect_descriptors(
    root: &File,
    project: &Value,
    stage: Stage,
    report: &mut Report,
) -> Result<()> {
    let descriptors = &project["artifacts"];
    if let Some(descriptors) = descriptors.as_object() {
        for (name, descriptor) in descriptors {
            let field = format!("artifacts.{name}");
            check_binding(
                root,
                &descriptor["path"],
                &descriptor["sha256"],
                &format!("{field}.path"),
                &format!("{field}.sha256"),
                report,
                "artifact",
            )?;
        }
    }
    for (key, path) in EXPORT_READY.iter().copied().chain(
        FINAL
            .iter()
            .copied()
            .filter(|_| stage != Stage::ExportReady),
    ) {
        required_descriptor(descriptors, key, Some(path), report);
    }
    if stage != Stage::ExportReady {
        let expected = project["project_id"]
            .as_str()
            .filter(|id| is_identifier(id, 48, false))
            .map(|id| format!("exports/{id}.pdf"));
        required_descriptor(descriptors, "pdf", expected.as_deref(), report);
    }
    Ok(())
}

fn required_descriptor(
    descriptors: &Value,
    name: &str,
    expected: Option<&str>,
    report: &mut Report,
) {
    if let Some(value) = descriptors.get(name) {
        if value.is_object()
            && let Some(expected) = expected
        {
            report.ensure(
                value["path"] == expected,
                &format!("artifacts.{name}.path"),
                format!("must equal {expected}"),
            );
        }
    } else {
        report.add(
            format!("artifacts.{name}"),
            "required artifact descriptor is missing",
        );
    }
}

pub(super) fn check_binding(
    root: &File,
    path: &Value,
    digest: &Value,
    path_field: &str,
    digest_field: &str,
    report: &mut Report,
    kind: &str,
) -> Result<()> {
    let (Some(path), Some(expected)) = (path.as_str(), digest.as_str()) else {
        return Ok(());
    };
    match artifact(root, path) {
        Ok(None) => report.add(path_field, format!("referenced {kind} is missing")),
        Err(error) if matches!(error.code(), "CS-PROJ-001" | "CS-SEC-001") => {
            report.add(
                path_field,
                format!("referenced {kind} path escapes the project boundary"),
            );
        }
        Err(error) => return Err(error),
        Ok(Some((dir, name))) if is_sha256(expected) => {
            let file = project_fs::open_file(&dir, &name, false, false)?;
            report.ensure(
                sha256(file)? == expected,
                digest_field,
                format!("hash does not match the {kind}"),
            );
        }
        Ok(Some(_)) => {}
    }
    Ok(())
}

pub(super) fn sha256(mut file: File) -> io::Result<String> {
    let mut digest = Sha256::new();
    let mut bytes = [0; 16 * 1024];
    loop {
        let count = file.read(&mut bytes)?;
        if count == 0 {
            break;
        }
        digest.update(&bytes[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
