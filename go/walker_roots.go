// Roots discovery: primary root + extras from CLI flags + extras from
// ~/.claude/walker-roots.json. Deduped via filepath.EvalSymlinks, filtered
// to existing directories.
//
// Mirrors cpp/walker_roots.hpp and rust/src/walker_roots.rs. Failure modes
// follow the SPEC.md contract:
//   * Missing config file -> no extras (silent).
//   * Malformed JSON -> stderr diagnostic, treat as no extras (must NOT error).
//   * Listed path doesn't exist on disk -> skip silently with stderr line.
//   * EvalSymlinks() fails (broken symlink etc) -> fall back to filepath.Clean.
//   * Primary is allowed to not exist (empty-fleet case); no stderr for it.
//
// Uses encoding/json (not sonic) because this runs once at startup -- the
// per-MB hot-path parsers don't matter here and stdlib is clearer.

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// WalkerConfigPath returns the path to ~/.claude/walker-roots.json. Falls
// back to ".claude/walker-roots.json" if neither HOME nor USERPROFILE is set.
func WalkerConfigPath() string {
	if home, ok := os.LookupEnv("HOME"); ok && home != "" {
		return filepath.Join(home, ".claude", "walker-roots.json")
	}
	if home, ok := os.LookupEnv("USERPROFILE"); ok && home != "" {
		return filepath.Join(home, ".claude", "walker-roots.json")
	}
	return filepath.Join(".claude", "walker-roots.json")
}

type walkerConfig struct {
	ExtraRoots []string `json:"extra_roots"`
}

// ReadExtraRootsFromConfig parses extras from ~/.claude/walker-roots.json.
// Returns nil on any failure; emits a stderr diagnostic for malformed JSON
// or wrong-shape (non-object) bodies specifically.
func ReadExtraRootsFromConfig() []string {
	configPath := WalkerConfigPath()
	body, err := os.ReadFile(configPath)
	if err != nil {
		return nil // missing file or unreadable -- silent
	}
	if len(body) == 0 {
		return nil
	}
	// First check the body is an object (parity with cpp/rust diagnostics).
	var probe json.RawMessage
	if err := json.Unmarshal(body, &probe); err != nil {
		fmt.Fprintf(os.Stderr, "walker: malformed %s -- ignoring extra roots\n", configPath)
		return nil
	}
	// firstNonSpace returns the first non-whitespace byte or 0.
	first := byte(0)
	for _, b := range probe {
		if b == ' ' || b == '\t' || b == '\n' || b == '\r' {
			continue
		}
		first = b
		break
	}
	if first != '{' {
		fmt.Fprintf(os.Stderr, "walker: %s is not a JSON object -- ignoring\n", configPath)
		return nil
	}

	var cfg walkerConfig
	if err := json.Unmarshal(body, &cfg); err != nil {
		fmt.Fprintf(os.Stderr, "walker: malformed %s -- ignoring extra roots\n", configPath)
		return nil
	}
	extras := make([]string, 0, len(cfg.ExtraRoots))
	for _, p := range cfg.ExtraRoots {
		if p != "" {
			extras = append(extras, p)
		}
	}
	return extras
}

// ResolveRoots assembles the effective root list:
//
//	[primary] + cliExtras + (config extras if readConfig)
//	-> dedup via canonical (EvalSymlinks, fall back to Clean)
//	-> filter to existing directories
//
// Primary is allowed to not exist (empty-fleet case) and emits no diagnostic
// in that scenario. Extras that fail the existence/directory check are
// skipped with a stderr diagnostic matching cpp/rust output.
func ResolveRoots(primary string, cliExtras []string, readConfig bool) []string {
	type candidate struct {
		path      string
		isPrimary bool
	}
	combined := []candidate{{path: primary, isPrimary: true}}
	for _, p := range cliExtras {
		combined = append(combined, candidate{path: p})
	}
	if readConfig {
		for _, p := range ReadExtraRootsFromConfig() {
			combined = append(combined, candidate{path: p})
		}
	}

	var result []string
	seen := make(map[string]struct{})
	for _, c := range combined {
		info, err := os.Stat(c.path)
		if err != nil || !info.IsDir() {
			if !c.isPrimary {
				fmt.Fprintf(os.Stderr,
					"walker: extra root not a directory, skipping: %s\n", c.path)
			}
			continue
		}
		canonical, err := filepath.EvalSymlinks(c.path)
		if err != nil {
			canonical = filepath.Clean(c.path)
		}
		if _, exists := seen[canonical]; exists {
			continue
		}
		seen[canonical] = struct{}{}
		result = append(result, canonical)
	}
	return result
}
