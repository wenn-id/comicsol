mod contract;
mod input_raster;
mod prepare;
mod receipts;
mod snapshot;
pub(super) use contract::manifest as check_manifest;
pub(crate) use prepare::prepare_project;
pub(crate) use snapshot::{inspect_project, submit_result};

pub(crate) fn account_panel_attempt(
    root: &File,
    panel: &str,
    kind: &str,
) -> crate::error::Result<(Value, Value)> {
    let (document, counts, _) =
        snapshot::advance_counters(root, &json!({"subject_id": panel, "attempt_kind": kind}), 1)?;
    Ok((document, counts))
}

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::Read;
use std::path::Path;

use serde_json::{Value, json};

use super::{Issue, Report, artifact, bytes_hash, is_sha256, items, load, panels, qualified};
use crate::error::Error;
use crate::{manifest, project_fs};

const HANDOFF: &str = "handoff/manifest.json";
const BATCHES: &str = "generation/batches.json";
const SELECTION: &str = "logs/reference-selection.json";
const SCOPE_FIXED: &[&str] = &[
    BATCHES,
    SELECTION,
    super::CHARACTERS,
    super::STORY,
    super::STORYBOARD,
];
const MAX_READ: u64 = 128 * 1024 * 1024;

pub(crate) fn valid_job(value: &Value) -> bool {
    let mut issues = Vec::new();
    contract::job(
        value,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    issues.is_empty()
}

pub(crate) fn valid_receipt(value: &Value) -> bool {
    let mut issues = Vec::new();
    contract::receipt(
        value,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    issues.is_empty()
}

pub(crate) fn job_digest(value: &Value) -> String {
    contract::digest(value)
}

pub(crate) fn attempt_id(job: &str, ordinal: u64) -> String {
    contract::attempt_id(job, ordinal)
}

pub(super) fn inspect(
    root: &File,
    project: &Value,
    require_accepted: bool,
    issues: &mut Vec<Issue>,
) {
    let binding = &project["handoff"];
    if binding["manifest_path"] != HANDOFF {
        return;
    }
    let Some(expected_scope) = binding["locked_scope_sha256"]
        .as_str()
        .filter(|text| is_sha256(text))
    else {
        return;
    };
    let Some(handoff) = load(root, HANDOFF, issues) else {
        return;
    };
    let before = issues.len();
    contract::manifest(
        &handoff,
        &mut Report {
            path: HANDOFF,
            issues,
        },
    );
    if issues.len() != before {
        return;
    }
    let mut references = required_artifacts(root, &handoff, issues);
    let mut jobs = BTreeMap::new();
    let descriptors = items(&handoff["jobs"]);
    if let Some(batches) = batch_membership(root, &handoff["batches"]["sha256"], issues) {
        let expected: BTreeSet<_> = descriptors
            .iter()
            .filter_map(|job| job["job_id"].as_str())
            .collect();
        if expected != batches.keys().map(String::as_str).collect::<BTreeSet<_>>() {
            Report {
                path: HANDOFF,
                issues,
            }
            .add("jobs", "job IDs must exactly match generation/batches.json");
        }
        for descriptor in descriptors {
            if descriptor["status"] == "missing" {
                continue;
            }
            let (Some(id), Some(path)) =
                (descriptor["job_id"].as_str(), descriptor["path"].as_str())
            else {
                continue;
            };
            let Some(job) = load(root, path, issues) else {
                continue;
            };
            let mut report = Report { path, issues };
            let before = report.issues.len();
            contract::job(&job, &mut report);
            if report.issues.len() != before {
                continue;
            }
            let ready = descriptor["status"] == "ready";
            for (index, (reference, hash)) in contract::references(&job).enumerate() {
                references.insert(reference.to_owned());
                let field = format!("references[{index}]");
                raster(root, reference, &qualified(&field, "path"), &mut report);
                if ready {
                    input(
                        root,
                        reference,
                        hash,
                        &qualified(&field, "path"),
                        &qualified(&field, "sha256"),
                        &mut report,
                    );
                }
            }
            if ready && let Some(path) = job["prompt_path"].as_str() {
                input(
                    root,
                    path,
                    &job["prompt_sha256"],
                    "prompt_path",
                    "prompt_sha256",
                    &mut report,
                );
            }
            report.ensure(
                job["job_id"] == id,
                "job_id",
                "does not match handoff manifest",
            );
            if let Some((batch, kind)) = batches.get(id) {
                report.ensure(
                    job["batch_id"] == *batch,
                    "batch_id",
                    "does not match generation/batches.json",
                );
                report.ensure(
                    job["subject_kind"] == *kind,
                    "subject_kind",
                    "does not match generation/batches.json",
                );
            }
            report.ensure(
                descriptor["sha256"] == canonical_hash(&job),
                "sha256",
                "does not match handoff manifest",
            );
            jobs.insert(id, job);
        }
    }
    let mut report = Report {
        path: HANDOFF,
        issues,
    };
    let scope_stale = match scope(root, references) {
        Ok(actual) if actual != expected_scope => {
            report.add(
                "locked_scope_sha256",
                "does not match current project scope",
            );
            true
        }
        Ok(_) => false,
        Err(detail) => {
            report.add(
                "locked_scope_sha256",
                format!("cannot recompute current project scope: {detail}"),
            );
            false
        }
    };
    for (field, expected) in [
        ("locked_scope_sha256", &binding["locked_scope_sha256"]),
        ("project_id", &project["project_id"]),
        ("project_schema_version", &project["schema_version"]),
        ("stage", &project["status"]),
    ] {
        report.ensure(
            &handoff[field] == expected,
            field,
            format!("must match project.json {field}"),
        );
    }
    receipts::inspect(
        root,
        descriptors,
        &jobs,
        scope_stale,
        require_accepted,
        issues,
    );
}

fn canonical_hash(value: &Value) -> String {
    bytes_hash(&manifest::canonical(value))
}

fn compact_hash(value: &Value) -> String {
    bytes_hash(&manifest::compact(value))
}

enum ReadError {
    Missing,
    Invalid(&'static str),
}

impl ReadError {
    fn detail(&self) -> &str {
        match self {
            Self::Missing => "FileNotFoundError",
            Self::Invalid(detail) => detail,
        }
    }
}

fn read_error(error: Error) -> ReadError {
    match error.code() {
        "CS-PROJ-002" => ReadError::Missing,
        "CS-SEC-001" | "CS-PROJ-001" => ReadError::Invalid("ValueError"),
        "CS-SEC-002" => ReadError::Invalid("InputResourceLimitError"),
        "CS-PROJ-003" => ReadError::Invalid("PermissionError"),
        _ => ReadError::Invalid("OSError"),
    }
}

fn read(root: &File, path: &str, limit: u64) -> Result<Vec<u8>, ReadError> {
    let (dir, name) = artifact(root, path)
        .map_err(read_error)?
        .ok_or(ReadError::Missing)?;
    let file = project_fs::open_file(&dir, &name, false, false).map_err(read_error)?;
    if file
        .metadata()
        .map_err(|_| ReadError::Invalid("OSError"))?
        .len()
        > limit
    {
        return Err(ReadError::Invalid("InputResourceLimitError"));
    }
    let mut bytes = Vec::new();
    file.take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ReadError::Invalid("OSError"))?;
    if bytes.len() as u64 > limit {
        return Err(ReadError::Invalid("InputResourceLimitError"));
    }
    Ok(bytes)
}

fn read_hash(root: &File, path: &str) -> Result<String, ReadError> {
    read(root, path, MAX_READ).map(|bytes| bytes_hash(&bytes))
}

fn required_artifacts(root: &File, handoff: &Value, issues: &mut Vec<Issue>) -> BTreeSet<String> {
    let mut references = BTreeSet::new();
    for item in items(&handoff["required_artifacts"]) {
        let Some(path) = item["path"].as_str() else {
            continue;
        };
        if path.starts_with("references/characters/") || path.starts_with("references/scenes/") {
            references.insert(path.to_owned());
        }
        let mut report = Report { path, issues };
        match read_hash(root, path) {
            Ok(actual) => report.ensure(
                item["sha256"] == actual,
                "sha256",
                "does not match handoff manifest",
            ),
            Err(ReadError::Missing) => report.add("file", "required file is missing"),
            Err(error) => report.add(
                "file",
                format!("cannot read required artifact: {}", error.detail()),
            ),
        }
    }
    references
}

fn batch_membership(
    root: &File,
    hash: &Value,
    issues: &mut Vec<Issue>,
) -> Option<BTreeMap<String, (String, String)>> {
    let batches = load(root, BATCHES, issues)?;
    let mut report = Report {
        path: BATCHES,
        issues,
    };
    let before = report.issues.len();
    contract::batches(&batches, &mut report);
    if report.issues.len() != before {
        return None;
    }
    report.ensure(
        *hash == canonical_hash(&batches),
        "sha256",
        "does not match handoff manifest",
    );
    let mut result = BTreeMap::new();
    for batch in items(&batches["batches"]) {
        let (Some(id), Some(kind)) = (batch["batch_id"].as_str(), batch["kind"].as_str()) else {
            continue;
        };
        for job in items(&batch["job_ids"]).iter().filter_map(Value::as_str) {
            result.insert(job.to_owned(), (id.to_owned(), kind.to_owned()));
        }
    }
    Some(result)
}

fn input(
    root: &File,
    path: &str,
    hash: &Value,
    path_field: &str,
    hash_field: &str,
    report: &mut Report,
) {
    match read_hash(root, path) {
        Ok(actual) => report.ensure(*hash == actual, hash_field, "does not match input file"),
        Err(ReadError::Missing) => report.add(path_field, "input file is missing"),
        Err(error) => report.add(
            path_field,
            format!("cannot read input file: {}", error.detail()),
        ),
    }
}

fn directory(root: &File, path: &str) -> Result<Option<File>, ReadError> {
    let relative = project_fs::relative_path(path).map_err(read_error)?;
    let Some((parent, name)) = project_fs::target(root, &relative, false).map_err(read_error)?
    else {
        return Ok(None);
    };
    let name = Path::new(name);
    if project_fs::metadata(&parent, name)
        .map_err(read_error)?
        .is_none()
    {
        return Ok(None);
    }
    project_fs::open_dir(&parent, name)
        .map(Some)
        .map_err(read_error)
}

fn scope(root: &File, mut reference_paths: BTreeSet<String>) -> Result<String, String> {
    let missing = |path: &str| format!("required locked-scope path is missing: {path}");
    let mut paths: BTreeSet<_> = SCOPE_FIXED.iter().map(|&path| path.to_owned()).collect();
    if directory(root, "prompts")
        .map_err(scope_directory_error)?
        .is_none()
    {
        return Err("required prompt directory is missing: prompts".to_owned());
    }
    for directory_path in ["prompts/references", "prompts/panels"] {
        let Some(directory) = directory(root, directory_path).map_err(scope_directory_error)?
        else {
            continue;
        };
        let mut names = project_fs::names(&directory).map_err(|_| "OSError".to_owned())?;
        names.sort();
        for name in names {
            if Path::new(&name).extension().is_none_or(|ext| ext != "txt")
                || !project_fs::metadata(&directory, Path::new(&name))
                    .ok()
                    .flatten()
                    .is_some_and(|meta| meta.is_file())
            {
                return Err("HandoffContractError".into());
            }
            paths.insert(format!("{directory_path}/{}", name.to_string_lossy()));
        }
    }
    let selection = scope_json(root, SELECTION, &missing)?;
    let selected = selection["panels"]
        .as_array()
        .ok_or("HandoffContractError")?;
    for panel in selected {
        for reference in panel["selected"].as_array().ok_or("HandoffContractError")? {
            let path = reference["path"]
                .as_str()
                .filter(|path| contract::normalized(path) && path.starts_with("references/"))
                .ok_or("HandoffContractError")?;
            if artifact(root, path)
                .map_err(|error| read_error(error).detail().to_owned())?
                .is_none()
            {
                return Err(missing(path));
            }
            reference_paths.insert(path.to_owned());
        }
    }
    if reference_paths
        .iter()
        .any(|path| !contract::normalized(path) || !path.starts_with("references/"))
    {
        return Err("HandoffContractError".into());
    }
    paths.extend(reference_paths);
    let mut files = Vec::new();
    for path in paths {
        let sha256 = if path == SELECTION {
            compact_hash(&selection)
        } else if path.ends_with(".json") {
            compact_hash(&scope_json(root, &path, &missing)?)
        } else {
            read_hash(root, &path).map_err(|error| scope_read_error(error, &path, &missing))?
        };
        files.push(json!({"path": path, "sha256": sha256}));
    }
    Ok(compact_hash(
        &json!({"contract_version": "1.0", "files": files}),
    ))
}

fn scope_directory_error(error: ReadError) -> String {
    match error {
        ReadError::Invalid("OSError") => "HandoffContractError".to_owned(),
        other => other.detail().to_owned(),
    }
}

fn scope_read_error(error: ReadError, path: &str, missing: &impl Fn(&str) -> String) -> String {
    match error {
        ReadError::Missing => missing(path),
        other => other.detail().to_owned(),
    }
}

fn scope_json(root: &File, path: &str, missing: &impl Fn(&str) -> String) -> Result<Value, String> {
    let bytes = read(root, path, crate::error::contract::MAX_JSON_BYTES as u64)
        .map_err(|error| scope_read_error(error, path, missing))?;
    let text = std::str::from_utf8(&bytes).map_err(|_| "UnicodeDecodeError".to_owned())?;
    manifest::parse_document(text, path).map_err(|error| {
        if error.code() == "CS-SEC-002" {
            "InputResourceLimitError".to_owned()
        } else if error
            .detail()
            .is_some_and(|detail| detail.starts_with("JSONDecodeError"))
        {
            "JSONDecodeError".to_owned()
        } else {
            "ValueError".to_owned()
        }
    })
}

fn raster(root: &File, path: &str, field: &str, report: &mut Report) -> Option<panels::Raster> {
    panels::raster(
        root,
        &Value::String(path.to_owned()),
        field,
        None,
        Some(image::ImageFormat::Png),
        report,
    )
    .ok()
    .flatten()
}
