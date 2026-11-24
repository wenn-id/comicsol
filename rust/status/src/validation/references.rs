use std::collections::HashSet;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

use super::{items, repair::board_panels};

const CUES: &[(&str, &[&str])] = &[
    (
        "close-up",
        &[
            "extreme close",
            "close-up",
            "close up",
            "closeup",
            "close shot",
            "close on",
            "head shot",
            "headshot",
            "face shot",
            "insert shot",
            "detail shot",
        ],
    ),
    (
        "profile",
        &[
            "profile",
            "side view",
            "side-view",
            "side-on",
            "from the side",
        ],
    ),
    (
        "three-quarter",
        &[
            "three-quarter",
            "three quarter",
            "3/4",
            "over-the-shoulder",
            "over the shoulder",
            "medium-wide",
            "medium wide",
            "medium shot",
            "mid-shot",
            "mid shot",
            "waist-up",
            "waist up",
            "bust shot",
        ],
    ),
    (
        "full-body",
        &[
            "full-body",
            "full body",
            "full-length",
            "full length",
            "full figure",
            "full shot",
            "head-to-toe",
            "head to toe",
            "wide establishing",
            "establishing shot",
            "wide shot",
            "wide angle",
            "wide view",
            "wide framing",
            "long shot",
            "two-shot",
            "two shot",
        ],
    ),
];
const NEGATION: &[&str] = &[
    "avoid", "avoiding", "except", "instead", "never", "no", "none", "nor", "not", "rather",
    "without",
];
type Cue = (usize, &'static str, &'static str, Regex);
static MATCHERS: LazyLock<Vec<Cue>> = LazyLock::new(|| {
    CUES.iter()
        .enumerate()
        .flat_map(|(index, (class, cues))| {
            cues.iter().map(move |cue| {
                (
                    index,
                    *class,
                    *cue,
                    Regex::new(&format!(r"\b{}s?\b", regex::escape(cue))).expect("valid shot cue"),
                )
            })
        })
        .collect()
});

fn classify(value: &Value) -> (&'static str, Option<&'static str>) {
    let shot = caseless::default_case_fold_str(value.as_str().unwrap_or_default());
    MATCHERS
        .iter()
        .filter_map(|(index, class, cue, regex)| {
            regex
                .find_iter(&shot)
                .find(|m| {
                    !shot[..m.start()]
                        .split_whitespace()
                        .rev()
                        .take(3)
                        .map(|word| {
                            word.trim_matches([
                                '"', '\'', '(', ')', ',', '.', ';', ':', '!', '?', '-',
                            ])
                        })
                        .any(|word| NEGATION.contains(&word))
                })
                .map(|m| {
                    (
                        (m.start(), std::cmp::Reverse(cue.len()), *index),
                        *class,
                        *cue,
                    )
                })
        })
        .min_by_key(|(key, _, _)| *key)
        .map_or(("unclassified", None), |(_, class, cue)| (class, Some(cue)))
}

fn preference(class: &str) -> [&str; 5] {
    match class {
        "close-up" => [
            "canonical",
            "close-up",
            "three-quarter",
            "profile",
            "full-body",
        ],
        "profile" => [
            "canonical",
            "profile",
            "three-quarter",
            "close-up",
            "full-body",
        ],
        "three-quarter" => [
            "canonical",
            "three-quarter",
            "profile",
            "full-body",
            "close-up",
        ],
        "full-body" => [
            "canonical",
            "full-body",
            "three-quarter",
            "profile",
            "close-up",
        ],
        _ => [
            "canonical",
            "close-up",
            "profile",
            "three-quarter",
            "full-body",
        ],
    }
}

pub(super) fn plan(pack: &Value, board: &Value, budget: &Value) -> Option<Value> {
    let limit = if budget.is_null() {
        None
    } else {
        Some(budget.as_u64()?)
    };
    let pack = pack["characters"].as_array()?;
    let mut plans = Vec::new();
    for panel in board_panels(board)? {
        let roster: HashSet<_> = panel["characters"]
            .as_array()?
            .iter()
            .map(Value::as_str)
            .collect::<Option<_>>()?;
        let characters: Vec<_> = pack
            .iter()
            .filter_map(|entry| {
                entry["id"]
                    .as_str()
                    .filter(|id| roster.contains(id))
                    .map(|id| (id, entry))
            })
            .collect();
        if characters.len() != roster.len() {
            return None;
        }
        let (class, cue) = classify(&panel["shot"]);
        let preference = preference(class);
        let candidates: Vec<_> = characters
            .iter()
            .map(|(id, entry)| {
                let mut views: Vec<_> = items(&entry["reference_views"])
                    .iter()
                    .filter_map(|view| {
                        let name = view["view"].as_str()?;
                        let path = view["path"].as_str()?;
                        let rank = preference
                            .iter()
                            .position(|&ranked| ranked == name)
                            .unwrap_or(preference.len());
                        let reason = if rank == preference.len() {
                            "scene-specific"
                        } else if name == "canonical" {
                            "canonical-anchor"
                        } else if name == class {
                            "shot-aligned"
                        } else {
                            "identity-supplement"
                        };
                        Some((rank, name, path, reason))
                    })
                    .collect();
                views.sort_unstable_by_key(|(rank, name, path, _)| (*rank, *name, *path));
                (*id, views)
            })
            .collect();
        let mut seen = HashSet::new();
        let mut selected = Vec::new();
        let mut omitted = Vec::new();
        for depth in 0..candidates
            .iter()
            .map(|(_, views)| views.len())
            .max()
            .unwrap_or(0)
        {
            for (id, views) in &candidates {
                let Some((_, view, path, reason)) = views.get(depth) else {
                    continue;
                };
                let omit = if limit == Some(0) {
                    Some("references-unsupported")
                } else if seen.contains(path) {
                    Some("duplicate-path")
                } else if limit.is_some_and(|limit| selected.len() as u64 >= limit) {
                    Some("reference-budget")
                } else {
                    None
                };
                if let Some(reason) = omit {
                    omitted
                        .push(json!({"character_id":id,"view":view,"path":path,"reason":reason}));
                } else {
                    seen.insert(*path);
                    selected.push(json!({"character_id":id,"view":view,"path":path,"reason":reason,"rank":selected.len()+1}));
                }
            }
        }
        plans.push(json!({"panel_id":panel["id"],"shot_class":class,"shot_cue":cue,"reference_budget":budget,
            "characters":characters.iter().map(|(id,_)|*id).collect::<Vec<_>>(),"selected":selected,"omitted":omitted}));
    }
    Some(json!({"schema_version":"1.0","panels":plans}))
}
