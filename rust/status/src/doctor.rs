use std::fs;
use std::io::Write;
use std::path::Path;

use serde_json::{Value, json};

use crate::cli::Arguments;
use crate::init;
use crate::manifest;
use crate::project_fs::{self, StagedDirectory};

const NO_ACTION: &str = "No action required.";

pub fn run(args: &Arguments) -> Value {
    let root = args
        .path("output_root")
        .map_or_else(init::default_output, Path::to_owned);
    let writable = (|| {
        fs::create_dir_all(&root)?;
        let directory = project_fs::project_root(&root)?;
        probe(&directory)
    })();
    report(&root, writable, init::capability(args))
}

pub fn for_mcp(root: &Path, directory: &std::fs::File, observation: &Value) -> Value {
    let capability = (!observation.is_null()).then(|| {
        if crate::validation::keys(observation, &["status", "name", "supports_dimensions", "supports_reference_images"])
            && observation["supports_dimensions"].is_boolean() && observation["supports_reference_images"].is_boolean()
            && (observation["status"] == "available" && observation["name"] == "agent-image-generation"
                || observation["status"] == "unavailable" && observation["name"].is_null()
                    && observation["supports_dimensions"] == false && observation["supports_reference_images"] == false)
        {
            observation.clone()
        } else {
            json!({"status":"detection_failed","name":null,"supports_reference_images":false,"supports_dimensions":false})
        }
    });
    report(root, probe(directory), capability)
}

fn probe(directory: &std::fs::File) -> crate::error::Result<()> {
    let staging = StagedDirectory::create(directory, "doctor")?;
    let mut probe = project_fs::open_file(staging.file(), Path::new("probe"), true, true)?;
    probe.write_all(b"ok")?;
    probe.sync_all()?;
    Ok(())
}

fn report(root: &Path, writable: crate::error::Result<()>, observation: Option<Value>) -> Value {
    let (font_checks, missing) = crate::finalize::font_diagnostics();
    let font_ok = font_checks.iter().all(|(_, passed)| *passed);
    let templates = [
        include_str!("../../../templates/manifest.json"),
        include_str!("../../../templates/character-bible.json"),
        include_str!("../../../templates/story-plan.json"),
        include_str!("../../../templates/storyboard.json"),
        include_str!("../../../templates/panel-record.json"),
    ]
    .iter()
    .all(|template| {
        manifest::parse_document(template, "template")
            .is_ok_and(|value| value.as_object().is_some_and(|data| !data.is_empty()))
    }) && !include_str!("../../../templates/qa-report.md.tmpl").is_empty();
    let references = [
        include_str!("../../../references/workflow.md"),
        include_str!("../../../references/schemas.md"),
        include_str!("../../../references/starter-templates.md"),
        include_str!("../../../references/visual-qa.md"),
    ]
    .iter()
    .all(|text| !text.is_empty());
    let starters: Vec<_> = init::starter_names().collect();
    let output_message = if writable.is_ok() {
        "output root writable".to_owned()
    } else {
        let error = writable.as_ref().err().expect("failed output probe");
        let error = if root.is_file() {
            "FileExistsError"
        } else {
            error.exception()
        };
        format!("Output root is not writable: {error}")
    };
    let fonts = if font_ok {
        "Bundled fonts load at 42px."
    } else {
        "Required bundled fonts could not be loaded."
    };
    let typography = if missing.is_empty() {
        check(
            "typography",
            "pass",
            "Bundled fonts map all 179 required latin/greek/cyrillic codepoints.",
            NO_ACTION,
        )
    } else {
        let mut result = check(
            "typography",
            "fail",
            "Bundled fonts no longer letter required scripts.",
            "Restore the bundled assets/fonts directory so the documented scripts letter again.",
        );
        result["details"] = json!({"failures":missing});
        result
    };
    let mut starter_check = check(
        "starter-templates",
        "pass",
        &format!("{} versioned starter templates available.", starters.len()),
        NO_ACTION,
    );
    starter_check["details"] = json!({"available":starters,"version":"v1"});
    let (capability, human_capability) = image_diagnostic(observation);
    let checks = [
        check(
            "runtime",
            "pass",
            "Native Rust Comic Sol runtime",
            NO_ACTION,
        ),
        check(
            "raster",
            "pass",
            "Native raster codecs are linked.",
            NO_ACTION,
        ),
        checked(
            "fonts",
            font_ok,
            fonts,
            "Reinstall Comic Sol or restore the bundled assets/fonts directory.",
        ),
        typography,
        checked(
            "templates",
            templates,
            if templates {
                "Bundled templates available."
            } else {
                "Bundled templates invalid."
            },
            "Reinstall Comic Sol so its bundled templates are restored.",
        ),
        starter_check,
        checked(
            "references",
            references,
            if references {
                "Bundled workflow references available."
            } else {
                "Bundled workflow references invalid."
            },
            "Reinstall Comic Sol or restore its bundled references.",
        ),
        checked(
            "output-root",
            writable.is_ok(),
            &output_message,
            "Choose a writable project directory with --output-root or fix its permissions.",
        ),
        check(
            "mcp",
            "pass",
            "Native MCP stdio server is available.",
            NO_ACTION,
        ),
        capability,
    ];
    let healthy = checks.iter().all(|check| check["status"] != "fail");
    let mut messages = vec![
        format!(
            "{} Comic Sol diagnostics",
            if healthy { "READY" } else { "NOT READY" }
        ),
        "PASS native Rust Comic Sol runtime".into(),
        "PASS native raster codecs".into(),
    ];
    messages.extend(
        font_checks
            .into_iter()
            .map(|(font, passed)| format!("{} font {font} loads at 42px", status(passed))),
    );
    messages.extend([
        format!(
            "{} templates {}",
            status(templates),
            if templates { "available" } else { "invalid" }
        ),
        "PASS starter templates available".into(),
        format!(
            "{} references {}",
            status(references),
            if references { "available" } else { "invalid" }
        ),
        format!("{} {output_message}", status(writable.is_ok())),
        human_capability,
    ]);
    json!({"ready":healthy,"healthy":healthy,"checks":checks,"messages":messages})
}

fn status(ok: bool) -> &'static str {
    if ok { "PASS" } else { "FAIL" }
}

fn check(id: &str, status: &str, message: &str, remediation: &str) -> Value {
    json!({"id":id,"status":status,"message":message,"remediation":remediation})
}

fn checked(id: &str, ok: bool, message: &str, remediation: &str) -> Value {
    if ok {
        check(id, "pass", message, NO_ACTION)
    } else {
        check(id, "fail", message, remediation)
    }
}

fn image_diagnostic(observed: Option<Value>) -> (Value, String) {
    let capability = observed.clone().unwrap_or_else(|| json!({
        "status":"not_checked","name":null,"supports_reference_images":false,"supports_dimensions":false
    }));
    let (state, message, remedy, readiness, human) = match capability["status"].as_str() {
        Some("unavailable") => (
            "warn",
            "No usable image-generation capability is exposed in this agent session.".into(),
            "Enable an image-generation skill/tool that creates a local raster, then rerun doctor.",
            "missing",
            "WARN image capability: no usable text-to-image tool returning a local raster".into(),
        ),
        Some("available") => {
            let name = "agent-image-generation";
            let missing: Vec<_> = [
                ("supports_reference_images", "reference images"),
                ("supports_dimensions", "explicit dimensions"),
            ]
            .into_iter()
            .filter_map(|(key, name)| (capability[key] != true).then_some(name))
            .collect();
            if missing.is_empty() {
                (
                    "pass",
                    format!("Image-generation capability {name} is ready for Comic Sol."),
                    NO_ACTION,
                    "healthy",
                    format!(
                        "PASS image capability: {name}; reference images and explicit dimensions supported"
                    ),
                )
            } else {
                let limitations = missing.join(" and ");
                (
                    "warn",
                    format!(
                        "Image-generation capability {name} is usable but lacks {limitations} support."
                    ),
                    "Continue in degraded mode or enable a capability with the missing feature support.",
                    "partial",
                    format!(
                        "WARN image capability: {name} is usable but lacks {limitations} support"
                    ),
                )
            }
        }
        _ if observed.is_some() => (
            "warn",
            "Image-capability detection could not interpret the agent observation.".into(),
            "Inspect the exposed tools again and rerun doctor without credentials or provider payloads.",
            "unknown",
            "WARN image capability: detection failed; inspect exposed tools again".into(),
        ),
        _ => (
            "warn",
            "Image-generation capability must be inspected in the agent session.".into(),
            "Enable an image-generation skill/tool, then resume the project when panels are needed.",
            "unknown",
            "INFO image capability: inspect in agent session".into(),
        ),
    };
    let mut check = check("image-capability", state, &message, remedy);
    check["details"] = json!({"readiness":readiness,"capability":capability});
    (check, human)
}
