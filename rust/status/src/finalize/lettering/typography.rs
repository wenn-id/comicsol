use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Value, json};
use unicode_normalization::UnicodeNormalization;
use unicode_normalization::char::is_combining_mark;

use crate::error::{Error, Result, contract::UNICODE_BLOCKS};
use crate::manifest;
use crate::validation::{bytes_hash, python_whitespace};

use super::super::native::Font;

const REGULAR: usize = 0;
pub(super) const BOLD: usize = 1;
const FALLBACK: usize = 2;
const IDS: [&str; 3] = [
    "ComicNeue-Regular.ttf",
    "ComicNeue-Bold.ttf",
    "NotoSans-Regular.ttf",
];
const ROLES: [&str; 3] = ["regular", "bold", "fallback"];
const SOURCES: [&[u8]; 3] = [
    include_bytes!("../../../../../assets/fonts/ComicNeue-Regular.ttf"),
    include_bytes!("../../../../../assets/fonts/ComicNeue-Bold.ttf"),
    include_bytes!("../../../../../assets/fonts/NotoSans-Regular.ttf"),
];

pub(crate) fn diagnostics() -> (Vec<(&'static str, bool)>, Vec<String>) {
    let loaded: Vec<_> = SOURCES.iter().map(|source| Font::new(source)).collect();
    let names = [
        "Comic Neue Regular",
        "Comic Neue Bold",
        "Noto Sans fallback",
    ];
    let checks = names
        .into_iter()
        .zip(&loaded)
        .map(|(name, font)| {
            (
                name,
                font.as_ref()
                    .is_some_and(|font| font.layout("Ag".chars(), 42).is_some()),
            )
        })
        .collect();
    let alphabets = [
        (
            "latin",
            vec![
                0x0041..0x005b,
                0x0061..0x007b,
                0x0030..0x003a,
                0x21..0x22,
                0x2c..0x2d,
                0x2e..0x2f,
                0x3f..0x40,
            ],
        ),
        (
            "greek",
            vec![0x0391..0x03a2, 0x03a3..0x03aa, 0x03b1..0x03ca],
        ),
        ("cyrillic", vec![0x0410..0x0450]),
    ];
    let mut missing = Vec::new();
    for (script, ranges) in alphabets {
        let glyphs: Vec<_> = ranges
            .into_iter()
            .flatten()
            .filter_map(char::from_u32)
            .filter(|&c| !loaded.iter().flatten().any(|font| font.glyph_index(c) != 0))
            .collect();
        if !glyphs.is_empty() {
            let examples: Vec<_> = glyphs
                .iter()
                .take(8)
                .map(|&c| format!("U+{:04X}", c as u32))
                .collect();
            missing.push(format!(
                "{script} is missing {}{}",
                examples.join(", "),
                if glyphs.len() > 8 { " and more" } else { "" }
            ));
        }
    }
    (checks, missing)
}

#[derive(Clone, Copy)]
pub(super) struct Glyph {
    pub(super) character: char,
    pub(super) font: usize,
}

pub(super) struct Fonts {
    pub(super) faces: Vec<Font>,
    policy: Value,
    hash: String,
}

impl Fonts {
    pub(super) fn new() -> Result<Self> {
        let faces = SOURCES
            .iter()
            .map(|bytes| {
                Font::new(bytes).ok_or_else(|| {
                    Error::typography("font is not a readable TrueType/OpenType file")
                })
            })
            .collect::<Result<_>>()?;
        let mut policy = serde_json::Map::new();
        let mut binding = serde_json::Map::new();
        for (index, role) in ROLES.into_iter().enumerate() {
            policy.insert(role.into(), IDS[index].into());
            binding.insert(
                role.into(),
                json!({"font_id":IDS[index],"sha256":bytes_hash(SOURCES[index])}),
            );
        }
        Ok(Self {
            faces,
            policy: Value::Object(policy),
            hash: digest(&Value::Object(binding)),
        })
    }

    fn supports(&self, face: usize, character: char) -> bool {
        self.faces[face].glyph_index(character) != 0
    }

    fn select(&self, primary: usize, character: char, base: Option<usize>) -> Option<usize> {
        [base, Some(primary), Some(FALLBACK)]
            .into_iter()
            .flatten()
            .find(|&face| self.supports(face, character))
    }

    pub(super) fn tokens(&self, text: &str, primary: usize, emphasis: bool) -> Vec<Glyph> {
        let mut base = None;
        let mut result = Vec::new();
        for (span, bold) in styled(text, emphasis) {
            let face = if bold { BOLD } else { primary };
            for character in span.chars() {
                let combining = block(character).2;
                let font = if character == '\n' {
                    face
                } else {
                    self.select(face, character, if combining { base } else { None })
                        .unwrap_or(FALLBACK)
                };
                result.push(Glyph { character, font });
                if python_whitespace(character) {
                    base = None;
                } else if !combining {
                    base = Some(font);
                }
            }
        }
        result
    }

    pub(super) fn preflight(&self, items: &[Value]) -> Result<Value> {
        let mut glyphs = Vec::new();
        let mut whitespaces = Vec::new();
        let mut problems = Vec::new();
        let mut shaping_failed = false;
        let mut scripts = BTreeMap::<&str, (usize, BTreeSet<&str>)>::new();
        for (index, item) in items.iter().enumerate() {
            let id = item["id"]
                .as_str()
                .filter(|id| !id.is_empty())
                .ok_or_else(|| Error::invalid(format!("text item {} requires an ID", index + 1)))?;
            let authored = item
                .get("content")
                .map_or(Some(""), Value::as_str)
                .ok_or_else(|| {
                    Error::invalid(format!("text item {id} content must be a string"))
                })?;
            let content = display(item["kind"].as_str().unwrap_or_default(), authored);
            let mut base: Option<(char, usize)> = None;
            for (text, bold) in styled(&content, true) {
                let primary = if bold { BOLD } else { REGULAR };
                let style = if bold { "bold" } else { "regular" };
                for character in text.chars() {
                    let codepoint = format!("U+{:04X}", character as u32);
                    if python_whitespace(character) {
                        whitespaces.push(json!({"policy":if character=='\n' {"line-break"} else {"normalized-space"},"item_id":id,"codepoint":codepoint}));
                        base = None;
                        continue;
                    }
                    let (script, linear, combining, reason) = block(character);
                    let base_role = if combining {
                        base.map(|(_, face)| face)
                    } else {
                        None
                    };
                    let selected = self.select(primary, character, base_role);
                    let problem = if !linear {
                        Some(("unsupported-shaping", reason))
                    } else {
                        match selected {
                            None => Some(("missing-glyph", "")),
                            Some(face) if combining => match base {
                                None => Some((
                                    "unsupported-shaping",
                                    "combining mark has no base glyph to attach to",
                                )),
                                Some((base, _))
                                    if base.to_string().nfd().any(is_combining_mark) =>
                                {
                                    Some((
                                        "unsupported-shaping",
                                        "stacked combining marks require anchor positioning",
                                    ))
                                }
                                Some((_, base_face)) if base_face != face => Some((
                                    "unsupported-shaping",
                                    "combining mark and its base glyph resolve to different faces",
                                )),
                                _ => None,
                            },
                            _ => None,
                        }
                    };
                    if let Some((category, reason)) = problem {
                        shaping_failed |= category == "unsupported-shaping";
                        let mut tried = Vec::new();
                        for label in [base_role, Some(primary), Some(FALLBACK)]
                            .into_iter()
                            .flatten()
                            .map(|index| IDS[index])
                        {
                            if !tried.contains(&label) {
                                tried.push(label);
                            }
                        }
                        let reason = if reason.is_empty() {
                            String::new()
                        } else {
                            format!("{reason}; ")
                        };
                        let mut remediation =
                            "choose supported text or bundle a tested font".to_owned();
                        if category == "missing-glyph" {
                            if let Some((_, family, license)) =
                                crate::error::contract::SCRIPT_FONT_RECOMMENDATIONS
                                    .iter()
                                    .find(|(name, ..)| *name == script)
                            {
                                remediation = format!(
                                    "configure the {script} script font extension with {family} ({license}), or {remediation}"
                                );
                            }
                        }
                        problems.push(format!("{category}: {codepoint} in {id} ({script}; {style}; checked {}; {reason}{remediation})",tried.join(", ")));
                        continue;
                    }
                    let face = selected.expect("supported glyph");
                    glyphs.push(json!({"character":character.to_string(),"codepoint":codepoint,"coverage":"supported",
                        "font_id":IDS[face],"item_id":id,"script":script,"shaping":"supported","style":style}));
                    let entry = scripts.entry(script).or_default();
                    entry.0 += 1;
                    entry.1.insert(IDS[face]);
                    base = Some((character, face));
                }
            }
        }
        if !problems.is_empty() {
            let prefix = if shaping_failed {
                "unsupported shaping policy; "
            } else {
                ""
            };
            return Err(Error::typography(format!(
                "{prefix}{}",
                problems.join("; ")
            )));
        }
        let count = glyphs.len();
        let checks = [
            (
                "typography-shaping-policy",
                format!("Declared block policy admitted {count} glyph(s) as linearly placeable."),
            ),
            (
                "typography-glyph-coverage",
                format!("Every one of {count} glyph(s) resolved to a font that maps it."),
            ),
        ]
        .map(|(id, evidence)| {
            json!({"id":id,"evidence":evidence,"method":"font-cmap-policy-v1",
            "result":"pass","reviewer":"comic-sol","severity":"error"})
        });
        let scripts:Vec<_>=scripts.into_iter().map(|(script,(count,fonts))|
            json!({"script":script,"codepoints":count,"font_ids":fonts})).collect();
        Ok(
            json!({"checks":checks,"font_policy":self.policy,"font_policy_sha256":self.hash,
            "glyphs":glyphs,"input_sha256":digest(&json!(items)),"issues":[],"kind":"typography-preflight",
            "non_glyphs":whitespaces,"schema_version":"1.1","scripts":scripts,"status":"pass"}),
        )
    }
}

fn block(character: char) -> (&'static str, bool, bool, &'static str) {
    let code = character as u32;
    if code > 0xffff {
        return (
            "unassigned",
            false,
            false,
            "codepoint is outside the basic multilingual plane that lettering supports",
        );
    }
    UNICODE_BLOCKS
        .iter()
        .find(|(first, last, ..)| (*first..=*last).contains(&code))
        .map(|&(_, _, script, linear, combining, reason)| (script, linear, combining, reason))
        .unwrap_or((
            "unassigned",
            false,
            false,
            "codepoint belongs to no block the lettering policy has classified",
        ))
}

pub(super) fn font_runs(tokens: &[Glyph]) -> Vec<Value> {
    let mut runs: Vec<(usize, String)> = Vec::new();
    for &Glyph { character, font } in tokens {
        if let Some((last, text)) = runs.last_mut()
            && *last == font
        {
            text.push(character);
        } else {
            runs.push((font, String::from(character)));
        }
    }
    runs.into_iter().map(|(font,text)| json!({"font_id":IDS[font],"text":text,"style":if font==BOLD {"bold"} else {"regular"}})).collect()
}

fn styled(text: &str, emphasis: bool) -> Vec<(&str, bool)> {
    if !emphasis {
        return vec![(text, false)];
    }
    let spans: Vec<_> = text.split("**").collect();
    if spans.len() % 2 == 0
        || spans
            .iter()
            .enumerate()
            .any(|(i, part)| i % 2 == 1 && part.trim_matches(python_whitespace).is_empty())
    {
        return vec![(text, false)];
    }
    spans
        .into_iter()
        .enumerate()
        .filter(|(_, part)| !part.is_empty())
        .map(|(i, part)| (part, i % 2 == 1))
        .collect()
}

pub(super) fn display(kind: &str, text: &str) -> String {
    let clean: String = text
        .nfc()
        .map(|c| if c != '\n' && c.is_control() { ' ' } else { c })
        .collect();
    let clean = clean
        .split('\n')
        .map(|line| {
            line.split(python_whitespace)
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect::<Vec<_>>()
        .join("\n");
    let clean = clean.trim_matches(python_whitespace);
    if kind == "dialogue" {
        clean.to_uppercase().nfc().collect()
    } else {
        clean.to_owned()
    }
}

pub(super) fn digest(value: &Value) -> String {
    let mut bytes = manifest::compact(value);
    bytes.push(b'\n');
    bytes_hash(&bytes)
}
