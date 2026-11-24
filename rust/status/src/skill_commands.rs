mod character;
mod normalization;
mod reference;
mod repair;
mod review;
mod sfx;

use std::fs::File;
use std::path::Path;
use std::time::Duration;

use serde_json::{Value, json};

use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::mcp::lifecycle;
use crate::project_fs::{self, LockedProject};
use crate::recovery::{Change, publish};
use crate::resume;
use crate::validation::{file_hash, is_panel, read_value};
use crate::{finalize, manifest};

const BOARD: &str = "plan/storyboard.json";

pub fn run(args: &Arguments) -> Result<Value> {
    let command = args.command.strip_prefix("skill.").expect("skill command");
    if command == "font-coverage" {
        return finalize::font_inventory(args);
    }
    let path = args.paths("project_dir");
    let timeout = if matches!(command, "letter" | "compose" | "export" | "normalize-panel") {
        300
    } else {
        10
    };
    let locked = LockedProject::open_with_timeout(path, true, Duration::from_secs(timeout))?;
    let root = locked.root();
    let state = || manifest::read_project(root, false);
    let panel = || panel_id(required(args, "panel_id")?);
    let relative = || relative_arg(path, args.paths("path"));
    let stage = || {
        let name = required(args, "stage")?;
        resume::definitions()
            .iter()
            .any(|stage| stage.name == name)
            .then_some(name)
            .ok_or_else(|| Error::invalid("unknown resume stage"))
    };
    match command {
        "transition" => {
            let warning = args
                .text("warning")
                .map(|text| narrative(text, "transition warning", 500))
                .transpose()?;
            lifecycle::transition(&locked, state()?, required(args, "target")?, warning)
        }
        "resume-plan" => lifecycle::plan(root, &manifest::read_project(root, true)?),
        "invalidate" => lifecycle::invalidate(&locked, state()?, stage()?),
        "record-stage" => finalize::record_stage(&locked, &state()?, stage()?),
        "record-attempt" => {
            let kind = required(args, "kind")?;
            if !matches!(kind, "initial" | "visual_retry" | "transient_repeat") {
                return Err(Error::invalid("unknown generation attempt kind"));
            }
            lifecycle::record_attempt(&locked, panel()?, kind, &relative()?)
        }
        "promote-attempt" => lifecycle::promote(&locked, panel()?, &relative()?),
        "override-panel" => {
            let explanation = narrative(required(args, "reason")?, "override reason", 1000)?;
            lifecycle::override_panel(&locked, state()?, panel()?, explanation.trim())
        }
        "letter" => finalize::letter_for_skill(&locked, args),
        "compose" => finalize::compose_for_skill(&locked, state()?, args),
        "export" => finalize::export_for_skill(&locked, state()?, args),
        "render-report" => finalize::report_for_skill(&locked, state()?, args),
        "character-identity" => character::run(&locked, args),
        "reference-strategy" => reference::run(&locked, args),
        "character-quality" => review::run(&locked, args),
        "repair-strategy" => repair::run(&locked, args),
        "sfx-repair" => sfx::run(&locked, args),
        "quality-sample" => evidence(&locked, args),
        "normalize-panel" => normalization::run(&locked, args),
        _ => unreachable!("registered Agent Skill command"),
    }
}

fn required<'a>(args: &'a Arguments, name: &str) -> Result<&'a str> {
    args.text(name)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| Error::invalid(format!("required argument: {name}")))
}

fn narrative<'a>(text: &'a str, name: &str, limit: usize) -> Result<&'a str> {
    if text.trim().is_empty() || text.chars().count() > limit {
        return Err(Error::invalid(format!(
            "{name} must be non-empty and at most {limit} characters"
        )));
    }
    if crate::validation::has_secret(text) {
        return Err(Error::invalid(
            "narrative field must not contain secrets or credentials",
        ));
    }
    Ok(text)
}

fn panel_id(value: &str) -> Result<&str> {
    is_panel(value)
        .then_some(value)
        .ok_or_else(|| Error::invalid("invalid panel ID"))
}

fn one_action(args: &Arguments, names: &[&str]) -> Result<()> {
    (names.iter().filter(|name| args.has(name)).count() == 1)
        .then_some(())
        .ok_or_else(|| Error::invalid(format!("choose one of {}", names.join(", "))))
}

fn document(root: &File, path: &str) -> Result<Value> {
    read_value(root, path)?
        .filter(Value::is_object)
        .ok_or_else(|| Error::invalid(format!("{path} must contain a JSON object")))
}

fn relative_arg(root: &Path, path: &Path) -> Result<String> {
    let relative = if path.is_absolute() {
        path.strip_prefix(std::fs::canonicalize(root)?)
            .map_err(|_| Error::invalid("path must be within the project"))?
    } else {
        path
    };
    let value = relative
        .to_str()
        .ok_or_else(|| Error::invalid("path is not valid UTF-8"))?
        .replace('\\', "/");
    project_fs::relative_path(&value)?;
    Ok(value)
}

fn write_record(locked: &LockedProject, operation: &str, path: &str, record: &Value) -> Result<()> {
    publish(
        locked,
        operation,
        vec![Change::Replace(path.into(), manifest::canonical(record))],
    )
}

fn evidence(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    let record = match required(args, "mode")? {
        "deterministic" => json!({
            "mode": "deterministic", "scope": "mechanics-only", "proves_visual_quality": false,
        }),
        "live-visual" => {
            let attempt = args
                .text("retained-attempt")
                .filter(|value| !value.is_empty())
                .ok_or_else(|| Error::invalid("live-visual mode requires a retained attempt"))?;
            let digest = file_hash(locked.root(), attempt)?
                .ok_or_else(|| Error::invalid("retained attempt must be a local file"))?;
            let values = |name| {
                args.values(name)
                    .map(|arg| arg.to_string_lossy())
                    .collect::<Vec<_>>()
            };
            json!({
                "mode": "live-visual", "scope": "retained-attempt-visual-review",
                "proves_visual_quality": true, "retained_attempt": attempt,
                "attempt_sha256": digest, "provider": required(args, "provider")?,
                "model": required(args, "model")?, "references": values("reference"),
                "reviewer_method": required(args, "reviewer-method")?, "limitations": values("limitation"),
            })
        }
        _ => return Err(Error::invalid("mode must be deterministic or live-visual")),
    };
    write_record(locked, "quality-evidence", "qa/evidence.json", &record)?;
    Ok(Value::Null)
}
