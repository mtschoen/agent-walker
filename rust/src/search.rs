// Search subcommand: substring/regex match across transcript content.
// See ../SPEC.md (post-merge) or
// skills-dev/docs/superpowers/specs/claude-walker-search.md (pre-merge)
// for the CLI contract.

use rayon::prelude::*;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs::{metadata, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::time::{Instant, UNIX_EPOCH};

use crate::content::{extract_text, is_only_tool_blocks};
use crate::{current_unix, default_projects_root, parse_iso8601, walker_roots};

// === Flag types ===

#[derive(Clone, Copy, Debug, PartialEq)]
enum Role {
    User,
    Assistant,
    Both,
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Format {
    Pretty,
    Jsonl,
}

struct SearchArgs {
    pattern: String,
    regex: bool,
    case_sensitive: bool,
    role: Role,
    since: Option<f64>,
    until: Option<f64>,
    cwd: Option<String>,
    context: u32,
    limit: u32,
    count_only: bool,
    include_tool_blocks: bool,
    format: Format,
    snippet_chars: u32,
    projects_root: PathBuf,
    extra_projects_roots: Vec<PathBuf>,
    read_config: bool,
}

// === Arg parsing ===

fn parse_args(raw: &[String]) -> Result<SearchArgs, String> {
    let mut pattern: Option<String> = None;
    let mut regex = false;
    let mut case_sensitive = false;
    let mut role = Role::Both;
    let mut since_raw: Option<String> = None;
    let mut until_raw: Option<String> = None;
    let mut cwd: Option<String> = None;
    let mut any_cwd_explicit = false;
    let mut context: u32 = 1;
    let mut limit: u32 = 50;
    let mut count_only = false;
    let mut include_tool_blocks = false;
    let mut format = Format::Pretty;
    let mut snippet_chars: u32 = 240;
    let mut projects_root: Option<PathBuf> = None;
    let mut extra_projects_roots: Vec<PathBuf> = Vec::new();
    let mut read_config = true;
    let mut now_override: Option<f64> = None;

    let mut iter = raw.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--regex" => regex = true,
            "--case-sensitive" => case_sensitive = true,
            "--role" => {
                let v = iter.next().ok_or("--role needs a value")?;
                role = match v.as_str() {
                    "user" => Role::User,
                    "assistant" => Role::Assistant,
                    "both" => Role::Both,
                    other => return Err(format!(
                        "--role: invalid value {other}; expected user|assistant|both"
                    )),
                };
            }
            "--since" => since_raw = Some(iter.next().ok_or("--since needs a value")?.clone()),
            "--until" => until_raw = Some(iter.next().ok_or("--until needs a value")?.clone()),
            "--cwd" => cwd = Some(iter.next().ok_or("--cwd needs a value")?.clone()),
            "--any-cwd" => any_cwd_explicit = true,
            "--context" => {
                context = iter.next().ok_or("--context needs a value")?
                    .parse().map_err(|e| format!("--context: {e}"))?;
            }
            "--limit" => {
                limit = iter.next().ok_or("--limit needs a value")?
                    .parse().map_err(|e| format!("--limit: {e}"))?;
            }
            "--count-only" => count_only = true,
            "--include-tool-blocks" => include_tool_blocks = true,
            "--format" => {
                let v = iter.next().ok_or("--format needs a value")?;
                format = match v.as_str() {
                    "pretty" => Format::Pretty,
                    "jsonl" => Format::Jsonl,
                    other => return Err(format!(
                        "--format: invalid value {other}; expected pretty|jsonl"
                    )),
                };
            }
            "--snippet-chars" => {
                snippet_chars = iter.next().ok_or("--snippet-chars needs a value")?
                    .parse().map_err(|e| format!("--snippet-chars: {e}"))?;
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
                now_override = Some(iter.next().ok_or("--now needs a value")?
                    .parse().map_err(|e| format!("--now: {e}"))?);
            }
            s if s.starts_with("--") => return Err(format!("unknown flag: {s}")),
            _ => {
                if pattern.is_some() {
                    return Err(format!("unexpected positional argument: {arg}"));
                }
                pattern = Some(arg.clone());
            }
        }
    }

    let pattern = pattern.ok_or("pattern must be non-empty")?;
    if pattern.is_empty() {
        return Err("pattern must be non-empty".into());
    }
    if cwd.is_some() && any_cwd_explicit {
        return Err("--cwd and --any-cwd are mutually exclusive".into());
    }

    let now = now_override.unwrap_or_else(current_unix);
    let since = match since_raw {
        Some(s) => Some(parse_time_arg(&s, now).map_err(|e| format!("bad time: --since={s} ({e})"))?),
        None => None,
    };
    let until = match until_raw {
        Some(s) => Some(parse_time_arg(&s, now).map_err(|e| format!("bad time: --until={s} ({e})"))?),
        None => None,
    };

    Ok(SearchArgs {
        pattern, regex, case_sensitive, role, since, until, cwd,
        context, limit, count_only, include_tool_blocks, format, snippet_chars,
        projects_root: projects_root.unwrap_or_else(default_projects_root),
        extra_projects_roots,
        read_config,
    })
}

fn parse_time_arg(s: &str, now: f64) -> Result<f64, String> {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return Err("empty value".into());
    }
    if let Some(last) = trimmed.chars().last() {
        if matches!(last, 'd' | 'h' | 'm' | 's') {
            let head = &trimmed[..trimmed.len() - last.len_utf8()];
            if !head.is_empty() && head.chars().all(|c| c.is_ascii_digit() || c == '.') {
                let n: f64 = head.parse().map_err(|e| format!("relative prefix: {e}"))?;
                let secs = match last {
                    'd' => n * 86_400.0,
                    'h' => n * 3_600.0,
                    'm' => n * 60.0,
                    's' => n,
                    _ => unreachable!(),
                };
                return Ok(now - secs);
            }
        }
    }
    parse_iso8601(trimmed)
        .ok_or_else(|| format!("not RFC3339 or relative: {trimmed}"))
}

// === Scan / file IO ===

#[derive(Debug)]
struct ScanMessage {
    line_number: u32,
    timestamp: Option<f64>,
    timestamp_str: String,
    role: String,
    text_default: String,
    text_with_tools: String,
    is_only_tool_blocks: bool,
}

fn scan_file(path: &Path) -> Vec<ScanMessage> {
    let mut out: Vec<ScanMessage> = Vec::new();
    let file = match File::open(path) {
        Ok(f) => f,
        Err(_) => return out,
    };
    for (idx, line) in BufReader::new(file).lines().enumerate() {
        let line = match line {
            Ok(l) => l,
            Err(_) => continue,
        };
        if line.trim().is_empty() {
            continue;
        }
        let entry: Value = match serde_json::from_str(&line) {
            Ok(e) => e,
            Err(_) => continue,
        };
        let message = match entry.get("message") {
            Some(m) => m,
            None => continue,
        };
        let role = message.get("role").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if role.is_empty() {
            continue;
        }
        let content = match message.get("content") {
            Some(c) => c,
            None => continue,
        };
        let timestamp_str = entry.get("timestamp")
            .and_then(|v| v.as_str()).unwrap_or("").to_string();
        let timestamp = if timestamp_str.is_empty() {
            None
        } else {
            parse_iso8601(&timestamp_str)
        };
        let text_default = extract_text(content, false);
        let text_with_tools = extract_text(content, true);
        let only_tool_blocks = is_only_tool_blocks(content);
        out.push(ScanMessage {
            line_number: (idx + 1) as u32,
            timestamp,
            timestamp_str,
            role,
            text_default,
            text_with_tools,
            is_only_tool_blocks: only_tool_blocks,
        });
    }
    out
}

// === Discovery ===

struct DiscoveredFile {
    path: PathBuf,
    slug: String,
    session_id: String,
    host_root: String,
}

fn discover_files(
    roots: &[PathBuf],
    since: Option<f64>,
    cwd_slug: Option<&str>,
) -> Vec<DiscoveredFile> {
    let mut files: Vec<DiscoveredFile> = Vec::new();
    let slug_pat = cwd_slug.unwrap_or("*");
    for root in roots {
        let host_root = root.display().to_string();
        let parent_pattern = format!("{}/{}/*.jsonl", root.display(), slug_pat);
        if let Ok(entries) = glob::glob(&parent_pattern) {
            for entry in entries.flatten() {
                if let Some(cutoff) = since {
                    if let Ok(meta) = metadata(&entry) {
                        if let Ok(mt) = meta.modified() {
                            if let Ok(d) = mt.duration_since(UNIX_EPOCH) {
                                if d.as_secs_f64() < cutoff {
                                    continue;
                                }
                            }
                        }
                    }
                }
                let slug = entry.parent()
                    .and_then(|p| p.file_name())
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();
                let sid = entry.file_stem()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_default();
                files.push(DiscoveredFile {
                    path: entry,
                    slug,
                    session_id: sid,
                    host_root: host_root.clone(),
                });
            }
        }
    }
    files
}

// === Pattern matching ===

fn build_pattern_regex(args: &SearchArgs) -> Result<Regex, String> {
    let body = if args.regex {
        args.pattern.clone()
    } else {
        regex::escape(&args.pattern)
    };
    let full = if args.case_sensitive {
        body
    } else {
        format!("(?i){}", body)
    };
    Regex::new(&full).map_err(|e| format!("bad regex: {e}"))
}

// Find all non-overlapping match ranges within `text`. Returns byte offsets.
fn find_all_matches(re: &Regex, text: &str) -> Vec<(usize, usize)> {
    re.find_iter(text).map(|m| (m.start(), m.end())).collect()
}

// === Snippet generation ===

fn nudge_char_boundary(text: &str, mut idx: usize) -> usize {
    while idx < text.len() && !text.is_char_boundary(idx) {
        idx += 1;
    }
    idx
}

// Nudge `cut` outward toward the nearest whitespace within ±max_nudge bytes,
// favoring the direction given (negative = left/decrease, positive = right).
fn nudge_to_whitespace(text: &str, cut: usize, direction: i32, max_nudge: usize) -> usize {
    if cut == 0 || cut == text.len() {
        return cut;
    }
    let bytes = text.as_bytes();
    if direction < 0 {
        // Walk left up to max_nudge looking for whitespace.
        let lo = cut.saturating_sub(max_nudge);
        for i in (lo..cut).rev() {
            if text.is_char_boundary(i) && bytes[i].is_ascii_whitespace() {
                return i + 1;
            }
        }
    } else {
        let hi = (cut + max_nudge).min(text.len());
        for (i, &b) in bytes.iter().enumerate().take(hi).skip(cut) {
            if text.is_char_boundary(i) && b.is_ascii_whitespace() {
                return i;
            }
        }
    }
    cut
}

fn make_snippet(text: &str, first_match: (usize, usize), snippet_chars: u32) -> String {
    let half = (snippet_chars / 2) as usize;
    let (mstart, mend) = first_match;
    let raw_lo = mstart.saturating_sub(half);
    let raw_hi = (mend + half).min(text.len());
    let lo = nudge_char_boundary(text, raw_lo);
    let hi = nudge_char_boundary(text, raw_hi);
    // Only nudge to whitespace if we actually clipped on that side.
    let lo = if lo > 0 { nudge_to_whitespace(text, lo, -1, 20) } else { lo };
    let hi = if hi < text.len() { nudge_to_whitespace(text, hi, 1, 20) } else { hi };
    let lo = nudge_char_boundary(text, lo);
    let hi = nudge_char_boundary(text, hi);
    text[lo..hi].to_string()
}

// === Hit assembly ===

struct Hit {
    timestamp: f64,                 // for sorting; not emitted
    timestamp_str: String,
    session_id: String,
    cwd_slug: String,
    host_root: String,
    file_path: String,
    line_number: u32,
    role: String,
    snippet: String,
    match_offsets: Vec<(usize, usize)>,
    context_before: Vec<ContextTurn>,
    context_after: Vec<ContextTurn>,
}

struct ContextTurn {
    role: String,
    text: String,
    timestamp: String,
}

fn role_matches(filter: Role, role: &str) -> bool {
    matches!(
        (filter, role),
        (Role::Both, _) | (Role::User, "user") | (Role::Assistant, "assistant")
    )
}

fn build_context_turns(
    messages: &[ScanMessage],
    hit_idx: usize,
    context_n: u32,
) -> (Vec<ContextTurn>, Vec<ContextTurn>) {
    if context_n == 0 {
        return (vec![], vec![]);
    }
    let n = context_n as usize;
    let before: Vec<ContextTurn> = (hit_idx.saturating_sub(n)..hit_idx)
        .map(|i| ContextTurn {
            role: messages[i].role.clone(),
            text: messages[i].text_default.clone(),
            timestamp: messages[i].timestamp_str.clone(),
        })
        .collect();
    let end = (hit_idx + 1 + n).min(messages.len());
    let after: Vec<ContextTurn> = (hit_idx + 1..end)
        .map(|i| ContextTurn {
            role: messages[i].role.clone(),
            text: messages[i].text_default.clone(),
            timestamp: messages[i].timestamp_str.clone(),
        })
        .collect();
    (before, after)
}

fn process_file(
    file: &DiscoveredFile,
    args: &SearchArgs,
    re: &Regex,
) -> Vec<Hit> {
    let messages = scan_file(&file.path);
    let mut hits: Vec<Hit> = Vec::new();
    for (idx, m) in messages.iter().enumerate() {
        if !role_matches(args.role, &m.role) {
            continue;
        }
        if !args.include_tool_blocks && m.is_only_tool_blocks {
            continue;
        }
        if let Some(ts) = m.timestamp {
            if let Some(s) = args.since {
                if ts < s {
                    continue;
                }
            }
            if let Some(u) = args.until {
                if ts > u {
                    continue;
                }
            }
        } else if args.since.is_some() || args.until.is_some() {
            // No parseable timestamp + a time filter → skip (safer than including).
            continue;
        }
        let searchable_text = if args.include_tool_blocks {
            &m.text_with_tools
        } else {
            &m.text_default
        };
        if searchable_text.is_empty() {
            continue;
        }
        let matches = find_all_matches(re, searchable_text);
        if matches.is_empty() {
            continue;
        }
        let snippet = make_snippet(searchable_text, matches[0], args.snippet_chars);
        // Match offsets are snippet-relative; re-find inside the snippet so
        // any matches that fall within the window (not just the first) are
        // captured. The first match is always wholly inside the snippet
        // because the window is centered on it.
        let snippet_matches = find_all_matches(re, &snippet);
        let (context_before, context_after) = build_context_turns(&messages, idx, args.context);
        hits.push(Hit {
            timestamp: m.timestamp.unwrap_or(0.0),
            timestamp_str: m.timestamp_str.clone(),
            session_id: file.session_id.clone(),
            cwd_slug: file.slug.clone(),
            host_root: file.host_root.clone(),
            file_path: file.path.display().to_string(),
            line_number: m.line_number,
            role: m.role.clone(),
            snippet,
            match_offsets: snippet_matches,
            context_before,
            context_after,
        });
    }
    hits
}

// === Output ===

fn hit_to_json(h: &Hit) -> Value {
    json!({
        "type": "hit",
        "session_id": h.session_id,
        "cwd_slug": h.cwd_slug,
        "host_root": h.host_root,
        "file_path": h.file_path,
        "line_number": h.line_number,
        "timestamp": h.timestamp_str,
        "role": h.role,
        "snippet": h.snippet,
        "match_offsets": h.match_offsets.iter()
            .map(|(a, b)| json!([a, b]))
            .collect::<Vec<_>>(),
        "context_before": h.context_before.iter()
            .map(|t| json!({"role": t.role, "text": t.text, "timestamp": t.timestamp}))
            .collect::<Vec<_>>(),
        "context_after": h.context_after.iter()
            .map(|t| json!({"role": t.role, "text": t.text, "timestamp": t.timestamp}))
            .collect::<Vec<_>>(),
    })
}

/// Aggregate counters for a completed search, shared by the JSON-summary and
/// pretty-print output paths (replaces a 6-arg group threaded through both).
struct SearchSummary {
    total_hits: usize,
    sessions_matched: usize,
    roots_walked: usize,
    files_walked: usize,
    truncated: bool,
    elapsed_ms: u64,
}

fn summary_json(summary: &SearchSummary) -> Value {
    json!({
        "type": "summary",
        "hits": summary.total_hits,
        "sessions_matched": summary.sessions_matched,
        "roots_walked": summary.roots_walked,
        "files_walked": summary.files_walked,
        "truncated": summary.truncated,
        "elapsed_ms": summary.elapsed_ms,
    })
}

fn write_jsonl(hits: &[Hit], summary: &Value, suppress_hits: bool) {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    if !suppress_hits {
        for h in hits {
            let _ = writeln!(out, "{}", hit_to_json(h));
        }
    }
    let _ = writeln!(out, "{}", summary);
}

fn write_pretty(hits: &[Hit], summary: &SearchSummary, suppress_hits: bool) {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    if !suppress_hits {
        for h in hits {
            let _ = writeln!(
                out,
                "[{}] cwd={} role={} session={}",
                h.timestamp_str, h.cwd_slug, h.role, h.session_id
            );
            let _ = writeln!(out, "  {}:{}", h.file_path, h.line_number);
            for t in &h.context_before {
                let _ = writeln!(out, "  before: {}", truncate(&t.text, 120));
            }
            if let Some((mstart, mend)) = h.match_offsets.first() {
                let pre = &h.snippet[..(*mstart).min(h.snippet.len())];
                let mid = &h.snippet[*mstart..(*mend).min(h.snippet.len())];
                let post = &h.snippet[(*mend).min(h.snippet.len())..];
                let _ = writeln!(out, "  >>> {}[{}]{} <<<", pre, mid, post);
            } else {
                let _ = writeln!(out, "  {}", h.snippet);
            }
            for t in &h.context_after {
                let _ = writeln!(out, "  after:  {}", truncate(&t.text, 120));
            }
            let _ = writeln!(out);
        }
    }
    let _ = writeln!(
        out,
        "{} hits in {} sessions across {} roots ({} files). truncated={} elapsed {}ms.",
        summary.total_hits, summary.sessions_matched, summary.roots_walked,
        summary.files_walked, summary.truncated, summary.elapsed_ms
    );
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        let mut cut = max;
        while cut > 0 && !s.is_char_boundary(cut) {
            cut -= 1;
        }
        format!("{}…", &s[..cut])
    }
}

// === Top-level run ===

pub fn run(raw: &[String]) {
    let started = Instant::now();
    let args = match parse_args(raw) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("walker: search: {e}");
            std::process::exit(2);
        }
    };

    let re = match build_pattern_regex(&args) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("walker: search: {e}");
            std::process::exit(2);
        }
    };

    let roots = walker_roots::resolve_roots(
        args.projects_root.clone(),
        &args.extra_projects_roots,
        args.read_config,
    );
    let files = discover_files(&roots, args.since, args.cwd.as_deref());
    let files_walked = files.len();
    let roots_walked = roots.len();

    // Parallel per-file scan. Work unit = one file; each rayon task returns a
    // local Vec<Hit> which we concat via reduce. The sort below restores the
    // deterministic ordering required by SPEC. regex::Regex is Send+Sync for
    // read-only matching, and serde_json::from_str is per-call thread-safe,
    // so the shared `re` is fine. Each file carries its own host_root (the
    // root it was discovered under). Mirrors the cost-mode pattern in
    // main.rs::run_cost.
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

    let mut hits: Vec<Hit> = pool.install(|| {
        files
            .par_iter()
            .map(|f| process_file(f, &args, &re))
            .reduce(Vec::new, |mut acc, mut next| {
                if acc.is_empty() {
                    next
                } else {
                    acc.append(&mut next);
                    acc
                }
            })
    });

    // Sort newest first by timestamp; tiebreak by (session_id, line_number) for
    // deterministic ordering when timestamps collide.
    hits.sort_by(|a, b| {
        b.timestamp.partial_cmp(&a.timestamp).unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.session_id.cmp(&b.session_id))
            .then_with(|| a.line_number.cmp(&b.line_number))
    });

    // sessions_matched is counted BEFORE truncation: how many distinct sessions
    // had any matching message at all.
    let pre_truncate_sessions: HashSet<String> = hits.iter()
        .map(|h| format!("{}/{}", h.cwd_slug, h.session_id))
        .collect();
    let sessions_matched = pre_truncate_sessions.len();

    let total_unfiltered = hits.len();
    let truncated = total_unfiltered > args.limit as usize;
    if truncated {
        hits.truncate(args.limit as usize);
    }

    let elapsed_ms = started.elapsed().as_millis() as u64;
    let summary_stats = SearchSummary {
        total_hits: if args.count_only { total_unfiltered } else { hits.len() },
        sessions_matched,
        roots_walked,
        files_walked,
        truncated,
        elapsed_ms,
    };

    match args.format {
        Format::Jsonl => write_jsonl(&hits, &summary_json(&summary_stats), args.count_only),
        Format::Pretty => write_pretty(&hits, &summary_stats, args.count_only),
    }

    if truncated {
        eprintln!(
            "walker: search: truncated to --limit={} (had {} total); narrow with --since",
            args.limit, total_unfiltered
        );
    }
}
