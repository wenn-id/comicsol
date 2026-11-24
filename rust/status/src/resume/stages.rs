use std::collections::HashSet;
use std::fs::File;
use std::io;

use serde_json::{Map, Value, json};

use super::{CACHE, Definition, STAGES};
use crate::error::{Error, Result};
use crate::validation::{
    artifact, bytes_hash, file_hash, is_panel, is_sha256, items, keys, panels, read_value,
};
use crate::{manifest, project_fs};

const STORY: &str = "plan/story-plan.json";
const BIBLE: &str = "plan/character-bible.json";
const BOARD: &str = "plan/storyboard.json";
const REPORT: &str = "qa/report.md";
const VERIFICATION: &str = "exports/pdf-verification.json";
const GENERATED: &str = "generated-visual";

pub(crate) fn cache(root: &File) -> Result<(Value, bool)> {
    let present = match artifact(root, CACHE) {
        Ok(file) => file.is_some(),
        Err(error) => return Err(error),
    };
    let value = if present {
        read_value(root, CACHE).ok().flatten().filter(valid_cache)
    } else {
        None
    };
    Ok((
        value.unwrap_or_else(|| json!({"schema_version":"1.0","stages":{}})),
        present,
    ))
}

fn valid_cache(value: &Value) -> bool {
    keys(value, &["schema_version", "stages"])
        && value["schema_version"] == "1.0"
        && value["stages"].as_object().is_some_and(|stages| {
            stages.iter().all(|(name, stage)| {
                STAGES.iter().any(|definition| definition.name == name)
                    && keys(stage, &["key", "artifacts"])
                    && stage["key"].as_str().is_some_and(is_sha256)
                    && stage["artifacts"].as_object().is_some_and(|artifacts| {
                        artifacts.iter().all(|(path, digest)| {
                            path.split('/').all(|part| !matches!(part, "" | "." | ".."))
                                && !path.contains('\\')
                                && project_fs::relative_path(path).is_ok()
                                && digest.as_str().is_some_and(is_sha256)
                        })
                    })
            })
        })
}

fn object(root: &File, path: &str) -> Result<Value> {
    let value = read_value(root, path)?.ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(Error::invalid("expected a JSON object: <path>"))
    }
}

fn board_panels(board: &Value) -> impl Iterator<Item = &Value> {
    items(&board["pages"])
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .filter(|panel| panel.is_object())
}

fn panel_clean(root: &File, id: &str) -> Result<String> {
    let qa = object(root, &format!("qa/panels/{id}.json"))?;
    if qa["schema_version"] != "2.0" {
        return Ok(format!("panels/clean/{id}.png"));
    }
    let path = format!("panels/{id}/clean.png");
    if qa["bindings"]["clean_path"] == path {
        Ok(path)
    } else {
        Err(Error::invalid("panel QA clean path is not canonical"))
    }
}

fn page_path(directory: &str, n: i64, extension: &str) -> String {
    format!("{directory}/page-{n:03}.{extension}")
}

fn python_int(value: &Value) -> Option<i64> {
    value.as_i64().or_else(|| value.as_bool().map(i64::from))
}

pub(crate) fn outputs(root: &File, project: &Value, stage: &Definition) -> Result<Vec<String>> {
    match stage.name {
        "planning" => return Ok(vec![STORY.into(), BIBLE.into()]),
        "storyboard" => return Ok(vec![BOARD.into()]),
        _ => {}
    }
    let board = object(root, BOARD)?;
    let ids: Vec<_> = board_panels(&board)
        .filter_map(|panel| panel["id"].as_str())
        .collect();
    if ids.is_empty() {
        return Err(Error::invalid("storyboard has no panels"));
    }
    let paths = match stage.name {
        "generation" => {
            let mut paths: Vec<_> = ids
                .iter()
                .map(|id| format!("panels/raw/{id}.png"))
                .collect();
            for id in ids {
                paths.push(panel_clean(root, id)?);
            }
            paths
        }
        "lettering" => ids
            .iter()
            .map(|id| format!("panels/{id}/lettered.png"))
            .collect(),
        "composition" => items(&board["pages"])
            .iter()
            .filter_map(|page| Some(page_path("pages", python_int(&page["number"])?, "png")))
            .collect(),
        "export" => {
            let mut paths = vec![REPORT.into(), VERIFICATION.into()];
            if let Some(id) = project["project_id"]
                .as_str()
                .filter(|text| !text.is_empty())
            {
                paths.push(format!("exports/{id}.pdf"));
            }
            paths
        }
        _ => unreachable!("stage registry validated at build time"),
    };
    for path in &paths {
        let relative = project_fs::relative_path(path)?;
        if let Some((parent, name)) = project_fs::target(root, &relative, false)? {
            project_fs::metadata(&parent, std::path::Path::new(name))?;
        }
    }
    Ok(paths)
}

fn clean_sfx_defaults(item: &mut Value) {
    if item["kind"] == "sfx" && item["render_mode"] == GENERATED {
        item.as_object_mut()
            .expect("SFX object")
            .remove("render_mode");
    }
}

fn visual_material(panel: &Value) -> Value {
    let mut panel = panel.clone();
    let generated: Vec<_> = items(&panel["text"])
        .iter()
        .filter(|item| {
            item["kind"] == "sfx"
                && (item["render_mode"].is_null() || item["render_mode"] == GENERATED)
        })
        .cloned()
        .map(|mut item| {
            clean_sfx_defaults(&mut item);
            item
        })
        .collect();
    if generated.is_empty() {
        panel
            .as_object_mut()
            .expect("storyboard panel object")
            .remove("text");
    } else {
        panel["text"] = Value::Array(generated);
    }
    panel
}

fn generation_material(
    root: &File,
    project: &Value,
    characters: Value,
    panels: &[&Value],
    ids: &[&str],
) -> Result<(Value, Vec<String>)> {
    let visual: Vec<_> = panels.iter().map(|panel| visual_material(panel)).collect();
    let mut dependencies = Vec::new();
    let mut prompts = Vec::new();
    let mut actual_refs = Vec::new();
    for &id in ids {
        let record = object(root, &format!("qa/panels/{id}.json"))?;
        let (prompt, refs) = if record["schema_version"] == "2.0" && record["subject_id"] == id {
            let mut refs = Vec::new();
            let panel = panels
                .iter()
                .rev()
                .find(|panel| panel["id"] == id)
                .expect("listed panel");
            if let Some(scene) = panel["scene_id"].as_str() {
                let path = format!("references/scenes/{scene}.png");
                let relative = project_fs::relative_path(&path)?;
                if let Some((dir, name)) = project_fs::target(root, &relative, false)?
                    && project_fs::metadata(&dir, std::path::Path::new(name))?.is_some()
                {
                    refs.push(path);
                }
            }
            (format!("prompts/panels/{id}.txt"), refs)
        } else if record["schema_version"] != "2.0" && record["panel_id"] == id {
            let prompt = record["source_prompt_path"].as_str();
            let refs = record["generation"]["reference_paths"]
                .as_array()
                .and_then(|refs| {
                    refs.iter()
                        .map(|item| item.as_str().map(str::to_owned))
                        .collect::<Option<Vec<_>>>()
                });
            match (prompt, refs) {
                (Some(prompt), Some(refs)) => (prompt.to_owned(), refs),
                _ => return Err(Error::invalid("panel QA dependencies are invalid")),
            }
        } else {
            return Err(Error::invalid("panel QA record does not match its panel"));
        };
        dependencies
            .push(json!({"panel_id":id,"reference_paths":refs,"source_prompt_path":prompt}));
        prompts.push(prompt);
        actual_refs.extend(refs);
    }
    let references = items(&characters["characters"])
        .iter()
        .filter_map(|character| character["reference_path"].as_str().map(str::to_owned));
    let mut seen = HashSet::new();
    let files = prompts
        .into_iter()
        .chain(references)
        .chain(actual_refs)
        .filter(|path| seen.insert(path.clone()))
        .collect();
    let capability = project
        .get("capability")
        .cloned()
        .unwrap_or_else(|| json!({}));
    Ok((json!([visual, characters, capability, dependencies]), files))
}

fn material(root: &File, project: &Value, stage: &Definition) -> Result<(Value, Vec<String>)> {
    if stage.name == "planning" {
        return Ok((
            json!([object(root, "source/request.json")?]),
            vec!["source/input.txt".into()],
        ));
    }
    let story = object(root, STORY)?;
    let characters = object(root, BIBLE)?;
    if stage.name == "storyboard" {
        let identities: Vec<_> = items(&characters["characters"])
            .iter()
            .filter_map(|item| Some(json!({"id": item["id"].as_str()?})))
            .collect();
        return Ok((json!([story, identities]), Vec::new()));
    }
    let board = object(root, BOARD)?;
    let panels: Vec<_> = board_panels(&board).collect();
    let ids: Vec<_> = panels
        .iter()
        .filter_map(|panel| panel["id"].as_str())
        .collect();
    if ids.is_empty() {
        return Err(Error::invalid("storyboard has no panels"));
    }
    match stage.name {
        "generation" => generation_material(root, project, characters, &panels, &ids),
        "lettering" => {
            let texts: Vec<_> = panels
                .iter()
                .map(|panel| {
                    let mut text = panel.get("text").cloned().unwrap_or_else(|| json!([]));
                    if let Some(items) = text.as_array_mut() {
                        for item in items {
                            clean_sfx_defaults(item);
                        }
                    }
                    text
                })
                .collect();
            let files = ids
                .iter()
                .map(|id| panel_clean(root, id))
                .collect::<Result<Vec<_>>>()?;
            Ok((json!([texts]), files))
        }
        "composition" => {
            let geometry: Vec<_> = items(&board["pages"])
                .iter()
                .filter(|page| page.is_object())
                .map(|page| {
                    let rectangles: Vec<_> = items(&page["panels"])
                        .iter()
                        .filter(|panel| panel.is_object())
                        .map(|panel| &panel["rect"])
                        .collect();
                    json!({"number":page["number"],"layout":page["layout"],"panels":rectangles})
                })
                .collect();
            Ok((
                json!([geometry]),
                ids.iter()
                    .map(|id| format!("panels/{id}/lettered.png"))
                    .collect(),
            ))
        }
        "export" => {
            let settings = project
                .get("settings")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let count = python_int(&settings["page_count"]).unwrap_or_default();
            let mut files = Vec::new();
            for (directory, ext) in [("pages", "png"), ("qa/pages", "json")] {
                for page in 1..=count {
                    let path = page_path(directory, page, ext);
                    // Stop at the first missing input even when the persisted count is invalid.
                    if artifact(root, &path)?.is_none() {
                        return Err(io::Error::from(io::ErrorKind::NotFound).into());
                    }
                    files.push(path);
                }
            }
            files.push(REPORT.into());
            Ok((
                json!([{"project_id":project["project_id"],"settings":settings}]),
                files,
            ))
        }
        _ => unreachable!("stage registry validated at build time"),
    }
}

fn strip_times(value: &mut Value) {
    match value {
        Value::Object(map) => {
            map.retain(|key, child| {
                if matches!(
                    key.as_str(),
                    "created_at" | "updated_at" | "detected_at" | "completed_at" | "timestamp"
                ) {
                    false
                } else {
                    strip_times(child);
                    true
                }
            });
        }
        Value::Array(items) => items.iter_mut().for_each(strip_times),
        _ => {}
    }
}

pub(crate) fn hash(root: &File, path: &str) -> Result<String> {
    file_hash(root, path)?.ok_or_else(|| io::Error::from(io::ErrorKind::NotFound).into())
}

pub(crate) fn current_key(root: &File, project: &Value, stage: &Definition) -> Result<String> {
    let (mut inputs, files) = material(root, project, stage)?;
    let version = project["stage_versions"][stage.name]
        .as_str()
        .filter(|version| !version.is_empty())
        .ok_or_else(|| Error::invalid("stage version must be a non-empty string"))?;
    strip_times(&mut inputs);
    let files = files
        .iter()
        .map(|path| hash(root, path))
        .collect::<Result<Vec<_>>>()?;
    Ok(bytes_hash(&manifest::compact(&json!({
        "inputs":inputs,"files":files,"stage":stage.name,"stage_version":version
    }))))
}

pub(crate) fn preserved_count(root: &File, project: &Value, cache: &Value) -> Result<usize> {
    if !project["stage_versions"].is_object() {
        return Err(Error::invalid("manifest stage_versions must be an object"));
    }
    let descriptors = project.get("artifacts").and_then(Value::as_object);
    let semantic: HashSet<_> = ["story_plan", "character_bible", "storyboard"]
        .into_iter()
        .filter_map(|name| descriptors?.get(name)?["path"].as_str())
        .collect();
    let mut preserved = 0;
    for (index, stage) in STAGES.iter().enumerate() {
        let cached = &cache["stages"][stage.name];
        let mut stale = !cached.is_object()
            || index == 0 && project.get("artifacts").is_some() && descriptors.is_none();
        for descriptor in stage
            .artifacts
            .iter()
            .filter_map(|name| descriptors?.get(*name))
            .filter(|value| value.is_object())
        {
            let current = descriptor["path"]
                .as_str()
                .and_then(|path| file_hash(root, path).ok().flatten());
            stale |= current.is_none() || current.as_deref() != descriptor["sha256"].as_str();
        }
        if let Some(artifacts) = cached["artifacts"].as_object() {
            stale |=
                outputs(root, project, stage).map_or(true, |outputs| !same_set(artifacts, outputs));
            for (path, expected) in artifacts {
                if semantic.contains(path.as_str()) {
                    continue;
                }
                if file_hash(root, path)?.as_deref() != expected.as_str() {
                    stale = true;
                    break;
                }
            }
        }
        if !stale {
            stale = current_key(root, project, stage).ok().as_deref() != cached["key"].as_str();
        }
        if !stale && stage.name == "generation" {
            stale = panel_needs_regeneration(root, project)?;
        }
        if preserved == index && !stale {
            preserved += 1;
        }
    }
    Ok(preserved)
}

fn same_set(artifacts: &Map<String, Value>, outputs: Vec<String>) -> bool {
    let paths: HashSet<_> = outputs.into_iter().collect();
    paths.len() == artifacts.len() && artifacts.keys().all(|path| paths.contains(path))
}

fn panel_needs_regeneration(root: &File, project: &Value) -> Result<bool> {
    let board = read_value(root, BOARD).ok().flatten().unwrap_or_default();
    let ids: HashSet<_> = items(&project["panels"])
        .iter()
        .filter_map(Value::as_str)
        .chain(board_panels(&board).filter_map(|panel| panel["id"].as_str()))
        .filter(|id| is_panel(id))
        .collect();
    for id in ids {
        let path = format!("qa/panels/{id}.json");
        let relative = project_fs::relative_path(&path)?;
        let Some((parent, name)) = project_fs::target(root, &relative, false)? else {
            continue;
        };
        match project_fs::metadata(&parent, std::path::Path::new(name)) {
            Ok(None) => continue,
            Err(_) => return Ok(true),
            Ok(Some(_)) => {}
        }
        let Ok(record) = object(root, &path) else {
            return Ok(true);
        };
        let v2 = record["schema_version"] == "2.0";
        let Some(id) = record[if v2 { "subject_id" } else { "panel_id" }].as_str() else {
            continue;
        };
        let decision = record["decision"].as_str();
        let accepted = decision == Some("accept")
            || (v2 && decision == Some("accept-warning"))
            || (!v2 && decision == Some("accept_with_warnings"));
        if !accepted || !panels::reusable(root, &record, id)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn references_ready(root: &File) -> bool {
    let Ok(bible) = object(root, BIBLE) else {
        return false;
    };
    let characters = items(&bible["characters"]);
    !characters.is_empty()
        && characters.iter().all(|character| {
            character["id"].as_str().is_some_and(|id| {
                artifact(root, &format!("references/characters/{id}.png"))
                    .ok()
                    .flatten()
                    .is_some()
            })
        })
}
