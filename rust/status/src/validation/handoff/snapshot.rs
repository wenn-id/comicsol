use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io;
use std::path::Path;
use std::time::Duration;

use serde_json::{Value, json};

use super::super::{Issue, Report, bytes_hash, items, read_value};
use super::{
    BATCHES, HANDOFF, MAX_READ, ReadError, canonical_hash, contract, input_raster, read, read_hash,
    receipts, scope,
};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::project_fs::{self, LockedProject, ProjectLock};
use crate::recovery::{Change, publish};
use crate::{init, manifest, resume};

const COUNTERS: &str = "logs/generation-counters.json";

pub(super) struct Snapshot {
    pub batches: Value,
    pub handoff: Value,
    pub jobs: BTreeMap<String, Value>,
    pub effective: Vec<Value>,
    pub receipts: BTreeMap<String, receipts::Receipts>,
    pub retired: receipts::Receipts,
    pub stale: bool,
}

impl Snapshot {
    fn inspect(&self, stage: &Value) -> Value {
        let phase = if items(&self.batches["batches"])
            .iter()
            .any(|batch| batch["kind"] == "panel")
        {
            "panel"
        } else {
            "reference"
        };
        let action = next_action(phase, &self.effective);
        json!({
            "batches": self.batches["batches"], "jobs": self.effective, "next_action": action,
            "phase": phase, "prepared": true, "project_stage": stage,
            "scope_state": if self.stale { "stale" } else { "current" },
        })
    }
}

pub(super) fn next_action(phase: &str, jobs: &[Value]) -> &'static str {
    let statuses: BTreeSet<_> = jobs
        .iter()
        .filter_map(|job| job["status"].as_str())
        .collect();
    let completed = statuses.len() == 1 && statuses.contains("completed");
    if statuses.contains("stale") {
        "invalidate"
    } else if statuses.contains("failed") {
        "retry-exhausted"
    } else {
        match (phase, completed) {
            ("reference", true) => "prepare",
            ("reference", false) => "render-references",
            (_, true) => "visual-qa",
            (_, false) => "render-panels",
        }
    }
}

pub fn inspect_project(path: &Path) -> Result<Value> {
    let root = project_fs::project_root(path)?;
    let project = manifest::read_project(&root, true)?;
    let unprepared = |project: &Value| {
        json!({
            "batches": [], "jobs": [], "next_action": "prepare", "phase": null,
            "prepared": false, "project_stage": project["status"], "scope_state": "unprepared",
        })
    };
    if project["schema_version"] == crate::error::contract::LEGACY_PROJECT_SCHEMA_VERSION
        || empty_binding(&project["handoff"])
    {
        return Ok(unprepared(&project));
    }
    if !project["handoff"].is_object() {
        return Err(handoff_error(
            "project.json.handoff: required object is missing",
        ));
    }
    let _lock = ProjectLock::acquire(&root, Duration::from_secs(10))?;
    let project = manifest::read_project(&root, true)?;
    Ok(snapshot(&root, &project)?.map_or_else(
        || unprepared(&project),
        |snapshot| snapshot.inspect(&project["status"]),
    ))
}

fn empty_binding(binding: &Value) -> bool {
    binding.is_object()
        && binding["manifest_path"].is_null()
        && binding["locked_scope_sha256"].is_null()
}

pub(super) fn handoff_error(detail: &str) -> Error {
    Error::handoff(if detail.contains(['/', '\\']) {
        "HandoffContractError".into()
    } else {
        format!("invalid handoff contract: {detail}")
    })
}

pub(super) fn result_error(detail: &str) -> Error {
    Error::handoff_result(if detail.contains(['/', '\\']) {
        "HandoffResultError".into()
    } else {
        format!("invalid handoff contract: {detail}")
    })
}

pub(super) fn enforce(issues: Vec<Issue>, result: bool) -> Result<()> {
    if issues.is_empty() {
        return Ok(());
    }
    let details: BTreeSet<_> = issues
        .into_iter()
        .map(|issue| {
            let field = if issue.path.is_empty() {
                issue.field
            } else {
                format!("{}.{}", issue.path, issue.field)
            };
            format!("{field}: {}", issue.message)
        })
        .collect();
    let detail = details.into_iter().collect::<Vec<_>>().join("; ");
    Err(if result {
        result_error(&detail)
    } else {
        handoff_error(&detail)
    })
}

pub(super) fn document(root: &File, path: &str) -> Result<Value> {
    let value = read_value(root, path)?.ok_or_else(missing)?;
    if !value.is_object() {
        return Err(handoff_error(&format!(
            "{path}: must contain a JSON object"
        )));
    }
    Ok(value)
}

fn missing() -> Error {
    io::Error::from(io::ErrorKind::NotFound).into()
}

pub(super) fn read_failure(error: ReadError) -> Error {
    match error {
        ReadError::Missing => missing(),
        ReadError::Invalid("ValueError") => {
            Error::boundary("project path must not contain symlinks or reparse points")
        }
        ReadError::Invalid("InputResourceLimitError") => Error::limit("the artifact size limit"),
        ReadError::Invalid("PermissionError") => {
            io::Error::from(io::ErrorKind::PermissionDenied).into()
        }
        ReadError::Invalid(_) => io::Error::from(io::ErrorKind::InvalidData).into(),
    }
}

pub(super) fn snapshot(root: &File, project: &Value) -> Result<Option<Snapshot>> {
    let binding = &project["handoff"];
    if !binding.is_object() || empty_binding(binding) {
        return Ok(None);
    }
    if binding["manifest_path"] != HANDOFF || !binding["locked_scope_sha256"].is_string() {
        return Err(handoff_error(
            "project.json.handoff: populated binding is malformed",
        ));
    }
    let handoff = document(root, HANDOFF)?;
    let mut issues = Vec::new();
    contract::manifest(
        &handoff,
        &mut Report {
            path: HANDOFF,
            issues: &mut issues,
        },
    );
    enforce(issues, false)?;
    for (field, expected) in [
        ("project_id", &project["project_id"]),
        ("project_schema_version", &project["schema_version"]),
        ("stage", &project["status"]),
        ("locked_scope_sha256", &binding["locked_scope_sha256"]),
    ] {
        if &handoff[field] != expected {
            return Err(handoff_error(&format!(
                "{HANDOFF}.{field}: does not match project.json"
            )));
        }
    }
    let batches = document(root, BATCHES)?;
    let mut issues = Vec::new();
    contract::batches(
        &batches,
        &mut Report {
            path: BATCHES,
            issues: &mut issues,
        },
    );
    enforce(issues, false)?;
    if handoff["batches"]["sha256"] != canonical_hash(&batches) {
        return Err(handoff_error(&format!(
            "{BATCHES}: digest does not match handoff manifest"
        )));
    }
    let expected: BTreeSet<_> = items(&batches["batches"])
        .iter()
        .flat_map(|batch| items(&batch["job_ids"]))
        .filter_map(Value::as_str)
        .collect();
    let descriptors = items(&handoff["jobs"]);
    let ids: BTreeSet<_> = descriptors
        .iter()
        .filter_map(|job| job["job_id"].as_str())
        .collect();
    let mut jobs = BTreeMap::new();
    let mut references = BTreeSet::new();
    for descriptor in descriptors {
        if descriptor["status"] == "missing" {
            continue;
        }
        let id = descriptor["job_id"].as_str().expect("validated job ID");
        let path = descriptor["path"].as_str().expect("validated job path");
        let job = document(root, path)?;
        let mut issues = Vec::new();
        contract::job(
            &job,
            &mut Report {
                path,
                issues: &mut issues,
            },
        );
        enforce(issues, false)?;
        if job["job_id"] != id {
            return Err(handoff_error(&format!(
                "{path}.job_id: descriptor binding mismatch"
            )));
        }
        if descriptor["sha256"] != canonical_hash(&job) {
            return Err(handoff_error(&format!(
                "{path}.sha256: descriptor binding mismatch"
            )));
        }
        references.extend(contract::references(&job).map(|(path, _)| path.to_owned()));
        jobs.insert(id.to_owned(), job);
    }
    if expected != ids {
        return Err(handoff_error(
            "handoff jobs must exactly match generation batches",
        ));
    }
    let mut stale = false;
    for artifact in items(&handoff["required_artifacts"]) {
        let path = artifact["path"].as_str().expect("validated artifact path");
        if path.starts_with("references/characters/") || path.starts_with("references/scenes/") {
            references.insert(path.to_owned());
        }
        stale |= match read_hash(root, path) {
            Ok(hash) => artifact["sha256"] != hash,
            Err(ReadError::Missing) => true,
            Err(error) => return Err(read_failure(error)),
        };
    }
    stale |= match scope(root, references) {
        Ok(hash) => binding["locked_scope_sha256"] != hash,
        Err(detail) if detail.starts_with("required ") => true,
        Err(detail) if detail == "HandoffContractError" => return Err(Error::handoff(detail)),
        Err(detail) => return Err(Error::invalid(detail)),
    };
    let mut issues = Vec::new();
    let inventory = receipts::inventory(root, &mut issues);
    enforce(issues, false)?;
    let mut receipts: BTreeMap<_, receipts::Receipts> =
        jobs.keys().map(|id| (id.clone(), Vec::new())).collect();
    let mut retired = Vec::new();
    let current_attempts: BTreeSet<_> = jobs
        .iter()
        .flat_map(|(id, job)| {
            (1..=receipts::budget(job)).map(move |ordinal| contract::attempt_id(id, ordinal))
        })
        .collect();
    for (path, receipt) in inventory {
        let id = receipt["job_id"]
            .as_str()
            .expect("validated receipt job ID");
        let Some(group) = receipts.get_mut(id) else {
            if receipt["attempt_id"]
                .as_str()
                .is_some_and(|attempt| current_attempts.contains(attempt))
            {
                return Err(handoff_error(
                    "receipt job_id: does not name a current handoff job",
                ));
            }
            retired.push((path, receipt));
            continue;
        };
        let job = &jobs[id];
        if receipt["outcome"] == "success" {
            if receipt["raster_path"] != job["target_path"] {
                return Err(handoff_error(
                    "successful receipt raster_path: does not match generation job target_path",
                ));
            }
            let mut issues = Vec::new();
            let retained = receipts::inspect_retained(root, &path, &receipt, job, &mut issues);
            enforce(issues, false)?;
            if job["subject_kind"] == "reference" {
                let id = job["subject_id"].as_str().expect("validated subject ID");
                stale |= if let Some(canonical) = receipts::canonical_reference(root, id) {
                    match read(root, &canonical, MAX_READ) {
                        Ok(bytes) => Some(bytes) != retained,
                        Err(ReadError::Missing) => true,
                        Err(error) => return Err(read_failure(error)),
                    }
                } else {
                    true
                };
            }
        }
        group.push((path, receipt));
    }
    let effective = descriptors.iter().map(|descriptor| {
        let id = descriptor["job_id"].as_str().expect("validated descriptor ID");
        let Some(job) = jobs.get(id) else {
            return Ok(json!({
                "attempts_used": 0, "attempts_remaining": 0, "next_attempt": null, "job_id": id,
                "path": descriptor["path"], "status": "missing", "subject_id": null, "subject_kind": null,
            }));
        };
        let state = receipts::reconcile(job, descriptor, &receipts[id], stale).map_err(reconcile_error)?;
        Ok(json!({
            "attempts_used": state.used, "attempts_remaining": state.remaining,
            "next_attempt": (state.status == "ready").then_some(state.used + 1),
            "job_id": id, "path": descriptor["path"], "status": state.status,
            "subject_id": job["subject_id"], "subject_kind": job["subject_kind"],
        }))
    }).collect::<Result<_>>()?;
    Ok(Some(Snapshot {
        batches,
        handoff,
        jobs,
        effective,
        receipts,
        retired,
        stale,
    }))
}

fn reconcile_error((_, field, detail): (&str, &str, String)) -> Error {
    let detail = match (field, detail.as_str()) {
        ("job_sha256", _) => "receipt job_sha256: does not match the generation job".to_owned(),
        ("attempt_id", "attempt exceeds the generation job retry budget") => {
            "receipt attempt_id: does not match an allowed attempt ordinal".to_owned()
        }
        ("attempt_id", "receipt attempt ordinals must be contiguous from 1") => {
            "receipt attempts: ordinals must be contiguous from 1".to_owned()
        }
        ("attempt_id", "multiple successful receipts are not allowed") => {
            "receipt attempts: multiple successful receipts are not allowed".to_owned()
        }
        _ => detail,
    };
    handoff_error(&detail)
}

pub fn submit_result(args: &Arguments) -> Result<Value> {
    let path = args.paths("project_dir");
    let locked = LockedProject::open(path, false)?;
    let root = locked.root();
    let project = manifest::read_project(root, true)?;
    let mut snapshot =
        snapshot(root, &project)?.ok_or_else(|| result_error("handoff is not prepared"))?;
    let id = args.text("job_id").unwrap_or_default();
    let job = snapshot
        .jobs
        .get(id)
        .ok_or_else(|| result_error("job_id: does not name a current handoff job"))?;
    if snapshot.stale {
        return Err(handoff_error("job_id: job or locked scope is stale"));
    }
    let state = snapshot
        .effective
        .iter()
        .find(|job| job["job_id"] == id)
        .expect("effective job");
    let success = args.command == "handoff.accept-result";
    let is_reference = job["subject_kind"] == "reference";
    let target = job["target_path"].as_str().expect("validated target path");
    let mut raster_sha = None;
    let mut raster = None;
    let mut activated = None;
    if success {
        if is_reference != args.has("approve_reference") {
            let detail = if is_reference {
                "reference result requires --approve-reference"
            } else {
                "--approve-reference is valid only for reference jobs"
            };
            return Err(result_error(detail));
        }
        let source = init::expand(args.paths("raster_path"));
        let bytes =
            init::read_file(&source, MAX_READ as usize).map_err(|error| match error.code() {
                "CS-SEC-002" => error,
                "CS-SEC-001" => {
                    Error::boundary("security-error: result raster path must not contain symlinks")
                }
                _ => result_error("result raster: cannot be read safely"),
            })?;
        let sanitized = input_raster::sanitize(&bytes, job)?;
        raster_sha = Some(bytes_hash(&sanitized));
        raster = Some(sanitized);
    }
    let attempt = args
        .text("attempt")
        .and_then(|text| text.replace('_', "").parse::<u64>().ok())
        .filter(|&ordinal| ordinal > 0)
        .ok_or_else(|| result_error("attempt: must be a positive integer"))?;
    let identifier = contract::attempt_id(id, attempt);
    let relative = format!("generation/receipts/{identifier}.json");
    let category = if success {
        "accepted"
    } else {
        args.text("category").unwrap_or_default()
    };
    let receipt = json!({
        "schema_version": "1.0", "attempt_id": identifier, "job_id": id, "job_sha256": contract::digest(job),
        "category": category, "outcome": if success { "success" } else { "failure" },
        "raster_path": success.then_some(target), "raster_sha256": raster_sha,
        "executor_id": args.text("executor_id"), "executor_kind": args.text("executor_kind"),
        "model": args.text("model"), "provider": args.text("provider"),
        "capabilities_used": {
            "dimensions": args.has("used_dimensions"), "localized_edit": args.has("used_localized_edit"),
            "reference_images": args.has("used_reference_images"),
        },
    });
    let mut issues = Vec::new();
    contract::receipt(
        &receipt,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    enforce(issues, true)?;
    if let Some((_, existing)) = snapshot.receipts[id]
        .iter()
        .find(|(_, value)| value["attempt_id"] == identifier)
    {
        if *existing != receipt {
            return Err(result_error(&format!(
                "attempt {attempt}: conflicting duplicate receipt"
            )));
        }
        if success {
            let counters = if is_reference {
                Value::Null
            } else {
                read_counters(root)?
            };
            let reference = is_reference
                .then(|| canonical_reference(root, job))
                .transpose()?;
            return Ok(success_result(
                &receipt,
                &relative,
                reference.as_deref(),
                counters,
                true,
                &state["status"],
            ));
        }
        return Ok(json!({
            "attempt_id": identifier, "attempts_remaining": state["attempts_remaining"],
            "attempts_used": state["attempts_used"], "category": category, "duplicate": true,
            "job_id": id, "next_attempt": state["next_attempt"], "receipt_path": relative, "status": state["status"],
        }));
    }
    match state["status"].as_str() {
        Some("completed") => {
            return Err(result_error(if success {
                "job is completed and rejects another result"
            } else {
                "job is completed and rejects a failure"
            }));
        }
        Some("failed") => return Err(result_error("job retry budget is exhausted")),
        _ => {}
    }
    let next = state["next_attempt"].as_u64().expect("current ready job");
    if next != attempt {
        return Err(result_error(&format!(
            "attempt ordinal must equal next attempt {next}"
        )));
    }
    let mut changes = Vec::new();
    let mut counts = Value::Null;
    if let Some(bytes) = raster {
        if contained_entry_exists(root, target)? {
            return Err(result_error(
                "result target already exists without this receipt",
            ));
        }
        if is_reference {
            let canonical = canonical_reference(root, job)?;
            if contained_entry_exists(root, &canonical)?
                && !can_reactivate(root, &snapshot.retired, job, &canonical)?
            {
                return Err(result_error(
                    "canonical reference exists and must never be overwritten",
                ));
            }
            changes.push(Change::Replace(canonical.clone(), bytes.clone()));
            activated = Some(canonical);
        }
        changes.push(Change::Replace(target.into(), bytes));
    }
    changes.push(Change::Replace(
        relative.clone(),
        manifest::canonical(&receipt),
    ));
    if !is_reference {
        let (counters, panel_counts, sequence) = advance_counters(root, job, attempt)?;
        if success {
            let subject = job["subject_id"].as_str().expect("panel subject");
            let kind = job["attempt_kind"].as_str().expect("attempt kind");
            if target != format!("panels/attempts/{subject}/{kind}-{sequence:03}.png") {
                return Err(result_error(
                    "panel job target does not match the next retained counter path",
                ));
            }
        }
        counts = panel_counts;
        changes.push(Change::Replace(
            COUNTERS.into(),
            manifest::canonical(&counters),
        ));
    }
    let prior = snapshot.receipts.get_mut(id).expect("job receipts");
    prior.push((relative.clone(), receipt.clone()));
    let descriptor = snapshot.handoff["jobs"]
        .as_array_mut()
        .expect("handoff jobs")
        .iter_mut()
        .find(|descriptor| descriptor["job_id"] == id)
        .expect("job descriptor");
    let reconciled = receipts::reconcile(job, descriptor, prior, false).map_err(reconcile_error)?;
    descriptor["status"] = reconciled.status.into();
    changes.push(Change::Replace(
        HANDOFF.into(),
        manifest::canonical(&snapshot.handoff),
    ));
    let (event, details) = if success {
        let mut details = json!({
            "attempt_id": identifier, "attempt_path": target, "job_id": id,
            "kind": job["subject_kind"], "raster_sha256": raster_sha,
        });
        if let Some(activated) = &activated {
            details["activated_path"] = activated.clone().into();
            details["activated_sha256"] = json!(raster_sha);
        }
        ("handoff.result-accepted", details)
    } else {
        (
            "handoff.failure-recorded",
            json!({
                "attempt": attempt, "attempt_id": identifier, "category": category, "job_id": id, "kind": job["subject_kind"],
            }),
        )
    };
    changes.push(Change::Append(
        "logs/events.jsonl".into(),
        resume::event(event, details),
    ));
    publish(
        &locked,
        if success {
            "handoff-accept-result"
        } else {
            "handoff-record-failure"
        },
        changes,
    )?;
    if success {
        return Ok(success_result(
            &receipt,
            &relative,
            activated.as_deref(),
            counts,
            false,
            &json!(reconciled.status),
        ));
    }
    Ok(json!({
        "attempt_id": identifier, "attempts_remaining": reconciled.remaining,
        "attempts_used": reconciled.used, "category": category, "duplicate": false, "job_id": id,
        "next_attempt": (reconciled.status == "ready").then_some(reconciled.used + 1),
        "receipt_path": relative, "status": reconciled.status,
    }))
}

fn success_result(
    receipt: &Value,
    relative: &str,
    activated: Option<&str>,
    counts: Value,
    duplicate: bool,
    status: &Value,
) -> Value {
    json!({
        "activated_reference_path": activated,
        "activated_reference_sha256": activated.map(|_| &receipt["raster_sha256"]),
        "attempt_id": receipt["attempt_id"], "counters": counts, "duplicate": duplicate,
        "job_id": receipt["job_id"], "raster_path": receipt["raster_path"], "raster_sha256": receipt["raster_sha256"],
        "receipt_path": relative, "status": status,
    })
}

pub(super) fn contained_entry_exists(root: &File, path: &str) -> Result<bool> {
    let path = project_fs::relative_path(path)?;
    let Some((parent, leaf)) = project_fs::target(root, &path, false)? else {
        return Ok(false);
    };
    Ok(project_fs::metadata(&parent, Path::new(leaf))?.is_some())
}

fn canonical_reference(root: &File, job: &Value) -> Result<String> {
    let subject = job["subject_id"].as_str().expect("reference subject");
    let characters = document(root, super::super::CHARACTERS)?;
    let story = document(root, super::super::STORY)?;
    let has_character = items(&characters["characters"])
        .iter()
        .any(|entry| entry["id"] == subject);
    let has_scene = items(&story["scenes"])
        .iter()
        .any(|entry| entry["id"] == subject);
    match (has_character, has_scene) {
        (true, false) => Ok(format!("references/characters/{subject}.png")),
        (false, true) => Ok(format!("references/scenes/{subject}.png")),
        (true, true) => Err(result_error(
            "reference job identity is character/scene ambiguous",
        )),
        (false, false) => Err(result_error(
            "reference job identity is not present in project planning",
        )),
    }
}

fn can_reactivate(
    root: &File,
    retired: &receipts::Receipts,
    job: &Value,
    path: &str,
) -> Result<bool> {
    let Ok(current) = read_hash(root, path) else {
        return Ok(false);
    };
    for (_, receipt) in retired.iter().rev() {
        if receipt["outcome"] != "success" || receipt["raster_sha256"] != current {
            continue;
        }
        let id = receipt["job_id"].as_str().expect("receipt job ID");
        if let Ok(previous) = document(root, &format!("generation/jobs/{id}.json"))
            && super::valid_job(&previous)
            && previous["subject_kind"] == "reference"
            && previous["subject_id"] == job["subject_id"]
        {
            return Ok(previous["prompt_sha256"] != job["prompt_sha256"]);
        }
    }
    Ok(false)
}

pub(super) fn read_counters(root: &File) -> Result<Value> {
    Ok(read_value(root, COUNTERS)?
        .unwrap_or_else(|| json!({"schema_version": "1.0", "global_extra_calls": 0, "panels": {}})))
}

pub(super) fn advance_counters(
    root: &File,
    job: &Value,
    attempt: u64,
) -> Result<(Value, Value, u64)> {
    let mut counters = read_counters(root)?;
    let kinds = [
        (
            "initial",
            1,
            "at most one initial attempt is allowed per panel",
        ),
        (
            "transient_repeats",
            1,
            "at most one transient repeat is allowed per panel",
        ),
        (
            "visual_retries",
            2,
            "at most two visual retries are allowed per panel",
        ),
    ];
    let kind = match job["attempt_kind"].as_str() {
        Some("initial") => 0,
        Some("transient_repeat") => 1,
        Some("visual_retry") => 2,
        _ => return Err(Error::invalid("invalid attempt kind")),
    };
    let id = job["subject_id"].as_str().expect("validated panel ID");
    let global = counters
        .get("global_extra_calls")
        .unwrap_or(&Value::from(0))
        .as_u64()
        .ok_or_else(|| {
            Error::invalid("global generation counter must be a non-negative integer")
        })?;
    let panels = counters["panels"]
        .as_object_mut()
        .ok_or_else(|| Error::invalid("generation counter panels must be an object"))?;
    let panel = panels
        .entry(id)
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or_else(|| Error::invalid("panel generation counters must be an object"))?;
    for (name, _, _) in kinds {
        if panel
            .entry(name)
            .or_insert_with(|| 0.into())
            .as_u64()
            .is_none()
        {
            return Err(Error::invalid(
                "panel generation counters must be non-negative integers",
            ));
        }
    }
    let (name, limit, detail) = kinds[kind];
    let count = panel[name].as_u64().expect("validated panel counter");
    if attempt == 1 && count >= limit {
        return Err(Error::invalid(detail));
    }
    let extra = kind != 0 || attempt > 1;
    if extra && global >= 8 {
        return Err(Error::invalid(
            "at most eight extra calls are allowed per project",
        ));
    }
    if attempt == 1 {
        panel.insert(name.into(), (count + 1).into());
    }
    let counts = json!({
        "global_extra_calls": global + u64::from(extra),
        "initial": panel["initial"], "transient_repeats": panel["transient_repeats"],
        "visual_retries": panel["visual_retries"],
    });
    if extra {
        counters["global_extra_calls"] = (global + 1).into();
    }
    Ok((counters, counts, count + u64::from(attempt == 1)))
}
