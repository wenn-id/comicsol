mod archive;
mod bind;
mod cli;
mod clients;
mod doctor;
mod dogfood;
mod error;
mod finalize;
mod human;
mod init;
mod manifest;
mod mcp;
mod project_fs;
mod recovery;
mod resume;
mod skill;
mod validation;

use std::env;
use std::io::{self, Write};
use std::path::Path;
use std::process::ExitCode;

use serde_json::{Value, json};

use crate::cli::Invocation;
use crate::error::{Error, Result};

fn main() -> ExitCode {
    let raw: Vec<_> = env::args_os().skip(1).collect();
    let args = match cli::parse(&raw) {
        Ok(Invocation::Help(help)) => return stdout(help.as_bytes(), 0),
        Ok(Invocation::Version) => {
            return stdout(
                concat!("comic-sol ", env!("COMIC_SOL_VERSION"), "\n").as_bytes(),
                0,
            );
        }
        Ok(Invocation::Run(args)) => args,
        Err(error) => return failure(error, "", raw.iter().any(|arg| arg == "--json"), None),
    };
    let command = args.command.as_str();
    let as_json = args.has("as_json");
    if command == "mcp" {
        return match mcp::serve(args.paths("root")) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => failure(error, command, as_json, None),
        };
    }
    let mut last_stage = "lifecycle";
    let mut progress = |state: &str, stage: &'static str, completed, remaining| {
        last_stage = stage;
        if !as_json {
            stderr(&format!(
                "{state} stage={stage} completed={completed} remaining={remaining}"
            ));
        }
    };
    let data = match command {
        "status" if !as_json => {
            return match human::status(args.paths("project_dir")) {
                Ok(summary) => stdout(format!("{summary}\n").as_bytes(), 0),
                Err(error) => failure(error, command, false, None),
            };
        }
        "status" => read_status(args.paths("project_dir")),
        "bind-plan" => bind::run(args.paths("project_dir")),
        "init" => init::run(&args),
        "doctor" => Ok(doctor::run(&args)),
        "dogfood.report" | "dogfood.preview" | "dogfood.validate" => dogfood::run(&args),
        "handoff.inspect" => {
            let target = args.paths("target");
            if is_handoff_archive(target) {
                archive::inspect(target)
            } else {
                validation::handoff::inspect_project(target)
            }
        }
        "handoff.export" => archive::export(args.paths("project_dir"), args.paths("output_path")),
        "handoff.import" => archive::import(args.paths("archive_path"), args.paths("output_root")),
        "handoff.prepare" => validation::handoff::prepare_project(args.paths("project_dir")),
        "handoff.record-failure" | "handoff.accept-result" => {
            validation::handoff::submit_result(&args)
        }
        "skill-install" | "skill-uninstall" => skill::run(&args),
        "setup" | "repair" | "uninstall" => clients::run(&args),
        "validate" => validate(
            args.paths("project_dir"),
            args.text("stage").unwrap_or("all"),
        ),
        "resume" => resume::run(args.paths("project_dir"), &mut progress),
        "finalize" => finalize::run(args.paths("project_dir"), &mut progress),
        _ => unreachable!("registered CLI command"),
    };
    let lifecycle = matches!(command, "resume" | "finalize").then_some(last_stage);
    match data {
        Ok(data) if command == "repair" && clients::repair_error(&data).is_some() => {
            let error = clients::repair_error(&data).expect("repair failure");
            if as_json {
                json_stdout(
                    &json!({"command": command, "data": data, "error": error, "ok": false}),
                    1,
                )
            } else {
                stdout(human::clients(&data, true).as_bytes(), 0);
                let text = |key| error[key].as_str().unwrap_or_default();
                stderr(&format!(
                    "ERROR {} [{}]: {}\nReason: {}\nRecovery: {}",
                    text("code"),
                    text("category"),
                    text("message"),
                    text("reason"),
                    text("recovery")
                ));
                ExitCode::FAILURE
            }
        }
        Ok(data)
            if command == "validate"
                && data.as_array().is_some_and(|issues| !issues.is_empty()) =>
        {
            if as_json {
                json_stdout(&Error::validation_failure(data), 2)
            } else {
                stderr(&Error::validation_human(
                    data.as_array().expect("issue list").len(),
                ));
                json_stdout(&data, 2)
            }
        }
        Ok(data) if as_json => {
            let code = u8::from(command == "doctor" && data["healthy"] == false);
            json_stdout(
                &json!({"command": command, "data": data, "error": null, "ok": true}),
                code,
            )
        }
        Ok(data) if command == "init" => stdout(
            format!("{}\n", data["project_id"].as_str().unwrap_or_default()).as_bytes(),
            0,
        ),
        Ok(data) if command == "doctor" => {
            let messages = validation::items(&data["messages"])
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>();
            stdout(
                format!("{}\n", messages.join("\n")).as_bytes(),
                u8::from(data["healthy"] == false),
            )
        }
        Ok(data) if command.starts_with("dogfood.") => stdout(&manifest::canonical(&data), 0),
        Ok(data) if command.starts_with("handoff.") => stdout(
            format!("{}\n", human::handoff(command, &data)).as_bytes(),
            0,
        ),
        Ok(data) if command.starts_with("skill-") => {
            stdout(format!("{}\n", human::skill(&data)).as_bytes(), 0)
        }
        Ok(data) if matches!(command, "setup" | "repair" | "uninstall") => {
            stdout(human::clients(&data, false).as_bytes(), 0)
        }
        Ok(data) => json_stdout(&data, 0),
        Err(error) => failure(error, command, as_json, lifecycle),
    }
}

fn is_handoff_archive(path: &Path) -> bool {
    !path.is_dir()
        && path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(archive::SUFFIX))
}

fn stdout(bytes: &[u8], code: u8) -> ExitCode {
    match io::stdout().lock().write_all(bytes) {
        Ok(()) => ExitCode::from(code),
        Err(_) => ExitCode::FAILURE,
    }
}

fn json_stdout(value: &Value, code: u8) -> ExitCode {
    match manifest::write(value, io::stdout().lock()) {
        Ok(()) => ExitCode::from(code),
        Err(_) => ExitCode::FAILURE,
    }
}

fn stderr(message: &str) {
    let _ = writeln!(io::stderr().lock(), "{message}");
}

fn failure(error: Error, command: &str, as_json: bool, stage: Option<&str>) -> ExitCode {
    let exit = error.exit;
    if as_json {
        return json_stdout(&error.payload(command), exit);
    }
    if let Some(stage) = stage {
        let blocked = error.detail().is_some_and(|text| {
            let text = text.to_ascii_lowercase();
            text.contains("blocked") || text.contains("capability")
        });
        let status = if blocked { "BLOCKED" } else { "FAILED" };
        stderr(&format!("{status} stage={stage}"));
    }
    stderr(&error.human());
    ExitCode::from(exit)
}

fn read_status(path: &Path) -> Result<Value> {
    let project = project_fs::LockedProject::open(path, true)?;
    manifest::read_project(project.root(), true)
}

fn validate(path: &Path, name: &str) -> Result<Value> {
    let stage = validation::Stage::parse(name).ok_or_else(|| {
        let detail = if name.contains(['/', '\\']) {
            "ValueError".into()
        } else {
            format!("unknown validation stage: {name}")
        };
        Error::invalid(detail)
    })?;
    validation::inspect(path, stage)
}
