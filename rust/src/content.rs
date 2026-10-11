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
    // Accumulate into one String (push_str + '\n' separators) instead of a
    // Vec<String> + join: byte-identical output, one allocation instead of
    // one per block.
    let mut result = String::new();
    let push_part = |result: &mut String, part: &str| {
        if !result.is_empty() {
            result.push('\n');
        }
        result.push_str(part);
    };
    for block in arr {
        let block_type = block.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match block_type {
            "text" => {
                if let Some(t) = block.get("text").and_then(|v| v.as_str()) {
                    push_part(&mut result, t);
                }
            }
            "tool_use" if include_tool_blocks => {
                if let Some(input) = block.get("input") {
                    push_part(&mut result, &input.to_string());
                }
            }
            "tool_result" if include_tool_blocks => {
                if let Some(c) = block.get("content") {
                    if let Some(s) = c.as_str() {
                        push_part(&mut result, s);
                    } else if let Some(inner) = c.as_array() {
                        for ib in inner {
                            if ib.get("type").and_then(|v| v.as_str()) == Some("text") {
                                if let Some(t) = ib.get("text").and_then(|v| v.as_str()) {
                                    push_part(&mut result, t);
                                }
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    result
}

/// Extract a searchable role and message from a Codex `event_msg` payload.
/// Other event kinds and wrong-typed messages are intentionally ignored.
pub fn extract_codex_event(payload: &Value) -> Option<(&'static str, String)> {
    let role = match payload.get("type").and_then(Value::as_str)? {
        "user_message" => "user",
        "agent_message" => "assistant",
        _ => return None,
    };
    let message = payload.get("message").and_then(Value::as_str)?;
    Some((role, message.to_string()))
}

/// Extract searchable text from a current Codex `response_item` message.
/// A user message owns input_text blocks and an assistant message owns
/// output_text blocks. Other roles and blocks are not conversation turns.
pub fn extract_codex_response_item(payload: &Value) -> Option<(&'static str, String)> {
    if payload.get("type").and_then(Value::as_str)? != "message" {
        return None;
    }
    let (role, text_type) = match payload.get("role").and_then(Value::as_str)? {
        "user" => ("user", "input_text"),
        "assistant" => ("assistant", "output_text"),
        _ => return None,
    };
    let content = payload.get("content")?.as_array()?;
    let mut text = String::new();
    let mut accepted_block = false;
    for block in content {
        if block.get("type").and_then(Value::as_str) == Some(text_type) {
            if let Some(part) = block.get("text").and_then(Value::as_str) {
                if accepted_block {
                    text.push('\n');
                }
                text.push_str(part);
                accepted_block = true;
            }
        }
    }
    (!text.is_empty()).then_some((role, text))
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

/// Extract text from a `type: "queue-operation"` entry. Queue-ops have no
/// `message` object — the text lives in the entry's **root-level `content`
/// field** (a bare string). Returns `None` when the field is missing or empty
/// (e.g. `remove`/`dequeue` operations), so only content-bearing entries
/// (`enqueue`/`popAll`) surface under `--include-queue-ops`.
pub fn extract_queue_op_text(entry: &Value) -> Option<String> {
    let text = entry.get("content").and_then(|v| v.as_str())?;
    if text.is_empty() {
        None
    } else {
        Some(text.to_string())
    }
}

/// True when a `type: "user"` entry's content contains any tool_result block.
/// Used by beacons-history to distinguish a tool_result entry (agent-active)
/// from a real user prompt (user-idle gap).
pub fn user_content_is_tool_result(content: Option<&Value>) -> bool {
    let arr = match content.and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return false,
    };
    arr.iter()
        .any(|block| block.get("type").and_then(|v| v.as_str()) == Some("tool_result"))
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
    fn extract_codex_event_accepts_messages_and_skips_other_payloads() {
        assert_eq!(
            extract_codex_event(&json!({"type":"user_message","message":"hello"})),
            Some(("user", "hello".to_string()))
        );
        assert_eq!(
            extract_codex_event(&json!({"type":"agent_message","message":"answer"})),
            Some(("assistant", "answer".to_string()))
        );
        assert_eq!(
            extract_codex_event(&json!({"type":"token_count","message":"x"})),
            None
        );
        assert_eq!(
            extract_codex_event(&json!({"type":"agent_message","message":42})),
            None
        );
    }

    #[test]
    fn extract_codex_response_item_keeps_canonical_role_blocks() {
        for (role, block_type) in [("user", "input_text"), ("assistant", "output_text")] {
            for (parts, expected) in [(["", "answer"], "\nanswer"), (["", ""], "\n")] {
                assert_eq!(
                    extract_codex_response_item(&json!({
                        "type": "message", "role": role,
                        "content": parts.map(|text| json!({"type": block_type, "text": text}))
                    })),
                    Some((role, expected.to_string()))
                );
            }
        }
        assert_eq!(
            extract_codex_response_item(&json!({
                "type":"message",
                "role":"user",
                "content":[
                    {"type":"input_text","text":"question"},
                    {"type":"output_text","text":"ignored"},
                    {"type":"input_text","text":"follow-up"}
                ]
            })),
            Some(("user", "question\nfollow-up".to_string()))
        );
        assert_eq!(
            extract_codex_response_item(&json!({
                "type":"message",
                "role":"assistant",
                "content":[
                    {"type":"input_text","text":"ignored"},
                    {"type":"output_text","text":"answer"}
                ]
            })),
            Some(("assistant", "answer".to_string()))
        );
        assert_eq!(
            extract_codex_response_item(&json!({
                "type":"message",
                "role":"developer",
                "content":[{"type":"input_text","text":"ignored"}]
            })),
            None
        );
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
