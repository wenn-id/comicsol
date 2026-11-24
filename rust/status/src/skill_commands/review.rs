use std::collections::{BTreeMap, HashSet};
use std::io::{self, Read};

use serde_json::{Value, json};

use super::{BOARD, character, document, one_action, panel_id, reference, required, write_record};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::LockedProject;
use crate::validation::character::{PACK, REFERENCE_PLAN, TRAITS, pack_issues};
use crate::validation::{bytes_hash, items, panels, python_whitespace};

const MAX_ASSESSMENT_BYTES: usize = 256 * 1024;

pub(super) fn run(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    one_action(args, &["context", "record"])?;
    let id = panel_id(required(
        args,
        if args.has("record") {
            "record"
        } else {
            "context"
        },
    )?)?;
    let root = locked.root();
    let pack = document(root, PACK)?;
    let bible = document(root, "plan/character-bible.json")?;
    let plan = document(root, REFERENCE_PLAN)?;
    let board = document(root, BOARD)?;
    let (issues, valid) = pack_issues(root, &pack, &bible, false);
    if !valid {
        return Err(Error::invalid(format!(
            "identity pack does not match the character bible: {}",
            issues.into_iter().next().unwrap_or_default()
        )));
    }
    let context = context(&pack, &bible, &plan, &board, id)?;
    if args.has("context") {
        return Ok(context);
    }
    let method = required(args, "method")?;
    let reviewer = required(args, "reviewer")?;
    let mut input = Vec::new();
    io::stdin()
        .lock()
        .take(MAX_ASSESSMENT_BYTES as u64 + 1)
        .read_to_end(&mut input)?;
    if input.len() > MAX_ASSESSMENT_BYTES {
        return Err(Error::invalid("assessment input exceeds 256 KiB"));
    }
    let text = std::str::from_utf8(&input)
        .map_err(|_| Error::invalid("assessment input is not valid UTF-8"))?;
    let assessments = manifest::parse_document(text, "assessment input")?;
    let check = build_check(&context, &assessments, method, reviewer)?;
    let relative = format!("qa/panels/{id}.json");
    let mut record = document(root, &relative)?;
    apply(&mut record, check)?;
    if let Some(issue) = panels::record_issue(&record) {
        return Err(Error::invalid(format!(
            "invalid completed panel record: {issue}"
        )));
    }
    write_record(locked, "character-quality-review", &relative, &record)?;
    Ok(relative.into())
}

fn context(pack: &Value, bible: &Value, plan: &Value, board: &Value, id: &str) -> Result<Value> {
    let panel = reference::find(plan, id)?;
    if !panel["characters"].is_array() || !panel["selected"].is_array() {
        return Err(Error::invalid("reference plan panel is incomplete"));
    }
    let budget = if panel["reference_budget"].is_null() {
        None
    } else {
        Some(panel["reference_budget"].as_u64()
            .ok_or_else(|| Error::invalid("reference plan is invalid: reference budget must be a non-negative integer or null"))?)
    };
    let expected = reference::project_plan(pack, board, budget)?;
    if plan != &expected {
        return Err(Error::invalid(
            "reference plan does not match the identity pack and storyboard",
        ));
    }
    let subject = character::find_panel(board, id)?;
    let cast = character::cast(subject);
    if items(&subject["characters"]).len() != cast.len() {
        return Err(Error::invalid("storyboard panel characters must be unique"));
    }
    let mut characters = Vec::new();
    for identity in character::select(pack, &cast)? {
        let name = identity["id"].as_str().expect("validated character id");
        let source = items(&bible["characters"])
            .iter()
            .find(|entry| entry["id"] == name)
            .ok_or_else(|| Error::invalid(format!("unknown character: '{name}'")))?;
        let values = [
            &identity["immutable_traits"]["face"],
            &identity["immutable_traits"]["hair"],
            &source["age_band"],
            &identity["wardrobe"]["base"],
            &identity["wardrobe"]["accessories"],
            &identity["proportions"],
            &identity["immutable_traits"]["invariants"],
        ];
        let traits: Vec<_> = TRAITS
            .iter()
            .zip(values)
            .map(|(trait_name, expected)| json!({"expected":expected, "trait":trait_name}))
            .collect();
        let selected: Vec<_> = items(&panel["selected"])
            .iter()
            .filter(|entry| entry["character_id"] == name)
            .collect();
        characters.push(json!({
            "character_id":name, "selected_references":selected, "traits":traits,
            "source_fingerprint_sha256":identity["source_fingerprint_sha256"],
        }));
    }
    Ok(json!({
        "characters":characters, "identity_pack_sha256":bytes_hash(&manifest::canonical(pack)),
        "panel_id":id, "reference_plan_sha256":bytes_hash(&manifest::canonical(plan)),
    }))
}

fn non_passing(item: &Value) -> bool {
    item["result"] != "pass" || item["severity"] == "warning"
}

fn hard_failure(item: &Value) -> bool {
    item["result"] == "fail" && item["severity"] == "error"
}

fn warning(item: &Value) -> bool {
    item["result"] == "warning" || item["severity"] == "warning"
}

fn build_check(
    context: &Value,
    assessments: &Value,
    method: &str,
    reviewer: &str,
) -> Result<Value> {
    let mut indexed = BTreeMap::new();
    for item in assessments
        .as_array()
        .ok_or_else(|| Error::invalid("assessments must be an array"))?
    {
        if !item.is_object() {
            return Err(Error::invalid("assessments must contain objects"));
        }
        let id = item["character_id"]
            .as_str()
            .zip(item["trait"].as_str())
            .filter(|(_, name)| TRAITS.contains(name))
            .ok_or_else(|| Error::invalid("assessment character_id and trait are invalid"))?;
        let label = || format!("{}/{}", id.0, id.1);
        if indexed.contains_key(&id) {
            return Err(Error::invalid(format!("duplicate assessment: {}", label())));
        }
        if !matches!(item["result"].as_str(), Some("pass" | "warning" | "fail")) {
            return Err(Error::invalid(format!("invalid result: {}", label())));
        }
        if !matches!(item["severity"].as_str(), Some("warning" | "error")) {
            return Err(Error::invalid(format!("invalid severity: {}", label())));
        }
        let evidence = item["evidence"]
            .as_str()
            .unwrap_or_default()
            .split(python_whitespace)
            .filter(|word| !word.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        if evidence.is_empty() {
            return Err(Error::invalid(format!("missing evidence: {}", label())));
        }
        if matches!(
            caseless::default_case_fold_str(&evidence).as_str(),
            "verified" | "looks good" | "ok" | "pass"
        ) {
            return Err(Error::invalid(format!("generic evidence: {}", label())));
        }
        indexed.insert(id, (item, evidence));
    }
    let mut regions = Vec::new();
    let mut bind = Vec::new();
    for character in items(&context["characters"]) {
        let id = character["character_id"]
            .as_str()
            .expect("context character id");
        bind.push(json!({
            "character_id":id, "selected_references":character["selected_references"],
            "source_fingerprint_sha256":character["source_fingerprint_sha256"],
        }));
        for trait_ in items(&character["traits"]) {
            let name = trait_["trait"].as_str().expect("context trait");
            let (item, evidence) = indexed
                .remove(&(id, name))
                .ok_or_else(|| Error::invalid(format!("missing assessment: {id}/{name}")))?;
            let expected = &trait_["expected"];
            let guidance = non_passing(item).then(|| {
                format!(
                    "Repair {id} {name} to match {}; observed: {evidence}",
                    expectation(expected)
                )
            });
            regions.push(json!({
                "character_id":id, "evidence":evidence, "expected":expected, "repair_guidance":guidance,
                "result":item["result"], "severity":item["severity"], "trait":name,
            }));
        }
    }
    if let Some(((id, name), _)) = indexed.first_key_value() {
        return Err(Error::invalid(format!(
            "unexpected assessment: {id}/{name}"
        )));
    }
    let hard = regions.iter().filter(|item| hard_failure(item)).count();
    let warnings = regions
        .iter()
        .filter(|item| !hard_failure(item) && non_passing(item))
        .count();
    let pass = regions.len() - hard - warnings;
    let (result, severity) = if hard > 0 {
        ("fail", "error")
    } else if warnings > 0 {
        ("warning", "warning")
    } else {
        ("pass", "error")
    };
    let hard_label = if hard == 1 {
        "hard failure"
    } else {
        "hard failures"
    };
    let warning_label = if warnings == 1 { "warning" } else { "warnings" };
    Ok(json!({
        "evidence":format!("{} trait checks: {pass} pass, {warnings} {warning_label}, {hard} {hard_label}", regions.len()),
        "id":"character-identity", "method":method.trim_matches(python_whitespace),
        "provenance":{
            "characters":bind, "identity_pack_path":PACK, "identity_pack_sha256":context["identity_pack_sha256"],
            "panel_id":context["panel_id"], "reference_plan_path":REFERENCE_PLAN, "reference_plan_sha256":context["reference_plan_sha256"],
        },
        "regions":regions, "result":result, "reviewer":reviewer.trim_matches(python_whitespace), "severity":severity,
    }))
}

fn expectation(value: &Value) -> String {
    if value.is_array() {
        let words = character::strings(value);
        if words.is_empty() {
            "no accessories".into()
        } else {
            words.join("; ")
        }
    } else if value.is_object() {
        value["build"]
            .as_str()
            .into_iter()
            .chain(character::strings(&value["notes"]))
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join("; ")
    } else {
        value
            .as_str()
            .map_or_else(|| crate::validation::repr(value), str::to_owned)
    }
}

fn apply(record: &mut Value, check: Value) -> Result<()> {
    if record.get("override_reason").is_some() {
        return Err(Error::invalid(
            "an overridden panel must be regenerated before recording a new review",
        ));
    }
    let checks = record["checks"]
        .as_array_mut()
        .ok_or_else(|| Error::invalid("panel checks must be an array"))?;
    let mut matches = checks
        .iter_mut()
        .filter(|item| item["id"] == "character-identity");
    let identity = matches
        .next()
        .ok_or_else(|| Error::invalid("panel must contain one character-identity check"))?;
    if matches.next().is_some() {
        return Err(Error::invalid(
            "panel must contain one character-identity check",
        ));
    }
    *identity = check;
    let decision = if checks.iter().any(hard_failure) {
        "regenerate"
    } else if checks.iter().any(warning) {
        "accept-warning"
    } else {
        "accept"
    };
    let mut seen = HashSet::new();
    let mut warnings = Vec::new();
    for item in checks {
        if item["id"] == "character-identity" {
            for region in items(&item["regions"]) {
                if !hard_failure(region)
                    && non_passing(region)
                    && let Some(guidance) = region["repair_guidance"].as_str()
                    && seen.insert(guidance)
                {
                    warnings.push(guidance.to_owned());
                }
            }
        } else if warning(item)
            && let Some(evidence) = item["evidence"].as_str()
            && seen.insert(evidence)
        {
            warnings.push(evidence.to_owned());
        }
    }
    record["decision"] = decision.into();
    record["unresolved_warnings"] = json!(warnings);
    Ok(())
}
