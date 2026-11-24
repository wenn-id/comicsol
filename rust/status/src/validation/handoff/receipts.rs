use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::path::Path;

use serde_json::Value;

use super::super::{CHARACTERS, Issue, Report, STORY, bytes_hash, items, load, read_value};
use super::{HANDOFF, MAX_READ, ReadError, contract, directory, raster, read, read_hash};
use crate::project_fs;

const INVENTORY: &str = "generation/receipts";
pub(super) type Receipts = Vec<(String, Value)>;

pub(super) fn inventory(root: &File, issues: &mut Vec<Issue>) -> Receipts {
    let mut report = Report {
        path: INVENTORY,
        issues,
    };
    let directory = match directory(root, INVENTORY) {
        Ok(Some(directory)) => directory,
        Ok(None) => return Vec::new(),
        Err(error) => {
            let message = match error {
                ReadError::Invalid("OSError") => {
                    "receipt inventory must be a real directory".to_owned()
                }
                _ => format!(
                    "receipt inventory must be a real directory: {}",
                    error.detail()
                ),
            };
            report.add("file", message);
            return Vec::new();
        }
    };
    let mut names = match project_fs::names(&directory) {
        Ok(names) => names,
        Err(_) => {
            report.add("file", "cannot read receipt inventory: OSError");
            return Vec::new();
        }
    };
    names.sort();
    let mut receipts = Vec::new();
    for name in names {
        let name = Path::new(&name);
        let relative = format!("{INVENTORY}/{}", name.to_string_lossy());
        if name.extension().is_none_or(|ext| ext != "json")
            || !project_fs::metadata(&directory, name)
                .ok()
                .flatten()
                .is_some_and(|meta| meta.is_file())
        {
            Report {
                path: &relative,
                issues,
            }
            .add("file", "receipt must be a regular JSON file");
            continue;
        }
        let Some(receipt) = load(root, &relative, issues) else {
            continue;
        };
        let mut report = Report {
            path: &relative,
            issues,
        };
        let before = report.issues.len();
        contract::receipt(&receipt, &mut report);
        if report.issues.len() != before {
            continue;
        }
        report.ensure(
            name.file_stem().and_then(|stem| stem.to_str()) == receipt["attempt_id"].as_str(),
            "attempt_id",
            "must match the receipt filename",
        );
        receipts.push((relative, receipt));
    }
    receipts
}

pub(super) fn inspect(
    root: &File,
    descriptors: &[Value],
    jobs: &BTreeMap<&str, Value>,
    scope_stale: bool,
    require_accepted: bool,
    issues: &mut Vec<Issue>,
) {
    let mut grouped: BTreeMap<_, Receipts> = jobs.keys().map(|&id| (id, Vec::new())).collect();
    let current_attempts: BTreeSet<_> = jobs
        .iter()
        .flat_map(|(&id, job)| {
            (1..=budget(job)).map(move |ordinal| contract::attempt_id(id, ordinal))
        })
        .collect();
    for (path, receipt) in inventory(root, issues) {
        if let Some(group) = receipt["job_id"]
            .as_str()
            .and_then(|id| grouped.get_mut(id))
        {
            group.push((path, receipt));
        } else if receipt["attempt_id"]
            .as_str()
            .is_some_and(|id| current_attempts.contains(id))
        {
            Report {
                path: &path,
                issues,
            }
            .add("job_id", "does not name a current handoff job");
        }
    }
    for (index, descriptor) in descriptors.iter().enumerate() {
        let Some(id) = descriptor["job_id"].as_str() else {
            continue;
        };
        let Some(job) = jobs.get(id) else {
            continue;
        };
        let receipts = &grouped[id];
        let state = match reconcile(job, descriptor, receipts, scope_stale) {
            Ok(state) => state.status,
            Err((path, field, detail)) => {
                Report { path, issues }.add(field, detail);
                continue;
            }
        };
        let status = descriptor["status"].as_str().unwrap_or_default();
        if status != state {
            Report {
                path: HANDOFF,
                issues,
            }
            .add(
                format!("jobs[{index}].status"),
                format!("declared status {status} does not match effective status {state}"),
            );
        }
        for (path, receipt) in receipts {
            if receipt["outcome"] == "success" {
                inspect_success(root, path, receipt, job, require_accepted, issues);
            }
        }
    }
}

pub(super) fn budget(job: &Value) -> u64 {
    job["retry_limit"].as_u64().unwrap_or(0) + 1
}

pub(super) struct State {
    pub status: &'static str,
    pub used: u64,
    pub remaining: u64,
}

pub(super) fn reconcile<'a>(
    job: &Value,
    descriptor: &Value,
    receipts: &'a Receipts,
    stale: bool,
) -> Result<State, (&'a str, &'static str, String)> {
    let digest = contract::digest(job);
    let fallback = receipts.first().map_or(HANDOFF, |(path, _)| path.as_str());
    if descriptor["sha256"] != digest {
        let path = receipts
            .iter()
            .find(|(_, receipt)| receipt["job_sha256"] != digest)
            .map_or(fallback, |(path, _)| path.as_str());
        return Err((
            path,
            "job_sha256",
            "does not match the generation job".into(),
        ));
    }
    let id = job["job_id"].as_str().expect("validated job ID");
    let budget = budget(job);
    let allowed: BTreeMap<_, _> = (1..=budget)
        .map(|ordinal| (contract::attempt_id(id, ordinal), ordinal))
        .collect();
    let mut ordered = BTreeMap::new();
    for (path, receipt) in receipts {
        if receipt["job_id"] != id {
            return Err((
                fallback,
                "attempt_id",
                "receipt job_id: does not match the generation job".into(),
            ));
        }
        if receipt["job_sha256"] != digest {
            return Err((
                path,
                "job_sha256",
                "does not match the generation job".into(),
            ));
        }
        let Some(ordinal) = receipt["attempt_id"]
            .as_str()
            .and_then(|id| allowed.get(id))
        else {
            return Err((
                path,
                "attempt_id",
                "attempt exceeds the generation job retry budget".into(),
            ));
        };
        if let Some((_, existing)) = ordered.insert(*ordinal, (path, receipt))
            && existing != receipt
        {
            return Err((path, "attempt_id", "conflicting duplicate receipt".into()));
        }
    }
    for (expected, (&ordinal, (path, _))) in (1..).zip(&ordered) {
        if ordinal != expected {
            return Err((
                path,
                "attempt_id",
                "receipt attempt ordinals must be contiguous from 1".into(),
            ));
        }
    }
    let mut successful = ordered
        .iter()
        .filter(|(_, (_, receipt))| receipt["outcome"] == "success");
    let success = successful.next();
    if let Some((_, (path, _))) = successful.next() {
        return Err((
            path,
            "attempt_id",
            "multiple successful receipts are not allowed".into(),
        ));
    }
    if let Some((&ordinal, _)) = success
        && let Some((_, (path, _))) = ordered.range(ordinal + 1..).next()
    {
        return Err((
            path,
            "attempt_id",
            format!("receipt appears after successful receipt at attempt {ordinal}"),
        ));
    }
    let refusal = ordered.iter().find(|(_, (_, receipt))| {
        receipt["outcome"] == "failure" && receipt["category"] == "safety_refusal"
    });
    if refusal.is_some_and(|(&ordinal, _)| ordered.len() as u64 > ordinal) {
        return Err((
            fallback,
            "attempt_id",
            "receipt attempts: no receipt may follow a safety refusal".into(),
        ));
    }
    let status = if stale {
        "stale"
    } else if success.is_some() {
        "completed"
    } else if refusal.is_some() || ordered.len() as u64 == budget {
        "failed"
    } else {
        "ready"
    };
    let used = ordered.len() as u64;
    Ok(State {
        status,
        used,
        remaining: budget - used,
    })
}

fn inspect_success(
    root: &File,
    receipt_path: &str,
    receipt: &Value,
    job: &Value,
    require_accepted: bool,
    issues: &mut Vec<Issue>,
) {
    let Some(image) = inspect_retained(root, receipt_path, receipt, job, issues) else {
        return;
    };
    let Some(id) = job["subject_id"].as_str() else {
        return;
    };
    if job["subject_kind"] == "reference" {
        inspect_activation(root, receipt_path, id, &image, issues);
    } else if require_accepted && job["subject_kind"] == "panel" && !accepted(root, id) {
        Report { path: receipt_path, issues }.add("raster_path",
            format!("successful panel receipt must be promoted to panels/raw/{id}.png and accepted by matching visual QA"));
    }
}

pub(super) fn inspect_retained(
    root: &File,
    receipt_path: &str,
    receipt: &Value,
    job: &Value,
    issues: &mut Vec<Issue>,
) -> Option<Vec<u8>> {
    if receipt["raster_path"] != job["target_path"] {
        Report {
            path: receipt_path,
            issues,
        }
        .add(
            "raster_path",
            "successful receipt must match the generation job target_path",
        );
        return None;
    }
    let path = receipt["raster_path"].as_str()?;
    let mut report = Report { path, issues };
    let image = match read(root, path, MAX_READ) {
        Ok(bytes) => bytes,
        Err(ReadError::Missing) => {
            report.add("file", "successful receipt retained raster is missing");
            return None;
        }
        Err(error) => {
            report.add(
                "file",
                format!("cannot read successful receipt raster: {}", error.detail()),
            );
            return None;
        }
    };
    report.ensure(
        receipt["raster_sha256"] == bytes_hash(&image),
        "sha256",
        "does not match successful receipt raster digest",
    );
    if path.ends_with(".png")
        && let Some(info) = raster(root, path, "file", &mut report)
    {
        let dimensions = &job["requested_dimensions"];
        if dimensions.is_object() {
            report.ensure(
                dimensions["width"] == info.width && dimensions["height"] == info.height,
                "dimensions",
                "successful receipt raster does not match generation job dimensions",
            );
        }
        if let Some(ratio) = job["requested_aspect_ratio"].as_str() {
            report.ensure(
                contract::aspect_matches(info.width.into(), info.height.into(), ratio)
                    == Some(true),
                "aspect_ratio",
                "successful receipt raster does not match generation job aspect ratio",
            );
        }
    }
    Some(image)
}

fn inspect_activation(
    root: &File,
    receipt_path: &str,
    id: &str,
    image: &[u8],
    issues: &mut Vec<Issue>,
) {
    let Some(path) = canonical_reference(root, id) else {
        Report { path: receipt_path, issues }.add("raster_path",
            "successful reference receipt subject must resolve to exactly one canonical character or scene activation path");
        return;
    };
    let mut report = Report {
        path: &path,
        issues,
    };
    match read(root, &path, MAX_READ) {
        Ok(canonical) => report.ensure(
            canonical == image,
            "sha256",
            "activated canonical raster does not match successful reference receipt",
        ),
        Err(ReadError::Missing) => report.add(
            "file",
            "successful reference receipt activated canonical raster is missing",
        ),
        Err(error) => report.add(
            "file",
            format!(
                "cannot read successful reference receipt activation: {}",
                error.detail()
            ),
        ),
    }
}

pub(super) fn canonical_reference(root: &File, id: &str) -> Option<String> {
    let characters = read_value(root, CHARACTERS).ok().flatten()?;
    let story = read_value(root, STORY).ok().flatten()?;
    let character = items(&characters["characters"])
        .iter()
        .any(|item| item["id"] == id);
    let scene = items(&story["scenes"]).iter().any(|item| item["id"] == id);
    match (character, scene) {
        (true, false) => Some(format!("references/characters/{id}.png")),
        (false, true) => Some(format!("references/scenes/{id}.png")),
        _ => None,
    }
}

fn accepted(root: &File, id: &str) -> bool {
    let raw = format!("panels/raw/{id}.png");
    let Ok(sha256) = read_hash(root, &raw) else {
        return false;
    };
    read_value(root, &format!("qa/panels/{id}.json"))
        .ok()
        .flatten()
        .is_some_and(|qa| {
            qa["schema_version"] == "2.0"
                && qa["kind"] == "panel-qa"
                && qa["subject_id"] == id
                && matches!(qa["decision"].as_str(), Some("accept" | "accept-warning"))
                && qa["bindings"]["raw_path"] == raw
                && qa["bindings"]["raw_sha256"] == sha256
        })
}
