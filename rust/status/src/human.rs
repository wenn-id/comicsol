use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use serde_json::Value;

use crate::error::Result;
use crate::manifest;
use crate::project_fs::{self, LockedProject};
use crate::resume::{self, stages};
use crate::validation::{is_panel, items, read_value};

pub type Progress<'a> = dyn FnMut(&str, &'static str, usize, usize) + 'a;

pub fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\x{:02x}", c as u32)),
            _ => out.push(c),
        }
    }
    out
}

fn display(value: &Value) -> String {
    let raw = match value {
        Value::String(text) => return escape(text),
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    };
    escape(&raw)
}

pub fn handoff(command: &str, data: &Value) -> String {
    let text = |name| display(&data[name]);
    match command {
        "handoff.prepare" => format!(
            "{}: handoff prepared for {}\nJobs: {}\nNext action: {}",
            text("project_id"),
            text("phase"),
            ["missing", "ready", "completed", "failed", "stale"]
                .iter()
                .map(|name| format!("{name}={}", display(&data["job_counts"][name])))
                .collect::<Vec<_>>()
                .join(" "),
            text("next_action")
        ),
        "handoff.inspect" if data.get("valid").is_some() => format!(
            "Handoff archive {}: version={} valid={}",
            text("project_id"),
            text("format_version"),
            text("valid")
        ),
        "handoff.inspect" => format!(
            "Handoff: prepared={} phase={} scope={} jobs={}\nNext action: {}",
            text("prepared"),
            text("phase"),
            text("scope_state"),
            items(&data["jobs"]).len(),
            text("next_action")
        ),
        "handoff.record-failure" => format!(
            "{}: {} category={} attempts_remaining={} duplicate={}",
            text("job_id"),
            text("status"),
            text("category"),
            text("attempts_remaining"),
            text("duplicate")
        ),
        "handoff.accept-result" => format!(
            "{}: {} (duplicate={})",
            text("job_id"),
            text("status"),
            text("duplicate")
        ),
        "handoff.export" => format!(
            "{}: handoff archive exported to {}",
            text("project_id"),
            text("archive_path")
        ),
        "handoff.import" => format!(
            "{}: handoff archive imported to {}",
            text("project_id"),
            text("project_dir")
        ),
        _ => unreachable!("native handoff command"),
    }
}

pub fn skill(data: &Value) -> String {
    let text = |name| display(&data[name]);
    format!(
        "{} {}: {} — {}",
        text("target"),
        text("scope"),
        text("status"),
        text("message")
    )
}

pub fn clients(data: &Value, detailed: bool) -> String {
    let mut rendered = String::new();
    for value in items(data) {
        let text = |key| display(&value[key]);
        if detailed {
            rendered.push_str(&format!(
                "{}: {}/{} — {}",
                text("client"),
                text("state"),
                text("status"),
                text("message")
            ));
            if !value["backup_path"].is_null() {
                rendered.push_str(&format!("; backup={}", text("backup_path")));
            }
        } else {
            rendered.push_str(&format!(
                "{}: {} — {}",
                text("client"),
                text("status"),
                text("message")
            ));
        }
        rendered.push('\n');
    }
    rendered
}

pub fn status(path: &Path) -> Result<String> {
    let path = fs::canonicalize(if path.as_os_str().is_empty() {
        Path::new(".")
    } else {
        path
    })
    .map_err(crate::error::Error::from_path_io)?;
    let project = LockedProject::open(&path, true)?;
    let root = project.root();
    let data = manifest::read_project(root, true)?;
    let state = data["status"].as_str().unwrap_or_default();
    let (cache, _) = stages::cache(root)?;
    let preserved = stages::preserved_count(root, &data, &cache)?;
    let definitions = resume::definitions();
    let mut lines = vec![
        format!(
            "{}: {}",
            display(&data["project_id"]),
            display(&data["status"])
        ),
        "Stages:".to_owned(),
    ];
    for (i, stage) in definitions.iter().enumerate() {
        let (glyph, status) = match i.cmp(&preserved) {
            std::cmp::Ordering::Less => ("[x]", "complete"),
            std::cmp::Ordering::Equal if state == "BLOCKED" => ("[!]", "blocked"),
            std::cmp::Ordering::Equal => ("[~]", "stale"),
            std::cmp::Ordering::Greater => ("[ ]", "pending"),
        };
        lines.push(format!("  {glyph} {}: {status}", stage.name));
    }
    let board = read_value(root, "plan/storyboard.json")
        .ok()
        .flatten()
        .unwrap_or_default();
    let expected: BTreeSet<_> = items(&data["panels"])
        .iter()
        .filter_map(Value::as_str)
        .chain(
            items(&board["pages"])
                .iter()
                .flat_map(|page| items(&page["panels"]))
                .filter_map(|panel| panel["id"].as_str()),
        )
        .filter(|id| is_panel(id))
        .collect();
    let mut counts = [0; 4];
    let qa_path = Path::new("qa/panels");
    if let Some((parent, name)) = project_fs::target(root, qa_path, false)? {
        project_fs::metadata(&parent, Path::new(name))?;
    }
    for id in expected {
        let index = match read_value(root, &format!("qa/panels/{id}.json")) {
            Ok(Some(record)) => match record["decision"].as_str() {
                Some("accept" | "accept-warning" | "accept_with_warnings") => 0,
                Some("regenerate") => 1,
                _ => 2,
            },
            Ok(None) => 2,
            Err(_) => 3,
        };
        counts[index] += 1;
    }
    let [accepted, failed, pending, unreadable] = counts;
    lines.push(format!(
        "Panels: {accepted} accepted, {failed} failed, {pending} pending"
    ));
    if unreadable > 0 {
        lines.push(format!(
            "  WARNING: {unreadable} panel QA record(s) unreadable."
        ));
    }
    let blocked_reason = data["blocked_reason"]
        .as_str()
        .filter(|reason| !reason.is_empty());
    if let Some(reason) = blocked_reason {
        lines.push(format!("Blocked reason: {}", escape(reason)));
    }
    let warnings = items(&data["warnings"]);
    if !warnings.is_empty() {
        lines.push(format!("Warnings ({}):", warnings.len()));
        lines.extend(
            warnings
                .iter()
                .map(|warning| format!("  - {}", display(warning))),
        );
    }
    let action = if state == "BLOCKED" {
        if blocked_reason == Some("image-capability-unavailable")
            && data["capability"]["status"] != "available"
        {
            "blocked until image capability available; then run `resume`.".to_owned()
        } else {
            format!(
                "run `resume` to recover (blocked: {}).",
                escape(blocked_reason.unwrap_or("blocked"))
            )
        }
    } else if preserved == definitions.len()
        && matches!(state, "COMPLETE" | "COMPLETE_WITH_WARNINGS")
    {
        "project is complete.".to_owned()
    } else {
        let next = resume::next_action(&path, definitions.get(preserved));
        if let Some(command) = next["command"].as_str() {
            format!("run `{}`.", escape(command))
        } else if let Some(stage) = next["agent_required"].as_str() {
            format!("agent must produce the {} stage.", escape(stage))
        } else {
            "none — nothing to do.".to_owned()
        }
    };
    lines.push(format!("Next action: {action}"));
    Ok(lines.join("\n"))
}
