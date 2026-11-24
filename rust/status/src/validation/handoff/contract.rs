use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use super::super::{Report, SECRETS, has_duplicates, is_identifier, is_panel, items, qualified};
use super::{BATCHES, canonical_hash, compact_hash};

const ARTIFACT: &[&str] = &["path", "sha256"];
const JOB_IDENTITY: &[&str] = &[
    "subject_kind",
    "subject_id",
    "prompt_path",
    "prompt_sha256",
    "references",
    "requested_dimensions",
    "requested_aspect_ratio",
    "attempt_kind",
    "retry_limit",
    "batch_id",
    "target_path",
];
const ATTEMPT_KINDS: &[&str] = &["initial", "visual_retry", "transient_repeat"];
static ATTEMPT_FILE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(initial|transient_repeat|visual_retry)-0*[1-9][0-9]*\.(png|jpg|webp)$")
        .expect("retained raster filename")
});
static ASPECT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[1-9][0-9]*:[1-9][0-9]*$").expect("aspect ratio"));
static LOCATOR: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[A-Za-z][A-Za-z0-9+.-]*:\S+").expect("embedded URL"));

fn object<'a>(
    value: &'a Value,
    field: &str,
    fields: &[&str],
    report: &mut Report,
) -> Option<&'a Value> {
    if !value.is_object() {
        let issue = format!("{field} must be an object");
        report.add(&issue, &issue);
        return None;
    }
    report.object(value, field, fields, &[])?;
    Some(value)
}

fn identifier(value: &Value, field: &str, report: &mut Report) {
    report.ensure(
        value
            .as_str()
            .is_some_and(|id| is_identifier(id, 48, false)),
        field,
        "must be a stable lowercase identifier",
    );
}

pub(super) fn normalized(path: &str) -> bool {
    let bytes = path.as_bytes();
    !path.contains(['\\', '\0'])
        && !(bytes.get(1) == Some(&b':') && bytes[0].is_ascii_alphabetic())
        && !path.split('/').any(|part| matches!(part, "" | "." | ".."))
}

fn relative(value: &Value, field: &str, report: &mut Report) {
    if let Some(path) = value.as_str().filter(|path| !path.is_empty()) {
        report.ensure(
            normalized(path),
            field,
            "must be a normalized relative project path without traversal",
        );
    } else {
        report.add(field, "must be a relative project path");
    }
}

fn require(value: &Value, expected: &str, field: &str, report: &mut Report) {
    report.ensure(value == expected, field, format!("must equal {expected}"));
}

fn descriptor(value: &Value, field: &str, report: &mut Report) -> bool {
    if object(value, field, ARTIFACT, report).is_none() {
        return false;
    }
    relative(&value["path"], &qualified(field, "path"), report);
    report.sha(&value["sha256"], &qualified(field, "sha256"), false);
    true
}

fn job_status(value: &Value, field: &str, report: &mut Report) {
    report.ensure(
        matches!(
            value.as_str(),
            Some("missing" | "ready" | "completed" | "failed" | "stale")
        ),
        field,
        "unknown generation job status",
    );
}

pub(in crate::validation) fn manifest(value: &Value, report: &mut Report) {
    let fields = &[
        "schema_version",
        "project_schema_version",
        "project_id",
        "stage",
        "locked_scope_sha256",
        "batches",
        "jobs",
        "required_artifacts",
    ];
    report.object(value, "", fields, &[]);
    require(&value["schema_version"], "1.0", "schema_version", report);
    require(
        &value["project_schema_version"],
        "1.1",
        "project_schema_version",
        report,
    );
    identifier(&value["project_id"], "project_id", report);
    report.ensure(
        value["stage"].as_str().is_some_and(|stage| {
            super::super::planning::LINEAR.contains(&stage)
                || matches!(stage, "BLOCKED" | "COMPLETE_WITH_WARNINGS")
        }),
        "stage",
        "unknown lifecycle stage",
    );
    report.sha(&value["locked_scope_sha256"], "locked_scope_sha256", false);
    if descriptor(&value["batches"], "batches", report) {
        require(&value["batches"]["path"], BATCHES, "batches.path", report);
    }
    if let Some(jobs) = report.array(&value["jobs"], "jobs") {
        let mut ids = Vec::new();
        for (index, item) in jobs.iter().enumerate() {
            let field = format!("jobs[{index}]");
            if object(
                item,
                &field,
                &["job_id", "path", "sha256", "status"],
                report,
            )
            .is_none()
            {
                continue;
            }
            report.sha(&item["job_id"], &qualified(&field, "job_id"), false);
            relative(&item["path"], &qualified(&field, "path"), report);
            if let Some(id) = item["job_id"].as_str() {
                ids.push(id);
                require(
                    &item["path"],
                    &format!("generation/jobs/{id}.json"),
                    &qualified(&field, "path"),
                    report,
                );
            }
            report.sha(&item["sha256"], &qualified(&field, "sha256"), false);
            job_status(&item["status"], &qualified(&field, "status"), report);
        }
        report.ensure(!has_duplicates(&ids), "jobs", "job IDs must be unique");
    }
    if let Some(artifacts) = report.array(&value["required_artifacts"], "required_artifacts") {
        let mut paths = Vec::new();
        for (index, item) in artifacts.iter().enumerate() {
            if descriptor(item, &format!("required_artifacts[{index}]"), report)
                && let Some(path) = item["path"].as_str()
            {
                paths.push(path);
            }
        }
        report.ensure(
            !has_duplicates(&paths),
            "required_artifacts",
            "paths must be unique",
        );
    }
}

pub(super) fn batches(value: &Value, report: &mut Report) {
    report.object(value, "", &["schema_version", "batches"], &[]);
    require(&value["schema_version"], "1.0", "schema_version", report);
    let Some(batches) = report.array(&value["batches"], "batches") else {
        return;
    };
    let mut batch_ids = Vec::new();
    let mut job_ids = Vec::new();
    for (index, item) in batches.iter().enumerate() {
        let field = format!("batches[{index}]");
        if object(item, &field, &["batch_id", "kind", "job_ids"], report).is_none() {
            continue;
        }
        identifier(&item["batch_id"], &qualified(&field, "batch_id"), report);
        batch_ids.extend(item["batch_id"].as_str());
        report.ensure(
            matches!(item["kind"].as_str(), Some("reference" | "panel")),
            &qualified(&field, "kind"),
            "must be reference or panel",
        );
        let Some(ids) = item["job_ids"].as_array().filter(|ids| !ids.is_empty()) else {
            report.add(qualified(&field, "job_ids"), "must be a non-empty array");
            continue;
        };
        let displayed: Vec<_> = ids
            .iter()
            .map(|id| {
                id.as_str()
                    .map_or_else(|| super::super::repr(id), str::to_owned)
            })
            .collect();
        report.ensure(
            !has_duplicates(&displayed.iter().map(String::as_str).collect::<Vec<_>>()),
            &qualified(&field, "job_ids"),
            "job IDs must be unique",
        );
        for (job_index, id) in ids.iter().enumerate() {
            report.sha(id, &format!("{field}.job_ids[{job_index}]"), false);
            job_ids.extend(id.as_str());
        }
    }
    report.ensure(
        !has_duplicates(&batch_ids),
        "batches",
        "batch IDs must be unique",
    );
    report.ensure(
        !has_duplicates(&job_ids),
        "batches",
        "a job ID may belong to only one batch",
    );
}

pub(super) fn job(value: &Value, report: &mut Report) {
    let before = report.issues.len();
    let mut fields = JOB_IDENTITY.to_vec();
    fields.extend(["schema_version", "job_id"]);
    report.object(value, "", &fields, &[]);
    require(&value["schema_version"], "1.0", "schema_version", report);
    report.sha(&value["job_id"], "job_id", false);
    let kind = value["subject_kind"].as_str();
    let id = value["subject_id"].as_str();
    report.ensure(
        matches!(kind, Some("reference" | "panel")),
        "subject_kind",
        "must be reference or panel",
    );
    if kind == Some("panel") {
        report.ensure(
            id.is_some_and(is_panel),
            "subject_id",
            "panel jobs must use a pNN-NN panel ID",
        );
    } else {
        identifier(&value["subject_id"], "subject_id", report);
    }
    relative(&value["prompt_path"], "prompt_path", report);
    if value["prompt_path"].is_string()
        && let Some(id) = id
    {
        match kind {
            Some("panel") => require(
                &value["prompt_path"],
                &format!("prompts/panels/{id}.txt"),
                "prompt_path",
                report,
            ),
            Some("reference") => require(
                &value["prompt_path"],
                &format!("prompts/references/{id}.txt"),
                "prompt_path",
                report,
            ),
            _ => {}
        }
    }
    report.sha(&value["prompt_sha256"], "prompt_sha256", false);
    if let Some(references) = report.array(&value["references"], "references") {
        let mut paths = Vec::new();
        for (index, reference) in references.iter().enumerate() {
            let field = format!("references[{index}]");
            if descriptor(reference, &field, report)
                && let Some(path) = reference["path"].as_str()
            {
                paths.push(path);
                let parts: Vec<_> = path.split('/').collect();
                let valid = matches!(parts.as_slice(), ["references", "characters" | "scenes", name]
                    if name.strip_suffix(".png").is_some_and(|id| is_identifier(id, 48, false)));
                report.ensure(
                    valid,
                    &qualified(&field, "path"),
                    "must name a canonical local character or scene PNG reference",
                );
            }
        }
        report.ensure(
            !has_duplicates(&paths),
            "references",
            "paths must be unique",
        );
    }
    let dimensions = &value["requested_dimensions"];
    if !dimensions.is_null()
        && object(
            dimensions,
            "requested_dimensions",
            &["width", "height"],
            report,
        )
        .is_some()
    {
        for field in ["width", "height"] {
            let key = qualified("requested_dimensions", field);
            match positive_integer(&dimensions[field]) {
                None => report.add(key, "must be a positive integer"),
                Some(1..512) => report.add(key, "must be at least 512px"),
                _ => {}
            }
        }
    }
    let sizes = positive_integer(&dimensions["width"]).zip(positive_integer(&dimensions["height"]));
    if let Some((w, h)) = sizes {
        report.ensure(
            w.saturating_mul(h) <= 1600 * 2400 * 16,
            "requested_dimensions",
            "exceeds the decoded pixel limit",
        );
    }
    if !value["requested_aspect_ratio"].is_null() {
        let ratio = value["requested_aspect_ratio"]
            .as_str()
            .filter(|value| ASPECT.is_match(value));
        report.ensure(
            ratio.is_some(),
            "requested_aspect_ratio",
            "must be null or a positive W:H ratio",
        );
        if let (Some(ratio), Some((w, h))) = (ratio, sizes) {
            match aspect_matches(w, h, ratio) {
                None => report.add("requested_aspect_ratio", "components are too large"),
                Some(false) => report.add(
                    "requested_aspect_ratio",
                    "must match requested_dimensions width:height",
                ),
                Some(true) => {}
            }
        }
    }
    let attempt = value["attempt_kind"].as_str();
    report.ensure(
        attempt.is_some_and(|kind| ATTEMPT_KINDS.contains(&kind)),
        "attempt_kind",
        "unknown retained-attempt kind",
    );
    report.integer(&value["retry_limit"], "retry_limit", 0, 2);
    identifier(&value["batch_id"], "batch_id", report);
    relative(&value["target_path"], "target_path", report);
    if let (Some(kind @ ("panel" | "reference")), Some(id), Some(attempt), Some(target)) =
        (kind, id, attempt, value["target_path"].as_str())
    {
        let prefix = format!("{kind}s/attempts/{id}/");
        let valid = target.strip_prefix(&prefix).is_some_and(|filename| {
            let pattern = format!("{attempt}-");
            filename
                .strip_prefix(&pattern)
                .is_some_and(valid_attempt_suffix)
        });
        report.ensure(
            valid,
            "target_path",
            "must name the subject's retained raster attempt namespace",
        );
    }
    if report.issues.len() == before {
        report.ensure(
            value["job_id"] == derive_job_id(value),
            "job_id",
            "does not match canonical job inputs",
        );
    }
}

pub(super) fn derive_job_id(job: &Value) -> String {
    let identity: Map<_, _> = JOB_IDENTITY
        .iter()
        .map(|&key| (key.to_owned(), job[key].clone()))
        .collect();
    compact_hash(&json!({"contract_version": "1.0", "job": identity}))
}

fn valid_attempt_suffix(suffix: &str) -> bool {
    let Some((index, ext)) = suffix.split_once('.') else {
        return false;
    };
    !index.is_empty()
        && index.bytes().all(|byte| byte.is_ascii_digit())
        && index.bytes().any(|byte| byte != b'0')
        && matches!(ext, "png" | "jpg" | "webp")
}

fn positive_integer(value: &Value) -> Option<u64> {
    let number = value.as_number()?.as_str();
    (number.bytes().all(|digit| digit.is_ascii_digit()) && number != "0")
        .then(|| number.parse().unwrap_or(u64::MAX))
}

pub(super) fn aspect_matches(width: u64, height: u64, ratio: &str) -> Option<bool> {
    let (w, h) = ratio.split_once(':')?;
    if w.len() > 4300 || h.len() > 4300 {
        return None;
    }
    Some(multiply_decimal(w, height) == multiply_decimal(h, width))
}

fn multiply_decimal(value: &str, factor: u64) -> Vec<u8> {
    let mut carry = 0;
    let mut product = Vec::with_capacity(value.len());
    for digit in value.bytes().rev() {
        carry += u128::from(digit - b'0') * u128::from(factor);
        product.push((carry % 10) as u8);
        carry /= 10;
    }
    while carry > 0 {
        product.push((carry % 10) as u8);
        carry /= 10;
    }
    product
}

pub(super) fn receipt(value: &Value, report: &mut Report) {
    report.object(
        value,
        "",
        &[
            "schema_version",
            "attempt_id",
            "job_id",
            "job_sha256",
            "raster_path",
            "raster_sha256",
            "executor_kind",
            "executor_id",
            "provider",
            "model",
            "capabilities_used",
            "outcome",
            "category",
        ],
        &[],
    );
    require(&value["schema_version"], "1.0", "schema_version", report);
    identifier(&value["attempt_id"], "attempt_id", report);
    for field in ["job_id", "job_sha256"] {
        report.sha(&value[field], field, false);
    }
    let path = &value["raster_path"];
    let hash = &value["raster_sha256"];
    if path.is_null() != hash.is_null() {
        report.add(
            "raster_path and raster_sha256",
            "must both be null or both populated",
        );
    }
    if !path.is_null() && !hash.is_null() {
        relative(path, "raster_path", report);
        if let Some(path) = path.as_str() {
            let parts: Vec<_> = path.split('/').collect();
            let valid = match parts.as_slice() {
                ["panels", "attempts", id, filename] => {
                    is_panel(id) && ATTEMPT_FILE.is_match(filename)
                }
                ["references", "attempts", id, filename] => {
                    is_identifier(id, 48, false) && ATTEMPT_FILE.is_match(filename)
                }
                _ => false,
            };
            report.ensure(valid, "raster_path", "must name a retained raster attempt");
        }
        report.sha(hash, "raster_sha256", false);
    } else if value["outcome"] == "success" {
        report.add(
            "raster_path and raster_sha256",
            "must be populated when outcome is success",
        );
    }
    report.ensure(
        matches!(
            value["executor_kind"].as_str(),
            Some("native-tool" | "external-tool")
        ),
        "executor_kind",
        "must be native-tool or external-tool",
    );
    identifier(&value["executor_id"], "executor_id", report);
    for field in ["provider", "model"] {
        safe_label(&value[field], field, report);
    }
    let flags = &["reference_images", "dimensions", "localized_edit"];
    if let Some(capabilities) = object(
        &value["capabilities_used"],
        "capabilities_used",
        flags,
        report,
    ) {
        for flag in flags {
            report.ensure(
                capabilities[*flag].is_boolean(),
                &qualified("capabilities_used", flag),
                "must be boolean",
            );
        }
    }
    report.ensure(
        matches!(value["outcome"].as_str(), Some("success" | "failure")),
        "outcome",
        "must be success or failure",
    );
    if let Some(category) = value["category"]
        .as_str()
        .filter(|category| is_identifier(category, 64, true))
    {
        report.ensure(
            !SECRETS.is_match(category),
            "category",
            "must not contain secrets or credentials",
        );
    } else {
        report.add("category", "must be a stable sanitized category");
    }
}

fn private_path(value: &str) -> bool {
    let not_space =
        |character: Option<char>| character.is_some_and(|character| !character.is_whitespace());
    for (index, character) in value.char_indices() {
        if value[..index]
            .chars()
            .next_back()
            .is_some_and(|prior| prior.is_ascii_alphanumeric() || matches!(prior, ':' | '/' | '\\'))
        {
            continue;
        }
        let mut rest = value[index + character.len_utf8()..].chars();
        let next = rest.next();
        match character {
            '~' if matches!(next, Some('/' | '\\')) => return true,
            '/' if next != Some('/') && not_space(next) => return true,
            '\\' if not_space(rest.find(|character| *character != '\\').or(next)) => return true,
            letter
                if letter.is_ascii_alphabetic()
                    && next == Some(':')
                    && matches!(rest.next(), Some('/' | '\\')) =>
            {
                return true;
            }
            _ => {}
        }
    }
    false
}

fn safe_label(value: &Value, field: &str, report: &mut Report) {
    if value.is_null() {
        return;
    }
    let Some(label) = value
        .as_str()
        .filter(|text| !text.is_empty() && text.chars().count() <= 128)
    else {
        report.add(
            field,
            "must be a non-empty string of at most 128 characters",
        );
        return;
    };
    report.ensure(
        !label.chars().any(char::is_control),
        field,
        "must not contain control characters",
    );
    report.ensure(
        !SECRETS.is_match(label),
        field,
        "must not contain secrets or credentials",
    );
    report.ensure(
        !private_path(label),
        field,
        "must not contain a private absolute path",
    );
    let candidate = LOCATOR
        .find_iter(label)
        .find(|hit| {
            !label[..hit.start()]
                .chars()
                .next_back()
                .is_some_and(|prior| {
                    prior.is_ascii_alphanumeric() || matches!(prior, '+' | '.' | '-')
                })
        })
        .map_or(label, |hit| hit.as_str());
    let candidate = candidate.trim_start_matches(|c| c <= ' ');
    if let Some((scheme, remainder)) = candidate.split_once(':')
        && scheme.starts_with(|c: char| c.is_ascii_alphabetic())
        && scheme
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'.' | b'-'))
    {
        let authority = remainder
            .strip_prefix("//")
            .and_then(|rest| rest.split(['/', '?', '#']).next())
            .unwrap_or_default();
        let message = if authority.contains('[') != authority.contains(']') {
            "must not contain a malformed URL"
        } else if authority.contains('@') {
            "must not contain URL credentials"
        } else {
            "must not contain an endpoint URL"
        };
        report.add(field, message);
    }
}

pub(super) fn attempt_id(job: &str, ordinal: u64) -> String {
    format!(
        "attempt-{}",
        &compact_hash(&json!({"attempt": ordinal, "job_id": job}))[..40]
    )
}

pub(super) fn digest(job: &Value) -> String {
    canonical_hash(job)
}

pub(super) fn references(job: &Value) -> impl Iterator<Item = (&str, &Value)> {
    items(&job["references"])
        .iter()
        .filter_map(|item| Some((item["path"].as_str()?, &item["sha256"])))
}
