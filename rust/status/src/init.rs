use std::env;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};
use unicode_normalization::UnicodeNormalization;

use crate::bind;
use crate::cli::Arguments;
use crate::error::{Error, Result, contract};
use crate::manifest;
use crate::project_fs::{self, LockedProject, StagedDirectory};
use crate::recovery::{Change, publish};
use crate::resume::{event, timestamp};
use crate::validation::{self, Stage, bytes_hash, python_whitespace};

const DIRECTORIES: &[&str] = &[
    "source",
    "plan",
    "references/characters",
    "references/scenes",
    "prompts/references",
    "prompts/panels",
    "panels/raw",
    "panels/clean",
    "qa/panels",
    "pages",
    "exports",
    "logs",
];
const MAX_SOURCE: usize = 200 * 1024;
macro_rules! starter {
    ($name:literal) => {{
        macro_rules! file {
            ($path:literal) => {
                include_bytes!(concat!(
                    "../../../templates/starters/v1/",
                    $name,
                    "/",
                    $path
                ))
            };
        }
        (
            $name,
            &[
                file!("source/input.txt"),
                file!("source/request.json"),
                file!("plan/story-plan.json"),
                file!("plan/character-bible.json"),
                file!("plan/storyboard.json"),
            ],
        )
    }};
}

const STARTERS: &[(&str, &[&[u8]; 5])] = &[
    starter!("minimal-one-page"),
    starter!("dialogue-two-page"),
    starter!("action-focused"),
];

pub fn starter_names() -> impl Iterator<Item = &'static str> {
    STARTERS.iter().map(|(name, _)| *name)
}

pub fn default_output() -> PathBuf {
    let home = env::home_dir().unwrap_or_else(|| PathBuf::from("."));
    if cfg!(any(windows, target_os = "macos")) {
        home.join("Documents/Comic Sol")
    } else {
        home.join("Comic Sol")
    }
}

pub(crate) fn expand(path: &Path) -> PathBuf {
    path.strip_prefix("~")
        .ok()
        .and_then(|tail| env::home_dir().map(|home| home.join(tail)))
        .unwrap_or_else(|| path.to_owned())
}

struct Input {
    root: PathBuf,
    title: String,
    source: Vec<u8>,
    request: Value,
    pages: usize,
    plan: Vec<Change>,
}

pub fn run(args: &Arguments) -> Result<Value> {
    let input = if args.has("interactive") {
        guided()?
    } else {
        let root = args
            .path("output_root")
            .map_or_else(default_output, Path::to_owned);
        let title = args
            .text("title")
            .ok_or_else(|| Error::invalid("title must be UTF-8"))?
            .to_owned();
        if let Some(starter) = args.text("starter") {
            from_starter(root, title, starter)?
        } else {
            let source = source_file(args.paths("source"))?;
            let request = match args.path("request_json") {
                Some(path) => read_json(path)?,
                None => json!({"language":"en", "mode":"source_file"}),
            };
            let pages = args.text("page_count").map_or(2, |value| {
                value
                    .replace('_', "")
                    .parse()
                    .expect("validated page count")
            });
            Input {
                root,
                title,
                source,
                request,
                pages,
                plan: Vec::new(),
            }
        }
    };
    validate_input(&input)?;
    fs::create_dir_all(&input.root)?;
    let root = project_fs::project_root(&input.root)?;
    create(&root, input, capability(args))
}

pub fn from_mcp(root: &File, args: &Value) -> Result<Value> {
    let title = args["title"].as_str().ok_or_else(|| {
        Error::mcp_request("title must be a non-empty string of at most 200 characters")
    })?;
    let input = if let Some(starter) = args["starter"].as_str() {
        if !starter_names().any(|name| name == starter) {
            return Err(Error::mcp_request("starter must be one of"));
        }
        if ["source_text", "request_settings", "page_count"]
            .iter()
            .any(|field| !args[field].is_null())
        {
            return Err(Error::mcp_request(
                "starter cannot be combined with explicit source, request, or page count",
            ));
        }
        from_starter(PathBuf::new(), title.to_owned(), starter)?
    } else {
        if args["source_text"].is_null() || args["request_settings"].is_null() {
            return Err(Error::mcp_request(
                "blank init requires source_text and request_settings",
            ));
        }
        let source = args["source_text"]
            .as_str()
            .ok_or_else(|| Error::mcp_request("source must be at most 200 KiB as UTF-8 bytes"))?;
        let page_count = &args["page_count"];
        let pages = if page_count.is_null() {
            2
        } else {
            page_count
                .as_u64()
                .filter(|value| (1..=4).contains(value))
                .ok_or_else(|| Error::mcp_request("page count must be an integer from 1 to 4"))?
                as usize
        };
        Input {
            root: PathBuf::new(),
            title: title.into(),
            source: source.as_bytes().to_vec(),
            request: args["request_settings"].clone(),
            pages,
            plan: Vec::new(),
        }
    };
    validate_input(&input).map_err(Error::as_mcp_request)?;
    create(root, input, None)
}

fn validate_input(input: &Input) -> Result<()> {
    title(&input.title, false)?;
    validate_source(&input.source)?;
    validate_request(&input.request)
}

fn create(root: &File, input: Input, capability: Option<Value>) -> Result<Value> {
    let base = slugify(&input.title);
    let mut counter = 1u64;
    loop {
        let id = candidate(&base, counter);
        if !project_fs::entry_exists(root, Path::new(&id))? {
            match create_one(root, &input, capability.clone(), &id) {
                Err(error)
                    if error.is_exists() && project_fs::entry_exists(root, Path::new(&id))? => {}
                result => return result,
            }
        }
        counter += 1;
    }
}

fn create_one(root: &File, input: &Input, capability: Option<Value>, id: &str) -> Result<Value> {
    let starter = !input.plan.is_empty();
    let mut data = manifest::parse_document(
        include_str!("../../../templates/manifest.json"),
        "manifest template",
    )?;
    data["title"] = input.title.trim_matches(python_whitespace).into();
    data["input"]["mode"] = input
        .request
        .get("mode")
        .cloned()
        .unwrap_or_else(|| "short_prompt".into());
    data["input"]["language"] = input
        .request
        .get("language")
        .cloned()
        .unwrap_or_else(|| "en".into());
    let hash = bytes_hash(&input.source);
    data["input"]["source_sha256"] = hash.clone().into();
    data["settings"]["page_count"] = input.pages.into();
    let stage = StagedDirectory::create(root, "init")?;
    for path in DIRECTORIES {
        let relative = Path::new(path);
        let (parent, name) =
            project_fs::target(stage.file(), relative, true)?.expect("created parent");
        cap_primitives::fs::create_dir(
            &parent,
            Path::new(name),
            &cap_primitives::fs::DirOptions::new(),
        )?;
        project_fs::sync_directory(&parent)?;
    }
    let locked = LockedProject::from_root(
        stage.file().try_clone()?,
        false,
        std::time::Duration::from_secs(10),
    )?;
    let mut source = vec![
        Change::Replace("source/input.txt".into(), input.source.clone()),
        Change::Replace(
            "source/request.json".into(),
            manifest::canonical(&input.request),
        ),
    ];
    source.extend(input.plan.iter().cloned());
    publish(&locked, "init-inputs", source)?;
    let time = timestamp();
    data["created_at"] = time.clone().into();
    data["updated_at"] = time.clone().into();
    if let Some(mut capability) = capability {
        capability["detected_at"] = time.into();
        data["capability"] = capability;
    }
    data["project_id"] = id.into();
    if starter {
        bind::apply(locked.root(), &mut data)?;
    }
    publish(
        &locked,
        "init-manifest",
        vec![
            Change::Replace("project.json".into(), manifest::canonical(&data)),
            Change::Append(
                "logs/events.jsonl",
                event(
                    "project.created",
                    json!({
                        "project_id":id,"source_path":"source/input.txt","source_sha256":hash
                    }),
                ),
            ),
        ],
    )?;
    if starter {
        crate::finalize::require_valid(locked.root(), Stage::Storyboard)?;
        crate::finalize::record_stage(&locked, &data, "planning")?;
        crate::finalize::advance(&locked, &mut data, "INIT", "PLANNED")?;
        crate::finalize::advance(&locked, &mut data, "PLANNED", "SCRIPTED")?;
        crate::finalize::record_stage(&locked, &data, "storyboard")?;
        crate::finalize::advance(&locked, &mut data, "SCRIPTED", "STORYBOARDED")?;
    }
    drop(locked);
    stage.publish(Path::new(id))?;
    Ok(json!({"project_id":id,"project_dir":id}))
}

fn from_starter(root: PathBuf, title: String, starter: &str) -> Result<Input> {
    let files = STARTERS
        .iter()
        .find(|(name, _)| *name == starter)
        .map(|(_, files)| *files)
        .ok_or_else(|| Error::invalid("unknown starter"))?;
    let request = document(files[1], "starter request")?;
    let board = document(files[4], "starter storyboard")?;
    let pages = validation::items(&board["pages"]).len();
    let mut plan = Vec::new();
    for ((_, path), bytes) in bind::PLAN.into_iter().zip(&files[2..]) {
        plan.push(Change::Replace(
            path.into(),
            manifest::canonical(&document(bytes, path)?),
        ));
    }
    Ok(Input {
        root,
        title,
        source: files[0].to_vec(),
        request,
        pages,
        plan,
    })
}

fn candidate(base: &str, number: u64) -> String {
    if number == 1 {
        return base.to_owned();
    }
    let suffix = format!("-{number}");
    format!(
        "{}{suffix}",
        base[..base.len().min(48 - suffix.len())].trim_end_matches('-')
    )
}

fn slugify(title: &str) -> String {
    let plain: String = title
        .nfkd()
        .filter(char::is_ascii)
        .collect::<String>()
        .to_ascii_lowercase();
    let body = plain
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|piece| !piece.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    let prefixed = match body.as_bytes().first() {
        None => "comic-sol-project".to_owned(),
        Some(c) if !c.is_ascii_alphabetic() => format!("comic-sol-{body}"),
        _ => body,
    };
    prefixed[..prefixed.len().min(48)]
        .trim_end_matches('-')
        .to_owned()
}

fn title(value: &str, request: bool) -> Result<()> {
    if value.trim_matches(python_whitespace).is_empty() || value.chars().count() > 200 {
        return Err(Error::invalid(format!(
            "{}title must be a non-empty string of at most 200 characters",
            if request { "request " } else { "" }
        )));
    }
    if validation::has_secret(value) {
        return Err(Error::invalid(
            "narrative field must not contain secrets or credentials",
        ));
    }
    Ok(())
}

fn validate_request(value: &Value) -> Result<()> {
    static SENSITIVE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new("(?i)api[_-]?key|authorization|credential|password|private[_-]?key|secret|token")
            .expect("canonical request constraints")
    });
    fn check(value: &Value) -> Result<()> {
        if let Some(object) = value.as_object() {
            for (key, nested) in object {
                if SENSITIVE.is_match(key) {
                    return Err(Error::invalid(format!(
                        "sensitive request setting is not allowed: {key}"
                    )));
                }
                check(nested)?;
            }
        } else if let Some(items) = value.as_array() {
            for item in items {
                check(item)?;
            }
        }
        Ok(())
    }
    check(value)?;
    let data = value
        .as_object()
        .ok_or_else(|| Error::invalid("request must be a JSON object"))?;
    if let Some(key) = data
        .keys()
        .find(|k| !matches!(k.as_str(), "mode" | "language" | "title"))
    {
        return Err(Error::invalid(format!(
            "unsupported request setting: {key}"
        )));
    }
    if data.get("mode").is_some_and(|value| {
        !matches!(
            value.as_str(),
            Some("short_prompt" | "pasted_story" | "source_file" | "resume")
        )
    }) {
        return Err(Error::invalid(
            "request mode must be one of short_prompt, pasted_story, source_file, or resume",
        ));
    }
    if data.get("language").is_some_and(|value| {
        value.as_str().is_none_or(|text| {
            text.trim_matches(python_whitespace).is_empty() || text.chars().count() > 35
        })
    }) {
        return Err(Error::invalid(
            "request language must be a non-empty language tag",
        ));
    }
    if let Some(value) = data.get("title").filter(|value| !value.is_null()) {
        title(value.as_str().unwrap_or_default(), true)?;
    }
    Ok(())
}

pub fn capability(args: &Arguments) -> Option<Value> {
    let status = args.text("image_capability_status");
    let name = args.text("image_capability_name");
    let references = args.has("supports_reference_images");
    let dimensions = args.has("supports_dimensions");
    if status.is_none() && name.is_none() && !references && !dimensions {
        return None;
    }
    let valid = matches!(
        (status, name),
        (Some("available"), Some("agent-image-generation")) | (Some("unavailable"), None)
    ) && (status != Some("unavailable") || !(references || dimensions));
    Some(if valid {
        json!({"status":status,"name":name,"supports_reference_images":references,"supports_dimensions":dimensions})
    } else {
        json!({"status":"not_checked","name":null,"supports_reference_images":false,"supports_dimensions":false})
    })
}

pub fn read_file(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    let (parent, name) = project_fs::external_target(path)?;
    project_fs::metadata(&parent, Path::new(&name))?;
    let file = project_fs::open_file(&parent, Path::new(&name), false, false)?;
    let mut bytes = Vec::new();
    file.take((maximum + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > maximum {
        return Err(Error::limit(format!(
            "the file size limit of {maximum} bytes"
        )));
    }
    Ok(bytes)
}

pub fn read_json(path: &Path) -> Result<Value> {
    if !path.is_absolute() {
        return Err(Error::invalid("path must be absolute"));
    }
    let value = document(&read_file(path, contract::MAX_JSON_BYTES)?, "<path>")?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(Error::invalid("expected a JSON object: <path>"))
    }
}

fn document(bytes: &[u8], label: &str) -> Result<Value> {
    let text = std::str::from_utf8(bytes).map_err(|_| Error::invalid("ValueError"))?;
    manifest::parse_document(text, label)
}

fn validate_source(bytes: &[u8]) -> Result<()> {
    if bytes.len() > MAX_SOURCE {
        return Err(Error::invalid(
            "source must be at most 200 KiB as UTF-8 bytes",
        ));
    }
    std::str::from_utf8(bytes).map_err(|_| Error::invalid("source must be valid UTF-8"))?;
    Ok(())
}

fn source_file(path: &Path) -> Result<Vec<u8>> {
    let path = expand(path);
    let bytes = read_file(&path, MAX_SOURCE)?;
    if !path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("txt") || value.eq_ignore_ascii_case("md"))
    {
        return Err(Error::invalid("source file must use .txt or .md"));
    }
    validate_source(&bytes)?;
    Ok(bytes)
}

fn prompt(label: &str, default: Option<&str>) -> Result<String> {
    let suffix = default.map_or_else(String::new, |value| format!(" [{value}]"));
    loop {
        let mut err = io::stderr().lock();
        write!(err, "{label}{suffix}: ")?;
        err.flush()?;
        let mut answer = String::new();
        if io::stdin().read_line(&mut answer)? == 0 {
            return Err(Error::usage(
                "interactive initialization ended before completion".into(),
            ));
        }
        let answer = answer.trim_matches(python_whitespace);
        if !answer.is_empty() {
            return Ok(answer.to_owned());
        }
        if let Some(default) = default {
            return Ok(default.to_owned());
        }
        writeln!(err, "A value is required.")?;
    }
}

fn guided() -> Result<Input> {
    eprintln!("Comic Sol guided project initializer");
    let project_title = loop {
        let value = prompt("Project name", Some("Comic Sol Project"))?;
        if let Err(error) = title(&value, false) {
            eprintln!(
                "Invalid project name: {}",
                error.detail().unwrap_or("ValueError")
            );
        } else {
            break value;
        }
    };
    let choices: Vec<_> = starter_names().collect();
    let starter = loop {
        let value = prompt(
            &format!("Starter (blank/{})", choices.join("/")),
            Some("blank"),
        )?
        .to_lowercase();
        if matches!(value.as_str(), "blank" | "none") {
            break None;
        }
        if choices.contains(&value.as_str()) {
            break Some(value);
        }
        eprintln!("Starter must be blank or one of: {}.", choices.join(", "));
    };
    let mut input = if let Some(starter) = starter {
        from_starter(PathBuf::new(), project_title, &starter)?
    } else {
        let pages = loop {
            if let Ok(n @ 1..=4) = prompt("Page count (1-4)", Some("2"))?.parse::<usize>() {
                break n;
            }
            eprintln!("Page count must be an integer from 1 to 4.");
        };
        let mode = loop {
            match prompt("Story source (prompt/file)", Some("prompt"))?
                .to_lowercase()
                .as_str()
            {
                "prompt" | "p" => break "short_prompt",
                "file" | "f" => break "source_file",
                _ => eprintln!("Story source must be prompt or file."),
            }
        };
        let source = loop {
            let parsed = if mode == "source_file" {
                source_file(Path::new(&prompt("Story file (.txt or .md)", None)?))
            } else {
                let bytes = prompt("Story prompt", None)?.into_bytes();
                validate_source(&bytes).map(|()| bytes)
            };
            match parsed {
                Ok(bytes) => break bytes,
                Err(error) => eprintln!(
                    "Invalid story {}: {}",
                    if mode == "source_file" {
                        "file"
                    } else {
                        "prompt"
                    },
                    error.detail().unwrap_or("FileNotFoundError")
                ),
            }
        };
        Input {
            root: PathBuf::new(),
            title: project_title,
            source,
            pages,
            plan: vec![],
            request: json!({"language":"en","mode":mode}),
        }
    };
    input.root = loop {
        let root = expand(Path::new(&prompt(
            "Output location",
            Some(&default_output().to_string_lossy()),
        )?));
        if root.exists() && !root.is_dir() {
            eprintln!("Invalid output location: output location must be a directory or a new path");
        } else {
            break root;
        }
    };
    Ok(input)
}
