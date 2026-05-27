// Roots discovery: primary root + extras from CLI flags + extras from
// ~/.claude/walker-roots.json. Deduped via fs::canonicalize, filtered to
// existing directories.
//
// Mirrors cpp/walker_roots.hpp. Failure modes follow the SPEC.md contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras (must NOT error).
//   * Listed path doesn't exist on disk -> skip silently with stderr line.
//   * canonicalize() fails (broken symlink etc) -> fall back to the raw path.
//   * Primary is allowed to not exist (empty-fleet case); no stderr for it.

use serde_json::Value;
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::PathBuf;

/// Resolve the user's home directory the way every walker subcommand must.
///
/// On Windows, `USERPROFILE` is the canonical home; `HOME` is frequently
/// unset, or set by git-bash to a POSIX-style path (`/c/Users/...`) that is
/// not a valid native path — so prefer `USERPROFILE`, fall back to `HOME`.
/// On other platforms, `HOME` is canonical (fall back to `USERPROFILE`).
pub fn home_directory() -> Option<OsString> {
    if cfg!(windows) {
        std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))
    } else {
        std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))
    }
}

pub fn walker_config_path() -> PathBuf {
    match home_directory() {
        Some(h) => PathBuf::from(h).join(".claude").join("walker-roots.json"),
        None => PathBuf::from(".claude/walker-roots.json"),
    }
}

pub fn read_extra_roots_from_config() -> Vec<PathBuf> {
    let config = walker_config_path();
    if !config.exists() {
        return Vec::new();
    }
    let body = match fs::read_to_string(&config) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    if body.trim().is_empty() {
        return Vec::new();
    }
    let parsed: Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(_) => {
            eprintln!(
                "walker: malformed {} -- ignoring extra roots",
                config.display()
            );
            return Vec::new();
        }
    };
    let object = match parsed.as_object() {
        Some(o) => o,
        None => {
            eprintln!(
                "walker: {} is not a JSON object -- ignoring",
                config.display()
            );
            return Vec::new();
        }
    };
    let array = match object.get("extra_roots").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return Vec::new(),
    };
    let mut extras = Vec::new();
    for element in array {
        if let Some(s) = element.as_str() {
            if !s.is_empty() {
                extras.push(PathBuf::from(s));
            }
        }
    }
    extras
}

pub fn resolve_roots(
    primary: PathBuf,
    cli_extras: &[PathBuf],
    read_config: bool,
) -> Vec<PathBuf> {
    let mut combined: Vec<(PathBuf, bool)> = Vec::new();
    combined.push((primary, true));
    for p in cli_extras {
        combined.push((p.clone(), false));
    }
    if read_config {
        for p in read_extra_roots_from_config() {
            combined.push((p, false));
        }
    }

    let mut result = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for (path, is_primary) in combined {
        if !path.exists() || !path.is_dir() {
            if !is_primary {
                eprintln!(
                    "walker: extra root not a directory, skipping: {}",
                    path.display()
                );
            }
            continue;
        }
        // Dedup by canonical path (realpath) per SPEC, but WALK the original
        // path. On Windows `fs::canonicalize` returns extended-length `\\?\`
        // verbatim forms — and a mapped network drive (e.g. `Y:`) resolves to
        // a UNC target — which the `glob`-based discovery in transcript.rs
        // cannot enumerate, silently dropping the whole root. The canonical
        // form is only needed to detect two roots pointing at the same place.
        let key = fs::canonicalize(&path)
            .map(|c| c.to_string_lossy().into_owned())
            .unwrap_or_else(|_| path.to_string_lossy().into_owned());
        if seen.insert(key) {
            result.push(path);
        }
    }
    result
}
