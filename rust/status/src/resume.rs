pub(crate) mod stages;

use std::fs::{self, File};
use std::path::Path;

use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::LockedProject;
use crate::recovery::{Change, publish};
use crate::validation::{
    bytes_hash, handoff_contract, is_identifier, items, python_whitespace, read_value,
};

const CACHE: &str = "logs/stage-cache.json";
const EVENTS: &str = "logs/events.jsonl";
const HANDOFF: &str = "handoff/manifest.json";
const PROJECT: &str = "project.json";
pub(crate) const PUBLIC_REASONS: &[&str] = &[
    "artifact-missing",
    "executor-failure",
    "image-capability-unavailable",
    "input-invalid",
    "io-error",
    "permission-denied",
    "project-blocked",
    "provider-refusal",
    "quality-failure",
    "resource-limit",
    "validation-failure",
    "other",
];

pub(crate) struct Definition {
    pub(crate) name: &'static str,
    pub(crate) rewind: &'static str,
    pub(crate) complete: &'static str,
    runner: Option<&'static str>,
    pub(crate) artifacts: &'static [&'static str],
}

include!(concat!(env!("OUT_DIR"), "/resume_stages.rs"));

pub(crate) fn definitions() -> &'static [Definition] {
    STAGES
}

pub fn run(directory: &Path, progress: &mut crate::human::Progress<'_>) -> Result<Value> {
    let canonical = fs::canonicalize(if directory.as_os_str().is_empty() {
        Path::new(".")
    } else {
        directory
    })?;
    let locked = LockedProject::open(&canonical, true)?;
    progress("WORKING", "resume", 0, STAGES.len());
    let result = resume_locked(&locked, &canonical)?;
    let state = match result["status"].as_str() {
        Some("BLOCKED") => "BLOCKED",
        Some("COMPLETE" | "COMPLETE_WITH_WARNINGS") => "COMPLETE",
        _ => "WORKING",
    };
    progress(
        state,
        "resume",
        items(&result["preserved"]).len(),
        items(&result["invalidated"]).len(),
    );
    Ok(result)
}

pub(crate) fn resume_locked(locked: &LockedProject, canonical: &Path) -> Result<Value> {
    let root = locked.root();
    let mut project = manifest::read_project(root, false)?;
    let (mut cache, cache_exists) = stages::cache(root)?;
    let count = stages::preserved_count(root, &project, &cache)?;
    let stale = STAGES.get(count);
    let preserved: Vec<_> = STAGES[..count].iter().map(|stage| stage.name).collect();
    let invalidated: Vec<_> = STAGES[count..].iter().map(|stage| stage.name).collect();
    if project["status"] != "BLOCKED" {
        return Ok(json!({
            "status": project["status"], "preserved": preserved, "invalidated": invalidated,
            "next_action": next_action(canonical, stale),
        }));
    }
    let reason = project["blocked_reason"]
        .as_str()
        .filter(|reason| is_identifier(reason, 64, true))
        .unwrap_or("legacy-blocked")
        .to_owned();
    if reason == "image-capability-unavailable" && project["capability"]["status"] != "available" {
        return Ok(json!({
            "status":"BLOCKED", "preserved":preserved, "invalidated":[],
            "next_action":{"required":"image capability available"},
        }));
    }
    let last = STAGES[..count]
        .last()
        .map_or("INIT", |stage| stage.complete);
    let reached = project["blocked_from"]
        .as_str()
        .filter(|state| status_order(state).is_some())
        .unwrap_or(last);
    let rewind = stale.map_or(last, |stage| {
        if stage.rewind == "REFERENCES_READY" && !stages::references_ready(root) {
            "STORYBOARDED"
        } else {
            stage.rewind
        }
    });
    let state = if status_order(reached) < status_order(rewind) {
        reached
    } else {
        rewind
    }
    .to_owned();
    let mut events = Vec::new();
    for stage in &STAGES[..count] {
        let mut artifacts = serde_json::Map::new();
        for output in stages::outputs(root, &project, stage)? {
            artifacts.insert(output.clone(), stages::hash(root, &output)?.into());
            events.extend(event(
                "artifact.reused",
                json!({"artifact_path":output, "reused":true}),
            ));
        }
        cache["stages"][stage.name]["artifacts"] = Value::Object(artifacts);
    }
    if stale.is_some() {
        invalidate(&mut project, &mut cache, &STAGES[count..])?;
    }
    let warnings = cleared_warnings(items(&project["warnings"]), &reason);
    project["warnings"] = Value::Array(warnings);
    project["status"] = state.clone().into();
    project["blocked_from"] = Value::Null;
    project["blocked_reason"] = Value::Null;
    project["updated_at"] = timestamp().into();

    let mut changes = Vec::new();
    if cache_exists || count > 0 {
        changes.push(Change::Replace(CACHE.into(), manifest::canonical(&cache)));
    }
    let category = if PUBLIC_REASONS.contains(&reason.as_str()) {
        &reason
    } else {
        "other"
    };
    events.extend(event(
        "project.resumed",
        json!({"blocked_reason":category,"from":"BLOCKED","to":state}),
    ));
    changes.push(Change::Append(EVENTS, events));
    if let Some(handoff) = updated_handoff(root, &project, &state)? {
        changes.push(Change::Replace(
            HANDOFF.into(),
            manifest::canonical(&handoff),
        ));
    }
    changes.push(Change::Replace(
        PROJECT.into(),
        manifest::canonical(&project),
    ));
    publish(locked, "resume-recovery", changes)?;
    Ok(json!({
        "status":state, "preserved":preserved, "invalidated":invalidated,
        "next_action":next_action(canonical, stale),
    }))
}

pub(crate) fn invalidate(
    project: &mut Value,
    cache: &mut Value,
    invalidated: &[Definition],
) -> Result<()> {
    let artifacts = project["artifacts"]
        .as_object_mut()
        .ok_or_else(|| Error::invalid("manifest artifacts must be an object"))?;
    let stages = cache["stages"]
        .as_object_mut()
        .expect("validated stage cache");
    for stage in invalidated {
        stages.remove(stage.name);
        for artifact in stage.artifacts {
            artifacts.remove(*artifact);
        }
    }
    if project["handoff"].is_object() {
        project["handoff"] =
            json!({"contract_version":"1.0", "locked_scope_sha256":null, "manifest_path":null});
    }
    Ok(())
}

pub(crate) fn status_order(state: &str) -> Option<usize> {
    crate::validation::linear_statuses()
        .iter()
        .position(|current| *current == state)
}

fn cleared_warnings(warnings: &[Value], reason: &str) -> Vec<Value> {
    let matches_reason = |warning: &Value| {
        let Some(warning) = warning.as_str() else {
            return false;
        };
        let slug = legacy_slug(warning);
        if reason.strip_prefix("warning-").is_some_and(|suffix| {
            suffix.len() == 32
                && suffix
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        }) {
            let trimmed = warning.trim_matches(python_whitespace);
            !trimmed.is_empty()
                && !PUBLIC_REASONS.contains(&slug.as_str())
                && reason[8..] == bytes_hash(trimmed.as_bytes())[..32]
        } else {
            slug == reason
        }
    };
    let fallback = if reason == "project-blocked" && !warnings.iter().any(matches_reason) {
        let mut unclassified = warnings.iter().enumerate().filter(|(_, warning)| {
            warning.as_str().is_some_and(|warning| {
                !warning.trim_matches(python_whitespace).is_empty()
                    && legacy_slug(warning).is_empty()
            })
        });
        let first = unclassified.next().map(|(i, _)| i);
        first.filter(|_| unclassified.next().is_none())
    } else {
        None
    };
    warnings
        .iter()
        .enumerate()
        .filter(|(index, warning)| Some(*index) != fallback && !matches_reason(warning))
        .map(|(_, warning)| warning.clone())
        .collect()
}

pub(crate) fn legacy_slug(warning: &str) -> String {
    warning
        .to_lowercase()
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("-")
}

pub(crate) fn timestamp() -> String {
    let now = time::OffsetDateTime::now_utc();
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        now.year(),
        u8::from(now.month()),
        now.day(),
        now.hour(),
        now.minute(),
        now.second()
    )
}

pub(crate) fn event(name: &str, details: Value) -> Vec<u8> {
    let mut bytes =
        manifest::compact(&json!({"details":details, "event":name, "timestamp":timestamp()}));
    bytes.push(b'\n');
    bytes
}

pub(crate) fn updated_handoff(root: &File, project: &Value, state: &str) -> Result<Option<Value>> {
    let binding = &project["handoff"];
    if binding["manifest_path"] != HANDOFF || !binding["locked_scope_sha256"].is_string() {
        return Ok(None);
    }
    let mut handoff = match read_value(root, HANDOFF) {
        Ok(Some(value)) if value.is_object() => value,
        Ok(_) => return Ok(None),
        Err(error) if matches!(error.code(), "CS-PROJ-001" | "CS-SEC-002") => return Ok(None),
        Err(error) => return Err(error),
    };
    if handoff["stage"] == state {
        return Ok(None);
    }
    handoff["stage"] = state.into();
    handoff_contract(&handoff)?;
    Ok(Some(handoff))
}

pub(crate) fn next_action(project: &Path, stage: Option<&Definition>) -> Value {
    let Some(stage) = stage else {
        return Value::Null;
    };
    if stage.runner.is_none() {
        return json!({"agent_required":stage.name});
    }
    let executable = std::env::current_exe().unwrap_or_else(|_| Path::new("comic-sol").to_owned());
    json!({"command":format!(
        "{} finalize {}", quote(&display_path(&executable)), quote(&display_path(project))
    )})
}

fn display_path(path: &Path) -> String {
    let text = path.to_string_lossy();
    if cfg!(windows) {
        if let Some(tail) = text.strip_prefix(r"\\?\UNC\") {
            return format!(r"\\{tail}");
        }
        if let Some(tail) = text.strip_prefix(r"\\?\") {
            return tail.to_owned();
        }
    }
    text.into_owned()
}

fn quote(text: &str) -> String {
    if !text.is_empty()
        && text
            .chars()
            .all(|ch| ch.is_alphanumeric() || "_@%+=:,./-".contains(ch))
    {
        text.to_owned()
    } else {
        format!("'{}'", text.replace('\'', "'\"'\"'"))
    }
}
