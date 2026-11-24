use std::fs::File;
use std::path::Path;

use serde_json::{Value, json};

use super::{BOARD, character::strings, document, one_action, panel_id, write_record};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::project_fs::{self, LockedProject};
use crate::validation::{items, repair};

pub(super) fn run(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    one_action(args, &["panel", "plan", "check"])?;
    let root = locked.root();
    let can_edit = args.has("localized-edit");
    if let Some(id) = args.text("panel") {
        let id = panel_id(id)?;
        let path = format!("qa/panels/{id}.json");
        let plan = derive(root, &document(root, &path)?, id, can_edit)?;
        return Ok(if args.has("json") {
            plan
        } else {
            block(&plan).into()
        });
    }
    let board = document(root, BOARD)?;
    if args.has("check") {
        let plan = document(root, repair::PATH)?;
        let issues = repair::inspect(root, &board, &plan);
        if !issues.is_empty() {
            return Err(Error::invalid(
                issues.into_iter().collect::<Vec<_>>().join(", "),
            ));
        }
        return Ok("repair-plan-ok".into());
    }
    let panels = repair::board_panels(&board)
        .ok_or_else(|| Error::invalid("storyboard panels must have unique string IDs"))?;
    let mut plans = Vec::new();
    for panel in panels {
        let id = panel_id(panel["id"].as_str().expect("checked panel ID"))?;
        let path = format!("qa/panels/{id}.json");
        if let Some((parent, name)) = project_fs::target(root, Path::new(&path), false)?
            && project_fs::metadata(&parent, Path::new(name))?.is_some()
        {
            let record = document(root, &path)?;
            plans.push(derive(root, &record, id, can_edit)?);
        }
    }
    let plan = json!({"panels":plans, "schema_version":"1.0"});
    write_record(locked, "repair-plan", repair::PATH, &plan)?;
    Ok(repair::PATH.into())
}

fn derive(root: &File, record: &Value, id: &str, can_edit: bool) -> Result<Value> {
    if record["subject_id"] != id {
        return Err(Error::invalid(format!(
            "panel review does not identify {id}"
        )));
    }
    repair::derive(root, record, can_edit).ok_or_else(|| {
        Error::invalid(format!(
            "panel QA record cannot be used to plan repair for {id}"
        ))
    })
}

fn block(plan: &Value) -> String {
    let get = |key: &str| plan[key].as_str().unwrap_or_default();
    let strategy = get("strategy");
    let qualified = plan["fallback_reason"].as_str().map_or_else(
        || strategy.to_owned(),
        |reason| format!("{strategy} ({reason})"),
    );
    let editing = if plan["localized_edit_supported"] == true {
        "supported"
    } else {
        "unsupported"
    };
    let mut lines = vec![
        "REPAIR PLAN (repair-plan 1.0)".into(),
        format!("- panel: {}", get("panel_id")),
        format!("- quality decision: {}", get("decision")),
        format!("- strategy: {qualified}"),
        format!("- localized editing: {editing}"),
        format!(
            "- accepted raster: {} ({})",
            get("accepted_raw_path"),
            get("accepted_raw_sha256")
        ),
    ];
    let defects = items(&plan["defects"]);
    match strategy {
        "no-repair" => lines.push("- repair nothing: the review accepted this panel".into()),
        "selective-repair" => {
            lines.push("- repair only these targets, leaving all other pixels unchanged:".into());
            for target in items(&plan["targets"]) {
                lines.push(format!(
                    "  {}. {} {}",
                    target["rank"],
                    text(target, "scope"),
                    text(target, "target")
                ));
                lines.extend(
                    strings(&target["guidance"])
                        .into_iter()
                        .map(|clause| format!("     {clause}")),
                );
            }
        }
        _ => {
            lines.push("- regenerate the whole panel with one correction clause:".into());
            lines.extend(defects.iter().map(|defect| {
                let clause = defect["guidance"]
                    .as_str()
                    .unwrap_or_else(|| text(defect, "evidence"));
                format!("    {clause}")
            }));
        }
    }
    lines.push(if strategy == "full-regeneration" {
        "- reviewed clean, but a full regeneration re-rolls them:".into()
    } else {
        "- preserve:".into()
    });
    for key in ["checks", "subjects"] {
        let values = strings(&plan["unaffected"][key]);
        let listed = if values.is_empty() {
            "none".into()
        } else {
            values.join(", ")
        };
        lines.push(format!("  {key}: {listed}"));
    }
    if !defects.is_empty() {
        lines.push("- defects:".into());
        lines.extend(defects.iter().map(|defect| {
            let target = defect["target"].as_str().map_or_else(
                || "panel".into(),
                |name| format!("{} {name}", text(defect, "scope")),
            );
            let reason = defect["fallback_reason"]
                .as_str()
                .map_or_else(String::new, |reason| format!(" [{reason}]"));
            format!(
                "  {} {target} {}/{}{reason}",
                text(defect, "check_id"),
                text(defect, "result"),
                text(defect, "severity")
            )
        }));
    }
    lines.join("\n")
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or_default()
}
