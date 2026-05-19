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
use std::fs;
use std::path::PathBuf;

pub fn walker_config_path() -> PathBuf {
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"));
    match home {
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
        let canonical = fs::canonicalize(&path).unwrap_or_else(|_| path.clone());
        let key = canonical.to_string_lossy().to_string();
        if seen.insert(key) {
            result.push(canonical);
        }
    }
    result
}
