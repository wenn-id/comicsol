use std::io::Read;
use std::path::Path;

use serde_json::{Value, json};

use super::{BOARD, document, panel_id, required};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::{self, LockedProject};
use crate::recovery::{Change, publish};
use crate::resume::{event, timestamp};
use crate::validation::{artifact, bytes_hash, is_identifier, items, read_value};

const GENERATED: &str = "generated-visual";
const LETTERED: &str = "deterministic-lettering";
const NEGATIVE: &str = "generated sfx text";
const KINDS: [&str; 3] = ["raw", "clean", "lettered"];
const MAX_RASTER: u64 = 128 * 1024 * 1024;

pub(super) fn run(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    let panel_id = panel_id(required(args, "panel")?)?;
    let text_id = required(args, "text-id")?;
    if !is_identifier(text_id, 48, false) {
        return Err(Error::invalid("invalid text ID"));
    }
    let reason = required(args, "reason")?.trim();
    let root = locked.root();
    let mut project = manifest::read_project(root, false)?;
    if project["status"] == "BLOCKED" {
        return Err(Error::invalid(
            "cannot repair a BLOCKED project; resume it before replacing SFX",
        ));
    }
    if project["artifacts"]["storyboard"]["path"] != BOARD {
        return Err(Error::invalid(format!(
            "manifest has no storyboard artifact descriptor bound to {BOARD}; record the storyboard stage before repairing SFX"
        )));
    }

    let mut board = document(root, BOARD)?;
    let panel = board["pages"]
        .as_array_mut()
        .ok_or_else(|| Error::invalid("storyboard pages must be an array"))?
        .iter_mut()
        .filter_map(|page| page.get_mut("panels")?.as_array_mut())
        .flatten()
        .find(|panel| panel["id"] == panel_id)
        .ok_or_else(|| Error::invalid(format!("storyboard has no panel {panel_id}")))?;
    let effect = panel["text"]
        .as_array_mut()
        .ok_or_else(|| {
            Error::invalid(format!("storyboard panel {panel_id} text must be an array"))
        })?
        .iter_mut()
        .find(|item| item["id"] == text_id)
        .ok_or_else(|| {
            Error::invalid(format!(
                "storyboard panel {panel_id} has no text item {text_id}"
            ))
        })?;
    if effect["kind"] != "sfx" {
        return Err(Error::invalid(format!(
            "text item {text_id} is not SFX; only SFX has a render mode"
        )));
    }
    let mode = effect["render_mode"].as_str().unwrap_or(GENERATED);
    if mode != GENERATED {
        return Err(Error::invalid(format!(
            "text item {text_id} is already {mode}; there is no generated effect to replace"
        )));
    }
    let content = effect["content"].clone();
    effect["render_mode"] = LETTERED.into();
    let negative_added = !items(&panel["negative"])
        .iter()
        .filter_map(Value::as_str)
        .any(|text| caseless::default_case_fold_str(text).contains("generated sfx"));
    let negatives = panel["negative"].as_array_mut().ok_or_else(|| {
        Error::invalid(format!(
            "storyboard panel {panel_id} negative must be an array"
        ))
    })?;
    if negative_added {
        negatives.push(NEGATIVE.into());
    }

    let canonical = format!("panels/{panel_id}/clean.png");
    let clean = if artifact(root, &canonical)?.is_some() {
        canonical
    } else {
        format!("panels/clean/{panel_id}.png")
    };
    if artifact(root, &clean)?.is_none() {
        return Err(Error::invalid(format!(
            "panel {panel_id} has no clean raster to audit at {clean}; author the effect as deterministic lettering instead of repairing it"
        )));
    }
    let audit = format!("panels/{panel_id}/sfx-audit.json");
    let mut record = read_value(root, &audit)?.unwrap_or_else(|| {
        json!({
            "kind":"sfx-audit", "panel_id":panel_id, "replacements":[], "schema_version":"1.0",
        })
    });
    if record["kind"] != "sfx-audit"
        || record["panel_id"] != panel_id
        || record["schema_version"] != "1.0"
    {
        return Err(Error::invalid(format!(
            "SFX audit record is not readable: {audit}"
        )));
    }
    let replacements = record["replacements"]
        .as_array_mut()
        .ok_or_else(|| Error::invalid(format!("SFX audit record is not readable: {audit}")))?;
    let archive = |sequence, kind| {
        format!("panels/{panel_id}/sfx-audit/{text_id}.attempt-{sequence}.{kind}.png")
    };
    let mut sequence = 1u64;
    'slots: loop {
        for kind in KINDS {
            if let Some((parent, name)) =
                project_fs::target(root, Path::new(&archive(sequence, kind)), false)?
                && project_fs::metadata(&parent, Path::new(name))?.is_some()
            {
                sequence += 1;
                continue 'slots;
            }
        }
        break;
    }
    let mut changes = Vec::new();
    let mut archived = Vec::new();
    for (kind, source) in KINDS.into_iter().zip([
        format!("panels/raw/{panel_id}.png"),
        clean.clone(),
        format!("panels/{panel_id}/lettered.png"),
    ]) {
        let Some((parent, name)) = artifact(root, &source)? else {
            continue;
        };
        let file = project_fs::open_file(&parent, &name, false, false)?;
        let mut bytes = Vec::new();
        file.take(MAX_RASTER + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_RASTER {
            return Err(Error::limit("the raster byte limit"));
        }
        let path = archive(sequence, kind);
        archived.push(
            json!({"kind":kind, "path":path, "sha256":bytes_hash(&bytes), "source_path":source}),
        );
        changes.push(Change::Replace(path, bytes));
    }
    replacements.push(json!({
        "archived":archived, "content":content, "from_render_mode":GENERATED,
        "negative_added":negative_added.then_some(NEGATIVE), "reason":reason,
        "sequence":sequence, "text_id":text_id, "to_render_mode":LETTERED,
    }));

    let board = manifest::canonical(&board);
    project["artifacts"]["storyboard"]["sha256"] = bytes_hash(&board).into();
    project["updated_at"] = timestamp().into();
    let mut events = event(
        "sfx.replacement-recorded",
        json!({
            "attempt":sequence, "from":GENERATED, "panel_id":panel_id, "text_id":text_id, "to":LETTERED,
        }),
    );
    events.extend(event(
        "artifact.regenerated",
        json!({"artifact_path":BOARD, "reused":false}),
    ));
    changes.extend([
        Change::Replace(BOARD.into(), board),
        Change::Replace(audit.clone(), manifest::canonical(&record)),
        Change::Replace("project.json".into(), manifest::canonical(&project)),
        Change::Append("logs/events.jsonl", events),
    ]);
    publish(locked, "replace-generated-sfx", changes)?;
    Ok(json!({
        "archived":archived, "audit_path":audit, "negative_added":negative_added,
        "next_action":format!("Re-letter the project. If the rejected effect is still visible in {clean}, re-review qa/panels/{panel_id}.json to `regenerate` before promoting a new raster."),
        "panel_id":panel_id, "render_mode":LETTERED, "sequence":sequence, "text_id":text_id,
    }))
}
