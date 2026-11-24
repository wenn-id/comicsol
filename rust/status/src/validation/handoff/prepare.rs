use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::path::Path;

use serde_json::{Value, json};

use super::super::{CHARACTERS, Report, STORY, STORYBOARD, Stage, character, items, references};
use super::snapshot::{
    Snapshot, contained_entry_exists, document, enforce, handoff_error, next_action, read_counters,
    read_failure, snapshot,
};
use super::{
    BATCHES, HANDOFF, MAX_READ, ReadError, SCOPE_FIXED, SELECTION, bytes_hash, canonical_hash,
    compact_hash, contract, directory, raster, read, read_hash, receipts,
};
use crate::error::{Error, Result, contract as project_contract};
use crate::project_fs::{self, LockedProject};
use crate::recovery::{Change, publish};
use crate::{finalize, manifest, resume};

const IDENTITY: &str = "plan/character-identity-pack.json";
const MAX_EXTRA_CALLS: u64 = 8;
const BRIEF_FIELDS: &[&str] = &[
    "batch_id",
    "prompt_path",
    "prompt_sha256",
    "references",
    "requested_aspect_ratio",
    "requested_dimensions",
    "subject_id",
    "subject_kind",
];
type Files = BTreeMap<String, Vec<u8>>;
type Prompts = BTreeMap<String, (String, Vec<u8>)>;

pub fn prepare_project(path: &Path) -> Result<Value> {
    let locked = LockedProject::open(path, false)?;
    let root = locked.root();
    let mut project = manifest::read_project(root, true)?;
    let migrated = project["schema_version"] == project_contract::LEGACY_PROJECT_SCHEMA_VERSION;
    if migrated {
        if project.get("handoff").is_some() {
            return Err(Error::invalid(
                "project schema 1.0 must not contain a handoff object",
            ));
        }
        project["schema_version"] = project_contract::CURRENT_PROJECT_SCHEMA_VERSION.into();
        project["handoff"] =
            json!({"contract_version": "1.0", "locked_scope_sha256": null, "manifest_path": null});
        super::super::bound_manifest(&project)?;
    }
    let previous = snapshot(root, &project)?;
    if previous.as_ref().is_some_and(|prior| prior.stale) {
        return Err(handoff_error("locked_scope_sha256: stale project scope"));
    }
    finalize::require_valid(root, Stage::Storyboard)?;
    let story = document(root, STORY)?;
    let characters = document(root, CHARACTERS)?;
    let board = document(root, STORYBOARD)?;
    let identity = document(root, IDENTITY)?;
    let (issue, valid) = character::identity_pack(root, &identity, &characters);
    if !valid {
        return Err(handoff_error(
            issue
                .as_deref()
                .unwrap_or("character identity pack is invalid"),
        ));
    }
    let reference_prompts = prompts(root, "references")?;
    let panel_prompts = prompts(root, "panels")?;
    let mut issues = Vec::new();
    let inventory = receipts::inventory(root, &mut issues);
    enforce(issues, false)?;
    let (reference_candidates, active_references) =
        reference_jobs(root, &story, &characters, &reference_prompts, &inventory)?;
    let panel_ids: BTreeSet<_> = items(&board["pages"])
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .filter_map(|panel| panel["id"].as_str())
        .collect();
    if panel_ids != panel_prompts.keys().map(String::as_str).collect() {
        return Err(Error::handoff("HandoffContractError".into()));
    }
    let phase = if reference_candidates.is_empty() {
        "panel"
    } else {
        "reference"
    };
    let previous_phase = previous.as_ref().map(|prior| {
        if items(&prior.batches["batches"])
            .iter()
            .any(|batch| batch["kind"] == "panel")
        {
            "panel"
        } else {
            "reference"
        }
    });
    if previous_phase == Some("panel") && phase == "reference" {
        return Err(handoff_error("handoff phase cannot move backward"));
    }
    if previous_phase == Some("reference")
        && phase == "panel"
        && previous
            .as_ref()
            .expect("reference handoff")
            .effective
            .iter()
            .any(|job| job["subject_kind"] == "reference" && job["status"] != "completed")
    {
        return Err(handoff_error(
            "reference jobs must be completed before panel prepare",
        ));
    }
    let budget = if phase == "reference" {
        json!(0)
    } else {
        Value::Null
    };
    let selection = references::plan(&identity, &board, &budget)
        .ok_or_else(|| handoff_error("reference selection is invalid"))?;
    let (batches, jobs) = if phase == "reference" {
        build_references(root, &reference_candidates, previous.as_ref(), &inventory)?
    } else {
        build_panels(
            root,
            &project,
            &board,
            &selection,
            &panel_prompts,
            previous.as_ref(),
        )?
    };
    let batches = json!({"schema_version": "1.0", "batches": batches});
    let mut issues = Vec::new();
    contract::batches(
        &batches,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    enforce(issues, false)?;
    let mut files = Files::new();
    let mut descriptors = Vec::new();
    for job in &jobs {
        let id = job["job_id"].as_str().expect("validated job ID");
        let local = format!("generation/jobs/{id}.json");
        let is_managed = previous
            .as_ref()
            .is_some_and(|prior| prior.jobs.contains_key(id));
        if !is_managed {
            let target = job["target_path"].as_str().expect("validated target");
            if contained_entry_exists(root, target)? {
                return Err(handoff_error(&format!(
                    "{target}: unmanaged retained target collision"
                )));
            }
            let prior = document(root, &local)
                .ok()
                .filter(|old| super::valid_job(old) && old == job);
            for ordinal in 1..=receipts::budget(job) {
                let attempt = contract::attempt_id(id, ordinal);
                let receipt_path = format!("generation/receipts/{attempt}.json");
                if contained_entry_exists(root, &receipt_path)? {
                    let receipt = document(root, &receipt_path)?;
                    if prior.is_none()
                        || receipt["job_id"] != id
                        || receipt["job_sha256"] != canonical_hash(job)
                    {
                        return Err(handoff_error(&format!(
                            "{receipt_path}: unmanaged receipt collision"
                        )));
                    }
                }
            }
        }
        let state = previous
            .as_ref()
            .and_then(|prior| prior.effective.iter().find(|job| job["job_id"] == id))
            .and_then(|job| job["status"].as_str())
            .unwrap_or("ready");
        let payload = manifest::canonical(job);
        descriptors.push(
            json!({"job_id": id, "path": local, "sha256": bytes_hash(&payload), "status": state}),
        );
        files.insert(local, payload);
    }
    let mut scope_files = Files::new();
    for fixed in SCOPE_FIXED
        .iter()
        .copied()
        .filter(|&path| path != BATCHES && path != SELECTION)
    {
        scope_files.insert(
            fixed.into(),
            read(root, fixed, MAX_READ).map_err(read_failure)?,
        );
    }
    scope_files.insert(BATCHES.into(), manifest::canonical(&batches));
    scope_files.insert(SELECTION.into(), manifest::canonical(&selection));
    for (path, bytes) in reference_prompts.values().chain(panel_prompts.values()) {
        scope_files.insert(path.clone(), bytes.clone());
    }
    for path in active_references.iter().map(String::as_str).chain(
        jobs.iter()
            .flat_map(contract::references)
            .map(|(path, _)| path),
    ) {
        if !scope_files.contains_key(path) {
            scope_files.insert(
                path.into(),
                read(root, path, MAX_READ).map_err(read_failure)?,
            );
        }
    }
    let hashes = scope_files
        .iter()
        .map(|(path, bytes)| {
            let hash = if path.ends_with(".json") {
                let text =
                    std::str::from_utf8(bytes).map_err(|_| Error::invalid("UnicodeDecodeError"))?;
                compact_hash(&manifest::parse_document(text, path)?)
            } else {
                bytes_hash(bytes)
            };
            Ok(json!({"path": path, "sha256": hash}))
        })
        .collect::<Result<Vec<_>>>()?;
    let locked_scope = compact_hash(&json!({"contract_version": "1.0", "files": hashes}));
    let batch_bytes = scope_files.remove(BATCHES).expect("batches");
    let selection_bytes = scope_files[SELECTION].clone();
    scope_files.insert(
        IDENTITY.into(),
        read(root, IDENTITY, project_contract::MAX_JSON_BYTES as u64).map_err(read_failure)?,
    );
    let required: Vec<_> = scope_files
        .iter()
        .map(|(path, bytes)| json!({"path": path, "sha256": bytes_hash(bytes)}))
        .collect();
    let handoff = json!({
        "schema_version": "1.0", "project_schema_version": project_contract::CURRENT_PROJECT_SCHEMA_VERSION,
        "project_id": project["project_id"], "stage": project["status"], "locked_scope_sha256": locked_scope,
        "batches": {"path": BATCHES, "sha256": bytes_hash(&batch_bytes)},
        "jobs": descriptors, "required_artifacts": required,
    });
    let mut issues = Vec::new();
    contract::manifest(
        &handoff,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    enforce(issues, false)?;
    files.insert(BATCHES.into(), batch_bytes);
    files.insert(SELECTION.into(), selection_bytes);
    files.insert(HANDOFF.into(), manifest::canonical(&handoff));
    project["handoff"] = json!({"contract_version": "1.0", "locked_scope_sha256": locked_scope, "manifest_path": HANDOFF});
    let project_bytes = manifest::canonical(&project);
    let project_changed = contents_differ(root, "project.json", &project_bytes)?;
    let mut changes = Vec::new();
    for (path, bytes) in files {
        if contents_differ(root, &path, &bytes)? {
            changes.push(Change::Replace(path, bytes));
        }
    }
    let changed = project_changed || !changes.is_empty();
    if previous_phase == Some(phase) && changed {
        return Err(handoff_error(
            "prepared handoff differs from the complete intended state",
        ));
    }
    let descriptors = items(&handoff["jobs"]);
    if changed {
        changes.push(Change::Append("logs/events.jsonl".into(), resume::event("handoff.prepared",
            json!({"count": descriptors.len(), "kind": phase, "project_id": project["project_id"]}))));
        changes.push(Change::Replace("project.json".into(), project_bytes));
        publish(&locked, "handoff-prepare", changes)?;
    }
    let mut counts = json!({"missing": 0, "ready": 0, "completed": 0, "failed": 0, "stale": 0});
    for state in descriptors.iter().filter_map(|job| job["status"].as_str()) {
        counts[state] = (counts[state].as_u64().expect("job count") + 1).into();
    }
    Ok(json!({
        "batch_count": items(&batches["batches"]).len(), "changed": changed, "job_counts": counts,
        "locked_scope_sha256": locked_scope, "manifest_path": HANDOFF, "migrated": migrated && changed,
        "next_action": next_action(phase, descriptors), "phase": phase, "project_id": project["project_id"],
    }))
}

fn contents_differ(root: &File, path: &str, expected: &[u8]) -> Result<bool> {
    match read(root, path, MAX_READ) {
        Ok(bytes) => Ok(expected != bytes),
        Err(ReadError::Missing) => Ok(true),
        Err(error) => Err(read_failure(error)),
    }
}

fn prompts(root: &File, kind: &str) -> Result<Prompts> {
    let folder = format!("prompts/{kind}");
    let dir = directory(root, &folder)
        .map_err(read_failure)?
        .ok_or_else(|| read_failure(ReadError::Missing))?;
    let mut prompts = Prompts::new();
    for name in project_fs::names(&dir)? {
        let name = name
            .to_str()
            .ok_or_else(|| Error::handoff("HandoffContractError".into()))?;
        let id = name
            .strip_suffix(".txt")
            .filter(|_| {
                project_fs::metadata(&dir, Path::new(name))
                    .is_ok_and(|meta| meta.is_some_and(|meta| meta.is_file()))
            })
            .ok_or_else(|| {
                handoff_error(&format!(
                    "{folder}/{name}: generation prompts must be regular .txt files"
                ))
            })?;
        let path = format!("{folder}/{name}");
        let bytes = read(root, &path, MAX_READ).map_err(read_failure)?;
        prompts.insert(id.into(), (path, bytes));
    }
    Ok(prompts)
}

fn reference_jobs(
    root: &File,
    story: &Value,
    characters: &Value,
    prompts: &Prompts,
    inventory: &receipts::Receipts,
) -> Result<(Vec<String>, BTreeSet<String>)> {
    let character_ids: Vec<_> = items(&characters["characters"])
        .iter()
        .filter_map(|entry| entry["id"].as_str())
        .collect();
    let scene_ids: Vec<_> = items(&story["scenes"])
        .iter()
        .filter_map(|entry| entry["id"].as_str())
        .collect();
    for id in prompts.keys().map(String::as_str) {
        match (character_ids.contains(&id), scene_ids.contains(&id)) {
            (true, true) => {
                return Err(handoff_error(&format!(
                    "prompts/references/{id}.txt: character/scene identity is ambiguous"
                )));
            }
            (false, false) => {
                return Err(handoff_error(&format!(
                    "prompts/references/{id}.txt: unknown reference identity"
                )));
            }
            _ => {}
        }
    }
    let mut provenance = BTreeMap::new();
    for (_, receipt) in inventory {
        if receipt["outcome"] != "success" {
            continue;
        }
        let id = receipt["job_id"].as_str().expect("validated job ID");
        if let Ok(job) = document(root, &format!("generation/jobs/{id}.json"))
            && super::valid_job(&job)
            && job["subject_kind"] == "reference"
        {
            provenance.insert(
                (
                    job["subject_id"].as_str().unwrap().to_owned(),
                    receipt["raster_sha256"].as_str().unwrap().to_owned(),
                ),
                job["prompt_sha256"].clone(),
            );
        }
    }
    let mut candidates = Vec::new();
    let mut active = BTreeSet::new();
    for (ids, namespace) in [(character_ids, "characters"), (scene_ids, "scenes")] {
        for id in ids {
            let path = format!("references/{namespace}/{id}.png");
            let prompt = prompts.get(id);
            if namespace == "characters"
                && !items(&characters["characters"])
                    .iter()
                    .any(|entry| entry["id"] == id && entry["reference_path"] == path)
            {
                return Err(handoff_error(&format!(
                    "character {id}: canonical reference path is invalid"
                )));
            }
            let exists = contained_entry_exists(root, &path)?;
            let superseded = exists
                && prompt.is_some_and(|(_, bytes)| {
                    read_hash(root, &path)
                        .ok()
                        .and_then(|hash| provenance.get(&(id.to_owned(), hash)))
                        .is_some_and(|old| *old != bytes_hash(bytes))
                });
            if exists && !superseded {
                verified_hash(root, &path)?;
                active.insert(path);
            } else if prompt.is_some() {
                candidates.push(id.to_owned());
            } else if namespace == "characters" {
                return Err(handoff_error(&format!(
                    "prompts/references/{id}.txt: required prompt is missing"
                )));
            }
        }
    }
    Ok((candidates, active))
}

fn verified_hash(root: &File, path: &str) -> Result<String> {
    let mut issues = Vec::new();
    raster(
        root,
        path,
        "",
        &mut Report {
            path,
            issues: &mut issues,
        },
    );
    if !issues.is_empty() {
        return Err(Error::invalid("ValueError"));
    }
    read_hash(root, path).map_err(read_failure)
}

fn reusable<'a>(previous: Option<&'a Snapshot>, brief: &Value) -> Option<&'a Value> {
    previous?.jobs.values().find(|job| {
        BRIEF_FIELDS.iter().all(|name| job[name] == brief[name])
            && job["retry_limit"].as_u64() <= brief["retry_limit"].as_u64()
    })
}

fn complete_job(mut brief: Value, kind: &str, target: String) -> Result<Value> {
    brief["attempt_kind"] = kind.into();
    brief["target_path"] = target.into();
    brief["schema_version"] = "1.0".into();
    brief["job_id"] = contract::derive_job_id(&brief).into();
    let mut issues = Vec::new();
    contract::job(
        &brief,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    enforce(issues, false)?;
    Ok(brief)
}

fn batch(id: &str, kind: &str, jobs: &[Value]) -> Value {
    let job_ids: Vec<_> = jobs.iter().map(|job| &job["job_id"]).collect();
    json!({"batch_id": id, "kind": kind, "job_ids": job_ids})
}

fn build_references(
    root: &File,
    candidates: &[String],
    previous: Option<&Snapshot>,
    inventory: &receipts::Receipts,
) -> Result<(Vec<Value>, Vec<Value>)> {
    let batch_id = "references-001";
    let bindings: BTreeMap<_, _> = inventory
        .iter()
        .filter_map(|(_, receipt)| {
            Some((
                receipt["raster_path"].as_str()?,
                receipt["raster_sha256"].as_str()?,
            ))
        })
        .collect();
    let mut jobs = Vec::new();
    for id in candidates {
        let path = format!("prompts/references/{id}.txt");
        let brief = json!({
            "batch_id": batch_id, "prompt_path": path, "prompt_sha256": read_hash(root, &path).map_err(read_failure)?,
            "references": [], "requested_aspect_ratio": null, "requested_dimensions": null,
            "retry_limit": 2, "subject_id": id, "subject_kind": "reference",
        });
        if let Some(reused) = reusable(previous, &brief) {
            jobs.push(reused.clone());
            continue;
        }
        let target = (1..)
            .map(|sequence| format!("references/attempts/{id}/initial-{sequence:03}.png"))
            .find(|path| {
                bindings
                    .get(path.as_str())
                    .is_none_or(|expected| read_hash(root, path).ok().as_deref() != Some(*expected))
            })
            .expect("attempt target");
        jobs.push(complete_job(brief, "initial", target)?);
    }
    Ok((vec![batch(batch_id, "reference", &jobs)], jobs))
}

struct PanelJob {
    brief: Value,
    prepared: Option<Value>,
    kind: &'static str,
    sequence: u64,
    retries: u64,
}

fn build_panels(
    root: &File,
    project: &Value,
    board: &Value,
    selection: &Value,
    prompts: &Prompts,
    previous: Option<&Snapshot>,
) -> Result<(Vec<Value>, Vec<Value>)> {
    let retry_limit = project["settings"]["max_panel_retries"]
        .as_u64()
        .ok_or_else(|| handoff_error("settings.max_panel_retries: invalid retry budget"))?;
    let counters = read_counters(root)?;
    let global = counters
        .get("global_extra_calls")
        .map_or(Some(0), Value::as_u64)
        .ok_or_else(|| handoff_error("global generation counter must be a non-negative integer"))?;
    let mut available = MAX_EXTRA_CALLS
        .checked_sub(global)
        .ok_or_else(|| handoff_error("global generation counter exceeds project retry budget"))?;
    let plan: BTreeMap<_, _> = items(&selection["panels"])
        .iter()
        .filter_map(|panel| Some((panel["panel_id"].as_str()?, panel)))
        .collect();
    let mut pages = Vec::new();
    for (index, page) in items(&board["pages"]).iter().enumerate() {
        let batch_id = format!("panels-{:03}", index + 1);
        let mut specs = Vec::new();
        for panel in items(&page["panels"]) {
            let id = panel["id"].as_str().expect("validated panel ID");
            let (prompt, bytes) = &prompts[id];
            let mut references = Vec::new();
            for entry in items(&plan[id]["selected"]) {
                let path = entry["path"].as_str().expect("selection path");
                references.push(json!({"path": path, "sha256": verified_hash(root, path)?}));
            }
            if let Some(scene) = panel["scene_id"].as_str() {
                let path = format!("references/scenes/{scene}.png");
                if contained_entry_exists(root, &path)?
                    && !references.iter().any(|entry| entry["path"] == path)
                {
                    references.push(json!({"path": path, "sha256": verified_hash(root, &path)?}));
                }
            }
            let width = panel["rect"]["width"].as_u64().filter(|&value| value > 0);
            let height = panel["rect"]["height"].as_u64().filter(|&value| value > 0);
            let (Some(width), Some(height)) = (width, height) else {
                return Err(handoff_error(&format!("panel {id}: rectangle is invalid")));
            };
            let divisor = gcd(width, height);
            let brief = json!({
                "batch_id": batch_id, "prompt_path": prompt, "prompt_sha256": bytes_hash(bytes), "references": references,
                "requested_aspect_ratio": format!("{}:{}", width / divisor, height / divisor),
                "requested_dimensions": {"width": width, "height": height}, "retry_limit": retry_limit,
                "subject_id": id, "subject_kind": "panel",
            });
            let prepared = reusable(previous, &brief);
            let (kind, sequence, reserved) = if let Some(job) = prepared {
                let state = previous
                    .expect("prepared jobs")
                    .effective
                    .iter()
                    .find(|item| item["job_id"] == job["job_id"])
                    .expect("effective job");
                let reserved = if state["status"] == "ready" {
                    state["attempts_remaining"]
                        .as_u64()
                        .unwrap_or(0)
                        .saturating_sub(u64::from(
                            job["attempt_kind"] == "initial" && state["attempts_used"] == 0,
                        ))
                } else {
                    0
                };
                ("initial", 0, reserved)
            } else {
                let counts = &counters["panels"][id];
                if counts["initial"].as_u64().unwrap_or(0) == 0 {
                    ("initial", 1, 0)
                } else {
                    let retries = counts
                        .get("visual_retries")
                        .map_or(Some(0), Value::as_u64)
                        .ok_or_else(|| {
                            handoff_error(&format!("panel {id}: generation counters are invalid"))
                        })?;
                    if retries >= 2 {
                        return Err(handoff_error(&format!(
                            "panel {id}: at most two visual retries are allowed per panel"
                        )));
                    }
                    ("visual_retry", retries + 1, 1)
                }
            };
            available = available.checked_sub(reserved).ok_or_else(|| {
                handoff_error("at most eight extra calls are allowed per project")
            })?;
            specs.push(PanelJob {
                brief,
                prepared: prepared.cloned(),
                kind,
                sequence,
                retries: 0,
            });
        }
        pages.push((batch_id, specs));
    }
    for _ in 0..retry_limit {
        for spec in pages
            .iter_mut()
            .flat_map(|(_, specs)| specs.iter_mut())
            .filter(|spec| spec.prepared.is_none())
        {
            if available == 0 {
                break;
            }
            available -= 1;
            spec.retries += 1;
        }
        if available == 0 {
            break;
        }
    }
    let mut batches = Vec::new();
    let mut jobs = Vec::new();
    if let Some(previous) = previous {
        for batch in items(&previous.batches["batches"])
            .iter()
            .filter(|batch| batch["kind"] == "reference")
        {
            batches.push(batch.clone());
            jobs.extend(
                items(&batch["job_ids"])
                    .iter()
                    .map(|id| previous.jobs[id.as_str().expect("job ID")].clone()),
            );
        }
    }
    for (batch_id, specs) in pages {
        let start = jobs.len();
        for spec in specs {
            let job = match spec.prepared {
                Some(job) => job,
                None => {
                    let mut brief = spec.brief;
                    let id = brief["subject_id"].as_str().expect("subject ID").to_owned();
                    brief["retry_limit"] = spec.retries.into();
                    complete_job(
                        brief,
                        spec.kind,
                        format!(
                            "panels/attempts/{id}/{}-{:03}.png",
                            spec.kind, spec.sequence
                        ),
                    )?
                }
            };
            jobs.push(job);
        }
        batches.push(batch(&batch_id, "panel", &jobs[start..]));
    }
    Ok((batches, jobs))
}

fn gcd(mut a: u64, mut b: u64) -> u64 {
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}
