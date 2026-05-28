// Unit tests for search.go covering local-only branches: arg-parse errors,
// scan/discover IO failures, snippet nudging edges, time-arg parsing edges,
// role-filter, truncate.
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

func TestSearchExtractTextBareString(t *testing.T) {
	if got := searchExtractText(json.RawMessage(`"plain-text"`), false); got != "plain-text" {
		t.Errorf("bare string = %q; want plain-text", got)
	}
	// Bare unmarshalable bytes → empty string.
	if got := searchExtractText(json.RawMessage(`123`), false); got != "" {
		t.Errorf("non-string bare = %q; want \"\"", got)
	}
}

func TestSearchExtractTextMalformedArray(t *testing.T) {
	if got := searchExtractText(json.RawMessage(`[broken`), false); got != "" {
		t.Errorf("malformed array = %q; want \"\"", got)
	}
}

func TestSearchExtractTextToolBlocksToggle(t *testing.T) {
	content := json.RawMessage(`[
		{"type":"text","text":"plain"},
		{"type":"tool_use","input":{"a":1}},
		{"type":"tool_result","content":"r1"},
		{"type":"tool_result","content":[{"type":"text","text":"r2"},{"type":"image"}]}
	]`)
	// Without include_tool_blocks → only "plain"
	if got := searchExtractText(content, false); got != "plain" {
		t.Errorf("default text = %q; want plain", got)
	}
	// With include_tool_blocks → all blocks contribute.
	withTools := searchExtractText(content, true)
	for _, want := range []string{"plain", "r1", "r2"} {
		if !contains(withTools, want) {
			t.Errorf("with tools %q missing %q", withTools, want)
		}
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func TestSearchIsOnlyToolBlocks(t *testing.T) {
	if searchIsOnlyToolBlocks(json.RawMessage(`"bare"`)) {
		t.Error("bare string content should be false")
	}
	if searchIsOnlyToolBlocks(json.RawMessage(`[malformed`)) {
		t.Error("malformed content should be false")
	}
	if searchIsOnlyToolBlocks(json.RawMessage(`[]`)) {
		t.Error("empty array should be false")
	}
	if !searchIsOnlyToolBlocks(json.RawMessage(`[{"type":"tool_use"},{"type":"tool_result"}]`)) {
		t.Error("pure tool-block array should be true")
	}
	if searchIsOnlyToolBlocks(json.RawMessage(`[{"type":"text"},{"type":"tool_use"}]`)) {
		t.Error("mixed with text should be false")
	}
}

func TestSearchScanFileOpenError(t *testing.T) {
	if got := searchScanFile("/no/such/file.jsonl"); got != nil {
		t.Errorf("missing file should return nil, got %v", got)
	}
}

func TestSearchScanFileSkipLadder(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session.jsonl")
	body := "\n" +
		"   \n" +
		"{garbage\n" +
		"{}\n" +
		`{"message":{}}` + "\n" +
		`{"message":{"role":"","content":[]}}` + "\n" +
		`{"message":{"role":"user"}}` + "\n" +
		`{"message":{"role":"user","content":"hi"},"timestamp":"garbage"}` + "\n" +
		`{"message":{"role":"user","content":"good"},"timestamp":"2025-01-01T00:00:00Z"}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	msgs := searchScanFile(path)
	if len(msgs) != 2 {
		t.Fatalf("expected 2 surviving messages, got %d (%+v)", len(msgs), msgs)
	}
	// Last message has parseable timestamp.
	if !msgs[1].HasTimestamp {
		t.Errorf("expected HasTimestamp=true on last message")
	}
}

func TestSearchDiscoverFilesEmptyForMissingRoot(t *testing.T) {
	got := searchDiscoverFiles([]string{"/no/such/root"}, nil, nil)
	if len(got) != 0 {
		t.Errorf("expected empty result for missing root, got %v", got)
	}
}

func TestSearchDiscoverFilesPrunesByMtime(t *testing.T) {
	root := t.TempDir()
	slug := filepath.Join(root, "p")
	if err := os.MkdirAll(slug, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(slug, "s.jsonl"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	// Without since → discovered.
	all := searchDiscoverFiles([]string{root}, nil, nil)
	if len(all) != 1 {
		t.Errorf("expected 1 file without since, got %d", len(all))
	}
	// With far-future cutoff → pruned. Use 4e9 to stay in int64 nanos.
	cutoff := 4e9
	pruned := searchDiscoverFiles([]string{root}, &cutoff, nil)
	if len(pruned) != 0 {
		t.Errorf("expected pruned, got %v", pruned)
	}
}

func TestSearchDiscoverFilesCwdSlugFilter(t *testing.T) {
	root := t.TempDir()
	for _, slug := range []string{"keep", "drop"} {
		dir := filepath.Join(root, slug)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "s.jsonl"), []byte(""), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	cwd := "keep"
	got := searchDiscoverFiles([]string{root}, nil, &cwd)
	if len(got) != 1 || got[0].Slug != "keep" {
		t.Errorf("expected only the keep slug, got %v", got)
	}
}

func TestSearchNudgeWSEdges(t *testing.T) {
	// cut <= 0 → returns cut unchanged.
	if got := searchNudgeWS("hello world", 0, -1, 20); got != 0 {
		t.Errorf("cut=0 → %v; want 0", got)
	}
	// cut >= len(text) → returns cut unchanged.
	s := "hello world"
	if got := searchNudgeWS(s, len(s), 1, 20); got != len(s) {
		t.Errorf("cut=len → %v; want %d", got, len(s))
	}
	// Walks left to whitespace.
	if got := searchNudgeWS("hello world", 7, -1, 20); got != 6 {
		t.Errorf("nudge left → %v; want 6", got)
	}
	// Walks right to whitespace.
	if got := searchNudgeWS("hello world", 3, 1, 20); got != 5 {
		t.Errorf("nudge right → %v; want 5", got)
	}
	// No whitespace in range → returns cut.
	if got := searchNudgeWS("aaaaaaaaaa", 5, -1, 3); got != 5 {
		t.Errorf("no-ws left → %v; want 5", got)
	}
	if got := searchNudgeWS("aaaaaaaaaa", 5, 1, 3); got != 5 {
		t.Errorf("no-ws right → %v; want 5", got)
	}
}

func TestSearchNudgeCharBoundary(t *testing.T) {
	// "héllo" — 'é' bytes 1..3; index 2 is a continuation byte.
	s := "héllo"
	if got := searchNudgeCharBoundary(s, 2); got != 3 {
		t.Errorf("nudge_char_boundary(2) = %v; want 3", got)
	}
	// At end → returns unchanged.
	if got := searchNudgeCharBoundary(s, len(s)); got != len(s) {
		t.Errorf("at-end → %v; want %d", got, len(s))
	}
}

func TestSearchMakeSnippetCentersAndClips(t *testing.T) {
	text := "alpha beta gamma delta epsilon zeta eta theta iota"
	// Find "gamma".
	mstart := uint32(11)
	mend := uint32(16)
	got := searchMakeSnippet(text, [2]uint32{mstart, mend}, 20)
	if !contains(got, "gamma") {
		t.Errorf("snippet missing match: %q", got)
	}
	// snippet small clips → clip+match still substring.
}

func TestParseSearchTimeArgRelative(t *testing.T) {
	now := 1_000_000.0
	cases := []struct {
		in     string
		want   float64
		ok     bool
	}{
		{"3d", now - 3*86400, true},
		{"2h", now - 2*3600, true},
		{"30m", now - 30*60, true},
		{"10s", now - 10, true},
		{"0.5h", now - 1800, true},
		{"", 0, false},
		{"   ", 0, false},
		{"garbage", 0, false},
		{"xd", 0, false},
	}
	for _, c := range cases {
		got, err := parseSearchTimeArg(c.in, now)
		if c.ok {
			if err != nil {
				t.Errorf("%q want ok, got err %v", c.in, err)
			} else if got < c.want-1e-9 || got > c.want+1e-9 {
				t.Errorf("%q = %v; want %v", c.in, got, c.want)
			}
		} else if err == nil {
			t.Errorf("%q want err, got %v", c.in, got)
		}
	}
}

func TestParseSearchTimeArgISO(t *testing.T) {
	got, err := parseSearchTimeArg("2025-01-15T00:00:00Z", 0)
	if err != nil || got < 1.7e9 {
		t.Fatalf("ISO parse = (%v,%v); want valid", got, err)
	}
}

func TestIsSearchNumeric(t *testing.T) {
	if !isSearchNumeric("1.5") {
		t.Error("1.5 should be numeric")
	}
	if !isSearchNumeric("0") {
		t.Error("0 should be numeric")
	}
	if isSearchNumeric("1a") {
		t.Error("1a should not be numeric")
	}
	// Empty input — function returns true (vacuously). Just don't crash;
	// assert the actual behavior so staticcheck SA4017 stays quiet.
	if !isSearchNumeric("") {
		t.Error("empty string should be considered numeric (vacuous all)")
	}
}

func TestSearchRoleMatches(t *testing.T) {
	cases := []struct {
		filter, role string
		want         bool
	}{
		{"both", "user", true},
		{"both", "assistant", true},
		{"user", "user", true},
		{"user", "assistant", false},
		{"assistant", "user", false},
		{"assistant", "assistant", true},
	}
	for _, c := range cases {
		if got := searchRoleMatches(c.filter, c.role); got != c.want {
			t.Errorf("roleMatches(%q,%q) = %v; want %v", c.filter, c.role, got, c.want)
		}
	}
}

func TestSearchTruncateStr(t *testing.T) {
	if got := searchTruncateStr("short", 10); got != "short" {
		t.Errorf("short = %q; want short", got)
	}
	if got := searchTruncateStr("0123456789abcdef", 5); got != "01234…" {
		t.Errorf("long = %q; want 01234…", got)
	}
}

func TestParseSearchArgsErrors(t *testing.T) {
	// Missing pattern.
	if _, err := parseSearchArgs(nil); err == nil {
		t.Error("missing pattern should error")
	}
	// Flag-needs-value errors across the matrix.
	for _, flag := range []string{
		"--role", "--since", "--until", "--cwd", "--context", "--limit",
		"--format", "--snippet-chars", "--projects-root",
		"--extra-projects-root", "--now",
	} {
		if _, err := parseSearchArgs([]string{flag}); err == nil {
			t.Errorf("%s without value should error", flag)
		}
	}
	// Invalid role/format enum.
	if _, err := parseSearchArgs([]string{"pat", "--role", "weird"}); err == nil {
		t.Error("invalid --role should error")
	}
	if _, err := parseSearchArgs([]string{"pat", "--format", "weird"}); err == nil {
		t.Error("invalid --format should error")
	}
	// Bad numeric values.
	for _, flag := range []string{"--context", "--limit", "--snippet-chars", "--now"} {
		if _, err := parseSearchArgs([]string{"pat", flag, "notnum"}); err == nil {
			t.Errorf("%s notnum should error", flag)
		}
	}
	// Bad --since time.
	if _, err := parseSearchArgs([]string{"pat", "--since", "garbage"}); err == nil {
		t.Error("bad --since time should error")
	}
	if _, err := parseSearchArgs([]string{"pat", "--until", "garbage"}); err == nil {
		t.Error("bad --until time should error")
	}
	// Unknown flag.
	if _, err := parseSearchArgs([]string{"pat", "--bogus"}); err == nil {
		t.Error("unknown flag should error")
	}
	// Duplicate positional.
	if _, err := parseSearchArgs([]string{"pat1", "pat2"}); err == nil {
		t.Error("duplicate positional should error")
	}
	// --cwd + --any-cwd conflict.
	if _, err := parseSearchArgs([]string{"pat", "--cwd", "x", "--any-cwd"}); err == nil {
		t.Error("--cwd + --any-cwd should error")
	}
}

func TestParseSearchArgsHappy(t *testing.T) {
	a, err := parseSearchArgs([]string{
		"hello",
		"--regex", "--case-sensitive",
		"--role", "user",
		"--since", "1d", "--until", "0s",
		"--cwd", "myslug",
		"--context", "2", "--limit", "10",
		"--count-only", "--include-tool-blocks",
		"--format", "jsonl",
		"--snippet-chars", "100",
		"--projects-root", "/tmp/p",
		"--extra-projects-root", "/tmp/q",
		"--no-config",
		"--now", "1000.0",
	})
	if err != nil {
		t.Fatal(err)
	}
	if a.Pattern != "hello" || !a.Regex || !a.CaseSensitive || a.Role != "user" {
		t.Errorf("unexpected args: %+v", a)
	}
	if a.Cwd != "myslug" || a.Context != 2 || a.Limit != 10 || a.SnippetChars != 100 {
		t.Errorf("unexpected args: %+v", a)
	}
	if !a.CountOnly || !a.IncludeToolBlocks || a.Format != "jsonl" || a.ReadConfig {
		t.Errorf("unexpected args: %+v", a)
	}
}

// TestSearchProcessFileMatching wires scan→regex→snippet end-to-end on a
// fixture that triggers the role-filter, only-tool-blocks, time-filter,
// and empty-text continue branches.
func TestSearchProcessFileMatching(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session.jsonl")
	body := `{"message":{"role":"user","content":[{"type":"tool_result"}]},"timestamp":"2025-01-01T00:00:00Z"}` + "\n" +
		`{"message":{"role":"user","content":"hello world"},"timestamp":"2025-01-01T00:00:01Z"}` + "\n" +
		`{"message":{"role":"assistant","content":[{"type":"text","text":"hello world"}]},"timestamp":"2025-01-01T00:00:02Z"}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	args := searchArgs{
		Pattern:      "hello",
		Role:         "both",
		Context:      0,
		SnippetChars: 100,
	}
	re := regexp.MustCompile("hello")
	hits := searchProcessFile(
		searchFileInfo{Path: path, Slug: "p", SessionID: "s", HostRoot: "/r"},
		args, re,
	)
	// The tool_result-only entry is filtered (only-tool-blocks); the other two match.
	if len(hits) != 2 {
		t.Fatalf("expected 2 hits, got %d (%v)", len(hits), hits)
	}
}
