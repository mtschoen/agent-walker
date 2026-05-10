// Native pace-walker -- Go implementation.
// See ../SPEC.md for the contract every implementation must honor.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

const version = "go/0.1.0"

// CLI arguments.
type arguments struct {
	periodSeconds uint64
	winStartUnix  float64
	nowUnix       float64
	projectsRoot  string
}

func parseArguments() (arguments, error) {
	var (
		period   uint64
		winStart float64
		now      float64
		root     string
		ver      bool
	)

	// Use a custom FlagSet so we control error output.
	flags := flag.NewFlagSet("walker", flag.ContinueOnError)
	flags.Uint64Var(&period, "period", 0, "trailing window in seconds (required)")
	flags.Float64Var(&winStart, "win-start", 0, "window start as Unix epoch (required)")
	flags.Float64Var(&now, "now", 0, "pin 'now' (optional; default = wall clock)")
	flags.StringVar(&root, "projects-root", "", "path to Claude projects root")
	flags.BoolVar(&ver, "version", false, "print version and exit")

	if err := flags.Parse(os.Args[1:]); err != nil {
		return arguments{}, err
	}
	if ver {
		fmt.Println(version)
		os.Exit(0)
	}
	if period == 0 {
		return arguments{}, fmt.Errorf("--period is required and must be > 0")
	}
	if winStart == 0 && !flagExplicitlySet(flags, "win-start") {
		return arguments{}, fmt.Errorf("--win-start is required")
	}

	if now == 0 && !flagExplicitlySet(flags, "now") {
		now = float64(time.Now().UnixNano()) / 1e9
	}
	if root == "" {
		root = defaultProjectsRoot()
	}

	return arguments{
		periodSeconds: period,
		winStartUnix:  winStart,
		nowUnix:       now,
		projectsRoot:  root,
	}, nil
}

// flagExplicitlySet reports whether a flag was passed on the command line.
func flagExplicitlySet(flags *flag.FlagSet, name string) bool {
	found := false
	flags.Visit(func(f *flag.Flag) {
		if f.Name == name {
			found = true
		}
	})
	return found
}

func defaultProjectsRoot() string {
	if home, ok := os.LookupEnv("USERPROFILE"); ok && home != "" {
		return filepath.Join(home, ".claude", "projects")
	}
	if home, ok := os.LookupEnv("HOME"); ok && home != "" {
		return filepath.Join(home, ".claude", "projects")
	}
	return filepath.Join(".claude", "projects")
}

// JSON structures for a JSONL line.

type entry struct {
	Timestamp string   `json:"timestamp"`
	Message   *message `json:"message"`
}

type message struct {
	Role  string  `json:"role"`
	ID    string  `json:"id"`
	Model string  `json:"model"`
	Usage *usage  `json:"usage"`
}

type usage struct {
	InputTokens               uint64 `json:"input_tokens"`
	OutputTokens              uint64 `json:"output_tokens"`
	CacheReadInputTokens      uint64 `json:"cache_read_input_tokens"`
	CacheCreationInputTokens  uint64 `json:"cache_creation_input_tokens"`
}

// ratesForModel returns (inputPerMTok, outputPerMTok) for a model string.
func ratesForModel(model string) (float64, float64) {
	lower := strings.ToLower(model)
	switch {
	case strings.Contains(lower, "opus"):
		return 5.0, 25.0
	case strings.Contains(lower, "haiku"):
		return 1.0, 5.0
	default: // sonnet or unknown -> sonnet
		return 3.0, 15.0
	}
}

// costForTurn computes the dollar cost of one assistant turn.
func costForTurn(u *usage, model string) float64 {
	if u == nil {
		return 0
	}
	inputRate, outputRate := ratesForModel(model)
	return (float64(u.InputTokens)*inputRate +
		float64(u.CacheReadInputTokens)*inputRate*0.10 +
		float64(u.CacheCreationInputTokens)*inputRate*1.25 +
		float64(u.OutputTokens)*outputRate) / 1_000_000.0
}

// parseISO8601 parses an ISO 8601 timestamp (with optional Z suffix) to a
// Unix epoch float64. Returns (0, false) on failure.
func parseISO8601(timestamp string) (float64, bool) {
	if timestamp == "" {
		return 0, false
	}
	// Replace trailing Z with +00:00 so time.Parse handles it.
	normalized := strings.TrimSuffix(timestamp, "Z")
	if len(normalized) < len(timestamp) {
		normalized += "+00:00"
	}

	// Try RFC3339Nano first (most common), then RFC3339.
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339} {
		if t, err := time.Parse(layout, normalized); err == nil {
			return float64(t.UnixNano()) / 1e9, true
		}
	}
	return 0, false
}

// groupResult holds the cost sums for one (slug, session) group.
type groupResult struct {
	trailing float64
	window   float64
}

// walkGroup processes all JSONL files in a session group with shared dedup.
func walkGroup(paths []string, periodCutoff, winStart float64) groupResult {
	earliest := math.Min(periodCutoff, winStart)
	var result groupResult
	seenIDs := make(map[string]struct{})

	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		// Increase token buffer for long lines (some JSONL entries can be large).
		buf := make([]byte, 0, 64*1024)
		scanner.Buffer(buf, 4*1024*1024)

		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" {
				continue
			}

			var e entry
			if err := json.Unmarshal([]byte(line), &e); err != nil {
				continue
			}

			msg := e.Message
			if msg == nil || msg.Role != "assistant" {
				continue
			}

			// Dedup by message ID (if present).
			if msg.ID != "" {
				if _, already := seenIDs[msg.ID]; already {
					continue
				}
				seenIDs[msg.ID] = struct{}{}
			}

			// Timestamp must be parseable and in range.
			if e.Timestamp == "" {
				continue
			}
			ts, ok := parseISO8601(e.Timestamp)
			if !ok || ts < earliest {
				continue
			}

			cost := costForTurn(msg.Usage, msg.Model)
			if ts >= periodCutoff {
				result.trailing += cost
			}
			if ts >= winStart {
				result.window += cost
			}
		}
		file.Close()
	}
	return result
}

// groupKey identifies a unique (slug, session) pair.
type groupKey struct {
	slug      string
	sessionID string
}

// discoverGroups finds all JSONL files under root, applies the mtime filter,
// and groups them by (slug, session_id).
func discoverGroups(root string, earliest float64) map[groupKey][]string {
	groups := make(map[groupKey][]string)
	earliestTime := time.Unix(0, int64(earliest*1e9))

	// Parents: <root>/<slug>/<session_id>.jsonl
	parentGlob := filepath.Join(root, "*", "*.jsonl")
	parentMatches, err := filepath.Glob(parentGlob)
	if err == nil {
		for _, path := range parentMatches {
			info, err := os.Stat(path)
			if err != nil {
				continue
			}
			if info.ModTime().Before(earliestTime) {
				continue
			}
			slug := filepath.Base(filepath.Dir(path))
			sessionID := strings.TrimSuffix(filepath.Base(path), ".jsonl")
			key := groupKey{slug: slug, sessionID: sessionID}
			groups[key] = append(groups[key], path)
		}
	}

	// Subagents: <root>/<slug>/<session_id>/subagents/agent-*.jsonl
	// filepath.Glob doesn't support **, so we walk two levels.
	slugEntries, err := os.ReadDir(root)
	if err == nil {
		for _, slugEntry := range slugEntries {
			if !slugEntry.IsDir() {
				continue
			}
			slugPath := filepath.Join(root, slugEntry.Name())
			sessionEntries, err := os.ReadDir(slugPath)
			if err != nil {
				continue
			}
			for _, sessionEntry := range sessionEntries {
				if !sessionEntry.IsDir() {
					continue
				}
				subagentsDir := filepath.Join(slugPath, sessionEntry.Name(), "subagents")
				subEntries, err := os.ReadDir(subagentsDir)
				if err != nil {
					continue // no subagents dir is normal
				}
				for _, subEntry := range subEntries {
					name := subEntry.Name()
					if subEntry.IsDir() || !strings.HasPrefix(name, "agent-") || !strings.HasSuffix(name, ".jsonl") {
						continue
					}
					path := filepath.Join(subagentsDir, name)
					info, err := os.Stat(path)
					if err != nil {
						continue
					}
					if info.ModTime().Before(earliestTime) {
						continue
					}
					key := groupKey{
						slug:      slugEntry.Name(),
						sessionID: sessionEntry.Name(),
					}
					groups[key] = append(groups[key], path)
				}
			}
		}
	}

	return groups
}

// output is the JSON shape we print to stdout.
type output struct {
	TrailingUSD float64 `json:"trailing_usd"`
	WindowUSD   float64 `json:"window_usd"`
	FilesWalked uint64  `json:"files_walked"`
	Groups      uint64  `json:"groups"`
	ElapsedMS   uint64  `json:"elapsed_ms"`
}

func main() {
	started := time.Now()

	args, err := parseArguments()
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: %v\n", err)
		os.Exit(2)
	}

	periodCutoff := args.nowUnix - float64(args.periodSeconds)
	earliest := math.Min(periodCutoff, args.winStartUnix)

	groups := discoverGroups(args.projectsRoot, earliest)

	// Count totals before we consume the map.
	totalGroups := uint64(len(groups))
	var totalFiles uint64
	for _, paths := range groups {
		totalFiles += uint64(len(paths))
	}

	// Collect groups as slices for concurrent processing.
	groupSlices := make([][]string, 0, len(groups))
	for _, paths := range groups {
		groupSlices = append(groupSlices, paths)
	}

	// Use min(8, numCPU) workers.
	numWorkers := runtime.NumCPU()
	if numWorkers > 8 {
		numWorkers = 8
	}

	type result struct {
		trailing float64
		window   float64
	}

	work := make(chan []string, len(groupSlices))
	results := make(chan result, len(groupSlices))

	var workerGroup sync.WaitGroup
	for workerIndex := 0; workerIndex < numWorkers; workerIndex++ {
		workerGroup.Add(1)
		go func() {
			defer workerGroup.Done()
			for paths := range work {
				r := walkGroup(paths, periodCutoff, args.winStartUnix)
				results <- result{trailing: r.trailing, window: r.window}
			}
		}()
	}

	// Feed work.
	for _, paths := range groupSlices {
		work <- paths
	}
	close(work)

	// Close results once all workers are done.
	go func() {
		workerGroup.Wait()
		close(results)
	}()

	// Collect results.
	var totalTrailing, totalWindow float64
	for r := range results {
		totalTrailing += r.trailing
		totalWindow += r.window
	}

	elapsedMS := uint64(time.Since(started).Milliseconds())

	out := output{
		TrailingUSD: totalTrailing,
		WindowUSD:   totalWindow,
		FilesWalked: totalFiles,
		Groups:      totalGroups,
		ElapsedMS:   elapsedMS,
	}

	// Marshal with 6-decimal precision matching the Rust reference output.
	// encoding/json will use shortest representation, so we format manually.
	fmt.Printf(
		"{\"trailing_usd\":%.6f,\"window_usd\":%.6f,\"files_walked\":%d,\"groups\":%d,\"elapsed_ms\":%d}\n",
		out.TrailingUSD, out.WindowUSD, out.FilesWalked, out.Groups, out.ElapsedMS,
	)

	_ = out // fields printed above via fmt.Printf
}
