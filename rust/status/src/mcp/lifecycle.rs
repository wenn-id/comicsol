use std::collections::BTreeSet;
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::project_fs::{self, LockedProject};
use crate::recovery::{Change, publish};
use crate::resume::{self, definitions, event, stages, timestamp, updated_handoff};
use crate::validation::{
    self, Stage, artifact, bytes_hash, file_hash, handoff, items, panels, read_value,
};
use crate::{finalize, manifest};

const PROJECT: &str = "project.json";
const CACHE: &str = "logs/stage-cache.json";
const EVENTS: &str = "logs/events.jsonl";
const HANDOFF: &str = "handoff/manifest.json";
const MAX_RASTER: u64 = 128 * 1024 * 1024;

pub fn transition(
    locked: &LockedProject,
    mut state: Value,
    target: &str,
    warning: Option<&str>,
) -> Result<Value> {
    let from = state["status"].as_str().unwrap_or("None").to_owned();
    let mut target = target;
    let terminal = matches!(target, "COMPLETE" | "COMPLETE_WITH_WARNINGS");
    if warning.is_some() && terminal {
        return Err(Error::invalid(
            "terminal transitions cannot add --warning; record warnings before rendering the QA report",
        ));
    }
    let list = state["warnings"]
        .as_array_mut()
        .ok_or_else(|| Error::invalid("manifest warnings must be an array"))?;
    if from == "EXPORTED" && target == "COMPLETE" && !list.is_empty() {
        target = "COMPLETE_WITH_WARNINGS";
    }
    let blocked = target == "BLOCKED";
    let normal = validation::linear_statuses();
    let allowed = normal
        .iter()
        .position(|&status| status == from)
        .is_some_and(|index| {
            index + 1 < normal.len()
                && (blocked
                    || normal[index + 1] == target
                    || from == "EXPORTED" && target == "COMPLETE_WITH_WARNINGS")
        });
    if !allowed {
        return Err(Error::invalid(format!(
            "invalid Comic Sol transition: {from} -> {target}"
        )));
    }
    if terminal {
        finalize::require_valid(locked.root(), Stage::Final)?;
        if target == "COMPLETE_WITH_WARNINGS" && list.is_empty() {
            return Err(Error::invalid(
                "COMPLETE_WITH_WARNINGS requires an unresolved warning",
            ));
        }
    }
    let warning = if blocked {
        Some(
            warning
                .unwrap_or("project blocked")
                .trim_matches(validation::python_whitespace),
        )
    } else {
        warning
    };
    if let Some(warning) = warning
        && !list.iter().any(|entry| entry == warning)
    {
        list.push(warning.into());
    }
    let mut details = json!({"from": from, "to": target, "warning_present": warning.is_some()});
    if blocked {
        let warning = warning.expect("blocked warning");
        let slug = resume::legacy_slug(warning);
        let reason = if resume::PUBLIC_REASONS.contains(&slug.as_str()) {
            details["blocked_reason"] = slug.clone().into();
            slug
        } else {
            format!("warning-{}", &bytes_hash(warning.as_bytes())[..32])
        };
        state["blocked_from"] = from.into();
        state["blocked_reason"] = reason.into();
    }
    state["status"] = target.into();
    state["updated_at"] = timestamp().into();
    let mut changes = vec![Change::Append(
        EVENTS,
        event("project.transitioned", details),
    )];
    update_handoff(locked, &state, target, &mut changes)?;
    changes.push(Change::Replace(PROJECT.into(), manifest::canonical(&state)));
    publish(
        locked,
        if blocked {
            "block-project"
        } else {
            "transition"
        },
        changes,
    )?;
    Ok(state)
}

pub fn invalidate(locked: &LockedProject, mut state: Value, stage: &str) -> Result<Value> {
    if state["status"] == "BLOCKED" {
        return Err(Error::invalid(
            "cannot invalidate a BLOCKED project; resume it first",
        ));
    }
    let index = definitions()
        .iter()
        .position(|definition| definition.name == stage)
        .expect("validated resume stage");
    let stages = &definitions()[index..];
    let root = locked.root();
    let (mut cache, exists) = resume::stages::cache(root)?;
    let removed: Vec<_> = stages
        .iter()
        .flat_map(|definition| definition.artifacts.iter().copied())
        .filter(|name| state["artifacts"].get(name).is_some())
        .collect();
    resume::invalidate(&mut state, &mut cache, stages)?;
    let mut target = stages[0].rewind;
    if target == "REFERENCES_READY" && !resume::stages::references_ready(root) {
        target = "STORYBOARDED";
    }
    let reached = state["status"].as_str().unwrap_or("None");
    if resume::status_order(reached)
        .zip(resume::status_order(target))
        .is_some_and(|(current, target)| current < target)
    {
        target = reached;
    }
    state["status"] = target.into();
    state["updated_at"] = timestamp().into();
    let mut changes = Vec::new();
    if exists {
        changes.push(Change::Replace(CACHE.into(), manifest::canonical(&cache)));
    }
    changes.push(Change::Replace(PROJECT.into(), manifest::canonical(&state)));
    publish(locked, "invalidate", changes)?;
    Ok(json!(removed))
}

pub fn record_attempt(
    locked: &LockedProject,
    panel: &str,
    kind: &str,
    path: &str,
) -> Result<Value> {
    let root = locked.root();
    read_raster(root, path, true)?;
    let (doc, counts) = handoff::account_panel_attempt(root, panel, kind)?;
    publish(
        locked,
        "record-generation-attempt",
        vec![
            Change::Replace(
                "logs/generation-counters.json".into(),
                manifest::canonical(&doc),
            ),
            Change::Append(
                EVENTS,
                event(
                    "generation.attempt-recorded",
                    json!({
                        "attempt_path": path, "kind": kind, "panel_id": panel,
                    }),
                ),
            ),
        ],
    )?;
    Ok(counts)
}

pub fn promote(locked: &LockedProject, panel: &str, path: &str) -> Result<Value> {
    let root = locked.root();
    let (new, _) = read_raster(root, path, false)?;
    let destination = format!("panels/raw/{panel}.png");
    let mut details = json!({"attempt_path": path, "panel_id": panel});
    let mut changes = Vec::new();
    let old = read_bytes(root, &destination)?;
    let replaced = old.is_some();
    if let Some(old) = old {
        if old == new {
            return Ok(destination.into());
        }
        let review = format!("qa/panels/{panel}.json");
        if let Some(record) = read_value(root, &review).map_err(|error| {
            if error.code() == "CS-SEC-002" {
                error
            } else {
                Error::invalid("ValueError")
            }
        })? {
            if record["decision"] != "regenerate" {
                return Err(Error::invalid("ValueError"));
            }
        }
        if let Ok(Some(plan)) = read_value(root, "logs/repair-plan.json")
            && let Some(strategy) = items(&plan["panels"])
                .iter()
                .find(|entry| entry["panel_id"] == panel)
                .and_then(|entry| entry["strategy"].as_str())
                .filter(|value| {
                    matches!(
                        *value,
                        "no-repair" | "selective-repair" | "full-regeneration"
                    )
                })
        {
            details["strategy"] = strategy.into();
        }
        let mut archive = None;
        for index in 1..=100_000 {
            let candidate = format!("panels/raw/{panel}.attempt-{index}.png");
            if candidate != path && read_bytes(root, &candidate).is_ok_and(|bytes| bytes.is_none())
            {
                archive = Some(candidate);
                break;
            }
        }
        changes.push(Change::Replace(
            archive.ok_or_else(|| Error::invalid("no retained attempt slot available"))?,
            old,
        ));
    }
    changes.push(Change::Replace(destination.clone(), new));
    let mut events = event("generation.attempt-promoted", details);
    if replaced {
        events.extend(event(
            "artifact.regenerated",
            json!({"artifact_path": destination, "reused": false}),
        ));
    }
    changes.push(Change::Append(EVENTS, events));
    publish(locked, "promote-attempt", changes)?;
    Ok(destination.into())
}

pub fn override_panel(
    locked: &LockedProject,
    mut state: Value,
    panel: &str,
    explanation: &str,
) -> Result<Value> {
    let path = format!("qa/panels/{panel}.json");
    let root = locked.root();
    let mut record =
        read_value(root, &path)?.ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
    let modern = record["schema_version"] == "2.0";
    if record[if modern { "subject_id" } else { "panel_id" }] != panel {
        return Err(Error::invalid(
            "panel QA record does not match the requested panel",
        ));
    }
    if !modern {
        match record["failure_category"].as_str() {
            Some("visual_qa") => {}
            Some(category @ ("corrupt" | "corrupt_image" | "safety" | "safety_refusal")) => {
                return Err(Error::invalid(format!("{category} cannot be overridden")));
            }
            _ => {
                return Err(Error::invalid(
                    "only non-safety visual QA errors can be overridden",
                ));
            }
        }
    }
    let failed = |check: &Value| check["result"] == "fail" && check["severity"] == "error";
    if !items(&record["checks"]).iter().any(failed) {
        return Err(Error::invalid(
            "override requires an error-level failed check",
        ));
    }
    if record["decision"] != "regenerate" {
        return Err(Error::invalid(
            "overridable visual QA errors must require regeneration",
        ));
    }
    if modern {
        if !panels::reusable(root, &record, panel)? {
            return Err(Error::invalid(
                "panel artifacts cannot be overridden: accepted panel QA record is invalid",
            ));
        }
    } else {
        let raw = format!("panels/raw/{panel}.png");
        let clean = format!("panels/clean/{panel}.png");
        let valid = || {
            let (bytes, size) = read_raster(root, &raw, false).ok()?;
            let (_, clean_size) = read_raster(root, &clean, false).ok()?;
            Some(
                record["raw_path"] == raw
                    && record["clean_path"] == clean
                    && record["raw_sha256"] == bytes_hash(&bytes)
                    && size == clean_size
                    && record["dimensions"]["width"] == size.0
                    && record["dimensions"]["height"] == size.1,
            )
        };
        if valid() != Some(true) {
            return Err(Error::invalid("corrupt images cannot be overridden"));
        }
    }
    let warnings = record["unresolved_warnings"]
        .as_array_mut()
        .ok_or_else(|| Error::invalid("panel unresolved_warnings must be an array"))?;
    if !warnings.iter().any(|warning| warning == explanation) {
        warnings.push(explanation.into());
    }
    let mut additions = false;
    let manifest_warnings = state["warnings"]
        .as_array_mut()
        .ok_or_else(|| Error::invalid("manifest warnings must be an array"))?;
    for warning in warnings {
        if !manifest_warnings.contains(warning) {
            manifest_warnings.push(warning.clone());
            additions = true;
        }
    }
    for check in record["checks"]
        .as_array_mut()
        .expect("failed checks")
        .iter_mut()
        .filter(|item| failed(item))
    {
        check["severity"] = "warning".into();
        if check["id"] == "character-identity"
            && let Some(regions) = check["regions"].as_array_mut()
        {
            for region in regions.iter_mut().filter(|item| failed(item)) {
                region["severity"] = "warning".into();
            }
        }
    }
    record["decision"] = if modern {
        "accept-warning"
    } else {
        "accept_with_warnings"
    }
    .into();
    record["override_reason"] = explanation.into();
    if !modern {
        record["retry_reason"] = Value::Null;
    }
    let mut changes = Vec::new();
    if state["status"] == "COMPLETE" && !items(&state["warnings"]).is_empty() {
        state["status"] = "COMPLETE_WITH_WARNINGS".into();
        update_handoff(locked, &state, "COMPLETE_WITH_WARNINGS", &mut changes)?;
        additions = true;
    }
    if additions {
        state["updated_at"] = timestamp().into();
    }
    changes.extend([
        Change::Replace(path, manifest::canonical(&record)),
        Change::Replace(PROJECT.into(), manifest::canonical(&state)),
        Change::Append(
            EVENTS,
            event(
                "panel.overridden",
                json!({"panel_id": panel, "action": "accepted"}),
            ),
        ),
    ]);
    publish(locked, "override", changes)?;
    Ok(format!("{panel}: accepted with warnings").into())
}

pub fn plan(root: &File, project: &Value) -> Result<Value> {
    if !project["stage_versions"].is_object() {
        return Err(Error::invalid("manifest stage_versions must be an object"));
    }
    let (cache, present) = stages::cache(root)?;
    let descriptors = project.get("artifacts").and_then(Value::as_object);
    let semantic: BTreeSet<_> = ["story_plan", "character_bible", "storyboard"]
        .into_iter()
        .filter_map(|name| descriptors?.get(name)?["path"].as_str())
        .collect();
    let mut actions = Vec::new();
    let mut stale = None;
    let mut generation_reusable = false;
    for (index, stage) in definitions().iter().enumerate() {
        let mut reason = None;
        if let Some(previous) = stale {
            reason = Some(format!("depends on stale {previous} stage"));
        } else {
            let cached = &cache["stages"][stage.name];
            if index == 0
                && present
                && cache["stages"]
                    .as_object()
                    .is_some_and(|value| value.is_empty())
                && read_value(root, CACHE).ok().flatten().as_ref() != Some(&cache)
            {
                reason = Some("stage cache is invalid: stage cache structure is invalid".into());
            }
            if index == 0 && project.get("artifacts").is_some() && descriptors.is_none() {
                reason.get_or_insert_with(|| "manifest artifacts are invalid".into());
            }
            for (name, value) in descriptors.into_iter().flatten().filter(|(name, value)| {
                stage.artifacts.contains(&name.as_str()) && value.is_object()
            }) {
                if reason.is_some() {
                    break;
                }
                let (Some(path), Some(hash)) = (value["path"].as_str(), value["sha256"].as_str())
                else {
                    reason = Some(format!("artifact descriptor is invalid: {name}"));
                    continue;
                };
                reason = artifact_problem(root, path, hash, Some(name))?;
            }
            if reason.is_none() {
                if let Some(artifacts) = cached["artifacts"].as_object() {
                    match stages::outputs(root, project, stage) {
                        Ok(outputs)
                            if outputs.iter().map(String::as_str).collect::<BTreeSet<_>>()
                                != artifacts.keys().map(String::as_str).collect() =>
                        {
                            reason =
                                Some("cached artifact set does not match stage outputs".into());
                        }
                        Ok(_) => {
                            for (path, hash) in artifacts
                                .iter()
                                .filter(|(path, _)| !semantic.contains(path.as_str()))
                            {
                                reason = artifact_problem(
                                    root,
                                    path,
                                    hash.as_str().unwrap_or_default(),
                                    None,
                                )?;
                                if reason.is_some() {
                                    break;
                                }
                            }
                        }
                        Err(error) => {
                            reason = Some(format!(
                                "stage outputs are unavailable: {}",
                                error.exception()
                            ))
                        }
                    }
                    if reason.is_none() {
                        reason = match stages::current_key(root, project, stage) {
                            Ok(key) if cached["key"] == key => None,
                            Ok(_) => Some("stage cache key changed".into()),
                            Err(error) => Some(format!(
                                "stage inputs are unavailable: {}",
                                error.exception()
                            )),
                        };
                    }
                } else {
                    reason = Some("stage cache entry is missing".into());
                }
            }
        }
        let action = if reason.is_none() {
            "reuse"
        } else if stage.name == "generation" {
            "regenerate"
        } else {
            "rerun"
        };
        if stale.is_none() && reason.is_some() {
            stale = Some(stage.name);
        }
        generation_reusable |= stage.name == "generation" && action == "reuse";
        actions.push(
            json!({"stage": stage.name, "action": action, "artifact": "stage",
            "reason": reason.as_deref().unwrap_or("cache key and artifacts match")}),
        );
    }
    let mut temporary = BTreeSet::new();
    scan_temporary(root, ".", &mut temporary, 0)?;
    for path in temporary {
        actions.push(
            json!({"stage": "generation", "action": "rerun", "artifact": path,
            "reason": "interrupted temporary file ignored and preserved"}),
        );
    }
    let board = read_value(root, "plan/storyboard.json")
        .ok()
        .flatten()
        .unwrap_or_default();
    let ids: BTreeSet<_> = items(&project["panels"])
        .iter()
        .filter_map(Value::as_str)
        .chain(
            items(&board["pages"])
                .iter()
                .flat_map(|page| items(&page["panels"]))
                .filter_map(|panel| panel["id"].as_str()),
        )
        .filter(|id| validation::is_panel(id))
        .collect();
    for id in ids {
        let path = format!("qa/panels/{id}.json");
        let (subject, accepted, problem) = match read_value(root, &path) {
            Ok(None) => continue,
            Err(error) => (
                id.to_owned(),
                false,
                Some(format!("panel QA record is invalid: {}", error.exception())),
            ),
            Ok(Some(record)) => {
                let v2 = record["schema_version"] == "2.0";
                let Some(subject) = record[if v2 { "subject_id" } else { "panel_id" }].as_str()
                else {
                    continue;
                };
                let accepted = record["decision"] == "accept"
                    || record["decision"]
                        == if v2 {
                            "accept-warning"
                        } else {
                            "accept_with_warnings"
                        };
                let problem = if accepted && !panels::reusable(root, &record, subject)? {
                    Some("accepted panel QA record is invalid".into())
                } else if accepted && !generation_reusable {
                    Some("generation stage cache is stale or missing".into())
                } else {
                    None
                };
                (subject.to_owned(), accepted, problem)
            }
        };
        let reuse = accepted && problem.is_none();
        actions.push(json!({"stage": "generation", "action": if reuse {"reuse"} else {"regenerate"}, "artifact": subject,
            "reason": problem.as_deref().unwrap_or(if reuse {"accepted QA artifact is reusable"} else {"panel QA requires regeneration"})}));
    }
    Ok(json!(actions))
}

fn artifact_problem(
    root: &File,
    path: &str,
    expected: &str,
    name: Option<&str>,
) -> Result<Option<String>> {
    Ok(match file_hash(root, path) {
        Ok(Some(hash)) if hash == expected => None,
        Ok(Some(_)) => Some(format!("artifact hash mismatch: {path}")),
        Ok(None) => Some(format!("artifact is missing: {path}")),
        Err(_) if name.is_some() => Some(format!(
            "artifact path escapes the project: {}",
            name.unwrap()
        )),
        Err(error) => return Err(error),
    })
}

fn scan_temporary(
    directory: &File,
    prefix: &str,
    out: &mut BTreeSet<String>,
    depth: usize,
) -> Result<()> {
    if depth > 128 {
        return Err(Error::limit("the maximum project path depth"));
    }
    for name in project_fs::names_bounded(directory, 100_000)? {
        let Some(name) = name.to_str() else {
            continue;
        };
        let path = if prefix == "." {
            name.to_owned()
        } else {
            format!("{prefix}/{name}")
        };
        let leaf = Path::new(name);
        if let Some(meta) = project_fs::metadata(directory, leaf)? {
            if meta.is_dir() {
                scan_temporary(
                    &project_fs::open_dir(directory, leaf)?,
                    &path,
                    out,
                    depth + 1,
                )?;
            } else if meta.is_file() && name.ends_with(".tmp") {
                out.insert(path);
            }
        }
    }
    Ok(())
}

fn update_handoff(
    locked: &LockedProject,
    state: &Value,
    target: &str,
    changes: &mut Vec<Change>,
) -> Result<()> {
    if let Some(handoff) = updated_handoff(locked.root(), state, target)? {
        changes.push(Change::Replace(
            HANDOFF.into(),
            manifest::canonical(&handoff),
        ));
    }
    Ok(())
}

fn read_bytes(root: &File, path: &str) -> Result<Option<Vec<u8>>> {
    let Some((parent, leaf)) = artifact(root, path)? else {
        return Ok(None);
    };
    let stream = project_fs::open_file(&parent, &leaf, false, false)?;
    let mut bytes = Vec::new();
    stream.take(MAX_RASTER + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_RASTER {
        return Err(Error::limit(format!(
            "the encoded raster size limit of {MAX_RASTER} bytes"
        )));
    }
    Ok(Some(bytes))
}

fn read_raster(root: &File, path: &str, missing_is_io: bool) -> Result<(Vec<u8>, (u32, u32))> {
    let bytes = read_bytes(root, path)?.ok_or_else(|| {
        if missing_is_io {
            io::Error::from(io::ErrorKind::NotFound).into()
        } else {
            Error::invalid("attempt path must be a retained file")
        }
    })?;
    let raster = panels::decode_bytes(&bytes)
        .map_err(|_| Error::invalid("attempt must be a readable raster"))?;
    if raster.width < 512 || raster.height < 512 {
        return Err(Error::invalid(
            "attempt must be a readable raster at least 512px",
        ));
    }
    Ok((bytes, (raster.width, raster.height)))
}
