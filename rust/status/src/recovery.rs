mod publish;
pub use publish::{Change, publish};

use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{self, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use cap_primitives::fs as cap;
use serde_json::Value;

use crate::error::{Error, Result, contract};
use crate::{manifest, project_fs};

enum Undo {
    Replace {
        path: PathBuf,
        backup: Option<OsString>,
    },
    Append {
        path: PathBuf,
        original: Option<AppendState>,
    },
}

struct AppendState {
    size: u64,
    repair_size: u64,
    tail: Vec<u8>,
}

pub fn recover(root: &File, _lock: &project_fs::ProjectLock) -> Result<()> {
    let path = Path::new("logs/transactions");
    let Some((logs, name)) = project_fs::target(root, path, false)? else {
        return Ok(());
    };
    let name = Path::new(name);
    if !project_fs::metadata(&logs, name)?.is_some_and(|metadata| metadata.is_dir()) {
        return Ok(());
    }
    let transactions = project_fs::open_dir(&logs, name)?;
    let mut ids: Vec<u64> = project_fs::names(&transactions)?
        .iter()
        .filter_map(|name| {
            let name = name.to_str()?;
            let id: u64 = name.parse().ok()?;
            (id.to_string() == name).then_some(id)
        })
        .collect();
    ids.sort_unstable();
    for id in ids {
        let name = id.to_string();
        let path = Path::new(&name);
        let Ok(dir) = project_fs::open_dir(&transactions, path) else {
            continue;
        };
        if recover_one(root, &dir, &name)? {
            drop(dir);
            let _ = cap::remove_dir(&transactions, path);
            project_fs::sync_directory(&transactions)?;
        }
    }
    Ok(())
}

fn recover_one(root: &File, dir: &File, id: &str) -> Result<bool> {
    let filename = Path::new("journal.json");
    let journal = match project_fs::metadata(dir, filename) {
        Ok(Some(meta)) if meta.is_file() => manifest::read(dir, "journal.json").ok(),
        Ok(None) => return discard_unpublished(dir),
        _ => None,
    };
    let Some(journal) = journal else {
        return Ok(false);
    };
    let Some(entries) = journal.get("targets").and_then(Value::as_array) else {
        return Ok(false);
    };
    if let Some(version) = journal.get("schema_version")
        && version.as_str() != Some(contract::JOURNAL_SCHEMA_VERSION)
    {
        return Ok(false);
    }
    match journal.get("phase").and_then(Value::as_str) {
        Some("staging" | "publishing" | "rolled_back") => {
            if entries.iter().any(|entry| {
                !entry.get("path").is_some_and(Value::is_string)
                    || !matches!(entry.get("operation"), None | Some(Value::Null))
                        && entry.get("operation").and_then(Value::as_str) != Some("append")
            }) {
                return Ok(false);
            }
            let plan = entries
                .iter()
                .map(|entry| undo_for(entry, id))
                .collect::<Result<Vec<_>>>()?;
            for undo in &plan {
                undo.check(root, dir)?;
            }
            for undo in plan.iter().rev() {
                undo.run(root, dir)?;
            }
        }
        Some("committed") => {}
        _ => return Ok(false),
    }
    for child in project_fs::names(dir)? {
        if child != filename.as_os_str() {
            let _ = cap::remove_file(dir, Path::new(&child));
        }
    }
    remove(dir, filename)?;
    project_fs::sync_directory(dir)?;
    Ok(true)
}

fn discard_unpublished(dir: &File) -> Result<bool> {
    let children = project_fs::names(dir)?;
    let only_temps = children.iter().all(|name| {
        name.to_str()
            .and_then(|name| name.strip_prefix(".journal.json.")?.strip_suffix(".tmp"))
            .is_some_and(|token| {
                token.len() == 8 && token.bytes().all(|byte| {
                    byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'
                })
            })
            && matches!(project_fs::metadata(dir, Path::new(name)), Ok(Some(meta)) if meta.is_file())
    });
    if only_temps {
        for name in &children {
            remove(dir, Path::new(name))?;
        }
        project_fs::sync_directory(dir)?;
    }
    Ok(only_temps)
}

fn undo_for(entry: &Value, id: &str) -> Result<Undo> {
    let malformed = || Error::invalid("transaction journal entry is invalid");
    let path = project_fs::relative_path(entry["path"].as_str().ok_or_else(malformed)?)?;
    let parts: Vec<_> = path
        .components()
        .map(|part| part.as_os_str().to_string_lossy())
        .collect();
    if parts[0].eq_ignore_ascii_case(project_fs::LOCK_NAME)
        || (parts.len() >= 2
            && parts[0].eq_ignore_ascii_case("logs")
            && parts[1].eq_ignore_ascii_case("transactions"))
    {
        return Err(Error::boundary(
            "security-error: recovery target is protected",
        ));
    }
    if entry["operation"].as_str() == Some("append") {
        let original = if entry["original_exists"].as_bool().ok_or_else(malformed)? {
            let size = entry["original_size"].as_u64().ok_or_else(malformed)?;
            let repair_size = entry["repair_size"].as_u64().ok_or_else(malformed)?;
            let encoded = entry
                .get("original_tail")
                .map(Value::as_str)
                .unwrap_or(Some(""))
                .ok_or_else(malformed)?;
            let tail = STANDARD.decode(encoded).map_err(|_| malformed())?;
            if size.checked_sub(repair_size) != Some(tail.len() as u64) {
                return Err(malformed());
            }
            Some(AppendState {
                size,
                repair_size,
                tail,
            })
        } else {
            None
        };
        return Ok(Undo::Append { path, original });
    }
    let backup = match entry.get("backup") {
        None | Some(Value::Null) => None,
        Some(Value::String(name)) if name.is_empty() => None,
        Some(Value::String(name)) => {
            let name = project_fs::relative_path(name)?;
            let prefix = Path::new("logs/transactions").join(id);
            let file = name
                .strip_prefix(prefix)
                .ok()
                .and_then(Path::file_name)
                .filter(|_| name.components().count() == 4)
                .filter(|name| {
                    name.to_str()
                        .is_some_and(|name| name.starts_with("backup-"))
                })
                .ok_or_else(|| {
                    Error::boundary("security-error: recovery backup is outside its transaction")
                })?;
            Some(file.to_owned())
        }
        _ => return Err(malformed()),
    };
    Ok(Undo::Replace { path, backup })
}

impl Undo {
    fn path(&self) -> &Path {
        match self {
            Self::Replace { path, .. } | Self::Append { path, .. } => path,
        }
    }

    fn check(&self, root: &File, tx: &File) -> Result<()> {
        if let Some((parent, name)) = project_fs::target(root, self.path(), false)? {
            check_existing_regular(&parent, name)?;
        }
        if let Self::Replace {
            backup: Some(backup),
            ..
        } = self
        {
            check_existing_regular(tx, backup)?;
        }
        Ok(())
    }

    fn run(&self, root: &File, tx: &File) -> Result<()> {
        match self {
            Self::Replace {
                path,
                backup: Some(backup),
            } => {
                let backup = Path::new(backup);
                if !project_fs::metadata(tx, backup)?.is_some_and(|metadata| metadata.is_file()) {
                    return Ok(());
                }
                let (parent, name) =
                    project_fs::target(root, path, true)?.expect("creation allows missing parents");
                cap::rename(tx, backup, &parent, Path::new(name))?;
                project_fs::sync_directory(&parent)?;
                project_fs::sync_directory(tx)?;
            }
            Self::Append {
                path,
                original: Some(original),
            } => {
                let Some((parent, name)) = project_fs::target(root, path, false)? else {
                    return Err(io::Error::from(io::ErrorKind::NotFound).into());
                };
                let mut file = project_fs::open_file(&parent, Path::new(name), true, false)?;
                if file.metadata()?.len() < original.repair_size {
                    return Err(Error::invalid(
                        "append journal refers to a missing log prefix",
                    ));
                }
                file.set_len(original.repair_size)?;
                file.seek(SeekFrom::Start(original.repair_size))?;
                file.write_all(&original.tail)?;
                file.set_len(original.size)?;
                file.sync_all()?;
                project_fs::sync_directory(&parent)?;
            }
            Self::Replace { path, backup: None }
            | Self::Append {
                path,
                original: None,
            } => {
                if let Some((parent, name)) = project_fs::target(root, path, false)? {
                    remove(&parent, Path::new(name))?;
                    project_fs::sync_directory(&parent)?;
                }
            }
        }
        Ok(())
    }
}

fn check_existing_regular(dir: &File, name: &OsStr) -> Result<()> {
    if project_fs::metadata(dir, Path::new(name))?.is_some_and(|metadata| !metadata.is_file()) {
        return Err(Error::invalid(
            "transaction recovery requires a regular file",
        ));
    }
    Ok(())
}

fn remove(dir: &File, name: &Path) -> io::Result<()> {
    match cap::remove_file(dir, name) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}
