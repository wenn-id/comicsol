use serde_json::{Value, json};

use super::{BOARD, character, document, one_action, write_record};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::project_fs::LockedProject;
use crate::validation::character::REFERENCE_PLAN;
use crate::validation::{items, references};

pub(super) fn run(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    one_action(args, &["plan", "panel"])?;
    let budget = args
        .text("budget")
        .map(|text| text.parse::<u64>())
        .transpose()
        .map_err(|_| Error::invalid("reference budget must be a non-negative integer or null"))?;
    let root = locked.root();
    let pack = character::checked(root)?;
    let board = document(root, BOARD)?;
    let plan = project_plan(&pack, &board, budget)?;
    if args.has("plan") {
        write_record(locked, "reference-selection", REFERENCE_PLAN, &plan)?;
        return Ok(REFERENCE_PLAN.into());
    }
    let id = super::required(args, "panel")?;
    let panel = find(&plan, id)?;
    Ok(if args.has("json") {
        panel.clone()
    } else {
        block(panel).into()
    })
}

pub(super) fn find<'a>(plan: &'a Value, id: &str) -> Result<&'a Value> {
    plan["panels"]
        .as_array()
        .ok_or_else(|| Error::invalid("reference plan panels must be an array"))?
        .iter()
        .find(|panel| panel["panel_id"] == id)
        .ok_or_else(|| Error::invalid(format!("reference plan has no panel '{id}'")))
}

pub(super) fn project_plan(pack: &Value, board: &Value, budget: Option<u64>) -> Result<Value> {
    references::plan(pack, board, &json!(budget)).ok_or_else(|| {
        Error::invalid(
            "reference plan requires unique storyboard panel IDs and a complete character roster",
        )
    })
}

fn block(panel: &Value) -> String {
    let text = |key: &str| panel[key].as_str().unwrap_or_default();
    let cue = panel["shot_cue"]
        .as_str()
        .map_or_else(String::new, |value| format!(" (cue: {value})"));
    let budget = panel["reference_budget"]
        .as_u64()
        .map_or_else(|| "unlimited".into(), |n| n.to_string());
    let mut lines = vec![
        "REFERENCE PLAN (reference-selection 1.0)".into(),
        format!("- panel: {}", text("panel_id")),
        format!("- shot class: {}{cue}", text("shot_class")),
        format!("- reference budget: {budget}"),
        "- attach in this order:".into(),
    ];
    let selected = items(&panel["selected"]);
    let description = |view: &Value| {
        format!(
            "{} {}={} ({})",
            view["character_id"].as_str().unwrap_or_default(),
            view["view"].as_str().unwrap_or_default(),
            view["path"].as_str().unwrap_or_default(),
            view["reason"].as_str().unwrap_or_default()
        )
    };
    if selected.is_empty() {
        lines.push("  none".into());
    }
    lines.extend(
        selected
            .iter()
            .map(|view| format!("  {}. {}", view["rank"], description(view))),
    );
    let omitted = items(&panel["omitted"]);
    if !omitted.is_empty() {
        lines.push("- omitted:".into());
        lines.extend(
            omitted
                .iter()
                .map(|view| format!("  {}", description(view))),
        );
    }
    lines.join("\n")
}
