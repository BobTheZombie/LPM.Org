use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use thiserror::Error;

#[derive(Debug, Error)]
pub enum FsError {
    #[error("io error: {0}")]
    Io(#[from] io::Error),
}

/// A helper representing a scoped filesystem operation.
pub struct OperationPhase<'a> {
    name: &'a str,
}

impl<'a> OperationPhase<'a> {
    pub fn new(name: &'a str) -> Self {
        Self { name }
    }
}

impl<'a> Drop for OperationPhase<'a> {
    fn drop(&mut self) {
        log::debug!("operation phase '{}' completed", self.name);
    }
}

/// Safely write data to a path using a temporary file and atomic rename.
pub fn safe_write(path: &Path, data: &[u8], mode: u32) -> Result<(), FsError> {
    let parent = path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    fs::create_dir_all(&parent)?;

    let mut temp = parent.clone();
    temp.push(format!(".{}.tmp", uuid::Uuid::new_v4()));

    let mut opts = OpenOptions::new();
    opts.write(true).create(true).truncate(true).mode(mode);
    let mut file = opts.open(&temp)?;
    file.write_all(data)?;
    file.sync_all()?;

    fs::rename(&temp, path)?;
    Ok(())
}

/// Write a JSON document with explicit permissions.
pub fn write_json(path: &Path, value: &serde_json::Value, mode: u32) -> Result<(), FsError> {
    let data = serde_json::to_vec_pretty(value).expect("json serialization");
    safe_write(path, &data, mode)
}

/// Append to a journal file, creating it if it does not exist.
pub fn journal_append(path: &Path, line: &str, mode: u32) -> Result<(), FsError> {
    let mut opts = OpenOptions::new();
    opts.append(true).create(true).mode(mode);
    let mut file = opts.open(path)?;
    file.write_all(line.as_bytes())?;
    file.write_all(b"\n")?;
    Ok(())
}
