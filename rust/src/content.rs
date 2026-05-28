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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extract_text_returns_empty_for_non_string_non_array() {
        // Covers line 21: content is e.g. a number/object/null → empty string.
        assert_eq!(extract_text(&json!(42), false), "");
        assert_eq!(extract_text(&json!(null), false), "");
        assert_eq!(extract_text(&json!({"k":"v"}), false), "");
    }

    #[test]
    fn extract_text_tool_result_text_array() {
        // Covers lines 41-49: tool_result.content is an array of text blocks.
        let content = json!([
            {"type": "tool_result", "content": [
                {"type": "text", "text": "inside-text"},
                {"type": "image"},  // non-text block — skipped
                {"type": "text", "text": "second-text"},
            ]}
        ]);
        let text = extract_text(&content, true);
        assert!(text.contains("inside-text"));
        assert!(text.contains("second-text"));
    }

    #[test]
    fn extract_text_tool_result_text_array_missing_text_field() {
        // Cover the inner text-extract that finds text:None.
        let content = json!([
            {"type": "tool_result", "content": [
                {"type": "text"},  // no "text" key → silent skip
            ]}
        ]);
        assert_eq!(extract_text(&content, true), "");
    }

    #[test]
    fn extract_text_tool_result_string_content() {
        let content = json!([
            {"type": "tool_result", "content": "bare-string-result"}
        ]);
        assert_eq!(extract_text(&content, true), "bare-string-result");
    }

    #[test]
    fn is_only_tool_blocks_empty_array_is_false() {
        // Covers line 68: empty array branch.
        let empty = json!([]);
        assert!(!is_only_tool_blocks(&empty));
    }

    #[test]
    fn is_only_tool_blocks_non_array_is_false() {
        assert!(!is_only_tool_blocks(&json!("bare")));
        assert!(!is_only_tool_blocks(&json!(null)));
    }

    #[test]
    fn is_only_tool_blocks_true_for_pure_tool() {
        let content = json!([{"type": "tool_use", "input": {}}, {"type": "tool_result"}]);
        assert!(is_only_tool_blocks(&content));
    }

    #[test]
    fn is_only_tool_blocks_false_when_text_present() {
        let content = json!([{"type": "tool_use"}, {"type": "text", "text": "x"}]);
        assert!(!is_only_tool_blocks(&content));
    }

    #[test]
    fn user_content_is_tool_result_none_or_non_array_false() {
        assert!(!user_content_is_tool_result(None));
        let bare = json!("just text");
        assert!(!user_content_is_tool_result(Some(&bare)));
    }

    #[test]
    fn user_content_is_tool_result_detects_tool_result_block() {
        let c = json!([{"type": "tool_result"}]);
        assert!(user_content_is_tool_result(Some(&c)));
        let c2 = json!([{"type": "text", "text": "hi"}]);
        assert!(!user_content_is_tool_result(Some(&c2)));
    }
}
