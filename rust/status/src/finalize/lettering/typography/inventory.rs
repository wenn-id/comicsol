use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::path::Path;

use serde_json::{Value, json};

use super::{Error, Font, IDS, ROLES, Result, SOURCES, UNICODE_BLOCKS, load_font};
use crate::error::contract::SCRIPT_FONT_RECOMMENDATIONS;

pub(in crate::finalize) fn coverage<'a>(entries: impl Iterator<Item = &'a OsStr>) -> Result<Value> {
    let mut policy = BTreeMap::new();
    for entry in entries {
        let raw = entry
            .to_str()
            .ok_or_else(|| Error::invalid("font policy entry must be UTF-8"))?;
        let (role, path) = raw
            .split_once('=')
            .map(|(role, path)| (role.trim(), path.trim()))
            .filter(|(role, path)| !role.is_empty() && !path.is_empty())
            .ok_or_else(|| Error::invalid(format!("font policy entry must be ROLE=PATH: {raw}")))?;
        policy.insert(role, path);
    }
    let mut fonts = serde_json::Map::new();
    let mut union = BTreeSet::new();
    let mut add = |role: &str, id: &str, font: Font| {
        let mapped: Vec<_> = (0..=0xffff)
            .filter_map(char::from_u32)
            .filter(|&ch| font.glyph_index(ch) != 0)
            .map(u32::from)
            .collect();
        union.extend(mapped.iter().copied());
        fonts.insert(
            role.to_owned(),
            json!({"codepoints":mapped.len(), "font_id":id, "ranges": ranges(&mapped)}),
        );
    };
    if policy.is_empty() {
        for ((role, id), source) in ROLES.into_iter().zip(IDS).zip(SOURCES) {
            let font = Font::new(source)
                .ok_or_else(|| Error::typography("bundled font is not readable"))?;
            add(role, id, font);
        }
    } else {
        for (role, path) in policy {
            let (font, name, _) = load_font(Path::new(path))?;
            add(role, &name, font);
        }
    }
    let mut scripts: BTreeMap<_, (Vec<Value>, usize, u32, bool)> = BTreeMap::new();
    let mut claimed = 0;
    for &(first, last, script, linear, _, _, block) in UNICODE_BLOCKS {
        let count = union.range(first..=last).count();
        let total = last - first + 1;
        let (blocks, covered, capacity, has_linear) = scripts.entry(script).or_default();
        *covered += count;
        *capacity += total;
        *has_linear |= linear;
        claimed += total;
        blocks.push(json!({
            "block":block, "covered":count, "range":format!("U+{first:04X}-U+{last:04X}"),
            "shaping":if linear {"linear"} else {"complex"}, "total":total,
        }));
    }
    let scripts: Vec<_> = scripts.into_iter().map(|(script, (blocks, covered, total, linear))| {
        let recommendation = SCRIPT_FONT_RECOMMENDATIONS.iter()
            .find(|entry| entry.0 == script).map(|&(_, family, license, file, upstream)| {
                json!({"family":family, "file_name":file, "license":license, "script":script, "upstream":upstream})
            });
        let status = if !linear {"shaping-unsupported"} else if covered == 0 {"uncovered"}
            else if covered == total as usize {"covered"} else {"partial"};
        json!({
            "blocks":blocks, "covered":covered, "recommended_font":recommendation, "script":script,
            "shaping":if linear {"linear"} else {"complex"}, "status":status, "total":total,
        })
    }).collect();
    Ok(json!({
        "fonts":fonts, "kind":"font-coverage-inventory", "schema_version":"1.0", "scripts":scripts,
        "summary":{
            "bundled_target_scripts":["latin","greek","cyrillic"], "claimed_codepoints":claimed,
            "covered_codepoints":union.len(), "extension_target_scripts":["armenian","ethiopic","georgian","han","hangul","kana"],
            "lettering_plane_last":"U+FFFF",
        },
    }))
}

fn ranges(covered: &[u32]) -> Vec<String> {
    let mut spans: Vec<(u32, u32)> = Vec::new();
    for &point in covered {
        if let Some((_, last)) = spans.last_mut()
            && *last + 1 == point
        {
            *last = point;
        } else {
            spans.push((point, point));
        }
    }
    spans
        .into_iter()
        .map(|(first, last)| {
            if first == last {
                format!("U+{first:04X}")
            } else {
                format!("U+{first:04X}-U+{last:04X}")
            }
        })
        .collect()
}
