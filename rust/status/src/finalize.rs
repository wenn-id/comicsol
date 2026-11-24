mod composition;
mod export;
mod lettering;
pub(crate) mod native;
pub(crate) mod raster;
mod report;

use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use std::time::Duration;

use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::{self, LockedProject};
use crate::recovery::{Change, publish};
use crate::resume::{definitions, event, stages, timestamp, updated_handoff};
use crate::validation::{self, Stage, artifact, bytes_hash, items, read_value};

const PROJECT: &str = "project.json";
const CACHE: &str = "logs/stage-cache.json";
const EVENTS: &str = "logs/events.jsonl";
const HANDOFF: &str = "handoff/manifest.json";
const BOARD: &str = "plan/storyboard.json";
const COMPOSITION: &str = "cache/composition.json";
const REPORT: &str = "qa/report.md";
const MAX_RASTER: u64 = 128 * 1024 * 1024;

pub fn font_diagnostics() -> (Vec<(&'static str, bool)>, Vec<String>) {
    lettering::typography::diagnostics()
}

pub fn run(path: &Path, progress: &mut crate::human::Progress<'_>) -> Result<Value> {
    let locked = LockedProject::open_with_timeout(path, false, Duration::from_secs(300))?;
    run_locked(&locked, progress)
}

pub(crate) fn run_locked(
    locked: &LockedProject,
    progress: &mut crate::human::Progress<'_>,
) -> Result<Value> {
    let root = locked.root();
    let mut project = manifest::read_project(root, false)?;
    let status = project["status"].as_str().unwrap_or_default();
    match status {
        "BLOCKED" => {
            return Err(Error::invalid(
                "cannot finalize a BLOCKED project; resume it first",
            ));
        }
        "COMPLETE" | "COMPLETE_WITH_WARNINGS" => {
            return Err(Error::invalid(format!(
                "cannot finalize a terminal project in {status} status"
            )));
        }
        "QA_READY" | "LETTERED" | "COMPOSED" | "EXPORTED" => {}
        status => {
            let status = if status.is_empty() { "unknown" } else { status };
            return Err(Error::invalid(format!(
                "cannot finalize from {status} status; finalization requires QA_READY or later"
            )));
        }
    }
    let (cache, _) = stages::cache(root)?;
    let preserved = stages::preserved_count(root, &project, &cache)?;
    let stages = definitions();
    if let Some(stale) = stages.get(preserved).filter(|_| preserved < 3) {
        return Err(Error::invalid(format!(
            "cannot finalize while the {} stage is stale; resume first",
            stale.name
        )));
    }
    progress("WORKING", "lettering", 0, 2);

    let board = object(root, BOARD)?;
    let fallback_ids: Vec<_> = items(&board["pages"])
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .filter_map(|panel| panel["id"].as_str())
        .collect();
    let ids: Vec<_> = if items(&project["panels"]).is_empty() {
        fallback_ids
    } else {
        items(&project["panels"])
            .iter()
            .filter_map(Value::as_str)
            .collect()
    };
    let missing_lettering = ids
        .iter()
        .any(|id| !exists(root, &format!("panels/{id}/lettered.png")));
    if preserved <= 3 || missing_lettering {
        lettering::run(locked, &board)?;
        record_stage(locked, &project, "lettering")?;
    }
    advance(locked, &mut project, "QA_READY", "LETTERED")?;
    progress("WORKING", "composition", 1, 1);

    if preserved <= 4 || !exists(root, COMPOSITION) {
        composition::run(locked, &mut project, &board)?;
        record_stage(locked, &project, "composition")?;
    }
    advance(locked, &mut project, "LETTERED", "COMPOSED")?;
    progress("WORKING", "export", 2, 0);

    let count = reviewed_page_count(root, &project)?;
    let pdf = export::run(locked, &mut project, count)?;
    advance(locked, &mut project, "COMPOSED", "EXPORTED")?;
    let final_status = if items(&project["warnings"]).is_empty() {
        "COMPLETE"
    } else {
        "COMPLETE_WITH_WARNINGS"
    };
    report::run(locked, &mut project, final_status)?;
    record_stage(locked, &project, "export")?;
    require_valid(root, Stage::Final)?;
    advance(locked, &mut project, "EXPORTED", final_status)?;
    progress("COMPLETE", "export", 3, 0);
    Ok(json!({"status":final_status, "pdf":pdf, "report":REPORT}))
}

fn reviewed_page_count(root: &File, project: &Value) -> Result<usize> {
    let count = project["settings"]["page_count"]
        .as_u64()
        .filter(|&count| count > 0)
        .ok_or_else(|| {
            Error::quality("page_qa_required: settings.page_count must be a positive integer")
        })?;
    for number in 1..=count {
        let qa = format!("qa/pages/page-{number:03}.json");
        let image = format!("pages/page-{number:03}.png");
        if !exists(root, &qa) || !exists(root, &image) {
            // Python redacts path-bearing exception text at the public boundary.
            return Err(Error::quality("ValueError"));
        }
        let record = read_value(root, &qa).ok().flatten().unwrap_or_default();
        if !validation::page_quality_issues(root, number as usize, &record, None).is_empty() {
            return Err(Error::quality("ValueError"));
        }
    }

    Ok(count as usize)
}

pub fn letter_only(locked: &LockedProject) -> Result<Value> {
    let board = object(locked.root(), BOARD)?;
    lettering::run(locked, &board)?;
    let paths: Vec<_> = items(&board["pages"])
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .filter_map(|panel| panel["id"].as_str())
        .map(|id| format!("panels/{id}/lettered.png"))
        .collect();
    Ok(json!(paths))
}

pub fn compose_only(locked: &LockedProject, mut project: Value) -> Result<Value> {
    let board = object(locked.root(), BOARD)?;
    composition::run(locked, &mut project, &board)?;
    Ok(json!(
        (1..=items(&board["pages"]).len())
            .map(|number| format!("pages/page-{number:03}.png"))
            .collect::<Vec<_>>()
    ))
}

pub fn export_only(locked: &LockedProject, mut project: Value) -> Result<Value> {
    let count = reviewed_page_count(locked.root(), &project)?;
    Ok(export::run(locked, &mut project, count)?.into())
}

pub fn report_only(locked: &LockedProject, mut project: Value) -> Result<Value> {
    let status = project["status"].as_str().unwrap_or("None").to_owned();
    report::run(locked, &mut project, &status)?;
    Ok(REPORT.into())
}

pub(crate) fn require_valid(root: &File, stage: Stage) -> Result<()> {
    let issues = validation::inspect_locked(root, stage)?;
    let count = items(&issues).len();
    if count == 0 {
        Ok(())
    } else {
        Err(Error::quality(format!(
            "Comic Sol project has {count} validation issue(s)"
        )))
    }
}

pub(crate) fn advance(
    locked: &LockedProject,
    project: &mut Value,
    from: &str,
    next: &str,
) -> Result<()> {
    if project["status"] != from {
        return Ok(());
    }
    if !project["warnings"].is_array() {
        return Err(Error::invalid("manifest warnings must be an array"));
    }
    project["status"] = next.into();
    project["updated_at"] = timestamp().into();
    let details = json!({"from":from,"to":next,"warning_present":false});
    let mut changes = vec![Change::Append(
        EVENTS,
        event("project.transitioned", details),
    )];
    if let Some(handoff) = updated_handoff(locked.root(), project, next)? {
        changes.push(Change::Replace(
            HANDOFF.into(),
            manifest::canonical(&handoff),
        ));
    }
    changes.push(Change::Replace(
        PROJECT.into(),
        manifest::canonical(project),
    ));
    publish(locked, "transition", changes)
}

pub(crate) fn record_stage(locked: &LockedProject, project: &Value, name: &str) -> Result<Value> {
    let root = locked.root();
    let stage = definitions()
        .iter()
        .find(|stage| stage.name == name)
        .expect("registered stage");
    if !project["stage_versions"][name].is_string() {
        return Err(Error::invalid(
            "manifest stage_versions must contain the stage version",
        ));
    }
    let mut hashes = serde_json::Map::new();
    for path in stages::outputs(root, project, stage)? {
        let sha256 = stages::hash(root, &path)?;
        hashes.insert(path, sha256.into());
    }
    let artifact_count = hashes.len();
    let key = stages::current_key(root, project, stage)?;
    let (mut cache, _) = stages::cache(root)?;
    cache["stages"][name] = json!({"key":key,"artifacts":hashes});
    publish(
        locked,
        "stage-committed",
        vec![
            Change::Replace(CACHE.into(), manifest::canonical(&cache)),
            Change::Append(EVENTS, event("stage.recorded", json!({"action":name}))),
        ],
    )?;
    Ok(json!({"artifacts": artifact_count, "stage": name}))
}

fn descriptor(project: &mut Value, name: &str, path: &str, bytes: &[u8]) {
    if !project["artifacts"].is_object() {
        project["artifacts"] = json!({});
    }
    project["artifacts"][name] = json!({"path":path, "sha256":bytes_hash(bytes)});
}

fn publish_manifest(
    locked: &LockedProject,
    operation: &str,
    project: &Value,
    mut changes: Vec<Change>,
) -> Result<()> {
    changes.push(Change::Replace(
        PROJECT.into(),
        manifest::canonical(project),
    ));
    publish(locked, operation, changes)
}

fn object(root: &File, path: &str) -> Result<Value> {
    let value = read_value(root, path)?.ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(Error::invalid("expected a JSON object: <path>"))
    }
}

fn read(root: &File, path: &str, limit: u64) -> Result<Vec<u8>> {
    let (parent, name) =
        artifact(root, path)?.ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
    let file = project_fs::open_file(&parent, &name, false, false)?;
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(Error::limit(format!(
            "the file size limit of {limit} bytes"
        )));
    }
    Ok(bytes)
}

fn exists(root: &File, path: &str) -> bool {
    artifact(root, path).ok().flatten().is_some()
}

fn names(root: &File, dir: &str) -> Result<Vec<String>> {
    let relative = project_fs::relative_path(dir)?;
    let Some((parent, name)) = project_fs::target(root, &relative, false)? else {
        return Ok(Vec::new());
    };
    let name = Path::new(name);
    if !project_fs::metadata(&parent, name)?.is_some_and(|meta| meta.is_dir()) {
        return Ok(Vec::new());
    }
    let directory = project_fs::open_dir(&parent, name)?;
    let mut entries: Vec<_> = project_fs::names(&directory)?
        .into_iter()
        .filter_map(|name| name.into_string().ok())
        .collect();
    entries.sort_unstable();
    Ok(entries)
}

fn page_number(filename: &str, suffix: &str) -> Option<usize> {
    let digits = filename.strip_prefix("page-")?.strip_suffix(suffix)?;
    if digits.len() != 3 || !digits.bytes().all(|digit| digit.is_ascii_digit()) {
        return None;
    }
    digits.parse().ok()
}
