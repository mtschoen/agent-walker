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
    } else if m.contains("sonnet") {
        (3.0, 15.0)
    } else {
        (3.0, 15.0) // unknown -> sonnet, per spec
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
