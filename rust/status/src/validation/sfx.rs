use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};
use unicode_normalization::UnicodeNormalization;

use super::{items, python_whitespace, repr};

const LETTERED: &str = "deterministic-lettering";
const GENERATED: &str = "generated-visual";

fn id(item: &Value) -> String {
    item.get("id").map_or_else(String::new, |value| {
        value.as_str().map_or_else(|| repr(value), str::to_owned)
    })
}

fn mode(item: &Value) -> &str {
    match item.get("render_mode") {
        None | Some(Value::Null) => GENERATED,
        Some(value) => value.as_str().unwrap_or_default(),
    }
}

fn flag(
    id: &str,
    ids: impl IntoIterator<Item = String>,
    evidence: String,
    remediation: &str,
) -> Value {
    json!({"id": id, "item_ids": ids.into_iter().collect::<BTreeSet<_>>(),
        "result":"warning", "severity":"warning", "evidence":evidence,
        "method":"sfx-policy-v1", "reviewer":"comic-sol", "remediation":remediation})
}

fn flags(panel: &Value, effects: &[&Value]) -> Vec<Value> {
    let mut risks = Vec::new();
    let mut duplicate = BTreeMap::<String, Vec<String>>::new();
    let mut budget = Vec::new();
    for effect in effects.iter().filter(|effect| mode(effect) == GENERATED) {
        let name = id(effect);
        let Some(content) = effect["content"].as_str() else {
            continue;
        };
        let normalized: String = content.nfc().collect();
        let risky = normalized
            .chars()
            .filter(|ch| {
                !python_whitespace(*ch)
                    && !ch.is_ascii_alphanumeric()
                    && !"!?.,'\"-–—*~/&:;()".contains(*ch)
            })
            .map(|ch| format!("U+{:04X}", u32::from(ch)))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>();
        if !risky.is_empty() {
            risks.push((name.clone(), risky.join(", ")));
        }
        let tokens: Vec<_> = normalized
            .split(python_whitespace)
            .filter(|token| !token.is_empty())
            .collect();
        let comparable = tokens.join(" ");
        if !comparable.is_empty() {
            duplicate
                .entry(caseless::default_case_fold_str(&comparable))
                .or_default()
                .push(name.clone());
        }
        let longest = tokens
            .iter()
            .map(|token| token.chars().count())
            .max()
            .unwrap_or(0);
        let total = comparable.chars().count();
        if longest > 12 {
            budget.push((name, format!("{longest}-character token exceeds 12")));
        } else if total > 24 {
            budget.push((name, format!("{total} characters exceed 24")));
        }
    }
    let mut flags = Vec::new();
    let detail = |entries: &[(String, String)]| {
        entries
            .iter()
            .map(|(id, text)| format!("{id}: {text}"))
            .collect::<Vec<_>>()
            .join("; ")
    };
    if !risks.is_empty() {
        flags.push(flag("sfx-glyph-risk", risks.iter().map(|(id, _)| id.clone()),
            format!("Generated SFX uses codepoints outside the repertoire an image model letters reliably ({}).", detail(&risks)),
            "author the effect with Latin letters, digits, and comic punctuation, or letter it with render_mode deterministic-lettering under a font policy that covers the script (letter_panels.py --font-script)"));
    }
    duplicate.retain(|_, ids| ids.len() > 1);
    if !duplicate.is_empty() {
        for ids in duplicate.values_mut() {
            ids.sort();
        }
        let details = duplicate
            .iter()
            .map(|(content, ids)| {
                format!(
                    "{} share {}",
                    ids.join(", "),
                    repr(&Value::from(content.as_str()))
                )
            })
            .collect::<Vec<_>>()
            .join("; ");
        flags.push(flag("sfx-duplicate-content", duplicate.into_values().flatten(),
            format!("Generated SFX items in one panel request the same effect, so a drawn effect cannot be attributed to one of them ({details})."),
            "give each effect distinct content, drop the duplicate, or letter one of them with render_mode deterministic-lettering"));
    }
    if !budget.is_empty() {
        flags.push(flag("sfx-legibility-budget", budget.iter().map(|(id, _)| id.clone()),
            format!("Generated SFX is long enough that the image model must compress it to fit the panel, which is where dropped letters come from ({}).", detail(&budget)),
            "shorten the effect, split it across panels, or letter it with render_mode deterministic-lettering"));
    }
    let lettered: Vec<_> = effects
        .iter()
        .filter(|effect| mode(effect) == LETTERED)
        .map(|effect| id(effect))
        .collect();
    if !lettered.is_empty()
        && !items(&panel["negative"])
            .iter()
            .filter_map(Value::as_str)
            .any(|negative| caseless::default_case_fold_str(negative).contains("generated sfx"))
    {
        flags.push(flag("sfx-unprohibited-generation", lettered,
            "This panel letters its own SFX, but its negatives never prohibit generated SFX, so regenerated artwork can bake an effect underneath the drawn one.".into(),
            "add a negative such as 'generated sfx text' to the panel"));
    }
    flags
}

pub(crate) fn provenance(panel: &Value, placements: &Value) -> Value {
    let mut effects: Vec<_> = items(&panel["text"])
        .iter()
        .filter(|item| item["kind"] == "sfx")
        .collect();
    effects.sort_by(|a, b| {
        a["priority"]
            .as_f64()
            .unwrap_or_default()
            .total_cmp(&b["priority"].as_f64().unwrap_or_default())
            .then_with(|| id(a).cmp(&id(b)))
    });
    let records: Vec<_> = effects
        .iter()
        .map(|effect| {
            let id = id(effect);
            let mode = mode(effect);
            let lettered = mode == LETTERED;
            let placement = if lettered {
                items(placements).iter().find(|entry| entry["id"] == id)
            } else {
                None
            };
            let anchor = placement
                .map(|entry| &entry["anchor"])
                .filter(|value| !value.is_null())
                .unwrap_or(&effect["anchor"]);
            let bounds = placement.and_then(|entry| {
                ["x", "y", "width", "height"]
                    .into_iter()
                    .map(|key| {
                        let value = &entry["box"][key];
                        let value = value
                            .as_i64()
                            .or_else(|| value.as_str()?.parse().ok())
                            .or_else(|| value.as_f64().filter(|v| v.is_finite()).map(|v| v as i64))
                            .or_else(|| value.as_bool().map(i64::from))?;
                        Some((key.to_owned(), Value::from(value)))
                    })
                    .collect::<Option<Map<String, Value>>>()
            });
            json!({"id":id, "render_mode":mode, "content":effect["content"], "anchor":anchor,
            "box":bounds, "origin":if lettered {"comic-sol-lettering"} else {"image-model"},
            "verification":if lettered {"deterministic"} else {"reviewer-only"}})
        })
        .collect();
    json!({"schema_version":"1.0", "flags":flags(panel, &effects), "items":records})
}
