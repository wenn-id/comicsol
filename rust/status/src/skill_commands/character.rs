use std::collections::HashSet;
use std::fs::File;

use serde_json::{Value, json};

use super::{BOARD, document, one_action, panel_id, write_record};
use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::manifest;
use crate::project_fs::LockedProject;
use crate::validation::character::{PACK, pack_issues};
use crate::validation::{bytes_hash, is_identifier, items, read_value};

const BIBLE: &str = "plan/character-bible.json";

pub(super) fn run(locked: &LockedProject, args: &Arguments) -> Result<Value> {
    one_action(args, &["derive", "check", "panel"])?;
    let root = locked.root();
    let bible = document(root, BIBLE)?;
    if args.has("allow-missing-references") && !args.has("derive") {
        return Err(Error::invalid(
            "--allow-missing-references requires --derive",
        ));
    }
    if args.has("derive") {
        let existing = read_value(root, PACK)?;
        let pack = derive(&bible, existing.as_ref())?;
        require_valid(root, &pack, &bible, !args.has("allow-missing-references"))?;
        write_record(locked, "character-identity-pack", PACK, &pack)?;
        return Ok(PACK.into());
    }
    let pack = checked_pack(root, &bible)?;
    if let Some(panel) = args.text("panel") {
        let id = panel_id(panel)?;
        let board = document(root, BOARD)?;
        let panel = find_panel(&board, id)?;
        return Ok(prompt(&pack, panel)?.into());
    }
    Ok("character-identity-pack-ok".into())
}

pub(super) fn checked(root: &File) -> Result<Value> {
    checked_pack(root, &document(root, BIBLE)?)
}

fn checked_pack(root: &File, bible: &Value) -> Result<Value> {
    let pack = read_value(root, PACK)?
        .ok_or_else(|| Error::invalid(format!("{PACK} is missing; derive it before generation")))?;
    require_valid(root, &pack, bible, true)?;
    Ok(pack)
}

fn require_valid(
    root: &File,
    pack: &Value,
    bible: &Value,
    require_reference_files: bool,
) -> Result<()> {
    let (issues, _) = pack_issues(root, pack, bible, require_reference_files);
    if issues.is_empty() {
        Ok(())
    } else {
        Err(Error::invalid(
            issues.into_iter().collect::<Vec<_>>().join("\n"),
        ))
    }
}

fn derive(bible: &Value, existing: Option<&Value>) -> Result<Value> {
    let characters = bible["characters"]
        .as_array()
        .ok_or_else(|| Error::invalid("character bible characters must be a list"))?;
    let mut entries = Vec::with_capacity(characters.len());
    for character in characters {
        if !character.is_object() {
            return Err(Error::invalid(
                "character bible entries must be JSON objects",
            ));
        }
        let id = character["id"]
            .as_str()
            .filter(|value| is_identifier(value, 48, false))
            .ok_or_else(|| {
                Error::invalid(format!(
                    "character id is not a valid ID: {}",
                    crate::validation::repr(&character["id"])
                ))
            })?;
        let fingerprint = &character["visual_fingerprint"];
        if !fingerprint.is_object() {
            return Err(Error::invalid(format!(
                "character {id} has no visual_fingerprint object"
            )));
        }
        let path = character["reference_path"]
            .as_str()
            .filter(|path| !path.trim().is_empty())
            .ok_or_else(|| Error::invalid(format!("character {id} has no reference_path")))?;
        let previous = existing.and_then(|old| {
            items(&old["characters"])
                .iter()
                .rev()
                .find(|entry| entry["id"] == id)
        });
        let mut views = vec![json!({"view":"canonical", "path":path.replace('\\', "/")})];
        let mut build = &fingerprint["silhouette"];
        let mut notes = Vec::new();
        if let Some(previous) = previous {
            let mut extra: Vec<_> = items(&previous["reference_views"])
                .iter()
                .filter_map(|entry| {
                    let view = entry["view"].as_str().filter(|name| *name != "canonical")?;
                    Some((view, entry["path"].as_str()?))
                })
                .collect();
            extra.sort_by_key(|(view, _)| *view);
            views.extend(
                extra
                    .into_iter()
                    .map(|(view, path)| json!({"view":view, "path":path})),
            );
            let proportions = &previous["proportions"];
            notes = strings(&proportions["notes"]);
            if proportions["build"]
                .as_str()
                .is_some_and(|value| !value.trim().is_empty())
            {
                build = &proportions["build"];
            }
        }
        entries.push(json!({
            "id":id, "avoid":strings(&fingerprint["avoid"]),
            "immutable_traits": {
                "face":fingerprint["face"], "hair":fingerprint["hair"],
                "invariants":strings(&fingerprint["invariants"]), "silhouette":fingerprint["silhouette"],
            },
            "proportions":{"build":build, "notes":notes}, "reference_views":views,
            "source_fingerprint_sha256":bytes_hash(&manifest::compact(fingerprint)),
            "wardrobe":{"accessories":strings(&fingerprint["signature_props"]), "base":fingerprint["wardrobe"], "palette":strings(&fingerprint["palette"])},
        }));
    }
    Ok(json!({"characters":entries, "schema_version":"1.0"}))
}

pub(super) fn strings(value: &Value) -> Vec<&str> {
    items(value)
        .iter()
        .map(Value::as_str)
        .collect::<Option<_>>()
        .unwrap_or_default()
}

pub(super) fn find_panel<'a>(board: &'a Value, id: &str) -> Result<&'a Value> {
    board["pages"]
        .as_array()
        .ok_or_else(|| Error::invalid("storyboard pages must be a list"))?
        .iter()
        .flat_map(|page| items(&page["panels"]))
        .find(|panel| panel["id"] == id)
        .ok_or_else(|| Error::invalid(format!("storyboard has no panel '{id}'")))
}

pub(super) fn cast(panel: &Value) -> Vec<&str> {
    let mut seen = HashSet::new();
    strings(&panel["characters"])
        .into_iter()
        .filter(|id| seen.insert(*id))
        .collect()
}

pub(super) fn select<'a>(pack: &'a Value, cast: &[&str]) -> Result<Vec<&'a Value>> {
    let entries = items(&pack["characters"]);
    let mut unknown: Vec<_> = cast
        .iter()
        .filter(|&&id| !entries.iter().any(|entry| entry["id"] == id))
        .copied()
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(Error::invalid(format!(
            "identity pack has no entry for: {}",
            unknown.join(", ")
        )));
    }
    Ok(entries
        .iter()
        .filter(|entry| entry["id"].as_str().is_some_and(|id| cast.contains(&id)))
        .collect())
}

fn prompt(pack: &Value, panel: &Value) -> Result<String> {
    let mut lines = vec!["IDENTITY LOCK (character-identity-pack 1.0)".into()];
    for entry in select(pack, &cast(panel))? {
        let traits = &entry["immutable_traits"];
        let wardrobe = &entry["wardrobe"];
        let proportions = &entry["proportions"];
        let text = |value: &Value| value.as_str().unwrap_or_default().to_owned();
        let mut build = vec![text(&proportions["build"])];
        build.extend(
            strings(&proportions["notes"])
                .into_iter()
                .map(str::to_owned),
        );
        lines.extend([
            format!("- {}", text(&entry["id"])),
            format!(
                "  immutable: {}",
                ["silhouette", "face", "hair"]
                    .map(|key| text(&traits[key]))
                    .join("; ")
            ),
            format!(
                "  invariants: {}",
                strings(&traits["invariants"]).join("; ")
            ),
            format!("  proportions: {}", build.join("; ")),
            format!("  wardrobe: {}", text(&wardrobe["base"])),
        ]);
        let accessories = strings(&wardrobe["accessories"]);
        if !accessories.is_empty() {
            lines.push(format!("  accessories: {}", accessories.join("; ")));
        }
        lines.push(format!(
            "  palette: {}",
            strings(&wardrobe["palette"]).join("; ")
        ));
        let avoid = strings(&entry["avoid"]);
        if !avoid.is_empty() {
            lines.push(format!("  avoid: {}", avoid.join("; ")));
        }
        let views: Vec<_> = items(&entry["reference_views"])
            .iter()
            .map(|view| format!("{}={}", text(&view["view"]), text(&view["path"])))
            .collect();
        lines.push(format!("  reference views: {}", views.join("; ")));
    }
    Ok(lines.join("\n"))
}
