mod lifecycle;

use std::fs::{self, File};
use std::io::{self, BufRead, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::Duration;

use serde_json::{Value, json};

use crate::error::{Error, Result, contract};
use crate::project_fs::{self, LockedProject};
use crate::validation::{self, Stage, is_identifier, is_panel, items};
use crate::{bind, doctor, finalize, init, manifest, resume};

static SURFACE: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("mcp-contract.json")).expect("MCP tool contract")
});
const MAX_FILES: usize = 100_000;
const MAX_DEPTH: usize = 128;

pub fn serve(path: &Path) -> Result<()> {
    if !path.is_absolute() {
        return Err(Error::invalid("output root must be an absolute path"));
    }
    fs::create_dir_all(path)?;
    let path = fs::canonicalize(path)?;
    let root = project_fs::project_root(&path)?;
    let mut session = Session {
        root,
        path,
        initialized: false,
    };
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let max = contract::MAX_JSON_BYTES;
    let mut frame = Vec::new();
    loop {
        frame.clear();
        if (&mut input)
            .take(max as u64 + 1)
            .read_until(b'\n', &mut frame)?
            == 0
        {
            return Ok(());
        }
        let oversized = frame.len() > max;
        let message = (!oversized)
            .then(|| std::str::from_utf8(&frame).ok())
            .flatten()
            .and_then(|text| manifest::parse_document(text, "MCP frame").ok());
        let response = message.map_or_else(
            || Some(protocol_error(&Value::Null, -32700, "Parse error")),
            |request| session.request(&request),
        );
        if let Some(response) = response {
            output.write_all(&manifest::compact(&response))?;
            output.write_all(b"\n")?;
            output.flush()?;
        }
        if oversized {
            return Ok(());
        }
    }
}

struct Session {
    root: File,
    path: PathBuf,
    initialized: bool,
}

impl Session {
    fn request(&mut self, request: &Value) -> Option<Value> {
        let id = &request["id"];
        let method = request["method"].as_str();
        if request["jsonrpc"] != "2.0"
            || method.is_none()
            || id.is_number()
                && !id
                    .as_number()
                    .is_some_and(|number| number.is_i64() || number.is_u64())
            || !id.is_number() && !id.is_string() && !id.is_null()
        {
            return Some(protocol_error(&Value::Null, -32600, "Invalid Request"));
        }
        let method = method.expect("valid method");
        if request.get("id").is_none() {
            return None;
        }
        let parameters = &request["params"];
        if !parameters.is_null() && !parameters.is_object() {
            return Some(protocol_error(id, -32602, "Invalid params"));
        }
        let result = match method {
            "initialize" => {
                let Some(requested) = parameters["protocolVersion"].as_str() else {
                    return Some(protocol_error(id, -32602, "Invalid params"));
                };
                let mut result = SURFACE["initialize"].clone();
                if matches!(
                    requested,
                    "2024-11-05" | "2025-03-26" | "2025-06-18" | "2025-11-25"
                ) {
                    result["protocolVersion"] = requested.into();
                }
                self.initialized = true;
                result
            }
            "ping" => json!({}),
            _ if !self.initialized => {
                return Some(protocol_error(
                    id,
                    -32602,
                    "Request received before initialization was complete",
                ));
            }
            "tools/list" => json!({"tools": SURFACE["tools"]}),
            "tools/call" => self.call(parameters),
            "resources/list" => json!({"resources": []}),
            "resources/templates/list" => json!({"resourceTemplates": []}),
            "prompts/list" => json!({"prompts": []}),
            _ => return Some(protocol_error(id, -32601, "Method not found")),
        };
        Some(json!({"jsonrpc": "2.0", "id": id, "result": result}))
    }

    fn call(&self, parameters: &Value) -> Value {
        let name = parameters["name"].as_str().unwrap_or_default();
        let Some(tool) = items(&SURFACE["tools"])
            .iter()
            .find(|tool| tool["name"] == name)
        else {
            return tool_error(&format!("Unknown tool: {}", crate::human::escape(name)));
        };
        let mut arguments = parameters["arguments"].clone();
        if arguments.is_null() {
            arguments = json!({});
        }
        let input = &tool["inputSchema"];
        if !arguments.is_object() {
            return tool_error("Invalid tool arguments");
        }
        for required in items(&input["required"]).iter().filter_map(Value::as_str) {
            if arguments.get(required).is_none() {
                return tool_error(&format!(
                    "Error executing tool {name}: required argument is missing: {required}"
                ));
            }
        }
        if let Some(properties) = input["properties"].as_object() {
            for (field, value) in properties {
                if arguments.get(field).is_none()
                    && let Some(default) = value.get("default")
                {
                    arguments[field] = default.clone();
                }
            }
        }
        match self.execute(name, &arguments) {
            Ok(result) => tool_success(result),
            Err(error) => {
                let mut payload = Vec::new();
                manifest::write(&error.mcp_payload(), &mut payload).expect("MCP error JSON");
                let payload = std::str::from_utf8(&payload).expect("UTF-8 MCP error");
                tool_error(&format!(
                    "Error executing tool {name}: {}",
                    payload.trim_end()
                ))
            }
        }
    }

    fn execute(&self, name: &str, args: &Value) -> Result<Value> {
        if name == "comic_doctor" {
            return Ok(doctor::for_mcp(
                &self.path,
                &self.root,
                &args["image_capability"],
            ));
        }
        if name == "comic_init" {
            return Ok(init::from_mcp(&self.root, args)?["project_id"].clone());
        }
        let id = args["project_id"]
            .as_str()
            .filter(|value| is_identifier(value, 48, false))
            .ok_or_else(|| Error::mcp_request("invalid project ID"))?;
        let resume_stage = || {
            args["stage"]
                .as_str()
                .filter(|name| {
                    resume::definitions()
                        .iter()
                        .any(|stage| stage.name == *name)
                })
                .ok_or_else(|| Error::mcp_request("unknown resume stage"))
        };
        let validation_stage = || {
            args["stage"]
                .as_str()
                .and_then(Stage::parse)
                .ok_or_else(|| Error::mcp_request("unknown validation stage"))
        };
        let target = || {
            args["target"]
                .as_str()
                .filter(|name| {
                    matches!(*name, "BLOCKED" | "COMPLETE_WITH_WARNINGS")
                        || validation::linear_statuses().contains(name)
                })
                .ok_or_else(|| Error::mcp_request("invalid target status"))
        };
        let panel_id = || {
            args["panel_id"]
                .as_str()
                .filter(|id| is_panel(id))
                .ok_or_else(|| Error::mcp_request("invalid panel ID"))
        };
        let kind = || {
            args["kind"]
                .as_str()
                .filter(|value| matches!(*value, "initial" | "visual_retry" | "transient_repeat"))
                .ok_or_else(|| Error::mcp_request("unknown generation attempt kind"))
        };
        let relative = || {
            let path = args["relative_path"]
                .as_str()
                .unwrap_or_default()
                .replace('\\', "/");
            if project_fs::relative_path(&path).is_err()
                || !path.split('/').all(|segment| {
                    !segment.is_empty()
                        && segment
                            .bytes()
                            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
                })
            {
                return Err(Error::mcp_request(
                    "attempt path must be a relative project path",
                ));
            }
            Ok(path)
        };
        let mut operand = None;
        let mut warning = None;
        let mut panel = None;
        let mut path = None;
        let mut issue_stage = None;
        match name {
            "comic_transition" => {
                operand = Some(target()?);
                if !args["warning"].is_null() {
                    warning = Some(narrative(&args["warning"], "transition warning", 500)?);
                }
            }
            "comic_validate" => issue_stage = Some(validation_stage()?),
            "comic_invalidate" | "comic_record_stage" => operand = Some(resume_stage()?),
            "comic_record_attempt" => {
                panel = Some(panel_id()?);
                operand = Some(kind()?);
                path = Some(relative()?);
            }
            "comic_promote_attempt" => {
                panel = Some(panel_id()?);
                path = Some(relative()?);
            }
            "comic_override_panel" => {
                panel = Some(panel_id()?);
                warning = Some(
                    narrative(&args["reason"], "override reason", 1000)?
                        .trim_matches(validation::python_whitespace),
                );
            }
            _ => {}
        }
        let root = self.project(id)?;
        if name == "comic_resume_plan" {
            let project = manifest::read_project(&root, true)?;
            return lifecycle::plan(&root, &project);
        }
        let timeout = if matches!(
            name,
            "comic_finalize" | "comic_letter" | "comic_compose" | "comic_export" | "comic_resume"
        ) {
            300
        } else {
            10
        };
        let project = LockedProject::from_root(root, true, Duration::from_secs(timeout))?;
        let progress = &mut |_: &str, _: &'static str, _: usize, _: usize| {};
        match name {
            "comic_status" => manifest::read_project(project.root(), true),
            "comic_bind_plan" => bind::bind_locked(&project),
            "comic_validate" => {
                validation::inspect_locked(project.root(), issue_stage.expect("validation stage"))
            }
            "comic_finalize" => finalize::run_locked(&project, progress),
            "comic_letter" => finalize::letter_only(&project),
            "comic_resume" => resume::resume_locked(&project, &self.path.join(id)),
            _ => {
                let state = manifest::read_project(project.root(), false)?;
                match name {
                    "comic_transition" => lifecycle::transition(
                        &project,
                        state,
                        operand.expect("transition target"),
                        warning,
                    ),
                    "comic_invalidate" => {
                        lifecycle::invalidate(&project, state, operand.expect("invalidation stage"))
                    }
                    "comic_record_stage" => {
                        finalize::record_stage(&project, &state, operand.expect("recorded stage"))
                    }
                    "comic_record_attempt" => lifecycle::record_attempt(
                        &project,
                        panel.unwrap(),
                        operand.unwrap(),
                        path.as_deref().unwrap(),
                    ),
                    "comic_promote_attempt" => {
                        lifecycle::promote(&project, panel.unwrap(), path.as_deref().unwrap())
                    }
                    "comic_override_panel" => {
                        lifecycle::override_panel(&project, state, panel.unwrap(), warning.unwrap())
                    }
                    "comic_compose" => finalize::compose_only(&project, state),
                    "comic_export" => finalize::export_only(&project, state),
                    "comic_render_report" => finalize::report_only(&project, state),
                    _ => unreachable!("registered MCP tool"),
                }
            }
        }
    }

    fn project(&self, id: &str) -> Result<File> {
        let leaf = Path::new(id);
        let uninitialized = || {
            Error::boundary(
                "security-error: project directory is not an initialized Comic Sol project",
            )
        };
        let outside =
            || Error::boundary("security-error: project directory resolves outside output root");
        project_fs::metadata(&self.root, leaf)
            .map_err(|error| {
                if error.code() == "CS-SEC-001" {
                    outside()
                } else {
                    error
                }
            })?
            .filter(|meta| meta.is_dir())
            .ok_or_else(uninitialized)?;
        let project = project_fs::open_dir(&self.root, leaf)?;
        let mut budget = MAX_FILES;
        scan_links(&project, &mut budget, 0)?;
        if !project_fs::metadata(&project, Path::new("project.json"))?
            .is_some_and(|meta| meta.is_file())
        {
            return Err(uninitialized());
        }
        Ok(project)
    }
}

fn scan_links(root: &File, budget: &mut usize, depth: usize) -> Result<()> {
    if depth > MAX_DEPTH {
        return Err(Error::limit("the maximum project path depth"));
    }
    let names = project_fs::names_bounded(root, *budget).map_err(|error| {
        if error.kind() == io::ErrorKind::InvalidData {
            Error::limit("the project entry count limit")
        } else {
            error.into()
        }
    })?;
    *budget -= names.len();
    for name in names {
        let leaf = Path::new(&name);
        let metadata = project_fs::metadata(root, leaf).map_err(|error| {
            if error.code() == "CS-SEC-001" {
                Error::boundary("security-error: project contains a symlink")
            } else {
                error
            }
        })?;
        if metadata.is_some_and(|meta| meta.is_dir()) {
            scan_links(&project_fs::open_dir(root, leaf)?, budget, depth + 1)?;
        }
    }
    Ok(())
}

fn narrative<'a>(value: &'a Value, field: &str, limit: usize) -> Result<&'a str> {
    let value = value
        .as_str()
        .filter(|value| {
            !value.trim_matches(validation::python_whitespace).is_empty()
                && value.chars().count() <= limit
        })
        .ok_or_else(|| {
            Error::mcp_request(&format!(
                "{field} must be a non-empty string of at most {limit} characters"
            ))
        })?;
    if validation::has_secret(value) {
        return Err(Error::mcp_request(
            "narrative field must not contain secrets or credentials",
        ));
    }
    Ok(value)
}

fn protocol_error(id: &Value, code: i32, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

fn tool_success(value: Value) -> Value {
    let content = |value: &Value| {
        let text = value.as_str().map(str::to_owned).unwrap_or_else(|| {
            String::from_utf8(manifest::canonical(value))
                .expect("UTF-8")
                .trim_end()
                .to_owned()
        });
        json!({"type": "text", "text": text})
    };
    let blocks = match &value {
        Value::Array(values) => values.iter().map(content).collect(),
        _ => vec![content(&value)],
    };
    let structured = if value.is_object() {
        value
    } else {
        json!({"result": value})
    };
    json!({"content": blocks, "isError": false, "structuredContent": structured})
}

fn tool_error(message: &str) -> Value {
    json!({"content": [{"type": "text", "text": message}], "isError": true})
}
