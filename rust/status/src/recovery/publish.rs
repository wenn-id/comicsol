use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

use base64::{Engine, engine::general_purpose::STANDARD};
use cap_fs_ext::OpenOptionsFollowExt;
use cap_primitives::fs::{self as cap, FollowSymlinks, OpenOptions};
use serde_json::{Value, json};

use super::{check_existing_regular, recover_one};
use crate::error::{Error, Result, contract};
use crate::manifest;
use crate::project_fs::{self, LockedProject};

const TX_BASE: &str = "logs/transactions";
const JOURNAL: &str = "journal.json";
const JOURNAL_TEMP: &str = ".journal.json.00000000.tmp";
const MAX_BACKUP: u64 = 128 * 1024 * 1024;

#[derive(Clone)]
pub enum Change {
    Replace(String, Vec<u8>),
    Append(&'static str, Vec<u8>),
}

pub fn publish(project: &LockedProject, operation: &str, changes: Vec<Change>) -> Result<()> {
    let root = project.root();
    let path = Path::new(TX_BASE);
    let (logs, name) = project_fs::target(root, path, true)?.expect("parents created");
    let name = Path::new(name);
    if project_fs::metadata(&logs, name)?.is_none() {
        cap::create_dir(&logs, name, &cap::DirOptions::new())?;
        project_fs::sync_directory(root)?;
        project_fs::sync_directory(&logs)?;
    }
    let base = project_fs::open_dir(&logs, name)?;
    let id = project_fs::names(&base)?
        .iter()
        .filter_map(|name| name.to_str()?.trim().parse::<u64>().ok())
        .max()
        .unwrap_or_default()
        .checked_add(1)
        .ok_or_else(|| Error::invalid("transaction ID exceeds supported range"))?;
    let id = id.to_string();
    let name = Path::new(&id);
    cap::create_dir(&base, name, &cap::DirOptions::new())?;
    let tx = project_fs::open_dir(&base, name)?;

    let outcome = (|| {
        project_fs::sync_directory(&base)?;
        write_journal(&tx, operation, "staging", &[])?;
        let mut targets = Vec::new();
        for (index, change) in changes.iter().enumerate() {
            targets.push(stage(root, &tx, &id, index + 1, change)?);
        }
        write_journal(&tx, operation, "publishing", &targets)?;
        for target in &targets {
            apply(root, &tx, target)?;
        }
        write_journal(&tx, operation, "committed", &targets)
    })();
    // Use the same rollback path as the next invocation after a process crash.
    if recover_one(root, &tx, &id)? {
        drop(tx);
        cap::remove_dir(&base, name)?;
        project_fs::sync_directory(&base)?;
    }
    outcome
}

fn write_journal(tx: &File, operation: &str, phase: &str, targets: &[Value]) -> Result<()> {
    let bytes = manifest::compact(&json!({
        "operation":operation,"phase":phase,"schema_version":contract::JOURNAL_SCHEMA_VERSION,
        "targets":targets,
    }));
    if bytes.len() > contract::MAX_JSON_BYTES {
        return Err(Error::limit(format!(
            "the file size limit of {} bytes",
            contract::MAX_JSON_BYTES
        )));
    }
    // Recovery must be able to read every journal the writer publishes.
    let document = std::str::from_utf8(&bytes).expect("canonical JSON is UTF-8");
    manifest::parse_document(document, JOURNAL)?;
    let mut temp = write_new(tx, JOURNAL_TEMP)?;
    temp.write_all(&bytes)?;
    temp.sync_all()?;
    drop(temp);
    cap::rename(tx, Path::new(JOURNAL_TEMP), tx, Path::new(JOURNAL))?;
    project_fs::sync_directory(tx)?;
    Ok(())
}

fn write_new(dir: &File, name: &str) -> Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    options.follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_primitives::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(cap::open(dir, Path::new(name), &options)?)
}

fn stage(root: &File, tx: &File, id: &str, index: usize, change: &Change) -> Result<Value> {
    let (path, payload) = match change {
        Change::Replace(path, data) => (path.as_str(), data),
        Change::Append(path, data) => (*path, data),
    };
    let relative = project_fs::relative_path(path)?;
    let mut existing = None;
    if let Some((parent, name)) = project_fs::target(root, &relative, false)? {
        let name = Path::new(name);
        if let Some(meta) = project_fs::metadata(&parent, name)? {
            if !meta.is_file() {
                return Err(io::Error::from(io::ErrorKind::IsADirectory).into());
            }
            existing = Some(project_fs::open_file(&parent, name, false, false)?);
        }
    }
    if matches!(change, Change::Append(_, _)) {
        let exists = existing.is_some();
        let (size, offset, torn, separator) = existing
            .as_mut()
            .map(append_position)
            .transpose()?
            .unwrap_or_default();
        let mut append = Vec::with_capacity(payload.len() + usize::from(separator));
        if separator {
            append.push(b'\n');
        }
        append.extend(payload);
        return Ok(json!({
            "operation":"append", "path":path, "original_exists":exists,
            "original_size":size, "repair_size":offset, "original_tail":STANDARD.encode(torn),
            "payload":STANDARD.encode(append),
        }));
    }
    let basename = relative
        .file_name()
        .and_then(|name| name.to_str())
        .expect("transaction basename");
    let backup_name = format!("backup-{index:03}-{basename}");
    let backup = if let Some(input) = existing {
        let mut destination = write_new(tx, &backup_name)?;
        let copied = io::copy(&mut input.take(MAX_BACKUP + 1), &mut destination)?;
        if copied > MAX_BACKUP {
            return Err(Error::limit(format!(
                "the file size limit of {MAX_BACKUP} bytes"
            )));
        }
        destination.sync_all()?;
        Some(format!("{TX_BASE}/{id}/{backup_name}"))
    } else {
        None
    };
    let staged = format!("staged-{index:03}-{basename}");
    let mut destination = write_new(tx, &staged)?;
    destination.write_all(payload)?;
    destination.sync_all()?;
    Ok(json!({"path":path,"backup":backup,"staged":format!("{TX_BASE}/{id}/{staged}")}))
}

fn append_position(file: &mut File) -> Result<(u64, u64, Vec<u8>, bool)> {
    let size = file.seek(SeekFrom::End(0))?;
    if size == 0 {
        return Ok((0, 0, Vec::new(), false));
    }
    let mut cursor = size;
    let mut chunks = Vec::new();
    let mut scanned = 0;
    let offset = loop {
        let count = cursor.min(64 * 1024) as usize;
        cursor -= count as u64;
        file.seek(SeekFrom::Start(cursor))?;
        let mut chunk = vec![0; count];
        file.read_exact(&mut chunk)?;
        if let Some(newline) = chunk.iter().rposition(|&byte| byte == b'\n') {
            chunks.push(chunk.split_off(newline + 1));
            break cursor + newline as u64 + 1;
        }
        scanned += count;
        if scanned > contract::MAX_JSON_BYTES {
            return Err(Error::limit(
                "the transaction journal size limit for a torn event log",
            ));
        }
        chunks.push(chunk);
        if cursor == 0 {
            break 0;
        }
    };
    let tail: Vec<_> = chunks.into_iter().rev().flatten().collect();
    if tail.is_empty() {
        Ok((size, size, tail, false))
    } else if serde_json::from_slice::<Value>(&tail).is_ok() {
        Ok((size, size, Vec::new(), true))
    } else {
        Ok((size, offset, tail, false))
    }
}

fn apply(root: &File, tx: &File, target: &Value) -> Result<()> {
    let path = project_fs::relative_path(target["path"].as_str().expect("staged target path"))?;
    let (parent, name) = project_fs::target(root, &path, true)?.expect("parents created");
    check_existing_regular(&parent, name)?;
    let name = Path::new(name);
    if target["operation"] == "append" {
        let offset = target["repair_size"]
            .as_u64()
            .expect("staged append offset");
        let bytes = STANDARD
            .decode(target["payload"].as_str().expect("staged append payload"))
            .expect("internally encoded append payload");
        let mut file = project_fs::open_file(&parent, name, true, true)?;
        if file.metadata()?.len() < offset {
            return Err(Error::invalid(
                "append journal refers to a missing log prefix",
            ));
        }
        file.set_len(offset)?;
        file.seek(SeekFrom::Start(offset))?;
        file.write_all(&bytes)?;
        file.sync_all()?;
    } else {
        let staged = Path::new(target["staged"].as_str().expect("staged filename"));
        let basename = Path::new(staged.file_name().expect("staged basename"));
        cap::rename(tx, basename, &parent, name)?;
        project_fs::sync_directory(tx)?;
    }
    project_fs::sync_directory(&parent)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let index = NEXT.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir()
                .join(format!("comic-sol-resume-{}-{index}", std::process::id()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn recovers_at_each_publication_boundary() {
        for committed in [false, true] {
            for published in 0..=3 {
                if committed && published != 3 {
                    continue;
                }
                let fixture = Fixture::new();
                let path = &fixture.0;
                fs::create_dir_all(path.join("logs/transactions")).unwrap();
                fs::write(path.join("project.json"), b"old").unwrap();
                fs::write(path.join("logs/stage-cache.json"), b"old cache").unwrap();
                fs::write(path.join("logs/events.jsonl"), b"{}\n{torn").unwrap();
                {
                    // Make the durable state a process would leave at this boundary.
                    let project = LockedProject::open(path, true)
                        .unwrap_or_else(|_| panic!("open {}", path.display()));
                    let base = project_fs::open_dir(project.root(), Path::new("logs")).unwrap();
                    let base = project_fs::open_dir(&base, Path::new("transactions")).unwrap();
                    cap::create_dir(&base, Path::new("1"), &cap::DirOptions::new()).unwrap();
                    let tx = project_fs::open_dir(&base, Path::new("1")).unwrap();
                    let changes = [
                        Change::Replace("logs/stage-cache.json".into(), b"new cache".to_vec()),
                        Change::Append("logs/events.jsonl", b"{\"done\":true}\n".to_vec()),
                        Change::Replace("project.json".into(), b"new".to_vec()),
                    ];
                    let targets: Vec<_> = changes
                        .iter()
                        .enumerate()
                        .map(|(index, change)| {
                            stage(project.root(), &tx, "1", index + 1, change).unwrap()
                        })
                        .collect();
                    write_journal(&tx, "resume-recovery", "publishing", &targets).unwrap();
                    for target in &targets[..published] {
                        apply(project.root(), &tx, target).unwrap();
                    }
                    if committed {
                        write_journal(&tx, "resume-recovery", "committed", &targets).unwrap();
                    }
                }
                let _project = LockedProject::open(path, true).unwrap();
                let expected: [(&str, &[u8], &[u8]); 3] = [
                    ("project.json", b"old", b"new"),
                    ("logs/stage-cache.json", b"old cache", b"new cache"),
                    ("logs/events.jsonl", b"{}\n{torn", b"{}\n{\"done\":true}\n"),
                ];
                for (relative, old, new) in expected {
                    assert_eq!(
                        fs::read(path.join(relative)).unwrap(),
                        if committed { new } else { old }
                    );
                }
                assert_eq!(
                    0,
                    fs::read_dir(path.join("logs/transactions"))
                        .unwrap()
                        .count()
                );
            }
        }
    }

    #[test]
    fn failed_staging_leaves_existing_file_bytes() {
        let fixture = Fixture::new();
        let path = &fixture.0;
        fs::create_dir_all(path.join("logs/events.jsonl")).unwrap();
        fs::write(path.join("project.json"), b"old").unwrap();
        fs::write(path.join("logs/stage-cache.json"), b"old cache").unwrap();
        let project = LockedProject::open(path, true).unwrap();
        assert!(
            publish(
                &project,
                "resume-recovery",
                vec![
                    Change::Replace("logs/stage-cache.json".into(), b"new".to_vec()),
                    Change::Append("logs/events.jsonl", b"invalid".to_vec()),
                    Change::Replace("project.json".into(), b"new".to_vec()),
                ]
            )
            .is_err()
        );
        assert_eq!(
            b"old",
            fs::read(path.join("project.json")).unwrap().as_slice()
        );
        assert_eq!(
            b"old cache",
            fs::read(path.join("logs/stage-cache.json"))
                .unwrap()
                .as_slice()
        );
        assert!(path.join("logs/events.jsonl").is_dir());
        assert_eq!(
            0,
            fs::read_dir(path.join("logs/transactions"))
                .unwrap()
                .count()
        );
    }
}
