// Compressed archive support: zstd inflate at the file-read chokepoint,
// transcript file-name classification shared by every discovery path, and
// expansion of a `claude-archive` root into one claude-code layout root per
// immediate `<archive>/<hostname>/` subdirectory.
//
// See ../SPEC.md sections "Roots", "Discovery", and "Filters". Failure modes:
//   * Missing or unreadable archive path -> empty expansion (caller diagnoses).
//   * Undecodable .zst -> None plus one stderr line; the walk continues.

use std::fs::{read_dir, File};
use std::io::{BufRead, BufReader, Cursor, Read, Result as IoResult};
use std::path::{Path, PathBuf};

/// The conventional directory name replica writes archives into, relative to
/// the user's home. Resolved through `walker_roots::home_directory` so it
/// honors the same USERPROFILE-then-HOME precedence as every other default.
const ARCHIVE_DIRECTORY_NAME: &str = "claude-archive";

const TRANSCRIPT_SUFFIX: &str = ".jsonl";
const COMPRESSED_SUFFIX: &str = ".jsonl.zst";
const SUBAGENT_PREFIX: &str = "agent-";

pub(crate) fn default_archive_root() -> PathBuf {
    match crate::walker_roots::home_directory() {
        Some(home) => PathBuf::from(home).join(ARCHIVE_DIRECTORY_NAME),
        None => PathBuf::from(ARCHIVE_DIRECTORY_NAME),
    }
}

/// One claude-code layout root per immediate subdirectory, sorted by name so
/// the effective root order is deterministic across filesystems. A missing or
/// unreadable path yields an empty vector; the caller emits the diagnostic.
pub(crate) fn expand_archive_root(archive_path: &Path) -> Vec<PathBuf> {
    let entries = match read_dir(archive_path) {
        Ok(entries) => entries,
        Err(_) => return Vec::new(),
    };
    let mut hosts: Vec<PathBuf> = entries
        .flatten()
        .filter(|entry| entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false))
        .map(|entry| entry.path())
        .collect();
    hosts.sort();
    hosts
}

/// `<session_id>.jsonl` or `<session_id>.jsonl.zst` -> the session id.
/// The compressed suffix is tested first: `.jsonl.zst` also ends in nothing
/// that `.jsonl` matches, but testing in this order keeps the intent obvious.
pub(crate) fn parent_session_id(file_name: &str) -> Option<&str> {
    file_name
        .strip_suffix(COMPRESSED_SUFFIX)
        .or_else(|| file_name.strip_suffix(TRANSCRIPT_SUFFIX))
}

/// `agent-<agent_id>.jsonl[.zst]` -> the agent id.
pub(crate) fn subagent_agent_id(file_name: &str) -> Option<&str> {
    parent_session_id(file_name.strip_prefix(SUBAGENT_PREFIX)?)
}

fn is_compressed(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .map(|name| name.ends_with(COMPRESSED_SUFFIX))
        .unwrap_or(false)
}

/// Whole-file read, inflating a `.zst` in memory. Returns None for an
/// unreadable file (silent, matching the existing open-failure posture) and
/// for an undecodable frame (one stderr line, per SPEC "Filters").
pub(crate) fn read_transcript(path: &Path) -> Option<Vec<u8>> {
    let raw = std::fs::read(path).ok()?;
    if !is_compressed(path) {
        return Some(raw);
    }
    match zstd::decode_all(&raw[..]) {
        Ok(plain) => Some(plain),
        Err(_) => {
            eprintln!(
                "walker: unreadable archive file, skipping: {}",
                path.display()
            );
            None
        }
    }
}

/// Line-oriented reader for the streaming call sites (cost, events, beacons).
/// A live `.jsonl` keeps its buffered file reader so the per-line hot path is
/// unchanged; a `.jsonl.zst` is inflated once and served from memory.
pub(crate) enum TranscriptReader {
    Plain(BufReader<File>),
    Inflated(Cursor<Vec<u8>>),
}

impl Read for TranscriptReader {
    fn read(&mut self, buffer: &mut [u8]) -> IoResult<usize> {
        match self {
            TranscriptReader::Plain(reader) => reader.read(buffer),
            TranscriptReader::Inflated(cursor) => cursor.read(buffer),
        }
    }
}

impl BufRead for TranscriptReader {
    fn fill_buf(&mut self) -> IoResult<&[u8]> {
        match self {
            TranscriptReader::Plain(reader) => reader.fill_buf(),
            TranscriptReader::Inflated(cursor) => cursor.fill_buf(),
        }
    }

    fn consume(&mut self, amount: usize) {
        match self {
            TranscriptReader::Plain(reader) => reader.consume(amount),
            TranscriptReader::Inflated(cursor) => cursor.consume(amount),
        }
    }
}

pub(crate) fn open_transcript(path: &Path) -> Option<TranscriptReader> {
    if is_compressed(path) {
        return read_transcript(path).map(|plain| TranscriptReader::Inflated(Cursor::new(plain)));
    }
    File::open(path).ok().map(|file| {
        TranscriptReader::Plain(BufReader::new(file))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::BufRead;
    use std::path::PathBuf;

    fn temporary_directory(suffix: &str) -> PathBuf {
        let mut path = std::env::temp_dir();
        let process_id = std::process::id();
        let nanoseconds = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or(0);
        path.push(format!("rust-archive-test-{suffix}-{process_id}-{nanoseconds}"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn parent_session_id_accepts_both_suffixes() {
        assert_eq!(parent_session_id("abc.jsonl"), Some("abc"));
        assert_eq!(parent_session_id("abc.jsonl.zst"), Some("abc"));
        assert_eq!(parent_session_id("abc.jsonl.gz"), None);
        assert_eq!(parent_session_id("abc.txt"), None);
        assert_eq!(parent_session_id(".jsonl"), Some(""));
    }

    #[test]
    fn subagent_agent_id_requires_the_prefix() {
        assert_eq!(subagent_agent_id("agent-aaa.jsonl"), Some("aaa"));
        assert_eq!(subagent_agent_id("agent-aaa.jsonl.zst"), Some("aaa"));
        assert_eq!(subagent_agent_id("aaa.jsonl"), None);
        assert_eq!(subagent_agent_id("agent-aaa.jsonl.gz"), None);
    }

    #[test]
    fn expand_archive_root_lists_immediate_subdirectories_sorted() {
        let root = temporary_directory("expand");
        fs::create_dir_all(root.join("llamabox")).unwrap();
        fs::create_dir_all(root.join("chonkers")).unwrap();
        fs::write(root.join("README.txt"), b"not a host").unwrap();
        let expanded = expand_archive_root(&root);
        assert_eq!(
            expanded,
            vec![root.join("chonkers"), root.join("llamabox")]
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn expand_archive_root_missing_path_is_empty() {
        assert!(expand_archive_root(&PathBuf::from("/no/such/archive")).is_empty());
    }

    #[test]
    fn read_transcript_inflates_a_zstd_file() {
        let root = temporary_directory("inflate");
        let plain = root.join("session.jsonl");
        fs::write(&plain, b"{\"a\":1}\n").unwrap();
        assert_eq!(read_transcript(&plain).unwrap(), b"{\"a\":1}\n".to_vec());

        let compressed_path = root.join("session.jsonl.zst");
        let encoded = zstd::encode_all(&b"{\"b\":2}\n"[..], 10).unwrap();
        fs::write(&compressed_path, encoded).unwrap();
        assert_eq!(
            read_transcript(&compressed_path).unwrap(),
            b"{\"b\":2}\n".to_vec()
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn read_transcript_returns_none_for_a_corrupt_frame() {
        let root = temporary_directory("corrupt");
        let broken = root.join("session.jsonl.zst");
        fs::write(&broken, b"this is not a zstd frame").unwrap();
        assert!(read_transcript(&broken).is_none());
        assert!(read_transcript(&root.join("missing.jsonl")).is_none());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn open_transcript_yields_lines_from_both_shapes() {
        let root = temporary_directory("open");
        let plain = root.join("session.jsonl");
        fs::write(&plain, b"one\ntwo\n").unwrap();
        let compressed_path = root.join("session.jsonl.zst");
        fs::write(
            &compressed_path,
            zstd::encode_all(&b"three\nfour\n"[..], 10).unwrap(),
        )
        .unwrap();

        for (path, expected) in [
            (&plain, vec!["one", "two"]),
            (&compressed_path, vec!["three", "four"]),
        ] {
            let mut reader = open_transcript(path).expect("opened");
            let mut collected: Vec<String> = Vec::new();
            let mut line = String::new();
            loop {
                line.clear();
                if reader.read_line(&mut line).unwrap() == 0 {
                    break;
                }
                collected.push(line.trim().to_string());
            }
            assert_eq!(collected, expected);
        }
        assert!(open_transcript(&root.join("nope.jsonl")).is_none());
        let _ = fs::remove_dir_all(&root);
    }
}
