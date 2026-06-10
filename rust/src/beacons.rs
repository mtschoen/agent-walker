// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.

use rayon::prelude::*;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
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

#[derive(Serialize, Clone, Debug)]
struct Beacon {
    kind: String,
    eta_seconds: f64,
    summary: String,
    /// Optional per SPEC: parses when absent, passed through when present, and
    /// omitted from beacons-latest output when the source beacon lacked it.
    #[serde(skip_serializing_if = "Option::is_none")]
    drift: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    beats_left: Option<i64>,
}

/// Deserialization shape: `eta_seconds` lands as an Option so `parse_beacon`
/// can apply the kind-aware rule — required for begin/report, optional for
/// end (defaults to 0). Agents routinely omit `eta_seconds` on end beacons,
/// and rejecting those left lifecycles permanently open. SPEC beacons-latest.
#[derive(Deserialize)]
struct RawBeacon {
    kind: String,
    #[serde(default)]
    eta_seconds: Option<f64>,
    summary: String,
    #[serde(default)]
    drift: Option<String>,
    #[serde(default)]
    beats_left: Option<i64>,
}

fn parse_beacon(body: &str) -> Option<Beacon> {
    let raw: RawBeacon = serde_json::from_str(body).ok()?;
    let eta_seconds = match raw.eta_seconds {
        Some(v) => v,
        None if raw.kind == "end" => 0.0,
        None => return None,
    };
    Some(Beacon {
        kind: raw.kind,
        eta_seconds,
        summary: raw.summary,
        drift: raw.drift,
        beats_left: raw.beats_left,
    })
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
    let mut reader = BufReader::new(file);
    // Reused line buffer; see walk_group in main.rs for the rationale.
    let mut line = String::with_capacity(8 * 1024);
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {}
            Err(_) => continue,
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let entry: Entry = match serde_json::from_str(trimmed) {
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
            // Group 1 is non-optional in the beacon regex, so index directly.
            if let Some(b) = parse_beacon(&caps[1]) {
                entry_beacon = Some(b);
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
    let mut reader = BufReader::new(file);
    // Reused line buffer; see walk_group in main.rs for the rationale.
    let mut buf = String::with_capacity(8 * 1024);
    loop {
        buf.clear();
        match reader.read_line(&mut buf) {
            Ok(0) => break,
            Ok(_) => {}
            Err(_) => continue,
        }
        let line = buf.trim();
        if line.is_empty() {
            continue;
        }
        let entry: Entry = match serde_json::from_str(line) {
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
            // Group 1 is non-optional in the beacon regex, so index directly.
            if let Some(b) = parse_beacon(&caps[1]) {
                beacons.push((b, ts));
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

/// Locate the transcript(s) for a single session id by walking the directory
/// tree directly, rather than compiling and matching two `glob` patterns.
/// Mirrors the glob semantics it replaces:
///   parent:   <root>/*/<session_id>.jsonl
///   subagent: <root>/*/*/subagents/agent-<session_id>.jsonl
/// For the parent form this probes one file per slug dir (a single stat)
/// instead of listing every slug dir's contents the way `glob` does, which is
/// the bulk of the win on a large fleet. Result order is irrelevant: the
/// caller reduces over the matches with `max_by` on timestamp.
fn discover_latest_paths(roots: &[PathBuf], session_id: &str) -> Vec<PathBuf> {
    let parent_name = format!("{session_id}.jsonl");
    let sub_name = format!("agent-{session_id}.jsonl");
    let mut paths: Vec<PathBuf> = Vec::new();
    for root in roots {
        let slug_entries = match std::fs::read_dir(root) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for slug in slug_entries.flatten() {
            if !slug.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                continue;
            }
            let slug_path = slug.path();
            // Parent transcript: <root>/<slug>/<session_id>.jsonl
            let parent = slug_path.join(&parent_name);
            if parent.is_file() {
                paths.push(parent);
            }
            // Subagent transcripts live one level deeper, under each session
            // directory's `subagents/` folder.
            let session_entries = match std::fs::read_dir(&slug_path) {
                Ok(e) => e,
                Err(_) => continue,
            };
            for session in session_entries.flatten() {
                if !session.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                    continue;
                }
                let sub = session.path().join("subagents").join(&sub_name);
                if sub.is_file() {
                    paths.push(sub);
                }
            }
        }
    }
    paths
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
    let roots =
        walker_roots::resolve_roots(primary, &parsed.extra_projects_roots, parsed.read_config);
    let now_unix = parsed.now_unix.unwrap_or_else(current_unix);

    // Try parent transcript first, then any subagent transcript, across
    // every resolved root.
    let paths = discover_latest_paths(&roots, &parsed.session_id);

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
    let roots =
        walker_roots::resolve_roots(primary, &parsed.extra_projects_roots, parsed.read_config);

    // Same discovery as cost/events, with the mtime prune disabled: history
    // pairing must see every transcript regardless of age.
    let groups = crate::transcript::discover_groups(&roots, f64::NEG_INFINITY);
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

    fn s(x: &str) -> String {
        x.to_string()
    }

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
            s("--session-id"),
            s("abc"),
            s("--projects-root"),
            s("/tmp/p"),
            s("--extra-projects-root"),
            s("/tmp/q"),
            s("--no-config"),
            s("--now"),
            s("1.5"),
        ])
        .unwrap();
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
        for flag in [
            "--win-start",
            "--projects-root",
            "--extra-projects-root",
            "--now",
        ] {
            assert!(
                parse_history_args(&[s("--period"), s("60"), s(flag)]).is_err(),
                "{} should error when value missing",
                flag
            );
        }
        assert!(parse_history_args(&[s("--period"), s("60"), s("--bogus")]).is_err());
    }

    #[test]
    fn parse_history_args_accepts_all_flags() {
        let r = parse_history_args(&[
            s("--period"),
            s("3600"),
            s("--win-start"),
            s("0"),
            s("--projects-root"),
            s("/tmp/p"),
            s("--extra-projects-root"),
            s("/tmp/q"),
            s("--no-config"),
            s("--now"),
            s("100.0"),
        ])
        .unwrap();
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
        let line_a = r#"{"timestamp":"2025-01-01T00:00:00Z","message":{"role":"assistant","content":[{"type":"text","text":"<progress-beacon>{\"kind\":\"report\",\"eta_seconds\":1,\"summary\":\"a\"}</progress-beacon>"}]}}"#.to_string();
        let line_b = r#"{"timestamp":"2025-01-02T00:00:00Z","message":{"role":"assistant","content":[{"type":"text","text":"<progress-beacon>{\"kind\":\"report\",\"eta_seconds\":2,\"summary\":\"b\"}</progress-beacon>"}]}}"#.to_string();
        // Also include a non-assistant + a blank + a malformed JSON to exercise skip ladder.
        let lines =
            format!("\n   \n{{junk\n{{\"message\":{{\"role\":\"user\"}}}}\n{line_a}\n{line_b}\n");
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
        // History discovery is shared transcript::discover_groups with the
        // mtime prune disabled; assert the no-prune call still finds both
        // parent and subagent layouts.
        let root = tempdir_path("hist-disc");
        let slug = root.join("slug");
        fs::create_dir_all(&slug).unwrap();
        fs::write(slug.join("sid-1.jsonl"), b"").unwrap();
        let subagents = slug.join("sid-2").join("subagents");
        fs::create_dir_all(&subagents).unwrap();
        fs::write(subagents.join("agent-x.jsonl"), b"").unwrap();
        let groups =
            crate::transcript::discover_groups(std::slice::from_ref(&root), f64::NEG_INFINITY);
        let k1 = ("slug".to_string(), "sid-1".to_string());
        let k2 = ("slug".to_string(), "sid-2".to_string());
        assert!(groups.contains_key(&k1));
        assert!(groups.contains_key(&k2));
        let _ = fs::remove_dir_all(&root);
    }

    /// discover_latest_paths returns empty when the root directory is unreadable.
    #[cfg(unix)]
    #[test]
    fn discover_latest_paths_unreadable_root_returns_empty() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempdir_path("latest-unreadable-root");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o000)).unwrap();
        let paths = discover_latest_paths(std::slice::from_ref(&root), "any-session");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);
        assert!(
            paths.is_empty(),
            "unreadable root should yield no paths"
        );
    }

    /// discover_latest_paths silently skips a slug directory that is unreadable
    /// when listing session entries within it.
    #[cfg(unix)]
    #[test]
    fn discover_latest_paths_unreadable_slug_dir_skipped() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempdir_path("latest-unreadable-slug");

        // Bad slug: a directory we can see from the root but cannot read.
        let bad_slug = root.join("bad-slug");
        fs::create_dir_all(&bad_slug).unwrap();
        fs::set_permissions(&bad_slug, fs::Permissions::from_mode(0o000)).unwrap();

        // Good slug: has a matching parent transcript.
        let good_slug = root.join("good-slug");
        fs::create_dir_all(&good_slug).unwrap();
        fs::write(good_slug.join("target-session.jsonl"), b"").unwrap();

        let paths = discover_latest_paths(std::slice::from_ref(&root), "target-session");

        fs::set_permissions(&bad_slug, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);

        // The bad slug is skipped; the good slug's transcript is found.
        assert_eq!(paths.len(), 1, "expected exactly one path from the good slug");
        assert!(
            paths[0].ends_with("target-session.jsonl"),
            "unexpected path: {:?}",
            paths[0]
        );
    }
}
