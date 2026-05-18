// Shared content extraction. Both beacons and search must use the loose
// `Value`-based parser here, because real transcripts mix a `Vec<ContentBlock>`
// (modern) with a bare string (legacy user-prompt format). A strictly-typed
// `Vec<...>` silently drops ~10% of older user prompts.

use serde_json::Value;

/// Extract the textual content of a message's `content` field.
///
/// Accepts either a bare string (legacy user-prompt format) or an array of
/// content blocks (modern format). With `include_tool_blocks`, the
/// concatenation also pulls `tool_use.input` (JSON-stringified) and
/// `tool_result.content` (string or text-block array) so search can reach
/// inside tool output when the caller asks for it.
pub fn extract_text(content: &Value, include_tool_blocks: bool) -> String {
    if let Some(s) = content.as_str() {
        return s.to_string();
    }
    let arr = match content.as_array() {
        Some(a) => a,
        None => return String::new(),
    };
    let mut parts: Vec<String> = Vec::new();
    for block in arr {
        let block_type = block.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match block_type {
            "text" => {
                if let Some(t) = block.get("text").and_then(|v| v.as_str()) {
                    parts.push(t.to_string());
                }
            }
            "tool_use" if include_tool_blocks => {
                if let Some(input) = block.get("input") {
                    parts.push(input.to_string());
                }
            }
            "tool_result" if include_tool_blocks => {
                if let Some(c) = block.get("content") {
                    if let Some(s) = c.as_str() {
                        parts.push(s.to_string());
                    } else if let Some(inner) = c.as_array() {
                        for ib in inner {
                            if ib.get("type").and_then(|v| v.as_str()) == Some("text") {
                                if let Some(t) = ib.get("text").and_then(|v| v.as_str()) {
                                    parts.push(t.to_string());
                                }
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    parts.join("\n")
}

/// True when content is an array entirely composed of `tool_use`/`tool_result`
/// blocks (no text blocks). Search uses this to skip pure tool messages under
/// default rules. Returns false for bare-string content (legacy form is always
/// real user text).
pub fn is_only_tool_blocks(content: &Value) -> bool {
    let arr = match content.as_array() {
        Some(a) => a,
        None => return false,
    };
    if arr.is_empty() {
        return false;
    }
    arr.iter().all(|block| {
        let t = block.get("type").and_then(|v| v.as_str()).unwrap_or("");
        matches!(t, "tool_use" | "tool_result")
    })
}

/// True when a `type: "user"` entry's content contains any tool_result block.
/// Used by beacons-history to distinguish a tool_result entry (agent-active)
/// from a real user prompt (user-idle gap).
pub fn user_content_is_tool_result(content: Option<&Value>) -> bool {
    let arr = match content.and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return false,
    };
    arr.iter().any(|block| {
        block.get("type").and_then(|v| v.as_str()) == Some("tool_result")
    })
}
