// Shared transcript parsing, discovery, and pricing helpers.
// Used by both cost mode (main.rs) and the events subcommand (events.rs).
// Extracted from main.rs so both modules reference a single definition.

use std::collections::HashMap;
use std::fs::metadata;
use std::path::PathBuf;
use std::time::UNIX_EPOCH;

use serde::Deserialize;

// ── Entry types ──────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub(crate) struct Entry {
    pub(crate) timestamp: Option<String>,
    pub(crate) message: Option<Message>,
}

#[derive(Deserialize)]
pub(crate) struct Message {
    pub(crate) role: Option<String>,
    pub(crate) id: Option<String>,
    pub(crate) model: Option<String>,
    pub(crate) usage: Option<Usage>,
}

#[derive(Deserialize, Default)]
pub(crate) struct Usage {
    #[serde(default)]
    pub(crate) input_tokens: u64,
    #[serde(default)]
    pub(crate) output_tokens: u64,
    #[serde(default)]
    pub(crate) cache_read_input_tokens: u64,
    #[serde(default)]
    pub(crate) cache_creation_input_tokens: u64,
    #[serde(default)]
    pub(crate) server_tool_use: ServerToolUse,
}

#[derive(Deserialize, Default)]
pub(crate) struct ServerToolUse {
    #[serde(default)]
    pub(crate) web_search_requests: u64,
}

// ── Pricing ───────────────────────────────────────────────────────────────────

/// Flat charge per server-side web search request (billed $10 / 1,000),
/// added on top of token cost. Matches SPEC.md and the Python reference.
pub(crate) const WEB_SEARCH_COST_USD: f64 = 0.01;

/// (input_per_mtok, output_per_mtok). Matches SPEC.md exactly.
pub(crate) fn rates_for(model: &str) -> (f64, f64) {
    let m = model.to_ascii_lowercase();
    if m.contains("opus") {
        (5.0, 25.0)
    } else if m.contains("haiku") {
        (1.0, 5.0)
    } else {
        // sonnet — and any unknown model falls back to sonnet rates, per SPEC.md
        (3.0, 15.0)
    }
}

pub(crate) fn cost_for(usage: &Usage, model: &str) -> f64 {
    let (i_rate, o_rate) = rates_for(model);
    let token_cost = (usage.input_tokens as f64 * i_rate
        + usage.cache_read_input_tokens as f64 * i_rate * 0.10
        + usage.cache_creation_input_tokens as f64 * i_rate * 1.25
        + usage.output_tokens as f64 * o_rate)
        / 1_000_000.0;
    token_cost + usage.server_tool_use.web_search_requests as f64 * WEB_SEARCH_COST_USD
}

// ── Discovery ─────────────────────────────────────────────────────────────────

/// Discover all `.jsonl` files under `roots`, grouped by `(slug, session_id)`.
/// Files whose mtime is earlier than `earliest` are skipped (fast-path prune).
pub(crate) fn discover_groups(
    roots: &[PathBuf],
    earliest: f64,
) -> HashMap<(String, String), Vec<PathBuf>> {
    let mut groups: HashMap<(String, String), Vec<PathBuf>> = HashMap::new();

    for root in roots {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::SystemTime;

    fn tempdir_path(suffix: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        let pid = std::process::id();
        let nanos = std::time::SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        p.push(format!("rust-transcript-test-{suffix}-{pid}-{nanos}"));
        fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn rates_for_opus_haiku_default() {
        assert_eq!(rates_for("claude-3-opus-20240229"), (5.0, 25.0));
        assert_eq!(rates_for("CLAUDE-OPUS-4-5"), (5.0, 25.0));
        assert_eq!(rates_for("claude-3-5-haiku"), (1.0, 5.0));
        assert_eq!(rates_for("claude-3-5-sonnet"), (3.0, 15.0));
        assert_eq!(rates_for("unknown-model"), (3.0, 15.0));
    }

    #[test]
    fn cost_for_includes_web_search_flat_fee() {
        let usage = Usage {
            input_tokens: 0,
            output_tokens: 0,
            cache_read_input_tokens: 0,
            cache_creation_input_tokens: 0,
            server_tool_use: ServerToolUse { web_search_requests: 3 },
        };
        // 3 * $0.01 = $0.03, no token cost.
        assert!((cost_for(&usage, "sonnet") - 0.03).abs() < 1e-9);
    }

    #[test]
    fn cost_for_token_breakdown() {
        // 1M input @ sonnet ($3) + 1M output @ sonnet ($15) = $18
        let usage = Usage {
            input_tokens: 1_000_000,
            output_tokens: 1_000_000,
            cache_read_input_tokens: 0,
            cache_creation_input_tokens: 0,
            server_tool_use: ServerToolUse::default(),
        };
        assert!((cost_for(&usage, "sonnet") - 18.0).abs() < 1e-6);
        // cache_read at 0.1× input rate; cache_creation at 1.25× input rate.
        let usage2 = Usage {
            input_tokens: 0,
            output_tokens: 0,
            cache_read_input_tokens: 10_000_000,    // 10M * $3 * 0.10 = $3
            cache_creation_input_tokens: 800_000,   // 800k * $3 * 1.25 = $3
            server_tool_use: ServerToolUse::default(),
        };
        assert!((cost_for(&usage2, "sonnet") - 6.0).abs() < 1e-6);
    }

    #[test]
    fn discover_groups_prunes_old_mtimes() {
        // Cover lines 96/122 (mtime < earliest continue). We pass an
        // `earliest` far in the future, so the just-created file's mtime
        // is necessarily below it and the entry is pruned.
        let root = tempdir_path("prune");
        let slug = root.join("test-slug");
        fs::create_dir_all(&slug).unwrap();
        fs::write(slug.join("session-1.jsonl"), b"").unwrap();
        // Mirror in a subagent layout to cover the second prune branch.
        let subagents = slug.join("session-2").join("subagents");
        fs::create_dir_all(&subagents).unwrap();
        fs::write(subagents.join("agent-a.jsonl"), b"").unwrap();

        let far_future = (SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)) + 1e9;
        let groups = discover_groups(&[root.clone()], far_future);
        assert!(groups.is_empty(), "future cutoff should prune everything, got {:?}", groups);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn discover_groups_includes_subagent_files() {
        let root = tempdir_path("subagent");
        let session_dir = root.join("slug").join("session-1");
        let subagents = session_dir.join("subagents");
        fs::create_dir_all(&subagents).unwrap();
        let agent_file = subagents.join("agent-aaa.jsonl");
        fs::write(&agent_file, b"").unwrap();
        let groups = discover_groups(&[root.clone()], 0.0);
        // Subagent file should be discovered under (slug, session-1).
        let key = ("slug".to_string(), "session-1".to_string());
        assert!(groups.contains_key(&key), "expected key in {:?}", groups.keys().collect::<Vec<_>>());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn discover_groups_empty_when_no_roots() {
        let groups = discover_groups(&[], 0.0);
        assert!(groups.is_empty());
    }
}
