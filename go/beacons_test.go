// Unit tests for beacons.go covering local-only branches: arg-parse errors,
// IO failure paths in findLatestInPath / collectSessionEventsInPath /
// discoverHistoryGroups, helper-function edges (extractText, biasFactor).
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestFirstNonSpaceByte covers leading-whitespace skip + empty payload.
func TestFirstNonSpaceByte(t *testing.T) {
	cases := []struct {
		in   string
		want byte
	}{
		{"", 0},
		{"   \t\n\r", 0},
		{"  {", '{'},
		{"[1,2]", '['},
	}
	for _, c := range cases {
		if got := firstNonSpaceByte(json.RawMessage(c.in)); got != c.want {
			t.Errorf("firstNonSpaceByte(%q) = %v; want %v", c.in, got, c.want)
		}
	}
}

// TestExtractTextBareStringReturnsEmpty — array-only behavior.
func TestExtractTextBareString(t *testing.T) {
	if got := extractText(json.RawMessage(`"plain"`)); got != "" {
		t.Errorf("extractText(bare string) = %q; want \"\"", got)
	}
}

// TestExtractTextHandlesMalformedJSON — sonic.Unmarshal err branch.
func TestExtractTextMalformedJSON(t *testing.T) {
	if got := extractText(json.RawMessage(`[broken`)); got != "" {
		t.Errorf("extractText(broken) = %q; want \"\"", got)
	}
}

// TestExtractTextConcatenatesTextBlocks.
func TestExtractTextConcatenatesTextBlocks(t *testing.T) {
	in := json.RawMessage(`[{"type":"text","text":"hello"},{"type":"image"},{"type":"text","text":"world"}]`)
	if got := extractText(in); got != "hello\nworld" {
		t.Errorf("extractText = %q; want %q", got, "hello\nworld")
	}
}

// TestUserContentIsToolResult — covers all three branches.
func TestUserContentIsToolResult(t *testing.T) {
	if userContentIsToolResult(json.RawMessage(`"bare"`)) {
		t.Error("bare string content should not be tool_result")
	}
	if userContentIsToolResult(json.RawMessage(`[malformed`)) {
		t.Error("malformed JSON should not be tool_result")
	}
	if !userContentIsToolResult(json.RawMessage(`[{"type":"tool_result"}]`)) {
		t.Error("array with tool_result should be detected")
	}
	if userContentIsToolResult(json.RawMessage(`[{"type":"text"}]`)) {
		t.Error("array without tool_result should be false")
	}
}

// TestRawBeaconToBeacon — required-field rejection + accepted shape.
func TestRawBeaconToBeacon(t *testing.T) {
	missingSummary := rawBeacon{}
	if _, ok := missingSummary.toBeacon(); ok {
		t.Error("missing required fields should reject")
	}
	kind, eta, summary := "report", 30.0, "doing things"
	rb := rawBeacon{Kind: &kind, EtaSeconds: &eta, Summary: &summary}
	b, ok := rb.toBeacon()
	if !ok || b.Kind != "report" || b.EtaSeconds != 30.0 || b.Summary != "doing things" {
		t.Fatalf("toBeacon = (%+v,%v); want valid", b, ok)
	}
}

// TestFindLatestInPathOpenError — covers os.Open err branch (line 146).
func TestFindLatestInPathOpenError(t *testing.T) {
	if _, ok := findLatestInPath("/no/such/file.jsonl"); ok {
		t.Error("missing file should return ok=false")
	}
}

// TestFindLatestInPathHappyAndSkips constructs a transcript with the full
// skip-ladder (blank/malformed/missing message/missing-content/bad-ts) plus
// two valid beacons; the higher-ts one must win.
func TestFindLatestInPathHappyAndSkips(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session.jsonl")
	body := "\n" +
		"{garbage\n" +
		"{}\n" +
		`{"timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":[]}}` + "\n" +
		`{"timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant"}}` + "\n" +
		`{"timestamp":"garbage","message":{"role":"assistant","content":[{"type":"text","text":"x"}]}}` + "\n" +
		`{"timestamp":"2025-01-02T00:00:00Z","message":{"role":"assistant","content":[{"type":"text","text":"<progress-beacon>{\"kind\":\"report\",\"eta_seconds\":5,\"summary\":\"first\"}</progress-beacon>"}]}}` + "\n" +
		`{"timestamp":"2025-01-03T00:00:00Z","message":{"role":"assistant","content":[{"type":"text","text":"<progress-beacon>{\"kind\":\"report\",\"eta_seconds\":7,\"summary\":\"latest\"}</progress-beacon>"}]}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	got, ok := findLatestInPath(path)
	if !ok {
		t.Fatal("expected a beacon")
	}
	if got.beacon.Summary != "latest" {
		t.Errorf("expected highest-ts beacon, got %q", got.beacon.Summary)
	}
}

// TestCollectSessionEventsOpenError — covers os.Open err branch.
func TestCollectSessionEventsOpenError(t *testing.T) {
	se := collectSessionEventsInPath("/no/such/file.jsonl")
	if len(se.beacons) != 0 || len(se.events) != 0 {
		t.Errorf("missing file should yield empty result; got %+v", se)
	}
}

// TestCollectSessionEventsClassifiesUsers — real user vs tool_result user.
func TestCollectSessionEventsClassifiesUsers(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session.jsonl")
	body := `{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}` + "\n" +
		`{"type":"user","timestamp":"2025-01-01T00:00:01Z","message":{"role":"user","content":[{"type":"tool_result"}]}}` + "\n" +
		`{"timestamp":"2025-01-01T00:00:02Z","message":{"role":"assistant","content":[{"type":"text","text":"<progress-beacon>{\"kind\":\"begin\",\"eta_seconds\":1,\"summary\":\"x\"}</progress-beacon>"}]}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	se := collectSessionEventsInPath(path)
	if len(se.beacons) != 1 {
		t.Errorf("expected 1 beacon, got %d", len(se.beacons))
	}
	if len(se.events) != 3 {
		t.Fatalf("expected 3 events, got %d", len(se.events))
	}
	if !se.events[0].isRealUser {
		t.Error("first event should be real user")
	}
	if se.events[1].isRealUser {
		t.Error("tool_result event should NOT be real user")
	}
}

// TestComputeIdleInWindow covers all branches.
func TestComputeIdleInWindow(t *testing.T) {
	// < 2 events → zero.
	if got := computeIdleInWindow(nil, 0, 100); got != 0 {
		t.Errorf("empty events idle = %v; want 0", got)
	}
	// Only non-user gap → zero.
	events := []event{{timestamp: 10}, {timestamp: 20}}
	if got := computeIdleInWindow(events, 0, 100); got != 0 {
		t.Errorf("non-user gap idle = %v; want 0", got)
	}
	// Clip gap to window.
	events2 := []event{{timestamp: 10}, {timestamp: 50, isRealUser: true}}
	got := computeIdleInWindow(events2, 20, 40)
	if got < 19.99 || got > 20.01 {
		t.Errorf("clipped idle = %v; want ~20", got)
	}
	// Window entirely outside the gap → zero.
	events3 := []event{{timestamp: 10}, {timestamp: 50, isRealUser: true}}
	if got := computeIdleInWindow(events3, 100, 200); got != 0 {
		t.Errorf("outside-window idle = %v; want 0", got)
	}
}

// TestBiasFactor covers empty, all-nonpositive, odd-count, even-count.
func TestBiasFactor(t *testing.T) {
	if _, ok := biasFactor(nil); ok {
		t.Error("empty pairs should return ok=false")
	}
	allBad := []historyPair{{beginEta: 0, activeElapsed: 5}, {beginEta: -1, activeElapsed: 3}}
	if _, ok := biasFactor(allBad); ok {
		t.Error("all-nonpositive should return ok=false")
	}
	odd := []historyPair{
		{beginEta: 10, activeElapsed: 5},
		{beginEta: 10, activeElapsed: 10},
		{beginEta: 10, activeElapsed: 20},
	}
	got, ok := biasFactor(odd)
	if !ok || got != 1.0 {
		t.Errorf("odd-count bias = (%v,%v); want (1.0,true)", got, ok)
	}
	even := []historyPair{
		{beginEta: 10, activeElapsed: 5},
		{beginEta: 10, activeElapsed: 10},
		{beginEta: 10, activeElapsed: 20},
		{beginEta: 10, activeElapsed: 40},
	}
	got, ok = biasFactor(even)
	if !ok || got < 1.49 || got > 1.51 {
		t.Errorf("even-count bias = (%v,%v); want ~1.5", got, ok)
	}
}

// TestParseLatestArgumentsErrors covers missing-flag-value / unknown / etc.
func TestParseLatestArgumentsErrors(t *testing.T) {
	// Missing required --session-id.
	if _, err := parseLatestArguments(nil); err == nil {
		t.Error("missing session-id should error")
	}
	// Flag value missing.
	for _, flag := range []string{"--session-id", "--projects-root", "--extra-projects-root", "--now"} {
		if _, err := parseLatestArguments([]string{flag}); err == nil {
			t.Errorf("%s without value should error", flag)
		}
	}
	// Bad --now value.
	if _, err := parseLatestArguments([]string{"--session-id", "x", "--now", "notnum"}); err == nil {
		t.Error("bad --now value should error")
	}
	// Unknown flag.
	if _, err := parseLatestArguments([]string{"--session-id", "x", "--bogus"}); err == nil {
		t.Error("unknown flag should error")
	}
}

func TestParseLatestArgumentsHappy(t *testing.T) {
	a, err := parseLatestArguments([]string{
		"--session-id", "abc",
		"--projects-root", "/tmp/p",
		"--extra-projects-root", "/tmp/q",
		"--no-config",
		"--now", "100.5",
	})
	if err != nil {
		t.Fatal(err)
	}
	if a.sessionID != "abc" || a.projectsRoot != "/tmp/p" {
		t.Errorf("unexpected parsed args: %+v", a)
	}
}

func TestParseHistoryArgumentsErrors(t *testing.T) {
	if _, err := parseHistoryArguments(nil); err == nil {
		t.Error("missing --period should error")
	}
	for _, flag := range []string{"--period", "--win-start", "--projects-root", "--extra-projects-root", "--now"} {
		if _, err := parseHistoryArguments([]string{flag}); err == nil {
			t.Errorf("%s without value should error", flag)
		}
	}
	if _, err := parseHistoryArguments([]string{"--period", "60", "--win-start", "no"}); err == nil {
		t.Error("bad --win-start should error")
	}
	if _, err := parseHistoryArguments([]string{"--period", "60", "--bogus"}); err == nil {
		t.Error("unknown flag should error")
	}
}

func TestParseHistoryArgumentsHappy(t *testing.T) {
	a, err := parseHistoryArguments([]string{
		"--period", "60", "--win-start", "10",
		"--projects-root", "/tmp/p",
		"--extra-projects-root", "/tmp/q",
		"--no-config",
		"--now", "99.0",
	})
	if err != nil {
		t.Fatal(err)
	}
	if a.periodSeconds != 60 || a.winStartUnix != 10 {
		t.Errorf("unexpected parsed history args: %+v", a)
	}
}

// TestDiscoverHistoryGroups exercises both parent + subagent layouts.
func TestDiscoverHistoryGroups(t *testing.T) {
	root := t.TempDir()
	slug := filepath.Join(root, "slug")
	if err := os.MkdirAll(slug, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(slug, "sid-1.jsonl"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	subDir := filepath.Join(slug, "sid-2", "subagents")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(subDir, "agent-a.jsonl"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	groups := discoverHistoryGroups([]string{root})
	if len(groups) != 2 {
		t.Fatalf("expected 2 groups, got %d (%v)", len(groups), groups)
	}
}

// TestDiscoverHistoryGroupsMissingRoot — readdir-err branches.
func TestDiscoverHistoryGroupsMissingRoot(t *testing.T) {
	groups := discoverHistoryGroups([]string{"/no/such/root"})
	if len(groups) != 0 {
		t.Errorf("expected empty result for missing root, got %v", groups)
	}
}

// TestFormatFloat — covers the integer-shortcut + decimal path.
func TestFormatFloat(t *testing.T) {
	if got := formatFloat(0); got != "0" {
		t.Errorf("formatFloat(0) = %q; want 0", got)
	}
	if got := formatFloat(1); got != "1" {
		t.Errorf("formatFloat(1) = %q; want 1", got)
	}
	if got := formatFloat(1.5); got != "1.5" {
		t.Errorf("formatFloat(1.5) = %q; want 1.5", got)
	}
}
