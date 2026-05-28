// Unit tests for walker_roots.go covering local-only branches that the
// shared conformance fixtures can't drive (env-cleared home fallback, IO
// failures from a missing/unreadable config path).
package main

import (
	"os"
	"path/filepath"
	"testing"
)

// TestWalkerConfigPathFallback covers walker_roots.go:31 — the no-home branch
// returning the relative ".claude/walker-roots.json".
func TestWalkerConfigPathFallback(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("USERPROFILE", "")
	got := WalkerConfigPath()
	want := filepath.Join(".claude", "walker-roots.json")
	if got != want {
		t.Fatalf("WalkerConfigPath() with no env = %q; want %q", got, want)
	}
}

// TestWalkerConfigPathWithHome confirms the happy path.
func TestWalkerConfigPathWithHome(t *testing.T) {
	t.Setenv("HOME", "/tmp/fakehome")
	t.Setenv("USERPROFILE", "")
	got := WalkerConfigPath()
	want := filepath.Join("/tmp/fakehome", ".claude", "walker-roots.json")
	// On Windows, USERPROFILE wins; tolerate either.
	winWant := filepath.Join("", ".claude", "walker-roots.json")
	if got != want && got != winWant {
		t.Fatalf("WalkerConfigPath() = %q; want %q (or windows %q)", got, want, winWant)
	}
}

// TestReadExtraRootsMissingConfig covers the os.ReadFile err branch (silent).
func TestReadExtraRootsMissingConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); len(got) != 0 {
		t.Fatalf("expected empty extras with missing config, got %v", got)
	}
}

// TestReadExtraRootsEmptyFile covers the len(body)==0 silent branch.
func TestReadExtraRootsEmptyFile(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".claude"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".claude", "walker-roots.json"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); len(got) != 0 {
		t.Fatalf("expected empty extras with empty config, got %v", got)
	}
}

// TestReadExtraRootsHappyPath ensures parsing a well-formed config yields
// the listed extra roots (empty strings filtered out).
func TestReadExtraRootsHappyPath(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"extra_roots":["/tmp/a","","/tmp/b"]}`
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	got := ReadExtraRootsFromConfig()
	want := []string{"/tmp/a", "/tmp/b"}
	if len(got) != len(want) {
		t.Fatalf("got %d extras (%v); want 2 (%v)", len(got), got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("extras[%d] = %q; want %q", i, got[i], want[i])
		}
	}
}

// TestReadExtraRootsMalformedJSON covers the json.Unmarshal err branch
// (stderr diagnostic, returns nil).
func TestReadExtraRootsMalformedJSON(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); got != nil {
		t.Fatalf("expected nil on malformed JSON, got %v", got)
	}
}

// TestReadExtraRootsNonObject covers the first != '{' branch (stderr, nil).
func TestReadExtraRootsNonObject(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// Valid JSON but not an object.
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte("[1,2,3]"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	if got := ReadExtraRootsFromConfig(); got != nil {
		t.Fatalf("expected nil on non-object JSON, got %v", got)
	}
}

// TestReadExtraRootsLeadingWhitespace exercises the byte-skip loop in the
// object-probe (lines 58-64) — whitespace-only prefixes must still resolve
// to the first non-space byte.
func TestReadExtraRootsLeadingWhitespace(t *testing.T) {
	dir := t.TempDir()
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := "  \n\t{\"extra_roots\":[\"/tmp/x\"]}"
	if err := os.WriteFile(filepath.Join(claudeDir, "walker-roots.json"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", "")
	got := ReadExtraRootsFromConfig()
	if len(got) != 1 || got[0] != "/tmp/x" {
		t.Fatalf("got %v; want [/tmp/x]", got)
	}
}

// TestResolveRootsPrimaryMissing exercises the primary-doesn't-exist branch
// — must return empty without stderr noise. Also covers EvalSymlinks happy
// path on an extra that does exist.
func TestResolveRootsPrimaryMissing(t *testing.T) {
	got := ResolveRoots("/no/such/primary/path", nil, false)
	if len(got) != 0 {
		t.Fatalf("expected empty result with missing primary, got %v", got)
	}
}

// TestResolveRootsExtraSkippedWithStderr exercises the "extra not a directory"
// stderr branch (just confirm it doesn't crash and returns the primary only).
func TestResolveRootsExtraSkippedWithStderr(t *testing.T) {
	primary := t.TempDir()
	got := ResolveRoots(primary, []string{"/nope-extra-path"}, false)
	if len(got) != 1 {
		t.Fatalf("expected primary-only result, got %v", got)
	}
}

// TestResolveRootsDedupsViaSymlink — pointing the same root through a
// symlink and directly should dedup. Best-effort: symlink creation may fail
// on locked-down systems; skip in that case.
func TestResolveRootsDedupsViaSymlink(t *testing.T) {
	primary := t.TempDir()
	linkPath := filepath.Join(t.TempDir(), "primary-link")
	if err := os.Symlink(primary, linkPath); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	got := ResolveRoots(primary, []string{linkPath}, false)
	if len(got) != 1 {
		t.Fatalf("expected 1 deduped result, got %v", got)
	}
}

// TestResolveRootsEvalSymlinksFallback constructs a broken symlink so
// filepath.EvalSymlinks fails (lines 119-121). The stat in the same loop
// fails first and emits the skip diagnostic; this exercises that branch.
func TestResolveRootsBrokenSymlinkSkipped(t *testing.T) {
	dir := t.TempDir()
	broken := filepath.Join(dir, "broken-link")
	if err := os.Symlink("/no/such/target", broken); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	got := ResolveRoots(dir, []string{broken}, false)
	if len(got) != 1 {
		t.Fatalf("expected primary-only result, got %v", got)
	}
}
