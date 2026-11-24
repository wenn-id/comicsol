use std::collections::{BTreeSet, HashSet};
use std::fs::File;

use serde_json::{Value, json};

use super::panels::IDS;
use super::{artifact, file_hash, is_panel, items, keys, read_value, text};

pub(crate) const PATH: &str = "logs/repair-plan.json";
const IDENTITY: &str = "character-identity";
const PANEL_WIDE: &[&str] = &["action", "composition", "continuity", "technical"];
const REGIONS: &[&str] = &[
    "area",
    "character_id",
    "evidence",
    "repair_guidance",
    "result",
    "severity",
];

fn area(name: &str) -> bool {
    matches!(
        name,
        "top-left"
            | "top-center"
            | "top-right"
            | "middle-left"
            | "middle-right"
            | "bottom-left"
            | "bottom-center"
            | "bottom-right"
    )
}

pub(super) fn result(value: &Value) -> bool {
    matches!(value.as_str(), Some("pass" | "warning" | "fail"))
}

pub(super) fn severity(value: &Value) -> bool {
    matches!(value.as_str(), Some("warning" | "error"))
}

pub(super) fn evidence(value: &Value) -> Option<String> {
    let normalized = text(value)?
        .split(super::python_whitespace)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    (!matches!(
        caseless::default_case_fold_str(&normalized).as_str(),
        "verified" | "looks good" | "ok" | "pass"
    ))
    .then_some(normalized)
}

fn non_passing(entry: &Value) -> bool {
    entry["result"] != "pass" || entry["severity"] == "warning"
}

fn scope(region: &Value) -> Option<(&'static str, &str)> {
    if !keys(region, REGIONS) {
        return None;
    }
    match (&region["character_id"], &region["area"]) {
        (character, Value::Null) => text(character)
            .filter(|id| id.chars().count() <= 48)
            .map(|id| ("subject", id)),
        (Value::Null, area_value) => area_value
            .as_str()
            .filter(|name| area(name))
            .map(|name| ("area", name)),
        _ => None,
    }
}

pub(super) fn region_issues(check: &Value) -> BTreeSet<&'static str> {
    if check["id"] == IDENTITY {
        return BTreeSet::new();
    }
    let Some(regions) = check["regions"].as_array() else {
        return BTreeSet::from(["repair-region-structure"]);
    };
    if regions.is_empty() {
        return BTreeSet::new();
    }
    if !matches!(check["id"].as_str(), Some("anatomy" | "text-free")) {
        return BTreeSet::from(["repair-region-scope"]);
    }
    let mut issues = BTreeSet::new();
    let mut seen = HashSet::new();
    for region in regions {
        let Some(target) = scope(region) else {
            issues.insert("repair-region-structure");
            continue;
        };
        if !seen.insert(target) {
            issues.insert("repair-region-duplicate");
        }
        if !result(&region["result"]) {
            issues.insert("repair-region-result");
        }
        if !severity(&region["severity"]) {
            issues.insert("repair-region-severity");
        }
        if evidence(&region["evidence"]).is_none() {
            issues.insert("repair-region-evidence");
        }
        if if non_passing(region) {
            text(&region["repair_guidance"]).is_none()
        } else {
            !region["repair_guidance"].is_null()
        } {
            issues.insert("repair-region-guidance");
        }
    }
    issues
}

fn defects(check: &Value, cast: &[&str]) -> Option<Vec<Value>> {
    let id = check["id"].as_str()?;
    if !result(&check["result"]) || !severity(&check["severity"]) {
        return None;
    }
    if !non_passing(check) {
        return Some(Vec::new());
    }
    let check_evidence = evidence(&check["evidence"])?;
    let whole = |why| {
        vec![
            json!({"check_id":id,"scope":"panel","target":null,"result":check["result"],
        "severity":check["severity"],"evidence":check_evidence,"guidance":null,"fallback_reason":why}),
        ]
    };
    if PANEL_WIDE.contains(&id) {
        return Some(whole("panel-wide-check"));
    }
    if id == IDENTITY && check.get("provenance").is_none() {
        return Some(whole("unlocalized-evidence"));
    }
    let mut defects = Vec::new();
    for region in check["regions"].as_array()? {
        let (scope, target) = if id == IDENTITY {
            ("subject", text(&region["character_id"])?)
        } else {
            scope(region)?
        };
        if scope == "subject" && !cast.contains(&target) {
            return None;
        }
        if !result(&region["result"]) || !severity(&region["severity"]) {
            return None;
        }
        if non_passing(region) {
            defects.push(
                json!({"check_id":id,"scope":scope,"target":target,"result":region["result"],
                "severity":region["severity"],"evidence":evidence(&region["evidence"])?,
                "guidance":evidence(&region["repair_guidance"])?,"fallback_reason":null}),
            );
        }
    }
    Some(if defects.is_empty() {
        whole("unlocalized-evidence")
    } else {
        defects
    })
}

fn accepted_stale(root: &File, bindings: &Value) -> bool {
    ["raw", "clean", "normalization"].iter().any(|kind| {
        let path = bindings[&format!("{kind}_path")].as_str();
        let expected = bindings[&format!("{kind}_sha256")].as_str();
        let actual = path.and_then(|path| file_hash(root, path).ok().flatten());
        expected.is_none() || expected != actual.as_deref()
    })
}

pub(crate) fn derive(root: &File, record: &Value, can_edit: bool) -> Option<Value> {
    if record["schema_version"] != "2.0" {
        return None;
    }
    let id = record["subject_id"].as_str().filter(|id| is_panel(id))?;
    let decision = record["decision"]
        .as_str()
        .filter(|value| matches!(*value, "accept" | "accept-warning" | "regenerate"))?;
    let bindings = &record["bindings"];
    let accepted_path = bindings["raw_path"].as_str()?;
    let accepted_sha = bindings["raw_sha256"].as_str()?;
    let checks = record["checks"].as_array()?;
    if !checks
        .iter()
        .map(|check| check["id"].as_str())
        .eq(IDS.iter().copied().map(Some))
    {
        return None;
    }
    let identity = &checks[0];
    if !super::character::identity_issues(identity, record).is_empty() {
        return None;
    }
    let provenance = &identity["provenance"];
    let mut seen = HashSet::new();
    let cast: Vec<_> = items(&provenance["characters"])
        .iter()
        .filter_map(|entry| entry["character_id"].as_str())
        .filter(|id| seen.insert(*id))
        .collect();
    if provenance.is_object()
        && (provenance["panel_id"] != id
            || items(&identity["regions"]).iter().any(|region| {
                !region["character_id"]
                    .as_str()
                    .is_some_and(|name| cast.contains(&name))
            }))
    {
        return None;
    }
    let mut reviewed_defects = Vec::new();
    let mut unaffected_checks = Vec::new();
    for check in checks {
        reviewed_defects.extend(defects(check, &cast)?);
        if !non_passing(check) {
            unaffected_checks.push(&check["id"]);
        }
    }
    let unaffected_subjects: Vec<_> = cast
        .into_iter()
        .filter(|name| {
            !reviewed_defects
                .iter()
                .any(|defect| defect["scope"] == "subject" && defect["target"] == *name)
        })
        .collect();
    let (strategy, fallback) = if decision != "regenerate" {
        ("no-repair", None)
    } else if accepted_stale(root, bindings) {
        ("full-regeneration", Some("stale-bindings"))
    } else if !can_edit {
        ("full-regeneration", Some("editing-unsupported"))
    } else if let Some(reason) = reviewed_defects
        .iter()
        .find_map(|defect| defect["fallback_reason"].as_str())
    {
        ("full-regeneration", Some(reason))
    } else if reviewed_defects.is_empty() {
        return None;
    } else {
        ("selective-repair", None)
    };
    let mut targets: Vec<(&str, &str, Vec<&Value>)> = Vec::new();
    if strategy == "selective-repair" {
        for defect in &reviewed_defects {
            let (Some(scope), Some(target)) = (defect["scope"].as_str(), defect["target"].as_str())
            else {
                continue;
            };
            let clause = if defect["guidance"].is_null() {
                &defect["evidence"]
            } else {
                &defect["guidance"]
            };
            if let Some((_, _, clauses)) = targets
                .iter_mut()
                .find(|(a, b, _)| *a == scope && *b == target)
            {
                clauses.push(clause);
            } else {
                targets.push((scope, target, vec![clause]));
            }
        }
    }
    let targets: Vec<_> = targets.into_iter().enumerate().map(|(index,(scope,target,guidance))|
        json!({"scope":scope,"target":target,"guidance":guidance,"rank":index+1})).collect();
    Some(
        json!({"accepted_raw_path":accepted_path,"accepted_raw_sha256":accepted_sha,"decision":decision,
        "defects":reviewed_defects,"fallback_reason":fallback,"localized_edit_supported":can_edit,"panel_id":id,
        "strategy":strategy,"targets":targets,"unaffected":{"checks":unaffected_checks,"subjects":unaffected_subjects}}),
    )
}

pub(crate) fn board_panels(board: &Value) -> Option<Vec<&Value>> {
    let mut panels = Vec::new();
    let mut seen = HashSet::new();
    for page in board["pages"].as_array()? {
        for panel in page["panels"].as_array()? {
            if !seen.insert(panel["id"].as_str()?) {
                return None;
            }
            panels.push(panel);
        }
    }
    Some(panels)
}

pub(crate) fn inspect(root: &File, board: &Value, document: &Value) -> BTreeSet<&'static str> {
    if !keys(document, &["panels", "schema_version"]) {
        return BTreeSet::from(["repair-plan-structure"]);
    }
    if document["schema_version"] != "1.0" {
        return BTreeSet::from(["repair-plan-schema-version"]);
    }
    let Some(panels) = document["panels"].as_array() else {
        return BTreeSet::from(["repair-plan-structure"]);
    };
    let Some(board_panels) = board_panels(board) else {
        return BTreeSet::from(["repair-plan-storyboard"]);
    };
    let order: Vec<_> = board_panels
        .iter()
        .filter_map(|panel| panel["id"].as_str())
        .collect();
    let mut ids = Vec::new();
    let mut issues = BTreeSet::new();
    for entry in panels {
        let Some(id) = entry["panel_id"].as_str() else {
            issues.insert("repair-plan-structure");
            continue;
        };
        ids.push(id);
        if !order.contains(&id) {
            issues.insert("repair-plan-panel-unknown");
            continue;
        }
        let Some(can_edit) = entry["localized_edit_supported"].as_bool() else {
            issues.insert("repair-plan-structure");
            continue;
        };
        let Ok(Some(record)) = read_value(root, &format!("qa/panels/{id}.json")) else {
            issues.insert("repair-plan-record-missing");
            continue;
        };
        if !record.is_object() {
            issues.insert("repair-plan-record-missing");
            continue;
        }
        let Some(expected) = derive(root, &record, can_edit) else {
            issues.insert("repair-plan-record-invalid");
            continue;
        };
        if entry != &expected && record["decision"] == "regenerate" {
            issues.insert("repair-plan-stale");
        }
    }
    let seen: HashSet<_> = ids.iter().copied().collect();
    if seen.len() != ids.len() {
        issues.insert("repair-plan-panel-duplicate");
    }
    if !order.iter().copied().filter(|id| seen.contains(id)).eq(ids) {
        issues.insert("repair-plan-panel-order");
    }
    if order.into_iter().filter(|id| !seen.contains(id)).any(|id| {
        let path = format!("qa/panels/{id}.json");
        match artifact(root, &path) {
            Ok(None) => false,
            Err(_) => true,
            Ok(Some(_)) => match read_value(root, &path) {
                Ok(Some(record)) if record.is_object() => record["decision"] == "regenerate",
                _ => true,
            },
        }
    }) {
        issues.insert("repair-plan-incomplete");
    }
    issues
}
