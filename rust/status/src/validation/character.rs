use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs::File;

use serde_json::{Value, json};

use super::{
    CHARACTERS, STORYBOARD, artifact, bytes_hash, has_duplicates, is_identifier, is_sha256, items,
    keys, read_value, references, repair, text,
};
use crate::manifest;

const PACK: &str = "plan/character-identity-pack.json";
const REFERENCE_PLAN: &str = "logs/reference-selection.json";
const TRAITS: &[&str] = &[
    "face",
    "hair",
    "age-appearance",
    "clothing",
    "accessories",
    "proportions",
    "immutable-traits",
];
const CHECK_FIELDS: &[&str] = &[
    "evidence",
    "id",
    "method",
    "provenance",
    "regions",
    "result",
    "reviewer",
    "severity",
];
const REGION_FIELDS: &[&str] = &[
    "character_id",
    "evidence",
    "expected",
    "repair_guidance",
    "result",
    "severity",
    "trait",
];
const PROVENANCE_FIELDS: &[&str] = &[
    "characters",
    "identity_pack_path",
    "identity_pack_sha256",
    "panel_id",
    "reference_plan_path",
    "reference_plan_sha256",
];

pub(super) fn identity_issues(check: &Value, record: &Value) -> BTreeSet<&'static str> {
    let mut issues = BTreeSet::new();
    let Some(provenance) = check.get("provenance") else {
        if !check["regions"].as_array().is_some_and(Vec::is_empty) {
            issues.insert("character-provenance-structure");
        }
        return issues;
    };
    if !keys(check, CHECK_FIELDS) {
        issues.insert("character-check-structure");
    }
    if !keys(provenance, PROVENANCE_FIELDS) {
        issues.insert("character-provenance-structure");
    } else {
        if provenance["identity_pack_path"] != PACK
            || provenance["reference_plan_path"] != REFERENCE_PLAN
        {
            issues.insert("character-provenance-path");
        }
        if !["identity_pack_sha256", "reference_plan_sha256"]
            .iter()
            .all(|&key| provenance[key].as_str().is_some_and(is_sha256))
        {
            issues.insert("character-provenance-hash");
        }
        if !provenance["panel_id"].is_string() {
            issues.insert("character-provenance-panel");
        }
        if !provenance["characters"].is_array() {
            issues.insert("character-provenance-characters");
        }
    }
    let Some(regions) = check["regions"]
        .as_array()
        .filter(|regions| !regions.is_empty())
    else {
        issues.insert("character-trait-structure");
        return issues;
    };
    let mut seen = HashSet::new();
    let mut order = BTreeMap::<&str, Vec<&str>>::new();
    for region in regions {
        if !keys(region, REGION_FIELDS) {
            issues.insert("character-trait-structure");
            continue;
        }
        let Some(id) = text(&region["character_id"]) else {
            issues.insert("character-trait-identity");
            continue;
        };
        let Some(trait_id) = region["trait"]
            .as_str()
            .filter(|value| TRAITS.contains(value))
        else {
            issues.insert("character-trait-id");
            continue;
        };
        if !seen.insert((id, trait_id)) {
            issues.insert("character-trait-duplicate");
        }
        order.entry(id).or_default().push(trait_id);
        let expected = &region["expected"];
        let strings = |value: &Value| {
            value
                .as_array()
                .is_some_and(|values| values.iter().all(|value| text(value).is_some()))
        };
        let valid = match trait_id {
            "face" | "hair" | "age-appearance" | "clothing" => text(expected).is_some(),
            "accessories" | "immutable-traits" => strings(expected),
            "proportions" => {
                keys(expected, &["build", "notes"])
                    && text(&expected["build"]).is_some()
                    && strings(&expected["notes"])
            }
            _ => false,
        };
        if !valid {
            issues.insert("character-trait-expected");
        }
        if !repair::result(&region["result"]) {
            issues.insert("character-trait-result");
        }
        if !repair::severity(&region["severity"]) {
            issues.insert("character-trait-severity");
        }
        if repair::evidence(&region["evidence"]).is_none() {
            issues.insert("character-trait-evidence");
        }
        let needs_repair = region["result"] != "pass" || region["severity"] == "warning";
        if if needs_repair {
            text(&region["repair_guidance"]).is_none()
        } else {
            !region["repair_guidance"].is_null()
        } {
            issues.insert("character-trait-repair-guidance");
        }
    }
    if order.values().any(|order| order != TRAITS) {
        issues.insert("character-trait-order");
    }
    let regions = regions.iter().filter(|region| region.is_object());
    let fail = |region: &Value| region["result"] == "fail" && region["severity"] == "error";
    let override_allowed = record.get("override_reason").is_some()
        && check["result"] == "fail"
        && check["severity"] == "warning";
    let expected = if regions.clone().any(fail) {
        ("fail", "error")
    } else if override_allowed
        && regions
            .clone()
            .any(|region| region["result"] == "fail" && region["severity"] == "warning")
    {
        ("fail", "warning")
    } else if regions.clone().any(|region| {
        !fail(region) && (region["result"] != "pass" || region["severity"] == "warning")
    }) {
        ("warning", "warning")
    } else {
        ("pass", "error")
    };
    if check["result"] != expected.0 || check["severity"] != expected.1 {
        issues.insert("character-check-outcome");
    }
    issues
}

fn fields(value: &Value, allowed: &[&str], prefix: &str, issues: &mut BTreeSet<String>) -> bool {
    let Some(obj) = value.as_object() else {
        issues.insert(format!("{prefix} must be an object"));
        return false;
    };
    let missing: BTreeSet<_> = allowed
        .iter()
        .copied()
        .filter(|key| !obj.contains_key(*key))
        .collect();
    let unknown: Vec<_> = obj
        .keys()
        .map(String::as_str)
        .filter(|key| !allowed.contains(key))
        .collect();
    if !missing.is_empty() {
        issues.insert(format!(
            "{prefix} is missing fields: {}",
            missing.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    if !unknown.is_empty() {
        issues.insert(format!(
            "{prefix} has unknown fields: {}",
            unknown.join(", ")
        ));
    }
    true
}

fn string(value: &Value, prefix: &str, issues: &mut BTreeSet<String>) {
    if text(value).is_none() {
        issues.insert(format!("{prefix} must be a non-empty string"));
    }
}

fn strings(value: &Value, prefix: &str, min: usize, max: usize, issues: &mut BTreeSet<String>) {
    let Some(array) = value.as_array() else {
        issues.insert(format!("{prefix} must be an array of strings"));
        return;
    };
    let Some(contents) = array.iter().map(text).collect::<Option<Vec<_>>>() else {
        issues.insert(format!("{prefix} must contain only non-empty strings"));
        return;
    };
    if has_duplicates(&contents) {
        issues.insert(format!("{prefix} must not repeat a value"));
    }
    if contents.len() < min {
        issues.insert(format!("{prefix} requires at least {min} entries"));
    }
    if contents.len() > max {
        issues.insert(format!("{prefix} allows at most {max} entries"));
    }
}

fn entries(value: &Value) -> BTreeMap<&str, &Value> {
    items(&value["characters"])
        .iter()
        .filter_map(|entry| entry["id"].as_str().map(|id| (id, entry)))
        .collect()
}

fn compare_identity(child: &Value, source: &Value) -> bool {
    let normalize = |value: &Value| {
        if value.is_array() && items(value).iter().any(|item| !item.is_string()) {
            json!([])
        } else {
            value.clone()
        }
    };
    normalize(child) == normalize(source)
}

pub(super) fn identity_pack(root: &File, pack: &Value, bible: &Value) -> (Option<String>, bool) {
    let mut issues = BTreeSet::new();
    let mut reference_issues = BTreeSet::new();
    fields(
        pack,
        &["characters", "schema_version"],
        "character-identity-pack",
        &mut issues,
    );
    if pack["schema_version"] != "1.0" {
        issues.insert("character-identity-pack.schema_version must be one of 1.0".into());
    }
    let Some(characters) = pack["characters"].as_array() else {
        issues.insert("character-identity-pack.characters must be an array".into());
        return (issues.into_iter().next(), false);
    };
    let mut seen_ids = HashSet::new();
    for (index, entry) in characters.iter().enumerate() {
        let prefix = format!("character-identity-pack.characters[{index}]");
        if !fields(
            entry,
            &[
                "avoid",
                "id",
                "immutable_traits",
                "proportions",
                "reference_views",
                "source_fingerprint_sha256",
                "wardrobe",
            ],
            &prefix,
            &mut issues,
        ) {
            continue;
        }
        let id = entry["id"]
            .as_str()
            .filter(|value| is_identifier(value, 48, false));
        if let Some(id) = id {
            if !seen_ids.insert(id) {
                issues.insert(
                    "character-identity-pack.characters must not repeat a character id".into(),
                );
            }
        } else {
            issues.insert(format!("{prefix}.id must be a valid ID"));
        }
        for (name, children, words, lists) in [
            (
                "immutable_traits",
                &["face", "hair", "invariants", "silhouette"][..],
                &["face", "hair", "silhouette"][..],
                &[("invariants", 2, 5)][..],
            ),
            (
                "wardrobe",
                &["accessories", "base", "palette"][..],
                &["base"][..],
                &[("accessories", 0, usize::MAX), ("palette", 1, usize::MAX)][..],
            ),
            (
                "proportions",
                &["build", "notes"][..],
                &["build"][..],
                &[("notes", 0, usize::MAX)][..],
            ),
        ] {
            let group = format!("{prefix}.{name}");
            let value = &entry[name];
            if fields(value, children, &group, &mut issues) {
                for &name in words {
                    string(&value[name], &format!("{group}.{name}"), &mut issues);
                }
                for &(name, min, max) in lists {
                    strings(
                        &value[name],
                        &format!("{group}.{name}"),
                        min,
                        max,
                        &mut issues,
                    );
                }
            }
        }
        strings(
            &entry["avoid"],
            &format!("{prefix}.avoid"),
            0,
            usize::MAX,
            &mut issues,
        );
        if !entry["source_fingerprint_sha256"]
            .as_str()
            .is_some_and(is_sha256)
        {
            issues.insert(format!(
                "{prefix}.source_fingerprint_sha256 must be a SHA-256 value"
            ));
        }
        let group = format!("{prefix}.reference_views");
        let Some(views) = entry["reference_views"]
            .as_array()
            .filter(|views| !views.is_empty())
        else {
            issues.insert(format!(
                "{group} must be a non-empty array of reference views"
            ));
            continue;
        };
        let mut names = Vec::new();
        for (index, view) in views.iter().enumerate() {
            let field = format!("{group}[{index}]");
            if !fields(view, &["path", "view"], &field, &mut issues) {
                continue;
            }
            if let Some(name) = view["view"]
                .as_str()
                .filter(|value| is_identifier(value, 48, false))
            {
                names.push(name);
            } else {
                issues.insert(format!("{field}.view must be a valid ID"));
            }
            if let Some(path) = text(&view["path"]) {
                if path.contains('\\')
                    || path.starts_with('/')
                    || path.split('/').any(|part| part == "..")
                {
                    issues.insert(format!(
                        "{field}.path must be a POSIX relative project path"
                    ));
                } else if let Some(id) = id {
                    match artifact(root, path) {
                        Ok(Some(_)) => {}
                        Ok(None) => {
                            reference_issues.insert(format!("character-identity-pack character '{id}' reference view file is missing: {path}"));
                        }
                        Err(error) => {
                            let detail = error.detail().unwrap_or("filesystem error");
                            reference_issues.insert(format!("character-identity-pack character '{id}' reference view is not a contained project path: {path} ({detail})"));
                        }
                    }
                }
            } else {
                issues.insert(format!("{field}.path must be a non-empty relative path"));
            }
        }
        if has_duplicates(&names) {
            issues.insert(format!("{group} must not repeat a view name"));
        }
        if !names.contains(&"canonical") {
            issues.insert(format!("{group} must include the 'canonical' view"));
        }
    }
    if !bible["characters"].is_array() {
        issues.insert("plan/character-bible.json characters must be an array".into());
    } else {
        let expected = entries(bible);
        let actual = entries(pack);
        for (id, source) in &expected {
            let Some(entry) = actual.get(id) else {
                issues.insert(format!(
                    "character-identity-pack is missing bible character '{id}'"
                ));
                continue;
            };
            let fingerprint = &source["visual_fingerprint"];
            if !fingerprint.is_object() {
                issues.insert(format!(
                    "character bible character '{id}' has no visual_fingerprint"
                ));
                continue;
            }
            if entry["source_fingerprint_sha256"] != bytes_hash(&manifest::compact(fingerprint)) {
                issues.insert(format!("character-identity-pack character '{id}' is stale: re-derive it after the character bible fingerprint changed"));
            }
            for (field, key) in [
                ("avoid", "avoid"),
                ("immutable_traits.face", "face"),
                ("immutable_traits.hair", "hair"),
                ("immutable_traits.invariants", "invariants"),
                ("immutable_traits.silhouette", "silhouette"),
                ("wardrobe.accessories", "signature_props"),
                ("wardrobe.base", "wardrobe"),
                ("wardrobe.palette", "palette"),
            ] {
                let value = field.split('.').fold(*entry, |value, key| &value[key]);
                if !compare_identity(value, &fingerprint[key]) {
                    issues.insert(format!("character-identity-pack character '{id}' {field} must match the bible verbatim"));
                }
            }
            let view = items(&entry["reference_views"])
                .iter()
                .find(|view| view["view"] == "canonical")
                .map_or(&Value::Null, |view| &view["path"]);
            if view != &source["reference_path"] {
                issues.insert(format!("character-identity-pack character '{id}' canonical view must be the character bible reference_path"));
            }
        }
        for id in actual.keys().filter(|id| !expected.contains_key(*id)) {
            issues.insert(format!(
                "character-identity-pack character '{id}' is not in the character bible"
            ));
        }
    }
    let valid = issues.is_empty();
    issues.extend(reference_issues);
    (issues.into_iter().next(), valid)
}

fn bound(
    root: &File,
    binding: &Value,
    key: &str,
    expected: &str,
    label: &str,
    issues: &mut BTreeSet<String>,
) -> Option<Value> {
    if binding[&format!("{key}_path")] != expected {
        issues.insert(format!("{label} path is not canonical"));
        return None;
    }
    let value = match read_value(root, expected) {
        Ok(Some(value)) => value,
        Ok(None) => {
            issues.insert(format!("{label} cannot be read: FileNotFoundError"));
            return None;
        }
        Err(error) => {
            let detail = error.detail().unwrap_or_default();
            let message = if detail.starts_with("JSONDecodeError") || detail == "UnicodeDecodeError"
            {
                format!("{label} is not valid JSON")
            } else {
                let kind = match error.code() {
                    "CS-PROJ-002" => "FileNotFoundError",
                    "CS-PROJ-003" => "PermissionError",
                    "CS-SEC-002" => "InputResourceLimitError",
                    "CS-SEC-001" | "CS-PROJ-001" => "ValueError",
                    _ => "OSError",
                };
                format!("{label} cannot be read: {kind}")
            };
            issues.insert(message);
            return None;
        }
    };
    if !value.is_object() {
        issues.insert(format!("{label} must contain a JSON object"));
        return None;
    }
    if binding[&format!("{key}_sha256")] != bytes_hash(&manifest::canonical(&value)) {
        issues.insert(format!("{label} hash does not match the reviewed artifact"));
    }
    Some(value)
}

pub(super) fn provenance(root: &File, record: &Value) -> BTreeSet<String> {
    let mut issues = BTreeSet::new();
    let Some(checks) = record["checks"].as_array() else {
        return BTreeSet::from(["panel checks must be an array".to_owned()]);
    };
    let Some(check) = checks
        .iter()
        .find(|check| check["id"] == "character-identity")
    else {
        return issues;
    };
    let Some(binding) = check.get("provenance") else {
        return issues;
    };
    if !binding.is_object() {
        return BTreeSet::from(["character identity provenance must be an object".to_owned()]);
    }
    let pack = bound(
        root,
        binding,
        "identity_pack",
        PACK,
        "identity pack",
        &mut issues,
    );
    let plan = bound(
        root,
        binding,
        "reference_plan",
        REFERENCE_PLAN,
        "reference plan",
        &mut issues,
    );
    if binding["panel_id"] != record["subject_id"] {
        issues.insert("reviewed panel does not match the QA subject".into());
    }
    let (Some(pack), Some(plan), Some(id)) = (pack, plan, record["subject_id"].as_str()) else {
        return issues;
    };
    let failure = "character identity context cannot be rebuilt: CharacterQualityError";
    let Some(bible) = context_document(root, CHARACTERS, &mut issues) else {
        return issues;
    };
    let (problem, valid) = identity_pack(root, &pack, &bible);
    if let Some(problem) = problem {
        issues.insert(format!(
            "character bible and identity pack disagree: {problem}"
        ));
    }
    let Some(board) = context_document(root, STORYBOARD, &mut issues) else {
        return issues;
    };
    let Some(panel) = items(&plan["panels"])
        .iter()
        .find(|panel| panel["panel_id"] == id)
    else {
        issues.insert(failure.into());
        return issues;
    };
    if !valid || references::plan(&pack, &board, &panel["reference_budget"]).as_ref() != Some(&plan)
    {
        issues.insert(failure.into());
        return issues;
    }
    let packed = entries(&pack);
    let source = entries(&bible);
    let mut characters = Vec::new();
    let mut expected_traits = Vec::new();
    for character_id in items(&panel["characters"]).iter().filter_map(Value::as_str) {
        let (Some(identity), Some(base)) = (packed.get(character_id), source.get(character_id))
        else {
            issues.insert(failure.into());
            return issues;
        };
        let traits = &identity["immutable_traits"];
        let wardrobe = &identity["wardrobe"];
        let values = [
            &traits["face"],
            &traits["hair"],
            &base["age_band"],
            &wardrobe["base"],
            &wardrobe["accessories"],
            &identity["proportions"],
            &traits["invariants"],
        ];
        expected_traits.extend(
            TRAITS
                .iter()
                .copied()
                .zip(values)
                .map(|(trait_id, value)| (character_id, trait_id, value)),
        );
        let selected: Vec<_> = items(&panel["selected"])
            .iter()
            .filter(|value| value["character_id"] == character_id)
            .collect();
        characters.push(
            json!({"character_id":character_id,"selected_references":selected,
            "source_fingerprint_sha256":identity["source_fingerprint_sha256"]}),
        );
    }
    if binding["characters"] != Value::Array(characters) {
        issues.insert("character identity or selected references changed since review".into());
    }
    let actual: Vec<_> = items(&check["regions"])
        .iter()
        .filter(|region| region.is_object())
        .map(|region| {
            (
                region["character_id"].as_str(),
                region["trait"].as_str(),
                &region["expected"],
            )
        })
        .collect();
    if !expected_traits
        .into_iter()
        .map(|(id, trait_id, value)| (Some(id), Some(trait_id), value))
        .eq(actual)
    {
        issues.insert("reviewed character trait expectations are stale".into());
    }
    issues
}

fn context_document(root: &File, path: &str, issues: &mut BTreeSet<String>) -> Option<Value> {
    let error = match read_value(root, path) {
        Ok(Some(value)) if value.is_object() => return Some(value),
        Ok(Some(_)) => "CharacterQualityError",
        Ok(None) => "FileNotFoundError",
        Err(error) => {
            let detail = error.detail().unwrap_or_default();
            if detail.starts_with("JSONDecodeError") {
                "JSONDecodeError"
            } else if detail == "UnicodeDecodeError" {
                "UnicodeDecodeError"
            } else if error.code() == "CS-SEC-002" {
                "InputResourceLimitError"
            } else {
                "ValueError"
            }
        }
    };
    issues.insert(format!(
        "character identity context cannot be rebuilt: {error}"
    ));
    None
}
