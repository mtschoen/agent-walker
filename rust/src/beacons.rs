// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::{current_unix, default_projects_root, parse_iso8601};

#[derive(Deserialize)]
struct Entry {
    timestamp: Option<String>,
    message: Option<Message>,
}

#[derive(Deserialize)]
struct Message {
    role: Option<String>,
    content: Option<Vec<ContentBlock>>,
}

#[derive(Deserialize)]
struct ContentBlock {
    #[serde(rename = "type")]
    block_type: Option<String>,
    text: Option<String>,
}

#[derive(Deserialize, Serialize, Clone, Debug)]
struct Beacon {
    kind: String,
    eta_seconds: f64,
    summary: String,
    drift: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    beats_left: Option<i64>,
}

fn beacon_re() -> Regex {
    // (?s) makes `.` match newlines so a multi-line JSON body works.
    // Non-greedy {.*?} so two beacons in one text don't merge.
    Regex::new(r"(?s)<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>")
        .expect("static regex compiles")
}

fn extract_text(content: &[ContentBlock]) -> String {
    content
        .iter()
        .filter(|b| b.block_type.as_deref() == Some("text"))
        .filter_map(|b| b.text.as_deref())
        .collect::<Vec<_>>()
        .join("\n")
}

/// Walk one transcript file. For each assistant entry, parse the latest
/// well-formed beacon embedded in its text content. Return the (beacon,
/// entry-timestamp) pair from the entry with the highest timestamp.
fn find_latest_in_path(path: &Path, re: &Regex) -> Option<(Beacon, f64)> {
    let file = File::open(path).ok()?;
    let mut latest: Option<(Beacon, f64)> = None;
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
        let content = match msg.content {
            Some(c) => c,
            None => continue,
        };
        let ts_str = match entry.timestamp {
            Some(s) if !s.is_empty() => s,
            _ => continue,
        };
        let ts = match parse_iso8601(&ts_str) {
            Some(t) => t,
            None => continue,
        };
        let combined = extract_text(&content);
        // Pick the last well-formed beacon in this entry, then update
        // `latest` if this entry's timestamp is the highest seen.
        let mut entry_beacon: Option<Beacon> = None;
        for caps in re.captures_iter(&combined) {
            if let Some(m) = caps.get(1) {
                if let Ok(b) = serde_json::from_str::<Beacon>(m.as_str()) {
                    entry_beacon = Some(b);
                }
            }
        }
        if let Some(b) = entry_beacon {
            if latest.as_ref().map_or(true, |(_, t)| ts >= *t) {
                latest = Some((b, ts));
            }
        }
    }
    latest
}

/// Walk one transcript file and collect ALL well-formed beacons together
/// with the entry timestamp at which each was emitted. Used by history
/// mode (we need begin and end events, not just the latest one).
fn find_all_in_path(path: &Path, re: &Regex) -> Vec<(Beacon, f64)> {
    let mut out = Vec::new();
    let file = match File::open(path) {
        Ok(f) => f,
        Err(_) => return out,
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
        let content = match msg.content {
            Some(c) => c,
            None => continue,
        };
        let ts_str = match entry.timestamp {
            Some(s) if !s.is_empty() => s,
            _ => continue,
        };
        let ts = match parse_iso8601(&ts_str) {
            Some(t) => t,
            None => continue,
        };
        let combined = extract_text(&content);
        for caps in re.captures_iter(&combined) {
            if let Some(m) = caps.get(1) {
                if let Ok(b) = serde_json::from_str::<Beacon>(m.as_str()) {
                    out.push((b, ts));
                }
            }
        }
    }
    out
}

// === beacons-latest ===

struct LatestArgs {
    session_id: String,
    projects_root: Option<PathBuf>,
    now_unix: Option<f64>,
}

fn parse_latest_args(args: &[String]) -> Result<LatestArgs, String> {
    let mut session_id: Option<String> = None;
    let mut projects_root: Option<PathBuf> = None;
    let mut now_unix: Option<f64> = None;
    let mut iter = args.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--session-id" => {
                session_id = Some(iter.next().ok_or("--session-id needs a value")?.clone());
            }
            "--projects-root" => {
                projects_root = Some(PathBuf::from(
                    iter.next().ok_or("--projects-root needs a value")?,
                ));
            }
            "--now" => {
                now_unix = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                );
            }
            _ => return Err(format!("unknown flag: {flag}")),
        }
    }
    let session_id = session_id.ok_or("--session-id is required")?;
    Ok(LatestArgs {
        session_id,
        projects_root,
        now_unix,
    })
}

pub fn run_latest(args: &[String]) {
    let started = Instant::now();
    let parsed = match parse_latest_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: beacons-latest: {e}");
            std::process::exit(2);
        }
    };
    let root = parsed.projects_root.unwrap_or_else(default_projects_root);
    let now_unix = parsed.now_unix.unwrap_or_else(current_unix);

    // Try parent transcript first, then any subagent transcript.
    let parent_pattern = format!("{}/*/{}.jsonl", root.display(), parsed.session_id);
    let sub_pattern = format!(
        "{}/*/*/subagents/agent-{}.jsonl",
        root.display(),
        parsed.session_id
    );
    let mut paths: Vec<PathBuf> = Vec::new();
    for pattern in [&parent_pattern, &sub_pattern] {
        if let Ok(entries) = glob::glob(pattern) {
            paths.extend(entries.flatten());
        }
    }

    let re = beacon_re();
    let result = paths
        .iter()
        .filter_map(|p| find_latest_in_path(p, &re))
        .max_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap());

    let elapsed_ms = started.elapsed().as_millis() as u64;
    let (beacon_v, emitted_at, age_seconds) = match result {
        Some((b, t)) => {
            let v = serde_json::to_value(&b).unwrap_or(Value::Null);
            (v, Some(t), Some(now_unix - t))
        }
        None => (Value::Null, None, None),
    };
    let out = json!({
        "beacon": beacon_v,
        "emitted_at": emitted_at,
        "age_seconds": age_seconds,
        "elapsed_ms": elapsed_ms,
    });
    println!("{}", out);
}

// === beacons-history ===

struct HistoryArgs {
    period_seconds: u64,
    win_start_unix: f64,
    projects_root: Option<PathBuf>,
    now_unix: Option<f64>,
}

fn parse_history_args(args: &[String]) -> Result<HistoryArgs, String> {
    let mut period_seconds: Option<u64> = None;
    let mut win_start_unix: Option<f64> = None;
    let mut projects_root: Option<PathBuf> = None;
    let mut now_unix: Option<f64> = None;
    let mut iter = args.iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--period" => {
                period_seconds = Some(
                    iter.next()
                        .ok_or("--period needs a value")?
                        .parse()
                        .map_err(|e| format!("--period: {e}"))?,
                );
            }
            "--win-start" => {
                win_start_unix = Some(
                    iter.next()
                        .ok_or("--win-start needs a value")?
                        .parse()
                        .map_err(|e| format!("--win-start: {e}"))?,
                );
            }
            "--projects-root" => {
                projects_root = Some(PathBuf::from(
                    iter.next().ok_or("--projects-root needs a value")?,
                ));
            }
            "--now" => {
                now_unix = Some(
                    iter.next()
                        .ok_or("--now needs a value")?
                        .parse()
                        .map_err(|e| format!("--now: {e}"))?,
                );
            }
            _ => return Err(format!("unknown flag: {flag}")),
        }
    }
    Ok(HistoryArgs {
        period_seconds: period_seconds.ok_or("--period is required")?,
        win_start_unix: win_start_unix.unwrap_or(0.0),
        projects_root,
        now_unix,
    })
}

fn discover_history_groups(root: &Path) -> HashMap<(String, String), Vec<PathBuf>> {
    let mut groups: HashMap<(String, String), Vec<PathBuf>> = HashMap::new();

    let parent_pattern = format!("{}/*/*.jsonl", root.display());
    if let Ok(paths) = glob::glob(&parent_pattern) {
        for entry in paths.flatten() {
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

    let sub_pattern = format!("{}/*/*/subagents/agent-*.jsonl", root.display());
    if let Ok(paths) = glob::glob(&sub_pattern) {
        for entry in paths.flatten() {
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

fn bias_factor(pairs: &[(f64, f64)]) -> Option<f64> {
    if pairs.is_empty() {
        return None;
    }
    let mut ratios: Vec<f64> = pairs
        .iter()
        .filter(|(eta, _)| *eta > 0.0)
        .map(|(eta, actual)| actual / eta)
        .collect();
    if ratios.is_empty() {
        return None;
    }
    ratios.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = ratios.len();
    Some(if n % 2 == 1 {
        ratios[n / 2]
    } else {
        (ratios[n / 2 - 1] + ratios[n / 2]) / 2.0
    })
}

pub fn run_history(args: &[String]) {
    let started = Instant::now();
    let parsed = match parse_history_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: beacons-history: {e}");
            std::process::exit(2);
        }
    };
    let now_unix = parsed.now_unix.unwrap_or_else(current_unix);
    let period_cutoff = now_unix - parsed.period_seconds as f64;
    // Pairs are emitted only when both begin AND end fall within the window.
    let window_lo = period_cutoff.max(parsed.win_start_unix);
    let root = parsed.projects_root.unwrap_or_else(default_projects_root);

    let groups = discover_history_groups(&root);
    let session_count = groups.len();
    let re = beacon_re();

    let mut pairs: Vec<(f64, f64)> = Vec::new();
    for paths in groups.values() {
        let mut all: Vec<(Beacon, f64)> = Vec::new();
        for path in paths {
            all.extend(find_all_in_path(path, &re));
        }
        // Filter to beacons inside the window.
        all.retain(|(_, ts)| *ts >= window_lo);

        // Earliest "begin" and latest "end" in the window.
        let begin = all
            .iter()
            .filter(|(b, _)| b.kind == "begin")
            .min_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap());
        let end = all
            .iter()
            .filter(|(b, _)| b.kind == "end")
            .max_by(|(_, a), (_, b)| a.partial_cmp(b).unwrap());
        if let (Some((begin_b, begin_ts)), Some((_end_b, end_ts))) = (begin, end) {
            if *end_ts > *begin_ts {
                let actual = *end_ts - *begin_ts;
                pairs.push((begin_b.eta_seconds, actual));
            }
        }
    }

    let bias = bias_factor(&pairs);
    let elapsed_ms = started.elapsed().as_millis() as u64;
    let pair_objs: Vec<Value> = pairs
        .iter()
        .map(|(eta, actual)| {
            json!({
                "begin_eta": eta,
                "actual_elapsed": actual,
            })
        })
        .collect();
    let out = json!({
        "pairs": pair_objs,
        "session_count": session_count,
        "n_pairs": pairs.len(),
        "bias_factor": bias,
        "elapsed_ms": elapsed_ms,
    });
    println!("{}", out);
}
