// Native pace-walker -- Rust implementation.
// See ../SPEC.md for the contract every implementation must honor.

use chrono::DateTime;
use rayon::prelude::*;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use transcript::{cost_for, discover_groups, Entry};

mod beacons;
mod content;
mod search;
mod transcript;
mod walker_roots;

#[derive(Default)]
struct Args {
    period_seconds: u64,
    win_start_unix: f64,
    now_unix: Option<f64>,
    projects_root: Option<PathBuf>,
    extra_projects_roots: Vec<PathBuf>,
    read_config: bool,
}

fn parse_cost_args(args: &[String]) -> Result<Args, String> {
    let mut result = Args {
        read_config: true,
        ..Args::default()
    };
    let mut iter = args.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--period" => {
                result.period_seconds = iter
                    .next()
                    .ok_or("--period needs a value")?
                    .parse()
                    .map_err(|e| format!("--period: {e}"))?
            }
            "--win-start" => {
                result.win_start_unix = iter
                    .next()
                    .ok_or("--win-start needs a value")?
                    .parse()
                    .map_err(|e| format!("--win-start: {e}"))?
            }
            "--now" => {
                result.now_unix = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                )
            }
            "--projects-root" => {
                result.projects_root = Some(PathBuf::from(
                    iter.next().ok_or("--projects-root needs a value")?,
                ))
            }
            "--extra-projects-root" => {
                result.extra_projects_roots.push(PathBuf::from(
                    iter.next().ok_or("--extra-projects-root needs a value")?,
                ));
            }
            "--no-config" => {
                result.read_config = false;
            }
            "--version" => {
                println!("rust/{}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            _ => return Err(format!("unknown flag: {flag}")),
        }
    }
    if result.period_seconds == 0 {
        return Err("--period is required".into());
    }
    Ok(result)
}


pub(crate) fn parse_iso8601(ts: &str) -> Option<f64> {
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


pub(crate) fn default_projects_root() -> PathBuf {
    if let Some(home) = std::env::var_os("HOME") {
        PathBuf::from(home).join(".claude").join("projects")
    } else if let Some(up) = std::env::var_os("USERPROFILE") {
        PathBuf::from(up).join(".claude").join("projects")
    } else {
        PathBuf::from(".claude/projects")
    }
}

pub(crate) fn current_unix() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn main() {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let first = raw.first().map(|s| s.as_str());
    let (subcommand, rest): (&str, &[String]) = match first {
        Some("cost") => ("cost", &raw[1..]),
        Some("beacons-latest") => ("beacons-latest", &raw[1..]),
        Some("beacons-history") => ("beacons-history", &raw[1..]),
        Some("search") => ("search", &raw[1..]),
        // Bare flag invocation = cost mode (back-compat).
        Some(s) if s.starts_with('-') => ("cost", &raw[..]),
        Some(s) => {
            eprintln!("walker: unknown subcommand: {}", s);
            std::process::exit(2);
        }
        None => ("cost", &raw[..]),
    };

    match subcommand {
        "cost" => run_cost(rest),
        "beacons-latest" => beacons::run_latest(rest),
        "beacons-history" => beacons::run_history(rest),
        "search" => search::run(rest),
        _ => unreachable!(),
    }
}

fn run_cost(args: &[String]) {
    let started = Instant::now();
    let parsed = match parse_cost_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: {e}");
            std::process::exit(2);
        }
    };

    let now_unix = parsed.now_unix.unwrap_or_else(current_unix);
    let period_cutoff = now_unix - parsed.period_seconds as f64;
    let earliest = period_cutoff.min(parsed.win_start_unix);
    let primary = parsed.projects_root.unwrap_or_else(default_projects_root);
    let roots = walker_roots::resolve_roots(
        primary,
        &parsed.extra_projects_roots,
        parsed.read_config,
    );

    let groups = discover_groups(&roots, earliest);
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
            .map(|paths| walk_group(paths, period_cutoff, parsed.win_start_unix))
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
