mod edit;

use std::collections::BTreeSet;
use std::env;
use std::fs::{self, File, Permissions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use serde_json::{Value, json};
use time::OffsetDateTime;

use crate::cli::Arguments;
use crate::error::{Error, Result, contract};
use crate::project_fs::{self, ProjectLock, StagedDirectory, external_target, same_directory};
use crate::{init, validation};

const MAX_CONFIG: u64 = 8 * 1024 * 1024;
const UNSUPPORTED: &str = "native config format or location is not verified";
const ACTION: &str = "set-comic-sol-entry";
const NOT_SELECTED: &str = "not selected";
const CUSTOM: &[&str] = &["hermes", "claude-code", "vscode"];

struct Client {
    name: &'static str,
    path: Option<PathBuf>,
}

#[derive(Clone, Copy)]
enum Operation {
    Setup,
    Uninstall,
    Repair { dry_run: bool },
}

impl Operation {
    fn repair(self) -> bool {
        matches!(self, Self::Repair { .. })
    }
}

impl Client {
    fn result(&self, status: &str, message: &str, backup: Option<&Path>) -> Value {
        json!({
            "client": self.name, "status": status, "message": message,
            "config_path": self.path.as_ref().map(|path| path.to_string_lossy()),
            "backup_path": backup.map(|path| path.to_string_lossy()),
        })
    }

    fn without_change(
        &self,
        operation: Operation,
        status: &str,
        message: &str,
        entry: Option<&Value>,
        verified: bool,
    ) -> Value {
        let mut result = self.result(status, message, None);
        if operation.repair() {
            repair_fields(
                &mut result,
                json!({"state": "no-op", "action": "none", "backup_required": false,
                "planned_entry": entry, "verified": verified, "restored": null, "error": null}),
            );
        }
        result
    }

    fn failure(&self, operation: Operation, failure: Failure, entry: Option<&Value>) -> Value {
        let Failure {
            message,
            backup,
            action,
            restored,
        } = failure;
        let status = match restored {
            Some(true) => "rolled-back",
            Some(false) => "rollback-failed",
            None => "failed",
        };
        if !operation.repair() {
            let status = if status == "rollback-failed" {
                "failed"
            } else {
                status
            };
            return self.result(status, &message, backup.as_deref());
        }
        let (definition, public) = if restored == Some(false) {
            (
                contract::ROLLBACK,
                "rollback could not be verified; restore the backup and run comic-sol doctor",
            )
        } else {
            (
                contract::REPAIR,
                "repair failed; run comic-sol doctor and retry",
            )
        };
        let [code, category, reason_message, reason, recovery] = definition;
        let detail = if message.contains(['/', '\\']) {
            "OSError".to_owned()
        } else {
            crate::human::escape(&message)
        };
        let mut result = self.result(status, public, backup.as_deref());
        repair_fields(
            &mut result,
            json!({
                "state": "failure", "action": if action { ACTION } else { "none" },
                "backup_required": action, "planned_entry": entry, "verified": false, "restored": restored,
                "error": {"code": code, "category": category, "command": "repair", "message": reason_message,
                    "reason": reason, "recovery": recovery, "detail": detail},
            }),
        );
        result
    }
}

struct Failure {
    message: String,
    backup: Option<PathBuf>,
    action: bool,
    restored: Option<bool>,
}

impl Failure {
    fn new(message: impl Into<String>, action: bool) -> Self {
        Self {
            message: message.into(),
            backup: None,
            action,
            restored: None,
        }
    }
}

pub fn run(args: &Arguments) -> Result<Value> {
    let operation = match args.command.as_str() {
        "setup" => Operation::Setup,
        "uninstall" => Operation::Uninstall,
        "repair" => Operation::Repair {
            dry_run: args.has("dry_run"),
        },
        _ => unreachable!("client command"),
    };
    let select: BTreeSet<_> = args
        .values("clients")
        .filter_map(|name| name.to_str())
        .collect();
    let clients = default_clients()?;
    if operation.repair()
        && let Some(unknown) = select.iter().find(|&&name| {
            !CUSTOM.contains(&name) && !clients.iter().any(|client| client.name == name)
        })
    {
        return Err(Error::invalid(format!("unsupported client: {unknown}")));
    }
    let output_root = args
        .path("output_root")
        .map_or_else(init::default_output, init::expand);
    let mut planned = None;
    let mut results = Vec::new();
    for client in clients {
        if !select.is_empty() && !select.contains(client.name) {
            results.push(client.without_change(operation, "skipped", NOT_SELECTED, None, false));
            continue;
        }
        let path = client.path.as_ref().expect("native integration location");
        if !path.is_file() {
            let message = if operation.repair() {
                "client config not found; no third-party config was created"
            } else {
                "client config not found"
            };
            results.push(client.without_change(operation, "skipped", message, None, false));
            continue;
        }
        if !matches!(operation, Operation::Uninstall) && planned.is_none() {
            match entry(&output_root) {
                Ok(value) => planned = Some(value),
                Err(error) if operation.repair() => {
                    results.push(client.failure(
                        operation,
                        Failure::new(error.detail().unwrap_or(error.exception()), false),
                        None,
                    ));
                    continue;
                }
                Err(error) => return Err(error),
            }
        }
        let result = configure(&client, operation, planned.as_ref());
        results.push(match result {
            Ok(value) => value,
            Err(error) => client.failure(operation, error, planned.as_ref()),
        });
    }
    for &name in CUSTOM {
        if select.is_empty() || select.contains(name) {
            let client = Client { name, path: None };
            results.push(if operation.repair() && !select.is_empty() {
                client.failure(operation, Failure::new(UNSUPPORTED, false), None)
            } else {
                client.without_change(operation, "unsupported", UNSUPPORTED, None, false)
            });
        }
    }
    Ok(json!(results))
}

fn default_clients() -> Result<Vec<Client>> {
    let home = env::home_dir().ok_or_else(|| Error::invalid("home cannot be resolved"))?;
    let codex = env::var_os("CODEX_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"));
    let claude = if cfg!(windows) {
        env::var_os("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData/Roaming"))
            .join("Claude")
    } else if cfg!(target_os = "macos") {
        home.join("Library/Application Support/Claude")
    } else {
        home.join(".config/Claude")
    };
    Ok([
        ("codex", codex.join("config.toml")),
        ("claude-desktop", claude.join("claude_desktop_config.json")),
        ("cursor", home.join(".cursor/mcp.json")),
        ("windsurf", home.join(".codeium/windsurf/mcp_config.json")),
    ]
    .into_iter()
    .map(|(name, path)| Client {
        name,
        path: Some(path),
    })
    .collect())
}

fn entry(root: &Path) -> Result<Value> {
    let executable = env::current_exe()?;
    if !executable
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            matches!(
                name.to_ascii_lowercase().as_str(),
                "comic-sol" | "comic-sol.exe"
            )
        })
    {
        return Err(Error::invalid("Comic Sol executable identity check failed"));
    }
    let root = normalize(root)?;
    Ok(
        json!({"command": display_path(&executable), "args": ["mcp", "--root", display_path(&root)]}),
    )
}

fn normalize(path: &Path) -> Result<PathBuf> {
    let absolute = std::path::absolute(path)?;
    let mut tail = Vec::new();
    let mut existing = absolute.as_path();
    while !existing.exists() {
        let Some(last) = existing.file_name() else {
            break;
        };
        tail.push(last);
        existing = existing.parent().expect("path parent");
    }
    let resolved = fs::canonicalize(existing).unwrap_or_else(|_| existing.to_owned());
    let mut root = PathBuf::new();
    for component in resolved.components() {
        match component {
            Component::ParentDir => {
                root.pop();
            }
            other => root.push(other.as_os_str()),
        }
    }
    for component in tail.into_iter().rev() {
        root.push(component);
    }
    Ok(root)
}

fn display_path(path: &Path) -> String {
    let raw = path.to_string_lossy();
    #[cfg(windows)]
    {
        if let Some(unc) = raw.strip_prefix(r"\\?\UNC\") {
            return format!(r"\\{unc}");
        }
        if let Some(dos) = raw.strip_prefix(r"\\?\") {
            return dos.to_owned();
        }
    }
    raw.into_owned()
}

fn repair_fields(result: &mut Value, fields: Value) {
    result
        .as_object_mut()
        .expect("client outcome")
        .extend(fields.as_object().expect("repair outcome").clone());
}

fn configure(
    client: &Client,
    operation: Operation,
    entry: Option<&Value>,
) -> std::result::Result<Value, Failure> {
    let path = client.path.as_deref().expect("native config path");
    let codex = client.name == "codex";
    let active_repair = matches!(operation, Operation::Repair { dry_run: false });
    let early = |message: String| Failure::new(message, active_repair);
    let (parent, name) = external_target(path).map_err(|error| early(public_error(&error)))?;
    let name = Path::new(&name);
    project_fs::metadata(&parent, name).map_err(|error| early(public_error(&error)))?;
    if !active_repair {
        let snapshot =
            Snapshot::read(&parent, name).map_err(|error| early(public_error(&error)))?;
        let change = edit::update(&snapshot.bytes, codex, entry).map_err(|error| {
            let message = if !operation.repair() && config_read_error(&error) {
                format!("malformed or unreadable config: {error}")
            } else {
                error
            };
            early(message)
        })?;
        if change.is_none() {
            return Ok(no_change(client, operation, entry));
        }
        if matches!(operation, Operation::Repair { dry_run: true }) {
            let mut result =
                client.result("planned", "would update the Comic Sol integration", None);
            repair_fields(
                &mut result,
                json!({"state": "success", "action": ACTION, "backup_required": true,
                "planned_entry": entry, "verified": false, "restored": null, "error": null}),
            );
            return Ok(result);
        }
    }
    let lock_name = format!(".{}.lock", name.to_string_lossy());
    let _lock = ProjectLock::config(&parent, Path::new(&lock_name))
        .map_err(|error| early(public_error(&error)))?;
    let source = Snapshot::read(&parent, name).map_err(|error| early(public_error(&error)))?;
    let change = edit::update(&source.bytes, codex, entry).map_err(early)?;
    let Some(change) = change else {
        return Ok(no_change(client, operation, entry));
    };
    let backup = backup_name(name);
    let backup_path = path.with_file_name(&backup);
    source
        .assert_current(&parent, name)
        .map_err(|error| early(public_error(&error)))?;
    let stage = StagedDirectory::create(&parent, "client-config")
        .map_err(|error| early(public_error(&error)))?;
    let original = Path::new("backup");
    write_member(
        stage.file(),
        original,
        &source.bytes,
        private_permissions(&source.permissions),
    )
    .map_err(|error| early(public_error(&error)))?;
    project_fs::publish_noreplace(stage.file(), original, &parent, &backup)
        .map_err(|error| early(public_error(&error.into())))?;
    let fail = |message: String, restored| Failure {
        message,
        backup: Some(backup_path.clone()),
        action: active_repair,
        restored,
    };
    let prepared =
        Snapshot::read(&parent, &backup).map_err(|error| fail(public_error(&error), None))?;
    if prepared.bytes != source.bytes {
        return Err(fail(
            "client config backup verification failed".into(),
            None,
        ));
    }
    project_fs::sync_directory(&parent).map_err(|error| fail(public_error(&error.into()), None))?;
    let candidate = Path::new("candidate");
    write_member(stage.file(), candidate, &change, source.permissions.clone())
        .map_err(|error| fail(public_error(&error), None))?;
    let candidate_snapshot = Snapshot::read(stage.file(), candidate)
        .map_err(|error| fail(public_error(&error), None))?;
    let candidate_path = stage.entry_name().join(candidate);
    let target_session = |expected: &Snapshot| -> Result<()> {
        let (live, _) = external_target(path)?;
        if !same_directory(&parent, &live)? {
            return Err(Error::invalid(
                "client config directory changed during repair",
            ));
        }
        expected.assert_current(&parent, name)
    };
    target_session(&source).map_err(|error| fail(public_error(&error), None))?;
    project_fs::exchange(&parent, &candidate_path, name)
        .map_err(|error| fail(public_error(&error.into()), None))?;
    let verify = || -> Result<()> {
        source.assert_current(stage.file(), candidate)?;
        let installed = Snapshot::read(&parent, name)?;
        target_session(&candidate_snapshot)?;
        if installed.bytes != change
            || edit::update(&installed.bytes, codex, entry).ok() != Some(None)
        {
            return Err(Error::invalid("persisted repair verification failed"));
        }
        project_fs::sync_directory(&parent)?;
        Ok(())
    };
    if let Err(error) = verify() {
        let restore = || -> Result<bool> {
            let previous = Snapshot::read(stage.file(), candidate)?;
            if !candidate_snapshot.matches(&parent, name)? {
                return Ok(false);
            }
            project_fs::exchange(&parent, &candidate_path, name)?;
            project_fs::sync_directory(&parent)?;
            previous.matches(&parent, name)
        };
        let restored = restore().unwrap_or(false);
        if !restored {
            // The original is also preserved in the verified sibling backup.
            std::mem::forget(stage);
        }
        return Err(fail(public_error(&error), Some(restored)));
    }
    let mut result = client.result(
        if matches!(operation, Operation::Uninstall) {
            "removed"
        } else {
            "configured"
        },
        "integration updated",
        Some(&backup_path),
    );
    if operation.repair() {
        repair_fields(
            &mut result,
            json!({"state": "success", "action": ACTION, "backup_required": true,
            "planned_entry": entry, "verified": true, "restored": null, "error": null}),
        );
    }
    Ok(result)
}

fn no_change(client: &Client, operation: Operation, entry: Option<&Value>) -> Value {
    let status = if matches!(operation, Operation::Uninstall) {
        "not-configured"
    } else {
        "unchanged"
    };
    client.without_change(operation, status, "no config change required", entry, true)
}

fn config_read_error(error: &str) -> bool {
    error.contains("line ")
        || error.contains("must be an object") && !error.starts_with("comic-sol")
        || error.contains("is not valid")
        || error.contains("must be a TOML table")
}

fn public_error(error: &Error) -> String {
    error.detail().unwrap_or(error.exception()).to_owned()
}

fn backup_name(name: &Path) -> PathBuf {
    let utc = OffsetDateTime::now_utc();
    let stamp = format!(
        "{:04}{:02}{:02}T{:02}{:02}{:02}{:06}Z",
        utc.year(),
        u8::from(utc.month()),
        utc.day(),
        utc.hour(),
        utc.minute(),
        utc.second(),
        utc.microsecond()
    );
    PathBuf::from(format!("{}.bak-{stamp}", name.to_string_lossy()))
}

fn private_permissions(existing: &Permissions) -> Permissions {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = existing;
        Permissions::from_mode(0o600)
    }
    #[cfg(not(unix))]
    existing.clone()
}

fn write_member(parent: &File, name: &Path, bytes: &[u8], permissions: Permissions) -> Result<()> {
    let mut file = project_fs::open_file(parent, name, true, true)?;
    file.write_all(bytes)?;
    file.set_permissions(permissions)?;
    file.sync_all()?;
    Ok(())
}

struct Snapshot {
    bytes: Vec<u8>,
    file: File,
    permissions: Permissions,
}

impl Snapshot {
    fn read(parent: &File, name: &Path) -> Result<Self> {
        let _ = project_fs::metadata(parent, name)?;
        let file = project_fs::open_file(parent, name, false, false)?;
        let original = file.metadata()?;
        let mut bytes = Vec::new();
        (&file).take(MAX_CONFIG + 1).read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 > MAX_CONFIG {
            return Err(Error::limit("the client configuration file size limit"));
        }
        if bytes.len() as u64 != original.len()
            || after.len() != original.len()
            || original.modified()? != after.modified()?
            || original.permissions() != after.permissions()
        {
            return Err(Error::invalid("client config changed while being read"));
        }
        Ok(Self {
            bytes,
            file,
            permissions: original.permissions(),
        })
    }

    fn matches(&self, parent: &File, name: &Path) -> Result<bool> {
        let other = Self::read(parent, name)?;
        Ok(same_directory(&self.file, &other.file)?
            && other.bytes == self.bytes
            && other.permissions == self.permissions)
    }

    fn assert_current(&self, parent: &File, name: &Path) -> Result<()> {
        if !self.matches(parent, name)? {
            return Err(Error::invalid("client config changed during update"));
        }
        Ok(())
    }
}

pub fn repair_error(results: &Value) -> Option<&Value> {
    let failures = validation::items(results)
        .iter()
        .filter(|result| result["state"] == "failure");
    failures
        .clone()
        .find(|result| result["error"]["code"] == "CS-INSTALL-003")
        .or_else(|| failures.into_iter().next())
        .map(|result| &result["error"])
}
