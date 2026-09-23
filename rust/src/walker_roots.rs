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

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum TranscriptFormat {
    ClaudeCode,
    Codex,
    /// A directory of per-hostname claude-code layout trees written by
    /// replica. Never walked directly; expanded by `archive::expand_archive_root`.
    ClaudeArchive,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct TranscriptRoot {
    pub(crate) path: PathBuf,
    pub(crate) format: TranscriptFormat,
    /// True when this root is one `<archive>/<hostname>` directory produced by
    /// expanding a claude-archive root. Only these roots report the
    /// unrecognized-suffix count, so a status-line cost tick over the live
    /// tree gains no new stderr. Not part of any dedup key.
    pub(crate) from_archive: bool,
}

/// A resolved root for the non-search modes, which have no format to carry.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResolvedRoot {
    pub path: PathBuf,
    pub from_archive: bool,
}

/// Resolve the user's home directory the way every walker subcommand must.
///
/// On Windows, `USERPROFILE` is the canonical home; `HOME` is frequently
/// unset, or set by git-bash to a POSIX-style path (`/c/Users/...`) that is
/// not a valid native path — so prefer `USERPROFILE`, fall back to `HOME`.
/// On other platforms, `HOME` is canonical (fall back to `USERPROFILE`).
#[cfg(windows)]
pub fn home_directory() -> Option<OsString> {
    std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))
}

#[cfg(not(windows))]
pub fn home_directory() -> Option<OsString> {
    std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))
}

pub fn walker_config_path() -> PathBuf {
    match home_directory() {
        Some(h) => PathBuf::from(h).join(".claude").join("walker-roots.json"),
        None => PathBuf::from(".claude/walker-roots.json"),
    }
}

#[allow(dead_code)]
pub fn read_extra_roots_from_config() -> Vec<PathBuf> {
    read_tagged_extra_roots_from_config()
        .into_iter()
        .filter(|root| root.format == TranscriptFormat::ClaudeCode)
        .map(|root| root.path)
        .collect()
}

pub(crate) fn read_tagged_extra_roots_from_config() -> Vec<TranscriptRoot> {
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
                extras.push(TranscriptRoot {
                    path: PathBuf::from(s),
                    format: TranscriptFormat::ClaudeCode,
                    from_archive: false,
                });
            }
        } else if let Some(tagged) = element.as_object() {
            let Some(path) = tagged.get("path").and_then(Value::as_str) else {
                continue;
            };
            let format = match tagged.get("format").and_then(Value::as_str) {
                Some("claude-code") => TranscriptFormat::ClaudeCode,
                Some("codex") => TranscriptFormat::Codex,
                Some("claude-archive") => TranscriptFormat::ClaudeArchive,
                _ => continue,
            };
            if !path.is_empty() {
                extras.push(TranscriptRoot {
                    path: PathBuf::from(path),
                    format,
                    from_archive: false,
                });
            }
        }
    }
    extras
}

pub(crate) fn default_codex_root() -> PathBuf {
    match home_directory() {
        Some(home) => PathBuf::from(home).join(".codex").join("sessions"),
        None => PathBuf::from(".codex/sessions"),
    }
}

/// Split one config read into the three format buckets. Reading the config
/// exactly once per invocation matters: `read_tagged_extra_roots_from_config`
/// prints the malformed-JSON diagnostic itself, so a second call would print
/// it twice.
fn config_roots_by_format(read_config: bool) -> (Vec<PathBuf>, Vec<PathBuf>, Vec<TranscriptRoot>) {
    if !read_config {
        return (Vec::new(), Vec::new(), Vec::new());
    }
    let mut claude_code = Vec::new();
    let mut archives = Vec::new();
    let mut tagged = Vec::new();
    for root in read_tagged_extra_roots_from_config() {
        match root.format {
            TranscriptFormat::ClaudeCode => claude_code.push(root.path.clone()),
            TranscriptFormat::ClaudeArchive => archives.push(root.path.clone()),
            TranscriptFormat::Codex => {}
        }
        tagged.push(root);
    }
    (claude_code, archives, tagged)
}

/// Archive roots in SPEC effective order (implicit, CLI, config), each already
/// expanded into its `<archive>/<hostname>` claude-code roots. The implicit
/// root is silent when absent (it is a convention, not a user request); a CLI
/// or config root that is not a directory gets the standard diagnostic.
fn archive_roots_in_effective_order(
    primary_explicit: bool,
    cli_archives: &[PathBuf],
    config_archives: &[PathBuf],
) -> Vec<PathBuf> {
    let mut expanded = Vec::new();
    if !primary_explicit {
        let implicit = crate::archive::default_archive_root();
        if implicit.is_dir() {
            expanded.extend(crate::archive::expand_archive_root(&implicit));
        }
    }
    for archive_path in cli_archives.iter().chain(config_archives.iter()) {
        if !archive_path.is_dir() {
            eprintln!(
                "walker: archive root not a directory, skipping: {}",
                archive_path.display()
            );
            continue;
        }
        expanded.extend(crate::archive::expand_archive_root(archive_path));
    }
    expanded
}

pub fn resolve_roots(
    primary: Option<PathBuf>,
    cli_extras: &[PathBuf],
    cli_archives: &[PathBuf],
    read_config: bool,
) -> Vec<ResolvedRoot> {
    let primary_explicit = primary.is_some();
    let (config_extras, config_archives, _tagged) = config_roots_by_format(read_config);
    // (path, is_primary, from_archive)
    let mut combined: Vec<(PathBuf, bool, bool)> = vec![(
        primary.unwrap_or_else(crate::default_projects_root),
        true,
        false,
    )];
    for path in cli_extras {
        combined.push((path.clone(), false, false));
    }
    for path in config_extras {
        combined.push((path, false, false));
    }
    // Archive hosts come last and are already known to be directories, so
    // they never produce the extra-root diagnostic a second time.
    for path in archive_roots_in_effective_order(primary_explicit, cli_archives, &config_archives) {
        combined.push((path, false, true));
    }

    let mut result = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for (path, is_primary, from_archive) in combined {
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
        // verbatim forms which the directory walk cannot enumerate.
        // `from_archive` is deliberately absent from the key: two roots at the
        // same place collapse to the first one seen, live or archived.
        let key = fs::canonicalize(&path)
            .map(|canonical| canonical.to_string_lossy().into_owned())
            .unwrap_or_else(|_| path.to_string_lossy().into_owned());
        if seen.insert(key) {
            result.push(ResolvedRoot { path, from_archive });
        }
    }
    result
}

pub(crate) fn resolve_search_roots(
    primary: Option<PathBuf>,
    cli_extras: &[PathBuf],
    cli_archives: &[PathBuf],
    read_config: bool,
) -> Vec<TranscriptRoot> {
    let primary_explicit = primary.is_some();
    let (_config_extras, config_archives, tagged) = config_roots_by_format(read_config);
    let mut combined = vec![TranscriptRoot {
        path: primary.unwrap_or_else(crate::default_projects_root),
        format: TranscriptFormat::ClaudeCode,
        from_archive: false,
    }];
    if !primary_explicit {
        combined.push(TranscriptRoot {
            path: default_codex_root(),
            format: TranscriptFormat::Codex,
            from_archive: false,
        });
    }
    combined.extend(cli_extras.iter().cloned().map(|path| TranscriptRoot {
        path,
        format: TranscriptFormat::ClaudeCode,
        from_archive: false,
    }));
    // Tagged config entries keep their order; archive entries are pulled out
    // here because they belong at the end, after expansion.
    combined.extend(
        tagged
            .into_iter()
            .filter(|root| root.format != TranscriptFormat::ClaudeArchive),
    );
    // An expanded host directory IS a claude-code layout tree, so it keeps
    // that format and the discovery dispatch is unchanged. Only from_archive
    // distinguishes it, and only for the suffix counter.
    combined.extend(
        archive_roots_in_effective_order(primary_explicit, cli_archives, &config_archives)
            .into_iter()
            .map(|path| TranscriptRoot {
                path,
                format: TranscriptFormat::ClaudeCode,
                from_archive: true,
            }),
    );

    let mut result = Vec::new();
    let mut seen: HashSet<(TranscriptFormat, String)> = HashSet::new();
    for (index, root) in combined.into_iter().enumerate() {
        if !root.path.is_dir() {
            if index > 0 && root.path.exists() {
                eprintln!(
                    "walker: extra root not a directory, skipping: {}",
                    root.path.display()
                );
            }
            continue;
        }
        let key = fs::canonicalize(&root.path)
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_else(|_| root.path.to_string_lossy().into_owned());
        if seen.insert((root.format, key)) {
            result.push(root);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::Mutex;

    // Env vars are process-global; serialize tests that mutate them.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Run `body` with HOME and USERPROFILE saved, set to the given values
    /// (None => remove), then restored regardless of panic.
    fn with_home_env<F, R>(home: Option<&str>, userprofile: Option<&str>, body: F) -> R
    where
        F: FnOnce() -> R,
    {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let saved_home = std::env::var_os("HOME");
        let saved_up = std::env::var_os("USERPROFILE");
        match home {
            Some(v) => std::env::set_var("HOME", v),
            None => std::env::remove_var("HOME"),
        }
        match userprofile {
            Some(v) => std::env::set_var("USERPROFILE", v),
            None => std::env::remove_var("USERPROFILE"),
        }
        let result = body();
        match saved_home {
            Some(v) => std::env::set_var("HOME", v),
            None => std::env::remove_var("HOME"),
        }
        match saved_up {
            Some(v) => std::env::set_var("USERPROFILE", v),
            None => std::env::remove_var("USERPROFILE"),
        }
        result
    }

    #[test]
    fn home_directory_prefers_home_on_unix() {
        // On Linux, HOME is canonical even if USERPROFILE is set.
        with_home_env(Some("/tmp/fakehome"), Some("/tmp/fakeprofile"), || {
            let h = home_directory().unwrap();
            if cfg!(windows) {
                assert_eq!(h, std::ffi::OsString::from("/tmp/fakeprofile"));
            } else {
                assert_eq!(h, std::ffi::OsString::from("/tmp/fakehome"));
            }
        });
    }

    #[test]
    fn home_directory_falls_back_to_userprofile_when_home_unset() {
        // HOME unset on Unix → USERPROFILE secondary kicks in (line 28 else branch).
        with_home_env(None, Some("/tmp/fakeprofile"), || {
            let h = home_directory();
            assert_eq!(h, Some(std::ffi::OsString::from("/tmp/fakeprofile")));
        });
    }

    #[test]
    fn home_directory_returns_none_when_both_env_unset() {
        with_home_env(None, None, || {
            assert!(home_directory().is_none());
        });
    }

    #[test]
    fn walker_config_path_falls_back_when_no_home() {
        // Covers line 35: the None arm of walker_config_path().
        with_home_env(None, None, || {
            let p = walker_config_path();
            assert_eq!(p, PathBuf::from(".claude/walker-roots.json"));
        });
    }

    #[test]
    fn read_extra_roots_returns_empty_when_config_missing() {
        // Covers line 42: config !exists silent path.
        let tmp = tempdir_path("walker-cfg-missing");
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            // No .claude dir at all → config doesn't exist.
            let v = read_extra_roots_from_config();
            assert!(v.is_empty());
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn read_extra_roots_unreadable_returns_empty() {
        // Covers line 46: fs::read_to_string Err branch.
        // Make the config file a directory so open fails with IsADirectory.
        let tmp = tempdir_path("walker-cfg-unreadable");
        let claude_dir = tmp.join(".claude");
        let bogus_config = claude_dir.join("walker-roots.json");
        fs::create_dir_all(&bogus_config).unwrap();
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            // .exists() returns true for a directory; fs::read_to_string errors.
            let v = read_extra_roots_from_config();
            assert!(v.is_empty());
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn read_extra_roots_skips_non_string_elements() {
        // Covers the `element.as_str()` returns None branch (the `else` of
        // the `if let Some(s) = …` at line 77).
        let tmp = tempdir_path("walker-cfg-mixed");
        let claude_dir = tmp.join(".claude");
        fs::create_dir_all(&claude_dir).unwrap();
        let config_path = claude_dir.join("walker-roots.json");
        // "extra_roots" with an integer (non-string), a valid string, an empty
        // string, and a null. Only the valid non-empty string survives.
        fs::write(&config_path, br#"{"extra_roots":[42,"/tmp/x","",null]}"#).unwrap();
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            let v = read_extra_roots_from_config();
            assert_eq!(v, vec![PathBuf::from("/tmp/x")]);
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn tagged_config_parses_known_formats_and_skips_malformed_objects() {
        let tmp = tempdir_path("walker-cfg-tagged");
        let claude_dir = tmp.join(".claude");
        fs::create_dir_all(&claude_dir).unwrap();
        fs::write(
            claude_dir.join("walker-roots.json"),
            br#"{"extra_roots":[{"format":"codex"},{"path":"/bad","format":"unknown"},{"path":"/claude","format":"claude-code"},{"path":"/codex","format":"codex"}]}"#,
        )
        .unwrap();
        with_home_env(Some(tmp.to_str().unwrap()), None, || {
            assert_eq!(
                read_tagged_extra_roots_from_config(),
                vec![
                    TranscriptRoot {
                        path: PathBuf::from("/claude"),
                        format: TranscriptFormat::ClaudeCode,
                        from_archive: false,
                    },
                    TranscriptRoot {
                        path: PathBuf::from("/codex"),
                        format: TranscriptFormat::Codex,
                        from_archive: false,
                    },
                ]
            );
        });
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn codex_default_and_non_directory_extra_paths() {
        with_home_env(None, None, || {
            assert_eq!(default_codex_root(), PathBuf::from(".codex/sessions"));
        });
        let tmp = tempdir_path("walker-search-roots");
        let primary = tmp.join("primary");
        fs::create_dir(&primary).unwrap();
        let not_directory = tmp.join("not-directory");
        fs::write(&not_directory, b"x").unwrap();
        let roots = resolve_search_roots(Some(primary.clone()), &[not_directory], &[], false);
        assert_eq!(
            roots,
            vec![TranscriptRoot {
                path: primary,
                format: TranscriptFormat::ClaudeCode,
                from_archive: false,
            }]
        );
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn tagged_config_parses_claude_archive_format() {
        let temporary = tempdir_path("walker-cfg-archive");
        let claude_directory = temporary.join(".claude");
        fs::create_dir_all(&claude_directory).unwrap();
        fs::write(
            claude_directory.join("walker-roots.json"),
            br#"{"extra_roots":[{"path":"/store","format":"claude-archive"},{"path":"/nope","format":"bogus"}]}"#,
        )
        .unwrap();
        with_home_env(Some(temporary.to_str().unwrap()), None, || {
            assert_eq!(
                read_tagged_extra_roots_from_config(),
                vec![TranscriptRoot {
                    path: PathBuf::from("/store"),
                    format: TranscriptFormat::ClaudeArchive,
                    from_archive: false,
                }]
            );
        });
        let _ = fs::remove_dir_all(&temporary);
    }

    #[test]
    fn resolve_roots_appends_expanded_archive_hosts_last() {
        let temporary = tempdir_path("walker-roots-archive-order");
        let primary = temporary.join("primary");
        let extra = temporary.join("extra");
        let archive = temporary.join("archive");
        fs::create_dir_all(&primary).unwrap();
        fs::create_dir_all(&extra).unwrap();
        fs::create_dir_all(archive.join("llamabox")).unwrap();
        fs::create_dir_all(archive.join("chonkers")).unwrap();

        let roots = resolve_roots(
            Some(primary.clone()),
            std::slice::from_ref(&extra),
            std::slice::from_ref(&archive),
            false,
        );
        assert_eq!(
            roots,
            vec![
                ResolvedRoot { path: primary, from_archive: false },
                ResolvedRoot { path: extra, from_archive: false },
                ResolvedRoot { path: archive.join("chonkers"), from_archive: true },
                ResolvedRoot { path: archive.join("llamabox"), from_archive: true },
            ]
        );
        let _ = fs::remove_dir_all(&temporary);
    }

    #[test]
    fn resolve_roots_adds_the_implicit_archive_only_without_an_explicit_primary() {
        let home = tempdir_path("walker-roots-implicit");
        fs::create_dir_all(home.join(".claude").join("projects")).unwrap();
        fs::create_dir_all(home.join("claude-archive").join("chonkers")).unwrap();
        with_home_env(Some(home.to_str().unwrap()), Some(home.to_str().unwrap()), || {
            let implicit = resolve_roots(None, &[], &[], false);
            assert!(
                implicit.contains(&ResolvedRoot {
                    path: home.join("claude-archive").join("chonkers"),
                    from_archive: true,
                }),
                "implicit archive host missing from {implicit:?}"
            );
            let explicit = resolve_roots(
                Some(home.join(".claude").join("projects")),
                &[],
                &[],
                false,
            );
            assert_eq!(
                explicit,
                vec![ResolvedRoot {
                    path: home.join(".claude").join("projects"),
                    from_archive: false,
                }]
            );
        });
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn resolve_roots_diagnoses_a_missing_cli_archive_root() {
        let temporary = tempdir_path("walker-roots-missing-archive");
        let primary = temporary.join("primary");
        fs::create_dir_all(&primary).unwrap();
        let missing = temporary.join("no-such-archive");
        let roots = resolve_roots(
            Some(primary.clone()),
            &[],
            std::slice::from_ref(&missing),
            false,
        );
        assert_eq!(
            roots,
            vec![ResolvedRoot { path: primary, from_archive: false }]
        );
        let _ = fs::remove_dir_all(&temporary);
    }

    #[test]
    fn resolve_search_roots_keeps_codex_before_archives() {
        let temporary = tempdir_path("walker-search-roots-archive");
        let primary = temporary.join("primary");
        let archive = temporary.join("archive");
        fs::create_dir_all(&primary).unwrap();
        fs::create_dir_all(archive.join("chonkers")).unwrap();
        let roots = resolve_search_roots(
            Some(primary.clone()),
            &[],
            std::slice::from_ref(&archive),
            false,
        );
        assert_eq!(
            roots,
            vec![
                TranscriptRoot {
                    path: primary,
                    format: TranscriptFormat::ClaudeCode,
                    from_archive: false,
                },
                TranscriptRoot {
                    path: archive.join("chonkers"),
                    format: TranscriptFormat::ClaudeCode,
                    from_archive: true,
                },
            ]
        );
        let _ = fs::remove_dir_all(&temporary);
    }

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-walker-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }
}
