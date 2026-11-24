mod zip;

use std::collections::BTreeMap;
use std::env;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{Value, json};

use crate::error::{Error, Result, contract};
use crate::project_fs::{self, LOCK_NAME, ProjectLock, StagedDirectory};
use crate::validation::{self, Stage, bytes_hash, is_sha256, items, keys};
use crate::{init, manifest};

pub const SUFFIX: &str = ".comic-sol-handoff";
const FORMAT: &str = "comic-sol-handoff.json";
const CHECKSUMS: &str = "checksums.json";
const PREFIX: &str = "project/";
const MAX_DEPTH: usize = 128;
const CONTROL_LIMIT: u64 = 4 * 1024 * 1024;

pub fn inspect(path: &Path) -> Result<Value> {
    let mut source = open_archive(path)?;
    let archive = verify(&mut source)?;
    let temp = env::temp_dir();
    let root = existing_directory(&temp, "archive temporary root")?.1;
    let stage = StagedDirectory::create(&root, "handoff-inspect").map_err(extraction)?;
    extract(&mut source, &archive, stage.file())?;
    Ok(json!({
        "archive_path": path.to_string_lossy(), "checksum_count": archive.digests.len(),
        "format_version": "1.0", "member_count": archive.members.len(), "project_id": archive.id, "valid": true,
    }))
}

pub fn import(path: &Path, output_root: &Path) -> Result<Value> {
    let (output_root, root) = existing_directory(output_root, "archive output root")?;
    let mut source = open_archive(path)?;
    let archive = verify(&mut source)?;
    let leaf = Path::new(&archive.id);
    if project_fs::entry_exists(&root, leaf)
        .map_err(|_| Error::archive("archive output root is not writable"))?
    {
        return Err(Error::archive("archive destination already exists"));
    }
    let stage = StagedDirectory::create(&root, "handoff").map_err(extraction)?;
    extract(&mut source, &archive, stage.file())?;
    stage.publish(leaf).map_err(|error| {
        if error.is_exists() {
            Error::archive("archive destination already exists")
        } else {
            Error::archive("archive directory publish failed")
        }
    })?;
    Ok(json!({"project_id": archive.id, "project_dir": output_root.join(leaf).to_string_lossy()}))
}

pub fn export(project: &Path, path: &Path) -> Result<Value> {
    let requested = std::path::absolute(init::expand(path))
        .map_err(|_| Error::archive("archive output path must name a file"))?;
    let leaf = requested
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .ok_or_else(|| Error::archive("archive output path must name a file"))?;
    if !leaf.ends_with(SUFFIX) {
        return Err(Error::archive(
            "archive output path must use the .comic-sol-handoff suffix",
        ));
    }
    let parent = requested.parent().expect("absolute archive path");
    let (parent_path, _) = existing_directory(parent, "archive output parent")?;
    let (source_path, source) = existing_directory(project, "handoff project")?;
    let directory = parent_path.join(leaf);
    let (parent, name) = project_fs::external_target_absolute(&directory, Some(&source), false)
        .map_err(|error| {
            if error.detail() == Some("output must be outside the project") {
                Error::archive("archive output path must not be inside the source project")
            } else {
                Error::archive("archive output parent cannot be opened safely")
            }
        })?;
    let name = Path::new(&name);
    if project_fs::entry_exists(&parent, name)
        .map_err(|_| Error::archive("archive output parent is not writable"))?
    {
        return Err(Error::archive(
            "archive destination exists; export would clobber it",
        ));
    }
    let _lock = ProjectLock::acquire(&source, Duration::from_secs(10))?;
    valid_project(&source, "handoff project is not valid")?;
    let manifest = validation::read_value(&source, "project.json")
        .ok()
        .flatten()
        .ok_or_else(|| Error::archive("handoff project manifest must be an object"))?;
    let id = project_id(&manifest["project_id"])?;
    if source_path.file_name().and_then(|name| name.to_str()) != Some(id) {
        return Err(Error::archive(
            "handoff project ID does not match its directory",
        ));
    }
    if manifest["handoff"]["manifest_path"] != "handoff/manifest.json" {
        return Err(Error::archive("handoff project is not completely prepared"));
    }
    let members = collect(&source)?;
    let checksums: Vec<_> = members
        .iter()
        .map(|(path, member)| json!({"path": format!("{PREFIX}{path}"), "sha256": member.digest}))
        .collect();
    let checksums = manifest::compact(
        &json!({"algorithm": "sha256", "files": checksums, "format_version": "1.0"}),
    );
    let format = manifest::compact(
        &json!({"format": "comic-sol-handoff", "version": "1.0", "project_id": id}),
    );
    let stage = StagedDirectory::create(&parent, "handoff-export").map_err(extraction)?;
    let staged = Path::new("archive");
    let mut output = project_fs::open_file(stage.file(), staged, true, true).map_err(extraction)?;
    let mut writer = zip::Writer::new(&mut output);
    writer.add(CHECKSUMS, &checksums)?;
    writer.add(FORMAT, &format)?;
    for (path, member) in members {
        let payload = read_project_file(&source, &path)?;
        if payload.len() as u64 != member.size || bytes_hash(&payload) != member.digest {
            return Err(Error::archive("handoff project changed during export"));
        }
        writer.add(&format!("{PREFIX}{path}"), &payload)?;
    }
    writer.finish()?;
    public_file(&output)?;
    output
        .sync_all()
        .map_err(|_| Error::archive("archive output cannot be synchronized"))?;
    verify(&mut output)?;
    drop(output);
    project_fs::publish_noreplace(stage.file(), staged, &parent, name).map_err(|error| {
        if error.kind() == std::io::ErrorKind::AlreadyExists {
            Error::archive("archive destination exists; export would clobber it")
        } else {
            Error::archive("archive output cannot be published")
        }
    })?;
    project_fs::sync_directory(&parent)
        .map_err(|_| Error::archive("archive output directory cannot be synchronized"))?;
    Ok(json!({"project_id": id, "archive_path": directory.to_string_lossy()}))
}

fn existing_directory(path: &Path, label: &str) -> Result<(PathBuf, File)> {
    let path = std::path::absolute(init::expand(path))
        .map_err(|_| Error::archive(format!("{label} must be an existing directory")))?;
    let metadata = fs::symlink_metadata(&path).map_err(|_| {
        Error::archive(format!(
            "{label} must be an existing directory: [Errno 2] No such file or directory: '<path>'"
        ))
    })?;
    if !metadata.is_dir() || metadata.is_symlink() {
        return Err(Error::archive(format!(
            "{label} must be a regular directory"
        )));
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes() & 0x400 != 0 {
            return Err(Error::archive(format!(
                "{label} must not be a reparse point"
            )));
        }
    }
    let canonical = fs::canonicalize(&path)
        .map_err(|_| Error::archive(format!("{label} cannot be opened safely")))?;
    let root = project_fs::project_root(&canonical)
        .map_err(|_| Error::archive(format!("{label} cannot be opened safely")))?;
    Ok((canonical, root))
}

fn portable(path: &str) -> bool {
    if path.is_empty() || path.contains(['\0', '\\']) || path.starts_with('/') {
        return false;
    }
    path.split('/').all(|part| {
        if part.is_empty()
            || part.ends_with([' ', '.'])
            || part.encode_utf16().count() > 255
            || part.contains(['<', '>', ':', '"', '|', '?', '*'])
            || part.chars().any(|ch| ch < ' ')
        {
            return false;
        }
        let stem = caseless::default_case_fold_str(
            part.split('.')
                .next()
                .unwrap_or_default()
                .trim_end_matches(' '),
        );
        !(matches!(
            stem.as_str(),
            "con" | "prn" | "aux" | "nul" | "conin$" | "conout$"
        ) || stem
            .strip_prefix("com")
            .or_else(|| stem.strip_prefix("lpt"))
            .is_some_and(|suffix| {
                ["1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³"].contains(&suffix)
            }))
    })
}

fn project_id(value: &Value) -> Result<&str> {
    let id = value
        .as_str()
        .ok_or_else(|| Error::archive("archive project ID must be a string"))?;
    if !portable(id) {
        return Err(Error::archive("archive project ID is unsafe"));
    }
    if id.contains('/') {
        return Err(Error::archive(
            "archive project ID must identify one directory",
        ));
    }
    Ok(id)
}

fn member_name(name: &str) -> Result<()> {
    if name.is_empty() || name.contains(['\0', '\\']) {
        return Err(Error::archive("archive contains an unsafe member path"));
    }
    if name.starts_with('/')
        || (name.as_bytes()[0].is_ascii_alphabetic() && name.as_bytes().get(1) == Some(&b':'))
    {
        return Err(Error::archive("archive contains an absolute member path"));
    }
    if name.split('/').any(|part| matches!(part, "" | "." | "..")) {
        return Err(Error::archive(
            "archive contains an unsafe traversal member path",
        ));
    }
    if name.split('/').count() > MAX_DEPTH {
        return Err(Error::archive(
            "archive member path nesting exceeds the limit",
        ));
    }
    if !portable(name) {
        return Err(Error::archive(
            "archive contains a member path that is not portable to Windows",
        ));
    }
    if !matches!(name, FORMAT | CHECKSUMS) && !name.starts_with(PREFIX) {
        return Err(Error::archive(
            "archive contains a member outside the project payload",
        ));
    }
    Ok(())
}

fn open_archive(path: &Path) -> Result<File> {
    let (parent, name) = project_fs::external_target(&init::expand(path))
        .map_err(|_| Error::archive("archive cannot be opened safely"))?;
    let leaf = Path::new(&name);
    match project_fs::metadata(&parent, leaf) {
        Ok(Some(meta)) if meta.is_file() => {}
        Ok(Some(_)) => return Err(Error::archive("archive input must be a regular file")),
        _ => return Err(Error::archive("archive cannot be opened safely")),
    }
    project_fs::open_file(&parent, leaf, false, false)
        .map_err(|_| Error::archive("archive cannot be opened safely"))
}

struct Verified {
    id: String,
    members: Vec<zip::Member>,
    digests: BTreeMap<String, String>,
}

fn verify(source: &mut File) -> Result<Verified> {
    let members = zip::index(source, member_name)?;
    let names: BTreeMap<_, _> = members
        .iter()
        .map(|member| (member.name.as_str(), member))
        .collect();
    let max_json = (contract::MAX_JSON_BYTES as u64).min(CONTROL_LIMIT);
    let control = |source: &mut File, name, label| {
        let member = names
            .get(name)
            .ok_or_else(|| Error::archive("archive is missing required metadata members"))?;
        let bytes = zip::read(source, member, max_json)?;
        let value = read_json(&bytes, label)?;
        if !value.is_object() || manifest::compact(&value) != bytes {
            return Err(Error::archive(format!(
                "archive {label} is not canonical JSON"
            )));
        }
        Ok(value)
    };
    let format = control(source, FORMAT, "format metadata")?;
    if !keys(&format, &["format", "project_id", "version"]) {
        return Err(Error::archive(
            "archive format metadata has unexpected fields",
        ));
    }
    if format["format"] != "comic-sol-handoff" {
        return Err(Error::archive("archive format metadata is invalid"));
    }
    if format["version"] != "1.0" {
        return Err(Error::archive("archive uses an unsupported format version"));
    }
    let id = project_id(&format["project_id"])?.to_owned();
    let checksums = control(source, CHECKSUMS, "checksum manifest")?;
    if !keys(&checksums, &["algorithm", "files", "format_version"]) {
        return Err(Error::archive(
            "archive checksum manifest has unexpected fields",
        ));
    }
    if checksums["algorithm"] != "sha256" {
        return Err(Error::archive("archive checksum algorithm is unsupported"));
    }
    if checksums["format_version"] != "1.0" {
        return Err(Error::archive(
            "archive checksum manifest has an unsupported version",
        ));
    }
    let all = checksums["files"]
        .as_array()
        .ok_or_else(|| Error::archive("archive checksum files must be an array"))?;
    let mut digests = BTreeMap::new();
    let mut prior = "";
    for entry in all {
        if !keys(entry, &["path", "sha256"]) {
            return Err(Error::archive("archive checksum entry is invalid"));
        }
        let path = entry["path"]
            .as_str()
            .filter(|text| text.starts_with(PREFIX))
            .ok_or_else(|| Error::archive("archive checksum path is invalid"))?;
        member_name(path)?;
        let digest = entry["sha256"]
            .as_str()
            .filter(|text| is_sha256(text))
            .ok_or_else(|| Error::archive("archive checksum digest is invalid"))?;
        if path <= prior {
            return Err(Error::archive("archive checksum paths are not sorted"));
        }
        prior = path;
        digests.insert(path.to_owned(), digest.to_owned());
    }
    if !digests.keys().map(String::as_str).eq(names
        .keys()
        .copied()
        .filter(|path| path.starts_with(PREFIX)))
    {
        return Err(Error::archive(
            "archive checksum manifest does not cover the project payload",
        ));
    }
    let project_path = "project/project.json";
    let handoff_path = "project/handoff/manifest.json";
    if !digests.contains_key(project_path) {
        return Err(Error::archive("archive project manifest is missing"));
    }
    if !digests.contains_key(handoff_path) {
        return Err(Error::archive("archive handoff manifest is missing"));
    }
    let (mut project, mut handoff) = (Value::Null, Value::Null);
    for (path, sha) in &digests {
        let member = names[path.as_str()];
        let limit = if matches!(
            path.as_str(),
            "project/project.json" | "project/handoff/manifest.json"
        ) {
            contract::MAX_JSON_BYTES as u64
        } else {
            zip::MAX_MEMBER
        };
        let bytes = zip::read(source, member, limit)?;
        if bytes_hash(&bytes) != *sha {
            return Err(Error::archive(format!(
                "archive checksum mismatch for {path}"
            )));
        }
        match path.as_str() {
            "project/project.json" => project = read_json(&bytes, "project manifest")?,
            "project/handoff/manifest.json" => handoff = read_json(&bytes, "handoff manifest")?,
            _ => {}
        }
    }
    if project["project_id"] != id {
        return Err(Error::archive(
            "archive project ID does not match the project manifest identity",
        ));
    }
    if !handoff.is_object() {
        return Err(Error::archive("archive handoff manifest must be an object"));
    }
    validation::handoff_contract(&handoff)
        .map_err(|_| Error::archive("archive handoff manifest is invalid"))?;
    if handoff["project_id"] != id
        || project["handoff"]["manifest_path"] != "handoff/manifest.json"
        || project["handoff"]["locked_scope_sha256"] != handoff["locked_scope_sha256"]
    {
        return Err(Error::archive(
            "archive project and handoff identities do not match",
        ));
    }
    Ok(Verified {
        id,
        members,
        digests,
    })
}

fn read_json(bytes: &[u8], name: &str) -> Result<Value> {
    let invalid = || Error::archive(format!("archive {name} is not valid JSON"));
    let text = std::str::from_utf8(bytes).map_err(|_| invalid())?;
    manifest::parse_document(text, name).map_err(|_| invalid())
}

fn valid_project(root: &File, detail: &str) -> Result<()> {
    let issues =
        validation::inspect_locked(root, Stage::Storyboard).map_err(|_| Error::archive(detail))?;
    if !items(&issues).is_empty() {
        return Err(Error::archive(detail));
    }
    Ok(())
}

fn extract(source: &mut File, archive: &Verified, root: &File) -> Result<()> {
    for member in &archive.members {
        let Some(expected) = archive.digests.get(&member.name) else {
            continue;
        };
        let path = &member.name[PREFIX.len()..];
        if protected(path) {
            return Err(Error::archive(
                "archive contains internal project lock or recovery data",
            ));
        }
        let bytes = zip::read(source, member, zip::MAX_MEMBER)?;
        if *expected != bytes_hash(&bytes) {
            return Err(Error::archive("archive checksum changed during extraction"));
        }
        let leaf = project_fs::relative_path(path).map_err(extraction)?;
        let (parent, name) = project_fs::target(root, &leaf, true)
            .map_err(extraction)?
            .ok_or_else(|| Error::archive("archive extraction target is unavailable"))?;
        let name = Path::new(name);
        if project_fs::entry_exists(&parent, name)
            .map_err(|_| Error::archive("archive cannot inspect its extraction destination"))?
        {
            return Err(Error::archive("archive extraction target already exists"));
        }
        let mut stream = project_fs::open_file(&parent, name, true, true).map_err(extraction)?;
        stream
            .write_all(&bytes)
            .map_err(|_| Error::archive("archive extraction failed"))?;
        public_file(&stream)?;
        stream
            .sync_all()
            .map_err(|_| Error::archive("archive extracted member could not be synchronized"))?;
        project_fs::sync_directory(&parent).map_err(|_| {
            Error::archive("archive extraction directory could not be synchronized")
        })?;
    }
    valid_project(root, "archive project validation failed")
}

fn protected(path: &str) -> bool {
    let path = caseless::default_case_fold_str(path);
    path == LOCK_NAME || path == "logs/transactions" || path.starts_with("logs/transactions/")
}

fn public_file(file: &File) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o644))
            .map_err(|_| Error::archive("archive file mode could not be set"))?;
    }
    #[cfg(not(unix))]
    let _ = file;
    Ok(())
}

fn extraction(_error: Error) -> Error {
    Error::archive("archive extraction or staging failed")
}

struct ProjectMember {
    size: u64,
    digest: String,
}

fn collect(root: &File) -> Result<BTreeMap<String, ProjectMember>> {
    let mut stack = vec![(
        String::new(),
        root.try_clone()
            .map_err(|_| Error::archive("handoff project cannot be scanned"))?,
    )];
    let mut budget = zip::MAX_MEMBERS;
    let mut total = 0;
    let mut files = BTreeMap::new();
    while let Some((prefix, directory)) = stack.pop() {
        let names = project_fs::names_bounded(&directory, budget).map_err(|_| {
            Error::archive("archive member count exceeds the limit during source traversal")
        })?;
        budget -= names.len();
        for name in names {
            let leaf = name
                .to_str()
                .ok_or_else(|| Error::archive("archive contains an unsafe member path"))?;
            let path = if prefix.is_empty() {
                leaf.to_owned()
            } else {
                format!("{prefix}/{leaf}")
            };
            if path == "logs/transactions" {
                continue;
            }
            let meta = project_fs::metadata(&directory, Path::new(leaf))
                .map_err(|_| Error::archive("handoff project contains a symlink or reparse point"))?
                .ok_or_else(|| Error::archive("handoff project changed during export"))?;
            if meta.is_dir() {
                if path.split('/').count() > MAX_DEPTH {
                    return Err(Error::archive(
                        "archive source path nesting exceeds the limit",
                    ));
                }
                let child =
                    project_fs::open_dir(&directory, Path::new(leaf)).map_err(extraction)?;
                stack.push((path, child));
            } else if !meta.is_file() {
                return Err(Error::archive(
                    "handoff project contains a non-regular file",
                ));
            } else if !protected(&path) {
                member_name(&format!("{PREFIX}{path}"))?;
                if files.len() + 3 > zip::MAX_MEMBERS || meta.len() > zip::MAX_MEMBER {
                    return Err(Error::archive("archive member exceeds the member limit"));
                }
                total += meta.len();
                if total > zip::MAX_TOTAL {
                    return Err(Error::archive(
                        "archive aggregate uncompressed size exceeds the limit",
                    ));
                }
                let payload = read_project_file(root, &path)?;
                if payload.len() as u64 != meta.len() {
                    return Err(Error::archive("handoff project changed during export"));
                }
                files.insert(
                    path,
                    ProjectMember {
                        size: payload.len() as u64,
                        digest: bytes_hash(&payload),
                    },
                );
            }
        }
    }
    if !files.contains_key("handoff/manifest.json") {
        return Err(Error::archive(
            "handoff project is missing its handoff manifest",
        ));
    }
    Ok(files)
}

fn read_project_file(root: &File, path: &str) -> Result<Vec<u8>> {
    let (parent, name) = validation::artifact(root, path)
        .map_err(extraction)?
        .ok_or_else(|| Error::archive("handoff project changed during export"))?;
    let file = project_fs::open_file(&parent, &name, false, false).map_err(extraction)?;
    let mut bytes = Vec::new();
    file.take(zip::MAX_MEMBER + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::archive("handoff project cannot be read"))?;
    if bytes.len() as u64 > zip::MAX_MEMBER {
        return Err(Error::archive("archive member exceeds the member limit"));
    }
    Ok(bytes)
}
