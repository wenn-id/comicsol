use std::ffi::{OsStr, OsString};
#[cfg(unix)]
use std::fs::TryLockError;
use std::fs::{self, File};
use std::io::{self, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use cap_fs_ext::{OpenOptionsFollowExt, OpenOptionsSyncExt};
use cap_primitives::ambient_authority;
use cap_primitives::fs::{self as cap, FollowSymlinks, Metadata, OpenOptions};

#[cfg(windows)]
use crate::error::contract::LOCK_BYTE_OFFSET;
use crate::error::{Error, Result};

pub const LOCK_NAME: &str = ".comic-sol.lock";
const LINK_DETAIL: &str = "project path must not contain symlinks or reparse points";

pub struct LockedProject {
    root: File,
    _lock: ProjectLock,
}

impl LockedProject {
    pub fn open(path: &Path, require_manifest: bool) -> Result<Self> {
        Self::open_with_timeout(path, require_manifest, Duration::from_secs(10))
    }

    pub fn open_with_timeout(
        path: &Path,
        require_manifest: bool,
        timeout: Duration,
    ) -> Result<Self> {
        let root = project_root(path)?;
        Self::from_root(root, require_manifest, timeout)
    }

    pub fn from_root(root: File, require_manifest: bool, timeout: Duration) -> Result<Self> {
        if require_manifest {
            metadata(&root, Path::new("project.json"))?
                .ok_or_else(|| io::Error::from(io::ErrorKind::NotFound))?;
        }
        let lock = ProjectLock::acquire(&root, timeout)?;
        crate::recovery::recover(&root, &lock)?;
        Ok(Self { root, _lock: lock })
    }

    pub fn root(&self) -> &File {
        &self.root
    }
}

pub fn project_root(path: &Path) -> Result<File> {
    let path = if path.as_os_str().is_empty() {
        Path::new(".")
    } else {
        path
    };
    let canonical = fs::canonicalize(path)?;
    let mut volume = PathBuf::new();
    let mut parts = canonical.components().peekable();
    while let Some(Component::Prefix(_) | Component::RootDir) = parts.peek() {
        volume.push(parts.next().expect("root component").as_os_str());
    }
    let mut dir = cap::open_ambient_dir(&volume, ambient_authority())?;
    for part in parts {
        dir = open_dir(&dir, Path::new(part.as_os_str()))?;
    }
    Ok(dir)
}

pub fn external_target(path: &Path) -> Result<(File, OsString)> {
    let absolute = std::path::absolute(path)?;
    external_target_absolute(&absolute, None, false)
}

pub fn external_target_absolute(
    absolute: &Path,
    excluded: Option<&File>,
    create_parents: bool,
) -> Result<(File, OsString)> {
    let mut volume = PathBuf::new();
    let mut parts = absolute.components().peekable();
    while let Some(Component::Prefix(_) | Component::RootDir) = parts.peek() {
        volume.push(parts.next().expect("volume component").as_os_str());
    }
    let mut dir = cap::open_ambient_dir(&volume, ambient_authority())?;
    while let Some(Component::Normal(part)) = parts.next() {
        if let Some(excluded) = excluded
            && same_directory(&dir, excluded)?
        {
            return Err(Error::invalid("output must be outside the project"));
        }
        if parts.peek().is_none() {
            return Ok((dir, part.to_owned()));
        }
        let name = Path::new(part);
        if create_parents && metadata(&dir, name)?.is_none() {
            match cap::create_dir(&dir, name, &cap::DirOptions::new()) {
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                result => result?,
            }
        }
        dir = open_dir(&dir, name)?;
    }
    Err(Error::invalid("file path must name a regular file"))
}

#[cfg(unix)]
pub fn same_directory(left: &File, right: &File) -> io::Result<bool> {
    use std::os::unix::fs::MetadataExt;
    let left = left.metadata()?;
    let right = right.metadata()?;
    Ok((left.dev(), left.ino()) == (right.dev(), right.ino()))
}

#[cfg(windows)]
pub fn same_directory(left: &File, right: &File) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };

    fn identity(file: &File) -> io::Result<(u32, u32, u32)> {
        let mut info = std::mem::MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::uninit();
        // The handle stays open and Windows fills the structure on success.
        if unsafe { GetFileInformationByHandle(file.as_raw_handle(), info.as_mut_ptr()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let info = unsafe { info.assume_init() };
        Ok((
            info.dwVolumeSerialNumber,
            info.nFileIndexHigh,
            info.nFileIndexLow,
        ))
    }

    Ok(identity(left)? == identity(right)?)
}

pub fn write_external(path: &Path, project: &Path, bytes: &[u8]) -> Result<()> {
    let project = project_root(project)?;
    let output = std::path::absolute(path)?;
    if output.components().any(|part| part == Component::ParentDir) {
        return Err(Error::invalid(
            "output path must not contain parent directory components",
        ));
    }
    let (parent, name) = external_target_absolute(&output, Some(&project), true)?;
    let name = Path::new(&name);
    if metadata(&parent, name)?.is_some_and(|value| !value.is_file()) {
        if let Ok(existing) = open_dir(&parent, name)
            && same_directory(&existing, &project)?
        {
            return Err(Error::invalid("output must be outside the project"));
        }
        return Err(Error::invalid("output must be a regular file"));
    }
    let stage = StagedDirectory::create(&parent, "output")?;
    let payload = Path::new("payload");
    let mut stream = open_file(stage.file(), payload, true, true)?;
    stream.write_all(bytes)?;
    stream.sync_all()?;
    drop(stream);
    cap::rename(stage.file(), payload, &parent, name)?;
    sync_directory(&parent)?;
    Ok(())
}

pub fn publish_noreplace(
    from: &File,
    name: &Path,
    to: &File,
    destination: &Path,
) -> io::Result<()> {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    return rustix::fs::renameat_with(
        from,
        name,
        to,
        destination,
        rustix::fs::RenameFlags::NOREPLACE,
    )
    .map_err(Into::into);
    #[cfg(windows)]
    {
        // On Windows hard links provide an exclusive, anchored publication for files.
        let _ = metadata(from, name).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        if let Ok(dir) = open_dir(from, name) {
            return publish_directory_windows(from, name, to, destination, &dir);
        }
        cap::hard_link(from, name, to, destination)?;
        cap::remove_file(from, name)
    }
    #[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
    Err(io::Error::from(io::ErrorKind::Unsupported))
}

#[cfg(windows)]
fn publish_directory_windows(
    from: &File,
    name: &Path,
    to: &File,
    destination: &Path,
    _dir: &File,
) -> io::Result<()> {
    if entry_exists(to, destination)? {
        return Err(io::Error::from(io::ErrorKind::AlreadyExists));
    }
    // The platform rename refuses to replace an existing directory.
    cap::rename(from, name, to, destination)
}

pub struct StagedDirectory<'a> {
    parent: &'a File,
    name: PathBuf,
    directory: Option<File>,
}

impl<'a> StagedDirectory<'a> {
    pub fn create(parent: &'a File, prefix: &str) -> Result<Self> {
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        #[cfg(unix)]
        let options = {
            let mut opts = cap::DirOptions::new();
            use cap_primitives::fs::DirBuilderExt;
            opts.mode(0o700);
            opts
        };
        #[cfg(not(unix))]
        let options = cap::DirOptions::new();
        loop {
            let ticket = SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let now = time::OffsetDateTime::now_utc().unix_timestamp_nanos();
            let name = PathBuf::from(format!(
                ".comic-sol-{prefix}-{}-{now:x}-{ticket:x}.tmp",
                std::process::id()
            ));
            match cap::create_dir(parent, &name, &options) {
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                result => result?,
            }
            let directory = Some(open_dir(parent, &name)?);
            return Ok(Self {
                parent,
                name,
                directory,
            });
        }
    }

    pub fn file(&self) -> &File {
        self.directory.as_ref().expect("unpublished directory")
    }

    pub fn entry_name(&self) -> &Path {
        &self.name
    }

    pub fn replace(
        mut self,
        destination: &Path,
        current: File,
        verify: impl FnOnce(&File) -> Result<()>,
    ) -> Result<()> {
        self.check_identity(&self.name, self.file())?;
        self.check_identity(destination, &current)?;
        sync_directory(self.file())?;
        exchange(self.parent, &self.name, destination)?;
        if let Err(error) = self
            .check_identity(&self.name, &current)
            .and_then(|()| verify(&current))
            .and_then(|()| sync_directory(self.parent).map_err(Into::into))
        {
            if let Err(rollback) = exchange(self.parent, &self.name, destination) {
                self.directory = None;
                return Err(rollback.into());
            }
            return Err(error);
        }
        self.directory = Some(current);
        Ok(())
    }

    pub fn detach(
        mut self,
        destination: &Path,
        current: File,
        verify: impl FnOnce(&File) -> Result<()>,
    ) -> Result<()> {
        self.check_identity(destination, &current)?;
        let retired = Path::new("retired");
        publish_noreplace(self.parent, destination, self.file(), retired)?;
        let confirm = || -> Result<()> {
            let moved = open_dir(self.file(), retired)?;
            if !same_directory(&moved, &current)? {
                return Err(Error::boundary("removed directory identity changed"));
            }
            verify(&moved)?;
            Ok(sync_directory(self.parent)?)
        };
        if let Err(error) = confirm() {
            if let Err(rollback) = publish_noreplace(self.file(), retired, self.parent, destination)
            {
                self.directory = None;
                return Err(rollback.into());
            }
            return Err(error);
        }
        Ok(())
    }

    fn check_identity(&self, name: &Path, expected: &File) -> Result<()> {
        let actual = open_dir(self.parent, name)?;
        if !same_directory(&actual, expected)? {
            return Err(Error::boundary("staging directory identity changed"));
        }
        Ok(())
    }

    pub fn publish(mut self, destination: &Path) -> Result<()> {
        sync_directory(self.file())?;
        self.check_identity(&self.name, self.file())?;
        publish_noreplace(self.parent, &self.name, self.parent, destination)?;
        self.directory = None;
        sync_directory(self.parent)?;
        Ok(())
    }
}

#[cfg(target_os = "linux")]
pub fn exchange(parent: &File, from: &Path, to: &Path) -> io::Result<()> {
    rustix::fs::renameat_with(parent, from, parent, to, rustix::fs::RenameFlags::EXCHANGE)
        .map_err(Into::into)
}

#[cfg(target_os = "macos")]
pub fn exchange(parent: &File, from: &Path, to: &Path) -> io::Result<()> {
    rustix::fs::renameat_with(parent, from, parent, to, rustix::fs::RenameFlags::SWAP)
        .map_err(Into::into)
}

#[cfg(windows)]
pub fn exchange(parent: &File, from: &Path, to: &Path) -> io::Result<()> {
    let temporary = StagedDirectory::create(parent, "swap")
        .map_err(|_| io::Error::from(io::ErrorKind::PermissionDenied))?;
    let retired = Path::new("retired");
    publish_noreplace(parent, to, temporary.file(), retired)?;
    if let Err(error) = publish_noreplace(parent, from, parent, to) {
        if let Err(rollback) = publish_noreplace(temporary.file(), retired, parent, to) {
            std::mem::forget(temporary);
            return Err(rollback);
        }
        return Err(error);
    }
    if let Err(error) = publish_noreplace(temporary.file(), retired, parent, from) {
        let rollback = publish_noreplace(parent, to, parent, from)
            .and_then(|_| publish_noreplace(temporary.file(), retired, parent, to));
        if rollback.is_err() {
            std::mem::forget(temporary);
        }
        return rollback.and(Err(error));
    }
    Ok(())
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
pub fn exchange(_parent: &File, _from: &Path, _to: &Path) -> io::Result<()> {
    Err(io::Error::from(io::ErrorKind::Unsupported))
}

pub fn entry_exists(dir: &File, path: &Path) -> io::Result<bool> {
    match cap::stat(dir, path, FollowSymlinks::No) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

impl Drop for StagedDirectory<'_> {
    fn drop(&mut self) {
        if let Some(directory) = self.directory.take() {
            let _ = cap::remove_open_dir_all(directory);
        }
    }
}

pub fn relative_path(value: &str) -> Result<PathBuf> {
    let value = value.replace('\\', "/");
    let bytes = value.as_bytes();
    let invalid = || Error::invalid("path must be a relative project path");
    if bytes.is_empty()
        || value.starts_with('/')
        || value.contains('\0')
        || (bytes.get(1) == Some(&b':') && bytes[0].is_ascii_alphabetic())
        || value.split('/').any(|part| part == "..")
    {
        return Err(invalid());
    }
    let path = PathBuf::from(value);
    if !matches!(path.components().next(), Some(Component::Normal(_))) {
        return Err(invalid());
    }
    #[cfg(windows)]
    for component in path.components() {
        let name = component.as_os_str().to_string_lossy();
        let stem = name
            .split('.')
            .next()
            .unwrap_or_default()
            .trim_end()
            .to_ascii_lowercase();
        if name.ends_with([' ', '.'])
            || name.contains([':', '<', '>', '"', '|', '?', '*'])
            || name.chars().any(|ch| ch < ' ')
            || matches!(
                stem.as_str(),
                "con" | "prn" | "aux" | "nul" | "conin$" | "conout$"
            )
            || stem
                .strip_prefix("com")
                .or_else(|| stem.strip_prefix("lpt"))
                .is_some_and(|suffix| {
                    matches!(
                        suffix,
                        "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                    )
                })
        {
            return Err(invalid());
        }
    }
    Ok(path)
}

fn check_metadata(metadata: &Metadata) -> Result<()> {
    #[cfg(windows)]
    let reparse = {
        use cap_primitives::fs::MetadataExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
        metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    };
    #[cfg(not(windows))]
    let reparse = false;
    if reparse || metadata.is_symlink() {
        return Err(Error::boundary(LINK_DETAIL));
    }
    Ok(())
}

pub fn metadata(dir: &File, name: &Path) -> Result<Option<Metadata>> {
    match cap::stat(dir, name, FollowSymlinks::No) {
        Ok(value) => {
            check_metadata(&value)?;
            Ok(Some(value))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub fn open_dir(dir: &File, name: &Path) -> Result<File> {
    let _ = metadata(dir, name)?;
    let opened = cap::open_dir_nofollow(dir, name)?;
    check_metadata(&Metadata::from_file(&opened)?)?;
    Ok(opened)
}

pub fn target<'a>(
    root: &File,
    path: &'a Path,
    create_parents: bool,
) -> Result<Option<(File, &'a OsStr)>> {
    let mut dir = root.try_clone()?;
    let mut components = path.components().peekable();
    while let Some(Component::Normal(part)) = components.next() {
        if components.peek().is_none() {
            return Ok(Some((dir, part)));
        }
        let name = Path::new(part);
        if metadata(&dir, name)?.is_none() {
            if !create_parents {
                return Ok(None);
            }
            match cap::create_dir(&dir, name, &cap::DirOptions::new()) {
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                result => result?,
            }
        }
        dir = open_dir(&dir, name)?;
    }
    Err(Error::invalid("path must be a relative project path"))
}

pub fn open_file(dir: &File, name: &Path, write: bool, create: bool) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(write).create(create);
    options.follow(FollowSymlinks::No).nonblock(true);
    #[cfg(unix)]
    {
        use cap_primitives::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = cap::open(dir, name, &options)?;
    let metadata = Metadata::from_file(&file)?;
    check_metadata(&metadata)?;
    if !metadata.is_file() {
        return Err(Error::limit("the regular-file input requirement"));
    }
    Ok(file)
}

pub fn names(dir: &File) -> io::Result<Vec<OsString>> {
    cap::read_base_dir(dir)?
        .map(|entry| entry.map(|entry| entry.file_name()))
        .collect()
}

pub fn names_bounded(dir: &File, maximum: usize) -> io::Result<Vec<OsString>> {
    let items: Vec<_> = cap::read_base_dir(dir)?
        .take(maximum + 1)
        .map(|entry| entry.map(|entry| entry.file_name()))
        .collect::<io::Result<_>>()?;
    if items.len() > maximum {
        return Err(io::Error::from(io::ErrorKind::InvalidData));
    }
    Ok(items)
}

pub fn sync_directory(dir: &File) -> io::Result<()> {
    #[cfg(unix)]
    {
        use rustix::fs::{Mode, OFlags, fsync, openat};
        // Linux traversal handles can be O_PATH, which fsync does not accept.
        let readable = openat(dir, ".", OFlags::RDONLY | OFlags::DIRECTORY, Mode::empty())?;
        fsync(readable)?;
    }
    #[cfg(not(unix))]
    let _ = dir;
    Ok(())
}

pub struct ProjectLock(File, #[cfg(windows)] u32);

impl ProjectLock {
    pub fn read_only(root: &File) -> Result<Option<Self>> {
        let file = open_file(root, Path::new(LOCK_NAME), cfg!(windows), false)?;
        Ok(try_lock(&file, true)?.then(|| Self::held(file, true)))
    }

    pub fn acquire(root: &File, timeout: Duration) -> Result<Self> {
        Self::acquire_named(root, Path::new(LOCK_NAME), timeout, true)
    }

    pub fn config(root: &File, name: &Path) -> Result<Self> {
        Self::acquire_named(root, name, Duration::from_secs(10), false)
    }

    fn held(file: File, _project: bool) -> Self {
        Self(
            file,
            #[cfg(windows)]
            lock_offset(_project),
        )
    }

    fn acquire_named(root: &File, name: &Path, timeout: Duration, project: bool) -> Result<Self> {
        let _ = metadata(root, name)?;
        let mut file = open_file(root, name, true, true)?;
        #[cfg(unix)]
        if !project {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        #[cfg(windows)]
        if !project && file.metadata()?.len() == 0 {
            file.write_all(b"\0")?;
        }
        let deadline = Instant::now() + timeout;
        while !try_lock(&file, project)? {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(io::Error::from(io::ErrorKind::TimedOut).into());
            }
            thread::sleep(Duration::from_millis(50).min(remaining));
        }
        if project {
            file.seek(SeekFrom::Start(0))?;
            file.set_len(0)?;
            writeln!(file, "{}", std::process::id())?;
        }
        Ok(Self::held(file, project))
    }
}

impl Drop for ProjectLock {
    fn drop(&mut self) {
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::UnlockFile;
            // Use the same one-byte range as Python's msvcrt.locking.
            unsafe { UnlockFile(self.0.as_raw_handle(), self.1, 0, 1, 0) };
        }
        #[cfg(unix)]
        let _ = self.0.unlock();
    }
}

#[cfg(unix)]
fn try_lock(file: &File, _project: bool) -> io::Result<bool> {
    match file.try_lock() {
        Ok(()) => Ok(true),
        Err(TryLockError::WouldBlock) => Ok(false),
        Err(TryLockError::Error(error)) => Err(error),
    }
}

#[cfg(windows)]
fn try_lock(file: &File, project: bool) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::LockFile;
    // File owns this handle for the entire lifetime of the range lock.
    if unsafe { LockFile(file.as_raw_handle(), lock_offset(project), 0, 1, 0) } != 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(33 | 36)) {
        return Ok(false);
    }
    Err(error)
}

#[cfg(windows)]
fn lock_offset(project: bool) -> u32 {
    if project { LOCK_BYTE_OFFSET } else { 0 }
}
