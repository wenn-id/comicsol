use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::cli::Arguments;
use crate::error::{Error, Result};
use crate::project_fs::{self, StagedDirectory};
use crate::validation::{bytes_hash, is_sha256, keys};
use crate::{init, manifest};

const BUNDLE: &[(&str, &[u8])] = include!(concat!(env!("OUT_DIR"), "/skill.rs"));
const MARKER: &str = ".comic-sol-install.json";
const MAX_MARKER: u64 = 64 * 1024;
const MAX_FILE: u64 = 32 * 1024 * 1024;
const MAX_TOTAL: u64 = 256 * 1024 * 1024;
const MAX_MANAGED: usize = 512;
const MAX_INSTALLED: usize = 4096;
const MAX_DEPTH: usize = 128;
type Hashes = BTreeMap<String, String>;

struct Placement<'a> {
    target: &'a str,
    scope: &'a str,
    path: PathBuf,
}

impl Placement<'_> {
    fn report(&self, status: &str, message: &str, digest: Option<&str>, paths: usize) -> Value {
        json!({
            "target": self.target, "scope": self.scope, "destination": self.path.to_string_lossy(),
            "status": status, "message": message, "bundle_digest": digest, "managed_paths": paths,
        })
    }
}

pub fn run(args: &Arguments) -> Result<Value> {
    let scope = args.text("scope").expect("required skill scope");
    let project = args.path("project_root");
    let home = lexical(
        &env::home_dir().ok_or_else(|| unsafe_path("home cannot be resolved"))?,
        "home",
    )?;
    let codex = env::var_os("CODEX_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"));
    let codex = lexical(&codex, "CODEX_HOME")?;
    let requested = args.text("target").expect("required skill target");
    let placement = if requested == "auto" {
        let names: &[&str] = if scope == "user" {
            &["claude", "codex", "zcode"]
        } else {
            &["antigravity", "claude"]
        };
        let detected = names
            .iter()
            .map(|name| {
                let location = placement(name, scope, project, &home, &codex)?;
                let host = location.path.ancestors().nth(2).expect("skill host");
                Ok(host.is_dir().then_some(location))
            })
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
        if detected.is_empty() {
            return Err(Error::usage(format!(
                "No supported host location was detected. Create or select one candidate with --target: {}",
                names.join(", ")
            )));
        }
        if detected.len() > 1 {
            let hosts: Vec<_> = detected.iter().map(|location| location.target).collect();
            return Err(Error::usage(format!(
                "Detected multiple supported hosts: {} Choose one explicitly with --target.",
                hosts.join(", ")
            )));
        }
        detected.into_iter().next().expect("single detected client")
    } else {
        placement(requested, scope, project, &home, &codex)?
    };
    if args.command == "skill-install" {
        install(&placement)
    } else {
        uninstall(&placement)
    }
}

fn placement<'a>(
    target: &'a str,
    scope: &'a str,
    project: Option<&Path>,
    home: &Path,
    codex: &Path,
) -> Result<Placement<'a>> {
    let host = match (target, scope) {
        ("codex", "user") => codex.to_owned(),
        ("claude", "user") => home.join(".claude"),
        ("zcode", "user") => home.join(".zcode"),
        ("claude" | "antigravity", "project") => {
            let root = project
                .ok_or_else(|| Error::usage("--scope project requires --project-root".into()))?;
            let root = lexical(&init::expand(root), "project root")?;
            if !root.is_dir() {
                return Err(unsafe_path("project root must be an existing directory"));
            }
            root.join(if target == "claude" {
                ".claude"
            } else {
                ".agents"
            })
        }
        _ => {
            let available = match target {
                "codex" | "zcode" => "user",
                "antigravity" => "project",
                _ => "user or project",
            };
            return Err(Error::usage(format!(
                "{target} does not support {scope} Skill scope. Supported placement: {available}."
            )));
        }
    };
    if scope == "user" && project.is_some() {
        return Err(Error::usage(
            "--project-root is valid only with --scope project".into(),
        ));
    }
    if scope == "user" && target != "codex" && !home.is_dir() {
        return Err(unsafe_path("home must be an existing directory"));
    }
    let path = host.join("skills/comic-sol");
    no_links(&path)?;
    Ok(Placement {
        target,
        scope,
        path,
    })
}

fn lexical(path: &Path, label: &str) -> Result<PathBuf> {
    if path.as_os_str().is_empty() || path.to_string_lossy().chars().any(char::is_control) {
        return Err(unsafe_path(&format!(
            "{label} contains a control character"
        )));
    }
    if path.components().any(|part| part == Component::ParentDir) {
        return Err(unsafe_path(&format!(
            "{label} contains traversal or a path alias"
        )));
    }
    Ok(std::path::absolute(path)?)
}

fn no_links(path: &Path) -> Result<()> {
    let mut checked = PathBuf::new();
    for component in path.components() {
        checked.push(component);
        if !matches!(component, Component::Normal(_)) {
            continue;
        }
        match fs::symlink_metadata(&checked) {
            Ok(meta) => {
                #[cfg(windows)]
                let reparse = {
                    use std::os::windows::fs::MetadataExt;
                    meta.file_attributes() & 0x400 != 0
                };
                #[cfg(not(windows))]
                let reparse = false;
                if meta.is_symlink() || reparse {
                    return Err(unsafe_path(
                        "a path component is a symlink or reparse point",
                    ));
                }
            }
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

fn unsafe_path(detail: &str) -> Error {
    let message = format!("security-error: unsafe Skill destination: {detail}");
    Error::boundary(&message)
}

fn target(path: &Path, create: bool) -> Result<Option<(File, std::ffi::OsString)>> {
    let folder = path.parent().expect("skill target parent");
    if !create && !folder.exists() {
        return Ok(None);
    }
    project_fs::external_target_absolute(path, None, create)
        .map(Some)
        .map_err(|error| {
            if error.code() == "CS-SEC-001" {
                unsafe_path("a path component is a symlink or reparse point")
            } else {
                error
            }
        })
}

fn install(placement: &Placement<'_>) -> Result<Value> {
    let managed: Hashes = BUNDLE
        .iter()
        .map(|(path, data)| ((*path).to_owned(), bytes_hash(data)))
        .collect();
    let digest = aggregate(&managed);
    let (parent, name) = target(&placement.path, true)?.expect("skill parent");
    let name = Path::new(&name);
    let existing = existing_dir(&parent, name)?;
    let mut retained = BTreeSet::new();
    let mut upgrading = false;
    let snapshot = if let Some(root) = &existing {
        let files = inventory(root)?;
        if let Some(old) = read_marker(root, &files)? {
            if old.target != placement.target || old.scope != placement.scope {
                return Err(Error::invalid(
                    "existing Comic Sol Skill marker targets another host or scope; no files were changed",
                ));
            }
            let actual: Hashes = files
                .hashes
                .iter()
                .filter(|(path, _)| !unmanaged(path))
                .map(|(path, hash)| (path.clone(), hash.clone()))
                .collect();
            if old.digest == digest {
                if old.managed != managed
                    || managed
                        .iter()
                        .any(|(path, hash)| actual.get(path) != Some(hash))
                {
                    return Err(Error::skill_verification(
                        "existing Skill claims this digest but its managed files do not match",
                    ));
                }
                return Ok(placement.report(
                    "unchanged",
                    "Skill already matches the packaged bundle.",
                    Some(&digest),
                    managed.len(),
                ));
            }
            if actual != old.managed {
                return Err(Error::skill_verification(
                    "existing managed Skill files were modified or contain unknown files; upgrade refused",
                ));
            }
            upgrading = true;
        } else if files.files.is_empty()
            || !files
                .files
                .iter()
                .all(|path| path.ends_with(".pyc") || path.ends_with(".pyo"))
            || parent_directories(&files.files) != files.directories
        {
            return Err(Error::invalid(
                "Comic Sol Skill marker is missing; no files were changed",
            ));
        }
        retained.extend(files.files.iter().filter(|path| unmanaged(path)).cloned());
        Some(files)
    } else {
        None
    };
    let stage = StagedDirectory::create(&parent, "skill-stage")?;
    for (path, bytes) in BUNDLE {
        write_file(stage.file(), path, bytes, true)?;
    }
    if let Some(root) = &existing {
        copy_files(
            root,
            stage.file(),
            &retained,
            &snapshot.as_ref().expect("existing inventory").hashes,
        )?;
    }
    let marker = json!({
        "bundle_digest": digest, "managed_paths": managed, "scope": placement.scope,
        "target": placement.target, "version": env!("COMIC_SOL_VERSION"),
    });
    let mut marker = manifest::compact(&marker);
    marker.push(b'\n');
    if marker.len() as u64 > MAX_MARKER {
        return Err(Error::skill_verification(
            "Skill installation marker exceeds its size limit",
        ));
    }
    write_file(stage.file(), MARKER, &marker, false)?;
    let staged_members = inventory(stage.file())?;
    if collect_hashes(
        stage.file(),
        staged_members.files.iter().filter(|path| !unmanaged(path)),
    )? != managed
        || read_file(stage.file(), MARKER, MAX_MARKER)? != marker
    {
        return Err(Error::skill_verification(
            "staged Skill payload digest verification failed",
        ));
    }
    if let Some(root) = existing {
        let snapshot = snapshot.expect("existing skill inventory");
        stage.replace(name, root, |root| confirm_inventory(root, snapshot))?;
    } else {
        stage.publish(name)?;
    }
    let (status, message) = if upgrading {
        ("upgraded", "Skill bundle upgraded transactionally.")
    } else {
        ("installed", "Skill installed.")
    };
    Ok(placement.report(status, message, Some(&digest), managed.len()))
}

fn uninstall(placement: &Placement<'_>) -> Result<Value> {
    let absent = || {
        placement.report(
            "not-installed",
            "No managed Comic Sol Skill installation was found.",
            None,
            0,
        )
    };
    let Some((parent, name)) = target(&placement.path, false)? else {
        return Ok(absent());
    };
    let name = Path::new(&name);
    let Some(existing) = existing_dir(&parent, name)? else {
        return Ok(absent());
    };
    let files = inventory(&existing)?;
    let Some(marker) = read_marker(&existing, &files)? else {
        return Ok(placement.report(
            "not-installed",
            "No managed Comic Sol Skill installation was found; existing files were preserved.",
            None,
            0,
        ));
    };
    if marker.target != placement.target || marker.scope != placement.scope {
        return Err(Error::invalid(
            "Comic Sol Skill marker does not match the requested host and scope; no files were changed",
        ));
    }
    let mut survivors = BTreeSet::new();
    for path in &files.files {
        let remove = if let Some(digest) = marker.managed.get(path) {
            files.hashes.get(path) == Some(digest)
        } else {
            false
        };
        if !remove {
            survivors.insert(path.clone());
        }
    }
    let stage = StagedDirectory::create(&parent, "skill-uninstall")?;
    copy_files(&existing, stage.file(), &survivors, &files.hashes)?;
    let (status, message) = if survivors.is_empty() {
        stage.detach(name, existing, |root| confirm_inventory(root, files))?;
        ("uninstalled", "Managed Comic Sol Skill files were removed.")
    } else {
        stage.replace(name, existing, |root| confirm_inventory(root, files))?;
        (
            "preserved",
            "Managed unchanged files were removed; modified or unrelated files were preserved.",
        )
    };
    Ok(placement.report(status, message, Some(&marker.digest), marker.managed.len()))
}

fn existing_dir(parent: &File, name: &Path) -> Result<Option<File>> {
    let meta = project_fs::metadata(parent, name)
        .map_err(|_| unsafe_path("an existing destination member is a link"))?;
    if let Some(meta) = meta {
        if !meta.is_dir() {
            return Err(unsafe_path("destination must be a directory"));
        }
        Ok(Some(project_fs::open_dir(parent, name)?))
    } else {
        Ok(None)
    }
}

#[derive(Default, PartialEq, Eq)]
struct Inventory {
    has_marker: bool,
    marker_hash: Option<String>,
    files: BTreeSet<String>,
    hashes: Hashes,
    directories: BTreeSet<String>,
}

fn inventory(root: &File) -> Result<Inventory> {
    let mut pending = vec![(String::new(), root.try_clone()?)];
    let mut out = Inventory::default();
    let mut remaining = MAX_INSTALLED;
    let mut total = 0;
    while let Some((prefix, dir)) = pending.pop() {
        let names = project_fs::names_bounded(&dir, remaining)
            .map_err(|_| unsafe_path("installed payload contains too many members"))?;
        remaining -= names.len();
        for name in names {
            let name = name.to_str().ok_or_else(|| {
                unsafe_path("managed paths must be non-empty portable relative paths")
            })?;
            let relative = if prefix.is_empty() {
                name.to_owned()
            } else {
                format!("{prefix}/{name}")
            };
            validate_path(&relative)?;
            let leaf = Path::new(name);
            let meta = project_fs::metadata(&dir, leaf)
                .map_err(|_| unsafe_path("an existing destination member is a link"))?
                .ok_or_else(|| {
                    Error::skill_verification("installed Skill changed during verification")
                })?;
            if meta.is_dir() {
                pending.push((relative.clone(), project_fs::open_dir(&dir, leaf)?));
                out.directories.insert(relative);
            } else if meta.is_file() {
                total += meta.len();
                if meta.len() > MAX_FILE || total > MAX_TOTAL {
                    return Err(unsafe_path("the installed Skill exceeds the size limit"));
                }
                if relative == MARKER {
                    out.has_marker = true;
                    out.marker_hash = Some(bytes_hash(&read_file(root, MARKER, MAX_MARKER)?));
                } else {
                    out.hashes.insert(
                        relative.clone(),
                        bytes_hash(&read_file(root, &relative, MAX_FILE)?),
                    );
                    out.files.insert(relative);
                }
            } else {
                return Err(unsafe_path("an existing destination member is not regular"));
            }
        }
    }
    Ok(out)
}

fn validate_path(path: &str) -> Result<()> {
    if path.len() > 4096
        || path.split('/').count() > MAX_DEPTH
        || path.chars().any(char::is_control)
    {
        return Err(unsafe_path(
            "managed paths must be non-empty portable relative paths",
        ));
    }
    project_fs::relative_path(path)
        .map_err(|_| unsafe_path("managed paths must not contain aliases or traversal"))?;
    if path.split('/').any(|part| matches!(part, "" | "." | "..")) {
        return Err(unsafe_path(
            "managed paths must use canonical POSIX separators",
        ));
    }
    Ok(())
}

fn confirm_inventory(root: &File, original: Inventory) -> Result<()> {
    if inventory(root)? != original {
        return Err(Error::skill_verification(
            "installed Skill changed during verification",
        ));
    }
    Ok(())
}

fn parent_directories(files: &BTreeSet<String>) -> BTreeSet<String> {
    files
        .iter()
        .flat_map(|path| {
            path.match_indices('/')
                .map(|(index, _)| path[..index].to_owned())
        })
        .collect()
}

fn unmanaged(path: &str) -> bool {
    path.split('/').any(|part| part == "__pycache__")
        || path.ends_with(".pyc")
        || path.ends_with(".pyo")
}

fn aggregate(hashes: &Hashes) -> String {
    let mut digest = Sha256::new();
    for (path, hash) in hashes {
        digest.update(path.as_bytes());
        digest.update(b"\0");
        for bytes in hash.as_bytes().chunks_exact(2) {
            let value = u8::from_str_radix(std::str::from_utf8(bytes).expect("digest digits"), 16)
                .expect("hex byte");
            digest.update([value]);
        }
    }
    format!("{:x}", digest.finalize())
}

struct Marker {
    target: String,
    scope: String,
    digest: String,
    managed: Hashes,
}

fn read_marker(root: &File, files: &Inventory) -> Result<Option<Marker>> {
    if !files.has_marker {
        return Ok(None);
    }
    let bytes = read_file(root, MARKER, MAX_MARKER)?;
    let value = std::str::from_utf8(&bytes)
        .ok()
        .and_then(|text| manifest::parse_document(text, MARKER).ok())
        .ok_or_else(|| {
            Error::invalid("Comic Sol Skill marker is malformed; no files were changed")
        })?;
    if !keys(
        &value,
        &[
            "target",
            "scope",
            "version",
            "bundle_digest",
            "managed_paths",
        ],
    ) {
        return Err(Error::invalid(
            "Comic Sol Skill marker has unexpected fields; no files were changed",
        ));
    }
    let invalid =
        || Error::invalid("Comic Sol Skill marker values are invalid; no files were changed");
    let target = value["target"].as_str().ok_or_else(invalid)?;
    let scope = value["scope"].as_str().ok_or_else(invalid)?;
    if !matches!(
        (target, scope),
        ("codex" | "claude" | "zcode", "user") | ("claude" | "antigravity", "project")
    ) || !value["version"]
        .as_str()
        .is_some_and(|version| !version.is_empty() && !version.chars().any(char::is_control))
    {
        return Err(invalid());
    }
    let digest = value["bundle_digest"]
        .as_str()
        .filter(|text| is_sha256(text))
        .ok_or_else(invalid)?;
    let entries = value["managed_paths"]
        .as_object()
        .filter(|entries| !entries.is_empty() && entries.len() <= MAX_MANAGED)
        .ok_or_else(invalid)?;
    let mut managed = Hashes::new();
    for (path, hash) in entries {
        validate_path(path)?;
        let hash = hash
            .as_str()
            .filter(|hash| is_sha256(hash))
            .ok_or_else(|| {
                Error::invalid("Comic Sol Skill marker paths are invalid; no files were changed")
            })?;
        if unmanaged(path) || path == MARKER {
            return Err(Error::invalid(
                "Comic Sol Skill marker claims environment-generated files; no files were changed",
            ));
        }
        managed.insert(path.clone(), hash.to_owned());
    }
    if aggregate(&managed) != digest {
        return Err(Error::invalid(
            "Comic Sol Skill marker digest is invalid; no files were changed",
        ));
    }
    Ok(Some(Marker {
        target: target.into(),
        scope: scope.into(),
        digest: digest.into(),
        managed,
    }))
}

fn read_file(root: &File, path: &str, max: u64) -> Result<Vec<u8>> {
    let (parent, name) = project_fs::target(root, Path::new(path), false)?
        .ok_or_else(|| Error::skill_verification("installed Skill member has disappeared"))?;
    let stream = project_fs::open_file(&parent, Path::new(name), false, false)?;
    let mut bytes = Vec::new();
    stream.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > max {
        return Err(unsafe_path("a Skill file exceeds its size limit"));
    }
    Ok(bytes)
}

fn write_file(root: &File, relative: &str, data: &[u8], public: bool) -> Result<()> {
    let path = Path::new(relative);
    let (parent, name) = project_fs::target(root, path, true)?.expect("skill member directory");
    let mut stream = project_fs::open_file(&parent, Path::new(name), true, true)?;
    stream.write_all(data)?;
    #[cfg(unix)]
    if public {
        use std::os::unix::fs::PermissionsExt;
        stream.set_permissions(fs::Permissions::from_mode(0o644))?;
    }
    #[cfg(not(unix))]
    let _ = public;
    stream.sync_all()?;
    project_fs::sync_directory(&parent)?;
    Ok(())
}

fn collect_hashes<'a>(root: &File, paths: impl Iterator<Item = &'a String>) -> Result<Hashes> {
    paths
        .map(|path| Ok((path.clone(), bytes_hash(&read_file(root, path, MAX_FILE)?))))
        .collect()
}

fn copy_files(
    source: &File,
    target: &File,
    files: &BTreeSet<String>,
    hashes: &Hashes,
) -> Result<()> {
    for name in files {
        let bytes = read_file(source, name, MAX_FILE)?;
        if hashes
            .get(name)
            .is_none_or(|hash| *hash != bytes_hash(&bytes))
        {
            return Err(Error::skill_verification(
                "installed Skill changed during verification",
            ));
        }
        write_file(target, name, &bytes, true)?;
    }
    Ok(())
}
