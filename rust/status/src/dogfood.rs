use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

use crate::cli::Arguments;
use crate::error::{Error, Result, contract};
use crate::project_fs::{self, ProjectLock};
use crate::resume::{PUBLIC_REASONS, definitions};
use crate::validation::{self, handoff, keys, terminal};
use crate::{init, manifest};

const KIND: &str = "comic-sol-dogfood-report";
const VERSION: &str = "1.0";
const MAX_MINUTES: u64 = 10_080;
const MAX_FAILED: u64 = 1_000;
const MAX_COUNT: u64 = 1_000_000;
const MAX_RECEIPTS: usize = 10_000;
const MAX_EVENT_LINES: usize = 100_000;
const MAX_READ_BYTES: u64 = 128 * 1024 * 1024;
const FLAGS: &[&str] = &["dimensions", "localized_edit", "reference_images"];
const FRICTION: &[&str] = &[
    "installation",
    "setup",
    "first-project",
    "story-planning",
    "image-generation",
    "character-consistency",
    "handoff",
    "blocked-recovery",
    "resume",
    "lettering",
    "composition",
    "pdf-export",
    "filesystem",
    "documentation",
    "performance",
    "other",
];
const LIMITATIONS: &[&str] = &[
    "Local opt-in report; no automatic upload.",
    "Derived only from persisted allowlisted project evidence.",
    "Successful resumes count committed project.resumed events; failed attempts are self-reported.",
    "Handoff route is omitted when persisted evidence cannot distinguish shared-folder from archive.",
];
const COUNTS: &[&str] = &[
    "page_count",
    "panel_count",
    "successful_resumes",
    "handoff_count",
    "handoff_completions",
    "unresolved_warning_count",
    "manual_override_count",
];
const CREATOR: &[&str] = &[
    "setup_minutes",
    "first_project_minutes",
    "pdf_minutes",
    "manual_intervention",
    "would_use_again",
    "failed_resume_attempts",
    "friction_categories",
    "cohort_alias",
];
static CATEGORY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$").expect("bounded category"));
static RELEASE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$").expect("bounded release"));
static ALIAS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$").expect("bounded alias"));

pub fn run(args: &Arguments) -> Result<Value> {
    if args.command == "dogfood.validate" {
        let path = init::expand(args.paths("report_path"));
        let read = || {
            let bytes = init::read_file(&path, contract::MAX_JSON_BYTES)?;
            let text = std::str::from_utf8(&bytes)
                .map_err(|_| Error::dogfood("dogfood report file must be UTF-8"))?;
            manifest::parse_document(text, "dogfood report")
        };
        let report =
            read().map_err(|_| Error::dogfood("dogfood report file is invalid or unreadable"))?;
        validate(&report, true)?;
        return Ok(report);
    }
    let integer = |key| {
        args.text(key)
            .and_then(|text| text.replace('_', "").parse::<u64>().ok())
    };
    let mut creator = json!({
        "setup_minutes": integer("setup_minutes"),
        "first_project_minutes": integer("first_project_minutes"),
        "pdf_minutes": integer("pdf_minutes"),
        "manual_intervention": args.text("manual_intervention") == Some("yes"),
        "would_use_again": args.text("would_use_again") == Some("yes"),
        "failed_resume_attempts": integer("failed_resume_attempts"),
        "friction_categories": args.values("friction").filter_map(|value| value.to_str()).collect::<Vec<_>>(),
        "cohort_alias": args.text("cohort_alias"),
    });
    creator["friction_categories"] = normalize_creator(&creator)?;
    let project = init::expand(args.paths("project_dir"));
    let (version, derived) = derive(&project)?;
    let report = json!({
        "kind": KIND, "schema_version": VERSION, "comic_sol_version": env!("COMIC_SOL_VERSION"),
        "project_schema_version": version, "derived": derived, "creator": creator,
        "consent": {"share_report": args.has("consent_to_share")}, "limitations": LIMITATIONS,
    });
    let persist = args.command == "dogfood.report";
    validate(&report, persist)?;
    if persist {
        let output = init::expand(args.paths("output_path"));
        project_fs::write_external(&output, &project, &manifest::canonical(&report)).map_err(
            |error| {
                let detail = match error.detail() {
                    Some("project path must not contain symlinks or reparse points") => {
                        "output path must not contain symlinks or reparse points"
                    }
                    Some(detail) => detail,
                    None => "output cannot be written safely",
                };
                Error::dogfood(format!("security-error: {detail}"))
            },
        )?;
    }
    Ok(report)
}

fn exact(value: &Value, fields: &[&str], name: &str) -> Result<()> {
    let Some(object) = value.as_object() else {
        return Err(Error::dogfood(format!("{name} must be an object")));
    };
    if keys(value, fields) {
        return Ok(());
    }
    let mut missing: Vec<_> = fields
        .iter()
        .copied()
        .filter(|key| !object.contains_key(*key))
        .collect();
    missing.sort_unstable();
    let unknown: Vec<_> = object
        .keys()
        .map(String::as_str)
        .filter(|key| !fields.contains(key))
        .collect();
    let mut details = Vec::new();
    if !missing.is_empty() {
        details.push(format!("missing {}", missing.join(", ")));
    }
    if !unknown.is_empty() {
        details.push(format!("unknown {}", unknown.join(", ")));
    }
    Err(Error::dogfood(format!(
        "{name} fields are invalid: {}",
        details.join("; ")
    )))
}

fn bounded(value: &Value, name: &str, maximum: u64) -> Result<u64> {
    value
        .as_u64()
        .filter(|&number| number <= maximum)
        .ok_or_else(|| {
            Error::dogfood(format!(
                "{name} must be an integer from 0 through {maximum}"
            ))
        })
}

fn boolean(value: &Value, name: &str) -> Result<bool> {
    value
        .as_bool()
        .ok_or_else(|| Error::dogfood(format!("{name} must be a boolean")))
}

fn normalize_creator(creator: &Value) -> Result<Value> {
    for name in &CREATOR[..3] {
        bounded(&creator[name], name, MAX_MINUTES)?;
    }
    for name in &CREATOR[3..5] {
        boolean(&creator[name], name)?;
    }
    if !creator["failed_resume_attempts"].is_null() {
        bounded(
            &creator["failed_resume_attempts"],
            "failed_resume_attempts",
            MAX_FAILED,
        )?;
    }
    let friction = creator["friction_categories"]
        .as_array()
        .ok_or_else(|| Error::dogfood("friction_categories must be an array"))?;
    let mut categories = Vec::new();
    for value in friction {
        let category = value
            .as_str()
            .filter(|value| FRICTION.contains(value))
            .ok_or_else(|| Error::dogfood("friction category is not in the bounded allowlist"))?;
        if !categories.contains(&category) {
            categories.push(category);
        }
    }
    if !creator["cohort_alias"].is_null() && !matches_pattern(&creator["cohort_alias"], &ALIAS) {
        return Err(Error::dogfood(
            "cohort_alias must be a lowercase slug of at most 48 characters",
        ));
    }
    Ok(json!(categories))
}

fn matches_pattern(value: &Value, pattern: &Regex) -> bool {
    value.as_str().is_some_and(|text| pattern.is_match(text))
}

fn allowed_status(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        matches!(text, "BLOCKED" | "COMPLETE_WITH_WARNINGS")
            || validation::linear_statuses().contains(&text)
    })
}

fn allowed_list(value: &Value, name: &str, allowed: &[&str]) -> Result<()> {
    let list = value
        .as_array()
        .and_then(|items| items.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
        .ok_or_else(|| Error::dogfood(format!("{name} must be an array of bounded strings")))?;
    if list.windows(2).any(|pair| pair[0] >= pair[1]) {
        return Err(Error::dogfood(format!(
            "{name} must be sorted and de-duplicated"
        )));
    }
    if list.iter().any(|item| !allowed.contains(item)) {
        return Err(Error::dogfood(format!(
            "{name} contains an unsupported category"
        )));
    }
    Ok(())
}

fn validate(report: &Value, require_consent: bool) -> Result<()> {
    exact(
        report,
        &[
            "kind",
            "schema_version",
            "comic_sol_version",
            "project_schema_version",
            "derived",
            "creator",
            "consent",
            "limitations",
        ],
        "report",
    )?;
    if report["kind"] != KIND || report["schema_version"] != VERSION {
        return Err(Error::dogfood(
            "dogfood report kind/schema must be comic-sol-dogfood-report 1.0",
        ));
    }
    if !matches_pattern(&report["comic_sol_version"], &RELEASE) {
        return Err(Error::dogfood("comic_sol_version is invalid"));
    }
    if !manifest::supported_version(&report["project_schema_version"]) {
        return Err(Error::dogfood("project_schema_version is not readable"));
    }
    if report["limitations"] != json!(LIMITATIONS) {
        return Err(Error::dogfood(
            "limitations must match the fixed privacy contract",
        ));
    }
    let creator = &report["creator"];
    exact(creator, CREATOR, "creator")?;
    if normalize_creator(creator)? != creator["friction_categories"] {
        return Err(Error::dogfood(
            "creator fields must be canonical and de-duplicated",
        ));
    }
    let consent = &report["consent"];
    exact(consent, &["share_report"], "consent")?;
    if !boolean(&consent["share_report"], "share_report")? && require_consent {
        return Err(Error::dogfood(
            "explicit consent to share is required for a persisted report",
        ));
    }
    let derived = &report["derived"];
    let mut fields = COUNTS.to_vec();
    fields.extend([
        "terminal_status",
        "completed_stages",
        "generation_attempts",
        "retry_rate",
        "blocked_categories",
        "handoff_routes",
        "executor_kinds",
        "executor_capabilities",
        "final_pdf_verified",
    ]);
    exact(derived, &fields, "derived")?;
    if !allowed_status(&derived["terminal_status"]) {
        return Err(Error::dogfood(
            "terminal_status must be a supported lifecycle status",
        ));
    }
    for name in COUNTS {
        bounded(&derived[name], name, MAX_COUNT)?;
    }
    if derived["handoff_completions"].as_u64() > derived["handoff_count"].as_u64() {
        return Err(Error::dogfood(
            "handoff completions cannot exceed prepared handoff units",
        ));
    }
    let completed = validation::items(&derived["completed_stages"]);
    let canonical: Vec<_> = definitions()
        .iter()
        .map(|stage| stage.name)
        .filter(|name| completed.iter().any(|item| item == *name))
        .collect();
    if derived["completed_stages"] != json!(canonical) {
        return Err(Error::dogfood(
            "completed_stages must follow the canonical stage order",
        ));
    }
    for (name, values) in [
        ("blocked_categories", PUBLIC_REASONS),
        (
            "executor_kinds",
            &["native-tool", "external-tool", "handoff"] as &[_],
        ),
        ("handoff_routes", &["shared-folder", "archive"] as &[_]),
    ] {
        allowed_list(&derived[name], name, values)?;
    }
    let capabilities = &derived["executor_capabilities"];
    exact(capabilities, FLAGS, "executor_capabilities")?;
    for flag in FLAGS {
        boolean(&capabilities[flag], flag)?;
    }
    boolean(&derived["final_pdf_verified"], "final_pdf_verified")?;
    let attempts = &derived["generation_attempts"];
    exact(
        attempts,
        &["initial", "retries", "total"],
        "generation_attempts",
    )?;
    let initial = bounded(&attempts["initial"], "initial attempts", MAX_COUNT)?;
    let retries = bounded(&attempts["retries"], "retry attempts", MAX_COUNT)?;
    let total = bounded(&attempts["total"], "total attempts", MAX_COUNT)?;
    if initial + retries != total {
        return Err(Error::dogfood("generation attempt totals are inconsistent"));
    }
    let rate = &derived["retry_rate"];
    exact(rate, &["numerator", "denominator"], "retry_rate")?;
    if rate["numerator"].as_u64() != Some(retries) || rate["denominator"].as_u64() != Some(total) {
        return Err(Error::dogfood("retry rate evidence is inconsistent"));
    }
    Ok(())
}

fn read_object(root: &File, relative: &str, optional: bool) -> Result<Value> {
    let name = relative.rsplit('/').next().expect("artifact name");
    let read = || {
        let path = project_fs::relative_path(relative)?;
        let Some((parent, leaf)) = project_fs::target(root, &path, false)? else {
            return Ok(None);
        };
        if project_fs::metadata(&parent, Path::new(leaf))?.is_none() {
            return Ok(None);
        }
        Ok(Some(
            manifest::read_document(&parent, Path::new(leaf), name)?.0,
        ))
    };
    let object = match read()
        .map_err(|_: Error| Error::dogfood(format!("invalid bounded dogfood evidence: {name}")))?
    {
        Some(value) => value,
        None if optional => return Ok(json!({})),
        None => {
            return Err(Error::dogfood(format!(
                "required dogfood evidence is missing: {relative}"
            )));
        }
    };
    if !object.is_object() {
        return Err(Error::dogfood(format!(
            "dogfood evidence must be an object: {name}"
        )));
    }
    Ok(object)
}

fn derive(project: &Path) -> Result<(Value, Value)> {
    let info = fs::symlink_metadata(project)
        .map_err(|_| Error::dogfood("project directory is not readable"))?;
    if info.is_symlink() || reparse_point(&info) {
        return Err(Error::dogfood(
            "project directory must not be a symlink or reparse point",
        ));
    }
    if !info.is_dir() {
        return Err(Error::dogfood("project directory is not readable"));
    }
    let root = project_fs::project_root(project)
        .map_err(|_| Error::dogfood("project directory is not readable"))?;
    let _lock = ProjectLock::read_only(&root)
        .map_err(|_| Error::dogfood("project lock cannot be opened safely"))?
        .ok_or_else(|| Error::dogfood("project is busy; retry after the active operation"))?;
    let directory = |path| project_directory(&root, path);
    if let Some(transactions) = directory("logs/transactions")
        .map_err(|_| Error::dogfood("project lock cannot be opened safely"))?
    {
        let names = project_fs::names(&transactions)
            .map_err(|_| Error::dogfood("project transaction evidence is unreadable"))?;
        if names
            .iter()
            .any(|name| name.to_str().is_some_and(validation::is_decimal))
        {
            return Err(Error::dogfood(
                "project has an incomplete transaction; recover it before creating a report",
            ));
        }
    }
    let manifest = read_object(&root, "project.json", false)?;
    let version = manifest
        .get("schema_version")
        .cloned()
        .unwrap_or_else(|| contract::LEGACY_PROJECT_SCHEMA_VERSION.into());
    if !manifest::supported_version(&version) {
        return Err(Error::dogfood(
            "project schema version is not readable by this release",
        ));
    }
    validation::bound_manifest(&manifest)
        .map_err(|_| Error::dogfood("project manifest evidence is invalid"))?;
    let page_count = bounded(&manifest["settings"]["page_count"], "page count", MAX_COUNT)?;
    let panel_count = bounded(
        &manifest["settings"]["panel_count"],
        "panel count",
        MAX_COUNT,
    )?;
    let (initial, retries) = generation_counts(&root)?;
    let total = initial + retries;
    let mut derived = event_counts(&root, &manifest)?;
    let (kinds, flags) = receipt_metrics(&root)?;
    let extras = json!({
        "terminal_status": manifest["status"], "page_count": page_count, "panel_count": panel_count,
        "generation_attempts": {"initial": initial, "retries": retries, "total": total},
        "retry_rate": {"numerator": retries, "denominator": total},
        "handoff_routes": [], "executor_kinds": kinds, "executor_capabilities": flags,
        "final_pdf_verified": verified_pdf(&root, &manifest, page_count as usize),
        "unresolved_warning_count": validation::items(&manifest["warnings"]).len(),
    });
    derived
        .as_object_mut()
        .expect("derived object")
        .extend(extras.as_object().expect("metrics object").clone());
    Ok((version, derived))
}

fn reparse_point(_info: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        _info.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    false
}

fn project_directory(root: &File, path: &str) -> Result<Option<File>> {
    let Some((parent, name)) = project_fs::target(root, Path::new(path), false)? else {
        return Ok(None);
    };
    let name = Path::new(name);
    if project_fs::metadata(&parent, name)?.is_none() {
        return Ok(None);
    }
    Ok(Some(project_fs::open_dir(&parent, name)?))
}

fn generation_counts(root: &File) -> Result<(u64, u64)> {
    let counters = read_object(root, "logs/generation-counters.json", true)?;
    if counters.as_object().expect("counters object").is_empty() {
        return Ok((0, 0));
    }
    if !keys(
        &counters,
        &["schema_version", "global_extra_calls", "panels"],
    ) {
        return Err(Error::dogfood(
            "generation counter evidence has invalid fields",
        ));
    }
    if counters["schema_version"] != VERSION {
        return Err(Error::dogfood("generation counter schema is unsupported"));
    }
    let panels = counters["panels"]
        .as_object()
        .ok_or_else(|| Error::dogfood("generation counter panels must be an object"))?;
    if panels.len() > MAX_RECEIPTS {
        return Err(Error::dogfood(
            "generation counter collection exceeds the report bound",
        ));
    }
    let mut initial = 0;
    let mut retries = 0;
    for counts in panels.values() {
        if !keys(counts, &["initial", "transient_repeats", "visual_retries"]) {
            return Err(Error::dogfood(
                "generation panel counters have invalid fields",
            ));
        }
        initial += bounded(&counts["initial"], "initial attempts", 1)?;
        retries += bounded(&counts["transient_repeats"], "transient repeats", 1)?;
        retries += bounded(&counts["visual_retries"], "visual retries", 2)?;
    }
    if bounded(
        &counters["global_extra_calls"],
        "global extra calls",
        MAX_COUNT,
    )? != retries
    {
        return Err(Error::dogfood("generation retry counters are inconsistent"));
    }
    if initial + retries > MAX_COUNT {
        return Err(Error::dogfood(
            "generation attempt total exceeds the report bound",
        ));
    }
    Ok((initial, retries))
}

fn contained_bytes(root: &File, relative: &str) -> Result<Option<Vec<u8>>> {
    let Some((parent, leaf)) = validation::artifact(root, relative)? else {
        return Ok(None);
    };
    let file = project_fs::open_file(&parent, &leaf, false, false)?;
    let mut bytes = Vec::new();
    file.take(MAX_READ_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_READ_BYTES {
        return Err(Error::limit("the artifact size limit"));
    }
    Ok(Some(bytes))
}

fn event_counts(root: &File, manifest: &Value) -> Result<Value> {
    let bytes = contained_bytes(root, "logs/events.jsonl")
        .map_err(|_| Error::dogfood("event evidence cannot be read safely"))?
        .unwrap_or_default();
    let text =
        std::str::from_utf8(&bytes).map_err(|_| Error::dogfood("event evidence must be UTF-8"))?;
    let mut blocked = BTreeSet::new();
    let mut completed = BTreeSet::new();
    let mut resumes = 0;
    let mut handoffs = 0;
    let mut completions = 0;
    let mut overrides = 0;
    let mut record_reason = |value: &Value| {
        if let Some(reason) = value.as_str().filter(|text| CATEGORY.is_match(text)) {
            let safe = PUBLIC_REASONS
                .iter()
                .copied()
                .find(|&name| name == reason)
                .unwrap_or("other");
            blocked.insert(safe);
        }
    };
    if text.lines().count() > MAX_EVENT_LINES {
        return Err(Error::dogfood(
            "event evidence exceeds the report line bound",
        ));
    }
    for line in text.lines().filter(|line| !line.is_empty()) {
        let event = manifest::parse_document(line, "events.jsonl event")
            .map_err(|_| Error::dogfood("event evidence contains invalid JSON"))?;
        if !event.is_object() {
            return Err(Error::dogfood("event evidence must contain objects"));
        }
        let details = &event["details"];
        if !details.is_object() {
            continue;
        }
        let blocked_status = |key| {
            details[key]
                .as_str()
                .is_some_and(|text| text.eq_ignore_ascii_case("BLOCKED"))
        };
        match event["event"].as_str() {
            Some("project.transitioned") if blocked_status("to") => {
                record_reason(&details["blocked_reason"])
            }
            Some("project.resumed")
                if blocked_status("from")
                    && allowed_status(&details["to"])
                    && !blocked_status("to")
                    && matches_pattern(&details["blocked_reason"], &CATEGORY) =>
            {
                resumes += 1;
                record_reason(&details["blocked_reason"]);
            }
            Some("stage.recorded") => {
                if let Some(stage) = definitions()
                    .iter()
                    .find(|stage| details["action"] == stage.name)
                {
                    completed.insert(stage.name);
                }
            }
            Some("handoff.prepared") => {
                handoffs += bounded(&details["count"], "prepared handoff count", MAX_COUNT)?;
            }
            Some("handoff.result-accepted") => completions += 1,
            Some("panel.overridden") => overrides += 1,
            _ => {}
        }
    }
    if manifest["status"] == "BLOCKED" {
        record_reason(&manifest["blocked_reason"]);
    }
    for (count, name) in [
        (resumes, "successful resume count"),
        (handoffs, "handoff count"),
        (completions, "handoff completion count"),
        (overrides, "manual override count"),
    ] {
        bounded(&json!(count), name, MAX_COUNT)?;
    }
    let stages: Vec<_> = definitions()
        .iter()
        .map(|stage| stage.name)
        .filter(|name| completed.contains(name))
        .collect();
    Ok(json!({
        "blocked_categories": blocked, "completed_stages": stages, "successful_resumes": resumes,
        "handoff_count": handoffs, "handoff_completions": completions, "manual_override_count": overrides,
    }))
}

fn receipt_metrics(root: &File) -> Result<(BTreeSet<String>, Value)> {
    let boundary = || Error::dogfood("project lock cannot be opened safely");
    let mut kinds = BTreeSet::new();
    let mut flags =
        json!({"dimensions": false, "localized_edit": false, "reference_images": false});
    let Some(dir) = project_directory(root, "generation/receipts").map_err(|_| boundary())? else {
        return Ok((kinds, flags));
    };
    let mut names = project_fs::names_bounded(&dir, MAX_RECEIPTS).map_err(|error| {
        if error.kind() == std::io::ErrorKind::InvalidData {
            Error::dogfood("generation receipt collection exceeds the report bound")
        } else {
            boundary()
        }
    })?;
    names.sort_unstable();
    for name in names.iter().filter_map(|name| name.to_str()) {
        let Some(stem) = name.strip_suffix(".json") else {
            continue;
        };
        let receipt = read_object(root, &format!("generation/receipts/{name}"), false)?;
        if !handoff::valid_receipt(&receipt) {
            return Err(Error::dogfood("generation receipt evidence is invalid"));
        }
        let id = receipt["job_id"].as_str().expect("validated job ID");
        if receipt["attempt_id"] != stem {
            return Err(Error::dogfood(
                "generation receipt identity binding is invalid",
            ));
        }
        let job = read_object(root, &format!("generation/jobs/{id}.json"), false)?;
        if !handoff::valid_job(&job) || job["job_id"] != id {
            return Err(Error::dogfood("generation receipt job binding is invalid"));
        }
        if receipt["job_sha256"] != handoff::job_digest(&job) {
            return Err(Error::dogfood("generation receipt job digest is stale"));
        }
        let limit = job["retry_limit"]
            .as_u64()
            .ok_or_else(|| Error::dogfood("generation receipt retry binding is invalid"))?;
        if !(1..=limit + 1).any(|ordinal| handoff::attempt_id(id, ordinal) == stem) {
            return Err(Error::dogfood(
                "generation receipt attempt binding is invalid",
            ));
        }
        if receipt["outcome"] == "success" {
            let path = receipt["raster_path"]
                .as_str()
                .ok_or_else(|| Error::dogfood("successful generation receipt is rasterless"))?;
            let bytes = contained_bytes(root, path).ok().flatten().ok_or_else(|| {
                Error::dogfood("successful generation receipt raster is unreadable")
            })?;
            if receipt["raster_sha256"] != validation::bytes_hash(&bytes) {
                return Err(Error::dogfood(
                    "successful generation receipt raster digest is stale",
                ));
            }
        }
        kinds.insert(
            receipt["executor_kind"]
                .as_str()
                .expect("validated executor kind")
                .to_owned(),
        );
        for name in FLAGS {
            if receipt["capabilities_used"][name] == true {
                flags[name] = true.into();
            }
        }
    }
    Ok((kinds, flags))
}

fn verified_pdf(root: &File, value: &Value, page_count: usize) -> bool {
    let Some(id) = value["project_id"].as_str() else {
        return false;
    };
    let path = format!("exports/{id}.pdf");
    let pdf = &value["artifacts"]["pdf"];
    let verification = &value["artifacts"]["pdf_verification"];
    let record_path = "exports/pdf-verification.json";
    let matches = |descriptor: &Value, expected: &str| {
        keys(descriptor, &["path", "sha256"]) && descriptor["path"] == expected
    };
    if !matches(pdf, &path) || !matches(verification, record_path) {
        return false;
    }
    let Some(bytes) = contained_bytes(root, &path).ok().flatten() else {
        return false;
    };
    bytes.starts_with(b"%PDF-")
        && pdf["sha256"] == validation::bytes_hash(&bytes)
        && validation::file_hash(root, record_path)
            .ok()
            .flatten()
            .is_some_and(|hash| verification["sha256"] == hash)
        && terminal::pdf_verification_valid(root, value, page_count)
}
