// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.

use rayon::prelude::*;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::time::Instant;

use crate::content::{extract_text, user_content_is_tool_result};
use crate::{current_unix, default_projects_root, parse_iso8601, walker_roots};

#[derive(Deserialize)]
struct Entry {
    #[serde(rename = "type")]
    entry_type: Option<String>,
    timestamp: Option<String>,
    message: Option<Message>,
}

#[derive(Deserialize)]
struct Message {
    role: Option<String>,
    /// Untyped because real-world transcripts use either a Vec of content
    /// blocks OR a bare string for the user role. A strictly-typed Vec<...>
    /// silently skips the bare-string variants and miscounts user events.
    content: Option<Value>,
}

#[derive(Deserialize, Serialize, Clone, Debug)]
struct Beacon {
    kind: String,
    eta_seconds: f64,
    summary: String,
    /// Optional per SPEC: parses when absent, passed through when present, and
    /// omitted from beacons-latest output when the source beacon lacked it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    drift: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    beats_left: Option<i64>,
}

fn beacon_re() -> Regex {
    // (?s) makes `.` match newlines so a multi-line JSON body works.
    // Non-greedy {.*?} so two beacons in one text don't merge.
    Regex::new(r"(?s)<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>")
        .expect("static regex compiles")
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
        let combined = extract_text(&content, false);
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
            if latest.as_ref().is_none_or(|(_, t)| ts >= *t) {
                latest = Some((b, ts));
            }
        }
    }
    latest
}

struct SessionEvents {
    beacons: Vec<(Beacon, f64)>,
    /// Sorted ascending by timestamp. `bool` is true for user-type entries,
    /// false for assistant-type entries. Used to detect agent-waiting-on-user
    /// idle gaps for bias-factor correction.
    events: Vec<(f64, bool)>,
}

/// Walk one transcript file and collect both beacons (with entry timestamps)
/// and the timestamp + user/assistant flag for every entry. The event list
/// powers idle-gap detection in beacons-history.
fn collect_session_events_in_path(path: &Path, re: &Regex) -> SessionEvents {
    let mut beacons: Vec<(Beacon, f64)> = Vec::new();
    let mut events: Vec<(f64, bool)> = Vec::new();
    let file = match File::open(path) {
        Ok(f) => f,
        Err(_) => return SessionEvents { beacons, events },
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
        let ts_str = match &entry.timestamp {
            Some(s) if !s.is_empty() => s.clone(),
            _ => continue,
        };
        let ts = match parse_iso8601(&ts_str) {
            Some(t) => t,
            None => continue,
        };
        let is_user_entry = entry.entry_type.as_deref() == Some("user");
        if is_user_entry {
            // Distinguish real user prompts (text or bare string) from
            // tool_result entries (which the JSONL also tags as `type: user`).
            // Tool results are agent-active time; only real prompts mark idle.
            let content_ref = entry.message.as_ref().and_then(|m| m.content.as_ref());
            let is_real_user = !user_content_is_tool_result(content_ref);
            events.push((ts, is_real_user));
            continue;
        }
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
        events.push((ts, false));
        let combined = extract_text(&content, false);
        for caps in re.captures_iter(&combined) {
            if let Some(m) = caps.get(1) {
                if let Ok(b) = serde_json::from_str::<Beacon>(m.as_str()) {
                    beacons.push((b, ts));
                }
            }
        }
    }
    events.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    SessionEvents { beacons, events }
}

/// Sum the portion of [lo, hi] occupied by gaps that precede a user entry.
/// A gap "precedes a user entry" when events[i] is user-type and the gap is
/// computed from events[i-1].timestamp to events[i].timestamp. Gaps are
/// clipped to [lo, hi] so events outside the begin/end window don't leak in.
fn compute_idle_in_window(events: &[(f64, bool)], lo: f64, hi: f64) -> f64 {
    if events.len() < 2 {
        return 0.0;
    }
    let mut idle = 0.0;
    for i in 1..events.len() {
        let (prev_ts, _) = events[i - 1];
        let (ts, is_user) = events[i];
        if !is_user {
            continue;
        }
        let gap_lo = prev_ts.max(lo);
        let gap_hi = ts.min(hi);
        if gap_hi > gap_lo {
            idle += gap_hi - gap_lo;
        }
    }
    idle
}

// === beacons-latest ===

struct LatestArgs {
    session_id: String,
    projects_root: Option<PathBuf>,
    extra_projects_roots: Vec<PathBuf>,
    read_config: bool,
    now_unix: Option<f64>,
}

fn parse_latest_args(args: &[String]) -> Result<LatestArgs, String> {
    let mut session_id: Option<String> = None;
    let mut projects_root: Option<PathBuf> = None;
    let mut extra_projects_roots: Vec<PathBuf> = Vec::new();
    let mut read_config = true;
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
            "--extra-projects-root" => {
                extra_projects_roots.push(PathBuf::from(
                    iter.next().ok_or("--extra-projects-root needs a value")?,
                ));
            }
            "--no-config" => {
                read_config = false;
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
        extra_projects_roots,
        read_config,
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
    let primary = parsed.projects_root.unwrap_or_else(default_projects_root);
    let roots = walker_roots::resolve_roots(
        primary,
        &parsed.extra_projects_roots,
        parsed.read_config,
    );
    let now_unix = parsed.now_unix.unwrap_or_else(current_unix);

    // Try parent transcript first, then any subagent transcript, across
    // every resolved root.
    let mut paths: Vec<PathBuf> = Vec::new();
    for root in &roots {
        let parent_pattern = format!("{}/*/{}.jsonl", root.display(), parsed.session_id);
        let sub_pattern = format!(
            "{}/*/*/subagents/agent-{}.jsonl",
            root.display(),
            parsed.session_id
        );
        for pattern in [&parent_pattern, &sub_pattern] {
            if let Ok(entries) = glob::glob(pattern) {
                paths.extend(entries.flatten());
            }
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
    extra_projects_roots: Vec<PathBuf>,
    read_config: bool,
    now_unix: Option<f64>,
}

fn parse_history_args(args: &[String]) -> Result<HistoryArgs, String> {
    let mut period_seconds: Option<u64> = None;
    let mut win_start_unix: Option<f64> = None;
    let mut projects_root: Option<PathBuf> = None;
    let mut extra_projects_roots: Vec<PathBuf> = Vec::new();
    let mut read_config = true;
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
            "--extra-projects-root" => {
                extra_projects_roots.push(PathBuf::from(
                    iter.next().ok_or("--extra-projects-root needs a value")?,
                ));
            }
            "--no-config" => {
                read_config = false;
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
        extra_projects_roots,
        read_config,
        now_unix,
    })
}

fn discover_history_groups(roots: &[PathBuf]) -> HashMap<(String, String), Vec<PathBuf>> {
    let mut groups: HashMap<(String, String), Vec<PathBuf>> = HashMap::new();

    for root in roots {
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
    // Beacons before window_lo are dropped; pairing runs on the survivors, so
    // a pair requires its begin (and end) timestamp inside the window.
    let window_lo = period_cutoff.max(parsed.win_start_unix);
    let primary = parsed.projects_root.unwrap_or_else(default_projects_root);
    let roots = walker_roots::resolve_roots(
        primary,
        &parsed.extra_projects_roots,
        parsed.read_config,
    );

    let groups = discover_history_groups(&roots);
    let session_count = groups.len();
    let re = beacon_re();

    // Flatten group paths into an indexable list so rayon can fan out one task
    // per group. Group identity (slug, sid) no longer matters past this point —
    // pair output is keyed by (eta, active) and conformance sorts pairs before
    // comparing. Mirrors the cost-mode and cpp parallel patterns.
    let group_paths: Vec<Vec<PathBuf>> = groups.into_values().collect();

    let workers = std::cmp::min(
        8,
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4),
    );
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(workers)
        .build()
        .expect("rayon pool");

    // Pairs feed bias_factor as (begin_eta, active_elapsed). pair_meta carries
    // the (wall, idle, active) breakdown for the JSON output, parallel-indexed.
    // regex::Regex is Send+Sync for read-only matching, so the shared `re`
    // works across workers. Each task gets local beacons/events buffers; final
    // pair tuples are concatenated via reduce.
    // Aliases keep the binding readable (clippy::type_complexity).
    type BiasPairs = Vec<(f64, f64)>; // (begin_eta, active_elapsed)
    type PairMeta = Vec<(f64, f64, f64)>; // (wall, idle, active)
    let (pairs, pair_meta): (BiasPairs, PairMeta) = pool.install(|| {
        group_paths
            .par_iter()
            .map(|paths| {
                let mut beacons_all: Vec<(Beacon, f64)> = Vec::new();
                let mut events_all: Vec<(f64, bool)> = Vec::new();
                for path in paths {
                    let se = collect_session_events_in_path(path, &re);
                    beacons_all.extend(se.beacons);
                    events_all.extend(se.events);
                }
                events_all.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
                // Drop beacons before the window, then iterate in timestamp
                // order tracking a single in-flight pending_begin: one pair per
                // properly-closed begin->end lifecycle. Stable sort keeps file
                // order on ties for cross-impl determinism.
                beacons_all.retain(|(_, ts)| *ts >= window_lo);
                beacons_all.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap());

                let mut local_pairs: Vec<(f64, f64)> = Vec::new();
                let mut local_meta: Vec<(f64, f64, f64)> = Vec::new();
                let mut pending_begin: Option<(f64, f64)> = None; // (begin_ts, begin_eta)
                for (b, ts) in &beacons_all {
                    match b.kind.as_str() {
                        "begin" => pending_begin = Some((*ts, b.eta_seconds)),
                        "end" => {
                            if let Some((begin_ts, begin_eta)) = pending_begin {
                                if *ts > begin_ts {
                                    let wall = *ts - begin_ts;
                                    let idle = compute_idle_in_window(&events_all, begin_ts, *ts);
                                    let active = (wall - idle).max(0.0);
                                    local_pairs.push((begin_eta, active));
                                    local_meta.push((wall, idle, active));
                                    pending_begin = None;
                                }
                            }
                        }
                        _ => {}
                    }
                }
                (local_pairs, local_meta)
            })
            .reduce(
                || (Vec::new(), Vec::new()),
                |(mut ap, mut am), (mut bp, mut bm)| {
                    ap.append(&mut bp);
                    am.append(&mut bm);
                    (ap, am)
                },
            )
    });

    let bias = bias_factor(&pairs);
    let elapsed_ms = started.elapsed().as_millis() as u64;
    let pair_objs: Vec<Value> = pairs
        .iter()
        .zip(pair_meta.iter())
        .map(|((eta, _active), (wall, idle, active))| {
            json!({
                "begin_eta": eta,
                "actual_elapsed": wall,
                "idle_excluded": idle,
                "active_elapsed": active,
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-beacons-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn s(x: &str) -> String { x.to_string() }

    #[test]
    fn bias_factor_empty_pairs_returns_none() {
        assert!(bias_factor(&[]).is_none());
    }

    #[test]
    fn bias_factor_all_nonpositive_eta_returns_none() {
        // All etas ≤ 0 → ratios is empty → returns None.
        let pairs = vec![(0.0, 5.0), (-1.0, 3.0)];
        assert!(bias_factor(&pairs).is_none());
    }

    #[test]
    fn bias_factor_odd_count_returns_middle() {
        let pairs = vec![(10.0, 5.0), (10.0, 10.0), (10.0, 20.0)];
        // ratios = [0.5, 1.0, 2.0] sorted → middle = 1.0
        assert_eq!(bias_factor(&pairs), Some(1.0));
    }

    #[test]
    fn bias_factor_even_count_averages_middle_two() {
        let pairs = vec![(10.0, 5.0), (10.0, 10.0), (10.0, 20.0), (10.0, 40.0)];
        // ratios sorted = [0.5, 1.0, 2.0, 4.0] → (1.0 + 2.0)/2 = 1.5
        assert_eq!(bias_factor(&pairs), Some(1.5));
    }

    #[test]
    fn compute_idle_in_window_too_few_events() {
        // <2 events → 0 idle.
        assert_eq!(compute_idle_in_window(&[], 0.0, 100.0), 0.0);
        assert_eq!(compute_idle_in_window(&[(10.0, true)], 0.0, 100.0), 0.0);
    }

    #[test]
    fn compute_idle_in_window_skips_non_user_gaps() {
        // Only gaps preceding a user-flagged event count.
        let events = vec![(10.0, false), (20.0, false), (30.0, false)];
        assert_eq!(compute_idle_in_window(&events, 0.0, 100.0), 0.0);
    }

    #[test]
    fn compute_idle_in_window_clips_to_window() {
        // Gap from 10..50 (40 seconds), clipped to window [20..40] = 20 seconds.
        let events = vec![(10.0, false), (50.0, true)];
        let idle = compute_idle_in_window(&events, 20.0, 40.0);
        assert!((idle - 20.0).abs() < 1e-9);
    }

    #[test]
    fn compute_idle_in_window_zero_when_outside_window() {
        // gap_hi <= gap_lo → idle stays zero.
        let events = vec![(10.0, false), (50.0, true)];
        let idle = compute_idle_in_window(&events, 100.0, 200.0);
        assert_eq!(idle, 0.0);
    }

    #[test]
    fn parse_latest_args_missing_session_id() {
        assert!(parse_latest_args(&[]).is_err());
    }

    #[test]
    fn parse_latest_args_unknown_flag() {
        assert!(parse_latest_args(&[s("--bogus")]).is_err());
    }

    #[test]
    fn parse_latest_args_extra_root_needs_value() {
        // Flag with no following value → error path.
        let r = parse_latest_args(&[s("--session-id"), s("x"), s("--extra-projects-root")]);
        assert!(r.is_err());
    }

    #[test]
    fn parse_latest_args_now_needs_numeric_value() {
        let r = parse_latest_args(&[s("--session-id"), s("x"), s("--now"), s("notanumber")]);
        assert!(r.is_err());
    }

    #[test]
    fn parse_latest_args_accepts_all_flags() {
        let r = parse_latest_args(&[
            s("--session-id"), s("abc"),
            s("--projects-root"), s("/tmp/p"),
            s("--extra-projects-root"), s("/tmp/q"),
            s("--no-config"),
            s("--now"), s("1.5"),
        ]).unwrap();
        assert_eq!(r.session_id, "abc");
        assert_eq!(r.projects_root, Some(PathBuf::from("/tmp/p")));
        assert_eq!(r.extra_projects_roots, vec![PathBuf::from("/tmp/q")]);
        assert!(!r.read_config);
        assert_eq!(r.now_unix, Some(1.5));
    }

    #[test]
    fn parse_history_args_missing_period_errors() {
        assert!(parse_history_args(&[]).is_err());
    }

    #[test]
    fn parse_history_args_period_needs_value_and_parses() {
        assert!(parse_history_args(&[s("--period")]).is_err());
        assert!(parse_history_args(&[s("--period"), s("nope")]).is_err());
        let r = parse_history_args(&[s("--period"), s("60")]).unwrap();
        assert_eq!(r.period_seconds, 60);
    }

    #[test]
    fn parse_history_args_all_flag_value_errors() {
        for flag in ["--win-start", "--projects-root", "--extra-projects-root", "--now"] {
            assert!(
                parse_history_args(&[s("--period"), s("60"), s(flag)]).is_err(),
                "{} should error when value missing", flag
            );
        }
        assert!(parse_history_args(&[s("--period"), s("60"), s("--bogus")]).is_err());
    }

    #[test]
    fn parse_history_args_accepts_all_flags() {
        let r = parse_history_args(&[
            s("--period"), s("3600"),
            s("--win-start"), s("0"),
            s("--projects-root"), s("/tmp/p"),
            s("--extra-projects-root"), s("/tmp/q"),
            s("--no-config"),
            s("--now"), s("100.0"),
        ]).unwrap();
        assert_eq!(r.period_seconds, 3600);
        assert_eq!(r.win_start_unix, 0.0);
        assert_eq!(r.now_unix, Some(100.0));
        assert!(!r.read_config);
    }

    #[test]
    fn find_latest_in_path_returns_none_on_missing_file() {
        let re = beacon_re();
        let missing = PathBuf::from("/nonexistent/x.jsonl");
        assert!(find_latest_in_path(&missing, &re).is_none());
    }

    #[test]
    fn find_latest_in_path_picks_highest_ts_beacon() {
        let dir = tempdir_path("latest");
        let path = dir.join("session.jsonl");
        let line_a = format!(
            r#"{{"timestamp":"2025-01-01T00:00:00Z","message":{{"role":"assistant","content":[{{"type":"text","text":"<progress-beacon>{{\"kind\":\"report\",\"eta_seconds\":1,\"summary\":\"a\"}}</progress-beacon>"}}]}}}}"#
        );
        let line_b = format!(
            r#"{{"timestamp":"2025-01-02T00:00:00Z","message":{{"role":"assistant","content":[{{"type":"text","text":"<progress-beacon>{{\"kind\":\"report\",\"eta_seconds\":2,\"summary\":\"b\"}}</progress-beacon>"}}]}}}}"#
        );
        // Also include a non-assistant + a blank + a malformed JSON to exercise skip ladder.
        let lines = format!(
            "\n   \n{{junk\n{{\"message\":{{\"role\":\"user\"}}}}\n{line_a}\n{line_b}\n"
        );
        fs::write(&path, lines).unwrap();
        let re = beacon_re();
        let (b, ts) = find_latest_in_path(&path, &re).unwrap();
        assert_eq!(b.summary, "b");
        assert!(ts > 0.0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn collect_session_events_open_error_returns_empty() {
        let re = beacon_re();
        let se = collect_session_events_in_path(&PathBuf::from("/no/such.jsonl"), &re);
        assert!(se.beacons.is_empty() && se.events.is_empty());
    }

    #[test]
    fn collect_session_events_separates_user_and_assistant() {
        let dir = tempdir_path("collect");
        let path = dir.join("session.jsonl");
        let body = concat!(
            // real user prompt
            r#"{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"hello"}}"#,
            "\n",
            // tool_result user — should NOT count as a real user prompt
            r#"{"type":"user","timestamp":"2025-01-01T00:00:01Z","message":{"role":"user","content":[{"type":"tool_result"}]}}"#,
            "\n",
            // assistant with embedded beacon
            r#"{"timestamp":"2025-01-01T00:00:02Z","message":{"role":"assistant","content":[{"type":"text","text":"<progress-beacon>{\"kind\":\"begin\",\"eta_seconds\":10,\"summary\":\"x\"}</progress-beacon>"}]}}"#,
            "\n",
        );
        fs::write(&path, body).unwrap();
        let re = beacon_re();
        let se = collect_session_events_in_path(&path, &re);
        assert_eq!(se.beacons.len(), 1);
        // Three events: real user (true), tool_result user (false), assistant (false).
        assert_eq!(se.events.len(), 3);
        // Sorted ascending by timestamp.
        assert!(se.events[0].0 <= se.events[1].0);
        assert!(se.events[1].0 <= se.events[2].0);
        // The real-user flag is set on the first event.
        assert!(se.events[0].1, "first event should be real user prompt");
        // Tool-result user must NOT be flagged as real user.
        assert!(!se.events[1].1);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_history_groups_parents_and_subagents() {
        let root = tempdir_path("hist-disc");
        let slug = root.join("slug");
        fs::create_dir_all(&slug).unwrap();
        fs::write(slug.join("sid-1.jsonl"), b"").unwrap();
        let subagents = slug.join("sid-2").join("subagents");
        fs::create_dir_all(&subagents).unwrap();
        fs::write(subagents.join("agent-x.jsonl"), b"").unwrap();
        let groups = discover_history_groups(&[root.clone()]);
        let k1 = ("slug".to_string(), "sid-1".to_string());
        let k2 = ("slug".to_string(), "sid-2".to_string());
        assert!(groups.contains_key(&k1));
        assert!(groups.contains_key(&k2));
        let _ = fs::remove_dir_all(&root);
    }
}
