// Native pace-walker -- Rust implementation.
// See ../SPEC.md for the contract every implementation must honor.

use chrono::DateTime;
use rayon::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::fs::{metadata, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

#[derive(Default)]
struct Args {
    period_seconds: u64,
    win_start_unix: f64,
    now_unix: Option<f64>,
    projects_root: Option<PathBuf>,
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args::default();
    let mut iter = std::env::args().skip(1);
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--period" => {
                args.period_seconds = iter
                    .next()
                    .ok_or("--period needs a value")?
                    .parse()
                    .map_err(|e| format!("--period: {e}"))?
            }
            "--win-start" => {
                args.win_start_unix = iter
                    .next()
                    .ok_or("--win-start needs a value")?
                    .parse()
                    .map_err(|e| format!("--win-start: {e}"))?
            }
            "--now" => {
                args.now_unix = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                )
            }
            "--projects-root" => {
                args.projects_root =
                    Some(PathBuf::from(iter.next().ok_or("--projects-root needs a value")?))
            }
            "--version" => {
                println!("rust/{}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            _ => return Err(format!("unknown flag: {flag}")),
        }
    }
    if args.period_seconds == 0 {
        return Err("--period is required".into());
    }
    Ok(args)
}

#[derive(Deserialize)]
struct Entry {
    timestamp: Option<String>,
    message: Option<Message>,
}

#[derive(Deserialize)]
struct Message {
    role: Option<String>,
    id: Option<String>,
    model: Option<String>,
    usage: Option<Usage>,
}

#[derive(Deserialize, Default)]
struct Usage {
    #[serde(default)]
    input_tokens: u64,
    #[serde(default)]
    output_tokens: u64,
    #[serde(default)]
    cache_read_input_tokens: u64,
    #[serde(default)]
    cache_creation_input_tokens: u64,
}

// (input_per_mtok, output_per_mtok). Match SPEC.md exactly.
fn rates_for(model: &str) -> (f64, f64) {
    let m = model.to_ascii_lowercase();
    if m.contains("opus") {
        (5.0, 25.0)
    } else if m.contains("haiku") {
        (1.0, 5.0)
    } else if m.contains("sonnet") {
        (3.0, 15.0)
    } else {
        (3.0, 15.0) // unknown -> sonnet, per spec
    }
}

fn cost_for(usage: &Usage, model: &str) -> f64 {
    let (i_rate, o_rate) = rates_for(model);
    (usage.input_tokens as f64 * i_rate
        + usage.cache_read_input_tokens as f64 * i_rate * 0.10
        + usage.cache_creation_input_tokens as f64 * i_rate * 1.25
        + usage.output_tokens as f64 * o_rate)
        / 1_000_000.0
}

fn parse_iso8601(ts: &str) -> Option<f64> {
    // Accept "...Z" or any RFC3339 variant.
    DateTime::parse_from_rfc3339(&ts.replace('Z', "+00:00"))
        .ok()
        .map(|dt| dt.timestamp() as f64 + dt.timestamp_subsec_nanos() as f64 / 1e9)
}

struct GroupResult {
    trailing: f64,
    window: f64,
}

fn walk_group(paths: &[PathBuf], period_cutoff: f64, win_start_unix: f64) -> GroupResult {
    let earliest = period_cutoff.min(win_start_unix);
    let mut trailing = 0.0;
    let mut window = 0.0;
    let mut seen_ids: std::collections::HashSet<String> = Default::default();
    for path in paths {
        let file = match File::open(path) {
            Ok(f) => f,
            Err(_) => continue,
        };
        for line in BufReader::new(file).lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => continue,
            };
            if line.trim().is_empty() {
                continue;
            }
            let entry: Entry = match serde_json::from_str(&line) {
                Ok(e) => e,
                Err(_) => continue,
            };
            let msg = match entry.message {
                Some(m) => m,
                None => continue,
            };
            if msg.role.as_deref() != Some("assistant") {
                continue;
            }
            if let Some(ref mid) = msg.id {
                if !seen_ids.insert(mid.clone()) {
                    continue;
                }
            }
            let ts_str = match entry.timestamp {
                Some(s) if !s.is_empty() => s,
                _ => continue,
            };
            let ts = match parse_iso8601(&ts_str) {
                Some(t) => t,
                None => continue,
            };
            if ts < earliest {
                continue;
            }
            let usage = msg.usage.unwrap_or_default();
            let model = msg.model.unwrap_or_default();
            let c = cost_for(&usage, &model);
            if ts >= period_cutoff {
                trailing += c;
            }
            if ts >= win_start_unix {
                window += c;
            }
        }
    }
    GroupResult { trailing, window }
}

fn discover_groups(root: &Path, earliest: f64) -> HashMap<(String, String), Vec<PathBuf>> {
    let mut groups: HashMap<(String, String), Vec<PathBuf>> = HashMap::new();

    // Parents: <root>/<slug>/<session_id>.jsonl
    let parent_pattern = format!("{}/*/*.jsonl", root.display());
    if let Ok(paths) = glob::glob(&parent_pattern) {
        for entry in paths.flatten() {
            if let Ok(meta) = metadata(&entry) {
                if let Ok(mt) = meta.modified() {
                    if let Ok(d) = mt.duration_since(UNIX_EPOCH) {
                        if d.as_secs_f64() < earliest {
                            continue;
                        }
                    }
                }
            }
            let slug = entry
                .parent()
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            let sid = entry
                .file_stem()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            groups.entry((slug, sid)).or_default().push(entry);
        }
    }

    // Subagents: <root>/<slug>/<session_id>/subagents/agent-*.jsonl
    let sub_pattern = format!("{}/*/*/subagents/agent-*.jsonl", root.display());
    if let Ok(paths) = glob::glob(&sub_pattern) {
        for entry in paths.flatten() {
            if let Ok(meta) = metadata(&entry) {
                if let Ok(mt) = meta.modified() {
                    if let Ok(d) = mt.duration_since(UNIX_EPOCH) {
                        if d.as_secs_f64() < earliest {
                            continue;
                        }
                    }
                }
            }
            // path = .../<slug>/<sid>/subagents/agent-*.jsonl
            let session_dir = entry.parent().and_then(|p| p.parent());
            let sid = session_dir
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            let slug = session_dir
                .and_then(|p| p.parent())
                .and_then(|p| p.file_name())
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            groups.entry((slug, sid)).or_default().push(entry);
        }
    }

    groups
}

fn default_projects_root() -> PathBuf {
    if let Some(home) = std::env::var_os("HOME") {
        PathBuf::from(home).join(".claude").join("projects")
    } else if let Some(up) = std::env::var_os("USERPROFILE") {
        PathBuf::from(up).join(".claude").join("projects")
    } else {
        PathBuf::from(".claude/projects")
    }
}

fn main() {
    let started = Instant::now();
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: {e}");
            std::process::exit(2);
        }
    };

    let now_unix = args.now_unix.unwrap_or_else(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    });
    let period_cutoff = now_unix - args.period_seconds as f64;
    let earliest = period_cutoff.min(args.win_start_unix);
    let root = args.projects_root.unwrap_or_else(default_projects_root);

    let groups = discover_groups(&root, earliest);
    let total_files: usize = groups.values().map(|v| v.len()).sum();
    let total_groups = groups.len();

    let group_paths: Vec<Vec<PathBuf>> = groups.into_values().collect();

    // Cap pool so the small-corpus case doesn't pay startup tax for nothing.
    let workers = std::cmp::min(8, std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4));
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers)
        .build()
        .expect("rayon pool");

    let (trailing, window) = pool.install(|| {
        group_paths
            .par_iter()
            .map(|paths| walk_group(paths, period_cutoff, args.win_start_unix))
            .reduce(
                || GroupResult { trailing: 0.0, window: 0.0 },
                |a, b| GroupResult {
                    trailing: a.trailing + b.trailing,
                    window: a.window + b.window,
                },
            )
    })
    .into();

    let elapsed_ms = started.elapsed().as_millis() as u64;
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    writeln!(
        out,
        "{{\"trailing_usd\":{:.6},\"window_usd\":{:.6},\"files_walked\":{},\"groups\":{},\"elapsed_ms\":{}}}",
        trailing, window, total_files, total_groups, elapsed_ms
    )
    .ok();
}

impl From<GroupResult> for (f64, f64) {
    fn from(g: GroupResult) -> Self {
        (g.trailing, g.window)
    }
}
