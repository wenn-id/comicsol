mod character;
pub(crate) mod handoff;
mod lettering;
mod page_quality;
pub(crate) mod panels;
mod planning;
mod references;
mod repair;
pub(crate) mod sfx;
pub(crate) mod storyboard;
pub(crate) mod terminal;

use std::collections::HashSet;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::{Regex, RegexSet};
use serde_json::{Map, Value, json};

use crate::error::{Result, contract};
use crate::project_fs::{self, LockedProject};
use crate::{manifest, project_fs::target};

const PROJECT: &str = "project.json";
const STORY: &str = "plan/story-plan.json";
const CHARACTERS: &str = "plan/character-bible.json";
const STORYBOARD: &str = "plan/storyboard.json";
type Object = Map<String, Value>;

#[derive(Eq, Ord, PartialEq, PartialOrd)]
pub struct Issue {
    path: String,
    field: String,
    message: String,
}

struct Report<'a> {
    path: &'a str,
    issues: &'a mut Vec<Issue>,
}

static SECRETS: LazyLock<RegexSet> = LazyLock::new(|| {
    RegexSet::new([
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\b(?:sk|rk)-[A-Za-z0-9]{16,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
        r"\bAIza[0-9A-Za-z_-]{30,}\b",
        r"(?i)(?:api[_-]?key|secret|password|token|credential)[=:]\s*\S",
    ])
    .expect("canonical secret patterns")
});
static DECIMAL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\p{Nd}+$").expect("decimal regex"));

#[derive(Clone, Copy, PartialEq)]
pub enum Stage {
    Plan,
    Storyboard,
    Panels,
    All,
    Final,
    ExportReady,
}

impl Stage {
    pub fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "plan" => Self::Plan,
            "storyboard" => Self::Storyboard,
            "panels" => Self::Panels,
            "all" => Self::All,
            "final" => Self::Final,
            "export-ready" => Self::ExportReady,
            _ => return None,
        })
    }

    fn terminal(self) -> bool {
        matches!(self, Self::All | Self::Final | Self::ExportReady)
    }
}

pub(crate) fn linear_statuses() -> &'static [&'static str] {
    planning::LINEAR
}

pub(crate) fn plan_inputs(story: &Value, characters: &Value, board: &Value) -> Result<()> {
    let mut issues = Vec::new();
    planning::story(
        story,
        &mut Report {
            path: STORY,
            issues: &mut issues,
        },
    );
    planning::characters(
        characters,
        &mut Report {
            path: CHARACTERS,
            issues: &mut issues,
        },
    );
    if issues.is_empty() {
        planning::reference_characters(
            story,
            characters,
            &mut Report {
                path: STORY,
                issues: &mut issues,
            },
        );
    }
    if issues.is_empty() {
        storyboard::validate(
            board,
            story,
            characters,
            &mut Report {
                path: STORYBOARD,
                issues: &mut issues,
            },
        );
    }
    first_issue(issues)
}

pub(crate) fn bound_manifest(data: &Value) -> Result<()> {
    let mut issues = Vec::new();
    planning::project(
        data,
        &mut Report {
            path: PROJECT,
            issues: &mut issues,
        },
    );
    first_issue(issues)
}

fn first_issue(issues: Vec<Issue>) -> Result<()> {
    if let Some(issue) = issues.into_iter().next() {
        let detail = format!("{}: {}: {}", issue.path, issue.field, issue.message);
        return Err(crate::error::Error::invalid(
            if detail.contains(['/', '\\']) {
                "ValueError".to_owned()
            } else {
                detail
            },
        ));
    }
    Ok(())
}

pub(crate) fn has_secret(text: &str) -> bool {
    SECRETS.is_match(text)
}

pub(crate) fn is_decimal(text: &str) -> bool {
    DECIMAL.is_match(text)
}

pub(crate) fn handoff_contract(value: &Value) -> Result<()> {
    let mut issues = Vec::new();
    handoff::check_manifest(
        value,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    if issues.is_empty() {
        return Ok(());
    }
    let mut details: Vec<_> = issues
        .into_iter()
        .map(|issue| {
            if issue.field == issue.message {
                issue.field
            } else {
                format!("{}: {}", issue.field, issue.message)
            }
        })
        .collect();
    details.sort_unstable();
    details.dedup();
    let message = format!("invalid handoff contract: {}", details.join("; "));
    if message.contains(['/', '\\']) {
        return Err(crate::error::Error::handoff("HandoffContractError".into()));
    }
    Err(crate::error::Error::handoff(message))
}

pub fn inspect(path: &Path, stage: Stage) -> Result<Value> {
    let path = if path.as_os_str().is_empty() {
        Path::new(".")
    } else {
        path
    };
    if !path.is_dir() {
        return Err(io::Error::from(io::ErrorKind::NotFound).into());
    }
    let project = LockedProject::open(path, false)?;
    inspect_locked(project.root(), stage)
}

pub(crate) fn inspect_locked(root: &File, stage: Stage) -> Result<Value> {
    let mut issues = Vec::new();
    let project_value = load(root, PROJECT, &mut issues);
    if let Some(value) = &project_value {
        planning::project(
            value,
            &mut Report {
                path: PROJECT,
                issues: &mut issues,
            },
        );
        handoff::inspect(
            root,
            value,
            stage == Stage::Panels || stage.terminal(),
            &mut issues,
        );
    }

    let story = load(root, STORY, &mut issues);
    if let Some(story) = &story {
        planning::story(
            story,
            &mut Report {
                path: STORY,
                issues: &mut issues,
            },
        );
    }
    let characters = load(root, CHARACTERS, &mut issues);
    if let Some(characters) = &characters {
        planning::characters(
            characters,
            &mut Report {
                path: CHARACTERS,
                issues: &mut issues,
            },
        );
    }
    if let (Some(story), Some(characters)) = (&story, &characters) {
        planning::reference_characters(
            story,
            characters,
            &mut Report {
                path: STORY,
                issues: &mut issues,
            },
        );
    }
    if stage != Stage::Plan
        && let Some(board) = load(root, STORYBOARD, &mut issues)
    {
        if let (Some(story), Some(characters)) = (&story, &characters) {
            storyboard::validate(
                &board,
                story,
                characters,
                &mut Report {
                    path: STORYBOARD,
                    issues: &mut issues,
                },
            );
        }
        if let Some(project) = &project_value {
            storyboard::consistency(
                &board,
                project,
                &mut Report {
                    path: PROJECT,
                    issues: &mut issues,
                },
            );
        }
        if stage == Stage::Panels || stage.terminal() {
            panels::inspect(root, &board, project_value.as_ref(), stage, &mut issues)?;
        }
    }
    if stage.terminal()
        && let Some(project) = &project_value
    {
        terminal::inspect(root, project, stage, &mut issues)?;
    }

    issues.sort_unstable();
    Ok(Value::Array(
        issues
            .into_iter()
            .map(
                |issue| json!({"path": issue.path, "field": issue.field, "message": issue.message}),
            )
            .collect(),
    ))
}

pub(crate) fn page_quality_issues(
    root: &File,
    page: usize,
    record: &Value,
    captured: Option<(&str, [u32; 2])>,
) -> Vec<(String, String)> {
    let mut issues = Vec::new();
    page_quality::inspect_with_page(
        root,
        page,
        record,
        captured,
        &mut Report {
            path: "",
            issues: &mut issues,
        },
    );
    issues.sort_unstable();
    issues
        .into_iter()
        .map(|issue| (issue.field, issue.message))
        .collect()
}

pub(crate) fn artifact(root: &File, path: &str) -> Result<Option<(File, PathBuf)>> {
    let relative = project_fs::relative_path(path)?;
    let Some((parent, name)) = target(root, &relative, false)? else {
        return Ok(None);
    };
    let name = Path::new(name);
    if !project_fs::metadata(&parent, name)?.is_some_and(|meta| meta.is_file()) {
        return Ok(None);
    }
    Ok(Some((parent, name.to_owned())))
}

pub(crate) fn read_value(root: &File, path: &str) -> Result<Option<Value>> {
    artifact(root, path)?
        .map(|(parent, name)| manifest::read_document(&parent, &name, path).map(|(value, _)| value))
        .transpose()
}

pub(crate) fn keys(value: &Value, expected: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == expected.len() && expected.iter().all(|key| object.contains_key(*key))
    })
}

pub(crate) fn file_hash(root: &File, path: &str) -> Result<Option<String>> {
    artifact(root, path)?
        .map(|(parent, name)| {
            let file = project_fs::open_file(&parent, &name, false, false)?;
            Ok(terminal::sha256(file)?)
        })
        .transpose()
}

pub(crate) fn bytes_hash(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}

pub(crate) fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

fn text(value: &Value) -> Option<&str> {
    value.as_str().filter(|value| !value.trim().is_empty())
}

fn load(root: &File, path: &str, issues: &mut Vec<Issue>) -> Option<Value> {
    let mut report = Report { path, issues };
    let result = (|| {
        let Some((parent, name)) = artifact(root, path)? else {
            return Ok(None);
        };
        manifest::read_document(&parent, &name, path).map(Some)
    })();
    let (value, raw) = match result {
        Ok(Some(data)) => data,
        Ok(None) => {
            report.add("file", "required file is missing");
            return None;
        }
        Err(error) => {
            let detail = error.detail().unwrap_or("OSError");
            let message = match error.code() {
                "CS-SEC-001" => {
                    if cfg!(windows) {
                        detail.to_owned()
                    } else {
                        "project path must not contain symlinks".into()
                    }
                }
                "CS-SEC-002" => format!("cannot read JSON: InputResourceLimitError: {detail}"),
                _ => format!("cannot read JSON: {detail}"),
            };
            report.add("file", message);
            return None;
        }
    };
    if !value.is_object() {
        report.add(
            "file",
            "cannot read JSON: ValueError: expected a JSON object",
        );
        return None;
    }
    if raw.replace("\r\n", "\n").as_bytes() != manifest::canonical(&value) {
        report.add(
            "file",
            "JSON must use canonical two-space sorted UTF-8 formatting",
        );
    }
    Some(value)
}

impl Report<'_> {
    fn add(&mut self, field: impl Into<String>, message: impl Into<String>) {
        self.issues.push(Issue {
            path: self.path.into(),
            field: field.into(),
            message: message.into(),
        });
    }

    fn ensure(&mut self, condition: bool, field: &str, message: impl Into<String>) {
        if !condition {
            self.add(field, message);
        }
    }

    fn object<'b>(
        &mut self,
        value: &'b Value,
        field: &str,
        required: &[&str],
        optional: &[&str],
    ) -> Option<&'b Object> {
        let Some(map) = value.as_object() else {
            self.add(field, "must be an object");
            return None;
        };
        for key in map.keys() {
            if !required.contains(&key.as_str()) && !optional.contains(&key.as_str()) {
                self.add(qualified(field, key), "unknown field");
            }
        }
        for &key in required {
            if !map.contains_key(key) {
                self.add(qualified(field, key), "required field is missing");
            }
        }
        Some(map)
    }

    fn string<'b>(&mut self, value: &'b Value, field: &str) -> Option<&'b str> {
        let string = value.as_str().filter(|text| !text.trim().is_empty());
        self.ensure(string.is_some(), field, "must be a non-empty string");
        string
    }

    fn narrative(&mut self, value: &Value, field: &str, max: usize) {
        if let Some(text) = self.string(value, field) {
            self.text_bounds(text, field, Some(max));
        }
    }

    fn text_bounds(&mut self, text: &str, field: &str, max: Option<usize>) {
        if let Some(max) = max {
            self.ensure(
                text.chars().count() <= max,
                field,
                format!("must be at most {max} characters"),
            );
        }
        self.ensure(
            !SECRETS.is_match(text),
            field,
            "must not contain secrets or credentials",
        );
    }

    fn array<'b>(&mut self, value: &'b Value, field: &str) -> Option<&'b [Value]> {
        let result = value.as_array().map(Vec::as_slice);
        self.ensure(result.is_some(), field, "must be an array");
        result
    }

    fn strings<'b>(
        &mut self,
        value: &'b Value,
        field: &str,
        min: usize,
        max: Option<usize>,
        chars: Option<usize>,
    ) -> Option<Vec<&'b str>> {
        let list = self.array(value, field)?;
        if list.len() < min || max.is_some_and(|max| list.len() > max) {
            let upper = max.map_or_else(|| "unbounded".into(), |value| value.to_string());
            self.add(field, format!("must contain {min} to {upper} items"));
        }
        for (idx, item) in list.iter().enumerate() {
            let field = format!("{field}[{idx}]");
            if let Some(text) = self.string(item, &field) {
                self.text_bounds(text, &field, chars);
            }
        }
        Some(list.iter().filter_map(Value::as_str).collect())
    }

    fn identifier<'b>(&mut self, value: &'b Value, field: &str) -> Option<&'b str> {
        let valid = value.as_str().filter(|text| is_identifier(text, 48, false));
        self.ensure(valid.is_some(), field, "must match ^[a-z][a-z0-9-]{0,47}$");
        valid
    }

    fn integer(&mut self, value: &Value, field: &str, min: i64, max: i64) {
        self.ensure(
            value.as_i64().is_some_and(|n| (min..=max).contains(&n)),
            field,
            format!("must be an integer from {min} to {max}"),
        );
    }

    fn relative(&mut self, value: &Value, field: &str, nullable: bool) {
        if value.is_null() && nullable {
            return;
        }
        let Some(text) = value.as_str().filter(|text| !text.is_empty()) else {
            self.add(field, "must be a relative project path");
            return;
        };
        if text.contains('\\') {
            self.add(field, "must use POSIX separators");
            return;
        }
        self.ensure(
            !text.split('/').any(|part| matches!(part, "" | "." | ".."))
                && !(text.as_bytes().get(1..3) == Some(b":/")
                    && text.as_bytes()[0].is_ascii_alphabetic()),
            field,
            "must be a normalized relative project path without traversal",
        );
    }

    fn sha(&mut self, value: &Value, field: &str, nullable: bool) {
        self.ensure(
            nullable && value.is_null() || value.as_str().is_some_and(is_sha256),
            field,
            "must be a lowercase 64-character SHA-256",
        );
    }
}

fn qualified(prefix: &str, field: &str) -> String {
    format!("{prefix}.{field}").trim_matches('.').to_owned()
}

pub(crate) fn is_sha256(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

pub(crate) fn is_identifier(text: &str, max: usize, category: bool) -> bool {
    let mut bytes = text.bytes();
    text.len() <= max
        && bytes.next().is_some_and(|first| {
            first.is_ascii_lowercase() || category && first.is_ascii_uppercase()
        })
        && bytes.all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || byte == b'-'
                || category && (byte.is_ascii_uppercase() || byte == b'_' || byte == b'.')
        })
}

pub(crate) fn is_panel(text: &str) -> bool {
    matches!(text.as_bytes(), [b'p', a, b, b'-', c, d] if [a, b, c, d].into_iter().all(u8::is_ascii_digit))
}

fn has_duplicates(values: &[&str]) -> bool {
    values.iter().collect::<HashSet<_>>().len() != values.len()
}

pub(crate) fn number_equal(value: &Value, n: usize) -> bool {
    value.as_f64().is_some_and(|val| val == n as f64)
        || value.as_bool().is_some_and(|val| usize::from(val) == n)
}

pub(crate) fn items(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

fn check_legacy_version(value: &Value, report: &mut Report) {
    report.ensure(
        value["schema_version"] == "1.0",
        "schema_version",
        "must equal 1.0",
    );
}

fn repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(text) => {
            let quote = if text.contains('\'') && !text.contains('"') {
                '"'
            } else {
                '\''
            };
            let mut rendered = String::from(quote);
            for ch in text.chars() {
                match ch {
                    '\n' => rendered.push_str("\\n"),
                    '\t' => rendered.push_str("\\t"),
                    '\r' => rendered.push_str("\\r"),
                    ch if ch == quote || ch == '\\' => {
                        rendered.push('\\');
                        rendered.push(ch);
                    }
                    ch if ch.is_control() => {
                        rendered.push_str(&format!("\\x{:02x}", u32::from(ch)))
                    }
                    ch => rendered.push(ch),
                }
            }
            rendered.push(quote);
            rendered
        }
        _ => value.to_string(),
    }
}
