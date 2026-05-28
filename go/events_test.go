// Unit tests for events.go covering arg-parse errors + walkGroupEvents IO
// and skip-ladder branches.
package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseEventsArgumentsRequiresPeriod(t *testing.T) {
	if _, err := parseEventsArguments(nil); err == nil {
		t.Error("missing --period should error")
	}
	if _, err := parseEventsArguments([]string{"--period", "0"}); err == nil {
		t.Error("--period 0 should error (must be > 0)")
	}
}

func TestParseEventsArgumentsFlagValueErrors(t *testing.T) {
	for _, flag := range []string{"--period", "--win-start", "--now", "--projects-root", "--extra-projects-root"} {
		if _, err := parseEventsArguments([]string{flag}); err == nil {
			t.Errorf("%s without value should error", flag)
		}
	}
}

func TestParseEventsArgumentsBadNumericValue(t *testing.T) {
	if _, err := parseEventsArguments([]string{"--period", "notnum"}); err == nil {
		t.Error("non-numeric --period should error")
	}
	if _, err := parseEventsArguments([]string{"--period", "60", "--win-start", "notnum"}); err == nil {
		t.Error("non-numeric --win-start should error")
	}
	if _, err := parseEventsArguments([]string{"--period", "60", "--now", "notnum"}); err == nil {
		t.Error("non-numeric --now should error")
	}
}

func TestParseEventsArgumentsUnknownFlag(t *testing.T) {
	if _, err := parseEventsArguments([]string{"--period", "60", "--bogus"}); err == nil {
		t.Error("unknown flag should error")
	}
}

func TestParseEventsArgumentsWinStartDefaultsToNowMinusPeriod(t *testing.T) {
	a, err := parseEventsArguments([]string{"--period", "100", "--now", "1000"})
	if err != nil {
		t.Fatal(err)
	}
	if a.winStartUnix != 900 {
		t.Errorf("default win-start = %v; want 900", a.winStartUnix)
	}
}

func TestParseEventsArgumentsAllFlags(t *testing.T) {
	a, err := parseEventsArguments([]string{
		"--period", "60", "--win-start", "10", "--now", "100",
		"--projects-root", "/tmp/p", "--extra-projects-root", "/tmp/q",
		"--no-config",
	})
	if err != nil {
		t.Fatal(err)
	}
	if a.periodSeconds != 60 || a.winStartUnix != 10 || a.nowUnix != 100 {
		t.Errorf("unexpected args: %+v", a)
	}
	if a.projectsRoot != "/tmp/p" || len(a.extraProjectsRoots) != 1 || a.extraProjectsRoots[0] != "/tmp/q" || a.readConfig {
		t.Errorf("unexpected args: %+v", a)
	}
}

func TestWalkGroupEventsOpenError(t *testing.T) {
	recs := walkGroupEvents([]string{"/no/such/file.jsonl"}, "s", "id", 0)
	if len(recs) != 0 {
		t.Fatalf("expected empty for missing file, got %v", recs)
	}
}

func TestWalkGroupEventsSkipLadder(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session.jsonl")
	body := "\n" +
		"{garbage\n" +
		"{}\n" +
		`{"timestamp":"2025-01-01T00:00:00Z","message":{"role":"user"}}` + "\n" +
		// Valid turn
		`{"timestamp":"2025-01-01T00:00:01Z","message":{"role":"assistant","id":"m1","model":"sonnet","usage":{"input_tokens":1000000}}}` + "\n" +
		// Dup id
		`{"timestamp":"2025-01-01T00:00:02Z","message":{"role":"assistant","id":"m1","model":"sonnet"}}` + "\n" +
		// Missing ts
		`{"message":{"role":"assistant","id":"m2","model":"sonnet"}}` + "\n" +
		// Bad ts
		`{"timestamp":"garbage","message":{"role":"assistant","id":"m3","model":"sonnet"}}` + "\n" +
		// Before cutoff
		`{"timestamp":"1970-01-01T00:00:00Z","message":{"role":"assistant","id":"m4","model":"sonnet"}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	recs := walkGroupEvents([]string{path}, "slug-x", "sid-y", 100)
	if len(recs) != 1 {
		t.Fatalf("expected 1 record, got %d (%v)", len(recs), recs)
	}
	if recs[0].Slug != "slug-x" || recs[0].SessionID != "sid-y" {
		t.Errorf("metadata mismatch: %+v", recs[0])
	}
}
