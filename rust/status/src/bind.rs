use std::fs::File;
use std::io;
use std::path::Path;

use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::LockedProject;
use crate::recovery::{Change, publish};
use crate::resume::{event, timestamp};
use crate::validation::{self, artifact, bytes_hash, items};

pub const PLAN: [(&str, &str); 3] = [
    ("story_plan", "plan/story-plan.json"),
    ("character_bible", "plan/character-bible.json"),
    ("storyboard", "plan/storyboard.json"),
];

pub fn run(directory: &Path) -> Result<Value> {
    let locked = LockedProject::open(directory, true)?;
    bind_locked(&locked)
}

pub fn bind_locked(locked: &LockedProject) -> Result<Value> {
    let current = manifest::read_project(locked.root(), false)?;
    let mut candidate = current.clone();
    apply(locked.root(), &mut candidate)?;
    let changed = candidate != current;
    let page_count = candidate["settings"]["page_count"].clone();
    let panel_count = candidate["settings"]["panel_count"].clone();
    if changed {
        let state = current["status"].as_str().unwrap_or("None");
        if !matches!(state, "INIT" | "PLANNED" | "SCRIPTED") {
            return Err(Error::invalid(format!(
                "plan bindings cannot change while project is {state}"
            )));
        }
        candidate["updated_at"] = timestamp().into();
        publish(
            locked,
            "plan-bind",
            vec![
                Change::Append(
                    "logs/events.jsonl",
                    event(
                        "plan.bound",
                        json!({"action":"bind-plan","page_count":page_count,"panel_count":panel_count}),
                    ),
                ),
                Change::Replace("project.json".into(), manifest::canonical(&candidate)),
            ],
        )?;
    }
    Ok(
        json!({"project_id":candidate["project_id"],"artifacts":PLAN.len(),
        "page_count":page_count,"panel_count":panel_count,"changed":changed}),
    )
}

pub fn apply(root: &File, data: &mut Value) -> Result<()> {
    let mut values = Vec::new();
    let mut bindings = Vec::new();
    for (key, path) in PLAN {
        let (dir, file) =
            artifact(root, path)?.ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
        let (value, raw) = manifest::read_document(&dir, &file, path)?;
        if !value.is_object() {
            return Err(Error::invalid("ValueError"));
        }
        values.push(value);
        bindings.push((
            key.to_owned(),
            json!({"path":path,"sha256":bytes_hash(raw.as_bytes())}),
        ));
    }
    let [story, characters, board] = values.as_slice() else {
        unreachable!()
    };
    validation::plan_inputs(story, characters, board)?;
    let pages = items(&board["pages"]);
    let panels: Vec<_> = pages
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .map(|panel| panel["id"].clone())
        .collect();
    if !data["settings"].is_object() || !data["artifacts"].is_object() {
        return Err(Error::invalid(
            "manifest settings and artifacts must be objects",
        ));
    }
    data["settings"]["page_count"] = pages.len().into();
    data["settings"]["panel_count"] = panels.len().into();
    data["panels"] = Value::Array(panels);
    data["artifacts"]
        .as_object_mut()
        .expect("artifact object")
        .extend(bindings);
    validation::bound_manifest(data)
}
