// Beacon-mode subcommands: beacons-latest and beacons-history.
// See ../SPEC.md "Subcommands" for the contract.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/bytedance/sonic"
)

var beaconRegex = regexp.MustCompile(`(?s)<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>`)

type beaconEntry struct {
	EntryType string         `json:"type"`
	Timestamp string         `json:"timestamp"`
	Message   *beaconMessage `json:"message"`
}

// beaconMessage uses json.RawMessage for Content because real-world
// transcripts have message.content as either an array of content-blocks
// OR a bare string. Strictly-typed []contentBlock silently fails to
// deserialize the bare-string variants (sonic errors -> entry skipped),
// which drops real user prompts from the idle calculation.
type beaconMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

type contentBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type beacon struct {
	Kind       string  `json:"kind"`
	EtaSeconds float64 `json:"eta_seconds"`
	Summary    string  `json:"summary"`
	Drift      string  `json:"drift"`
	BeatsLeft  *int64  `json:"beats_left,omitempty"`
}

type rawBeacon struct {
	Kind       *string  `json:"kind"`
	EtaSeconds *float64 `json:"eta_seconds"`
	Summary    *string  `json:"summary"`
	Drift      *string  `json:"drift"`
	BeatsLeft  *int64   `json:"beats_left"`
}

func (rb *rawBeacon) toBeacon() (*beacon, bool) {
	if rb.Kind == nil || rb.EtaSeconds == nil || rb.Summary == nil || rb.Drift == nil {
		return nil, false
	}
	return &beacon{
		Kind:       *rb.Kind,
		EtaSeconds: *rb.EtaSeconds,
		Summary:    *rb.Summary,
		Drift:      *rb.Drift,
		BeatsLeft:  rb.BeatsLeft,
	}, true
}

// firstNonSpaceByte returns the first non-whitespace byte of raw or 0
// when the payload is empty/whitespace. Used to dispatch on content shape.
func firstNonSpaceByte(raw json.RawMessage) byte {
	for _, b := range raw {
		switch b {
		case ' ', '\t', '\n', '\r':
			continue
		default:
			return b
		}
	}
	return 0
}

// extractText concatenates text blocks from an array-shaped content.
// Returns empty string if content is a bare string or non-array.
func extractText(content json.RawMessage) string {
	if firstNonSpaceByte(content) != '[' {
		return ""
	}
	var blocks []contentBlock
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return ""
	}
	parts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		if b.Type == "text" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "\n")
}

// userContentIsToolResult returns true iff content is a JSON array and
// any block has type == "tool_result". Tool-result entries are tagged
// type: "user" in JSONL but represent agent-active time, not user idle.
func userContentIsToolResult(content json.RawMessage) bool {
	if firstNonSpaceByte(content) != '[' {
		return false
	}
	var blocks []struct {
		Type string `json:"type"`
	}
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return false
	}
	for _, b := range blocks {
		if b.Type == "tool_result" {
			return true
		}
	}
	return false
}

type beaconWithTimestamp struct {
	beacon    beacon
	timestamp float64
}

// event is one entry in a session timeline. isRealUser is true only when
// the entry has type: "user" AND content is NOT a tool_result array.
type event struct {
	timestamp  float64
	isRealUser bool
}

func findLatestInPath(path string) (*beaconWithTimestamp, bool) {
	file, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 4*1024*1024)

	var latest *beaconWithTimestamp
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var entry beaconEntry
		if err := sonic.Unmarshal([]byte(line), &entry); err != nil {
			continue
		}
		if entry.Message == nil || entry.Message.Role != "assistant" {
			continue
		}
		if len(entry.Message.Content) == 0 {
			continue
		}
		if entry.Timestamp == "" {
			continue
		}
		ts, ok := parseISO8601(entry.Timestamp)
		if !ok {
			continue
		}
		combined := extractText(entry.Message.Content)
		matches := beaconRegex.FindAllStringSubmatch(combined, -1)
		var entryBeacon *beacon
		for _, m := range matches {
			if len(m) < 2 {
				continue
			}
			var rb rawBeacon
			if err := sonic.Unmarshal([]byte(m[1]), &rb); err != nil {
				continue
			}
			if b, ok := rb.toBeacon(); ok {
				entryBeacon = b
			}
		}
		if entryBeacon == nil {
			continue
		}
		if latest == nil || ts >= latest.timestamp {
			latest = &beaconWithTimestamp{beacon: *entryBeacon, timestamp: ts}
		}
	}
	if latest == nil {
		return nil, false
	}
	return latest, true
}

type sessionEvents struct {
	beacons []beaconWithTimestamp
	events  []event
}

// collectSessionEventsInPath walks one transcript and collects beacons +
// per-entry events for the idle-gap calc. Events are NOT sorted; callers
// concatenate across the session group and sort once.
func collectSessionEventsInPath(path string) sessionEvents {
	var out sessionEvents
	file, err := os.Open(path)
	if err != nil {
		return out
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 4*1024*1024)

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var entry beaconEntry
		if err := sonic.Unmarshal([]byte(line), &entry); err != nil {
			continue
		}
		if entry.Timestamp == "" {
			continue
		}
		ts, ok := parseISO8601(entry.Timestamp)
		if !ok {
			continue
		}
		if entry.EntryType == "user" {
			var contentRaw json.RawMessage
			if entry.Message != nil {
				contentRaw = entry.Message.Content
			}
			isRealUser := !userContentIsToolResult(contentRaw)
			out.events = append(out.events, event{timestamp: ts, isRealUser: isRealUser})
			continue
		}
		if entry.Message == nil || entry.Message.Role != "assistant" {
			continue
		}
		if len(entry.Message.Content) == 0 {
			continue
		}
		out.events = append(out.events, event{timestamp: ts, isRealUser: false})
		combined := extractText(entry.Message.Content)
		matches := beaconRegex.FindAllStringSubmatch(combined, -1)
		for _, m := range matches {
			if len(m) < 2 {
				continue
			}
			var rb rawBeacon
			if err := sonic.Unmarshal([]byte(m[1]), &rb); err != nil {
				continue
			}
			if b, ok := rb.toBeacon(); ok {
				out.beacons = append(out.beacons, beaconWithTimestamp{
					beacon: *b, timestamp: ts,
				})
			}
		}
	}
	return out
}

// computeIdleInWindow sums the portion of [lo, hi] occupied by gaps that
// immediately precede a real-user event. events MUST be sorted ascending.
func computeIdleInWindow(events []event, lo, hi float64) float64 {
	if len(events) < 2 {
		return 0
	}
	var idle float64
	for i := 1; i < len(events); i++ {
		if !events[i].isRealUser {
			continue
		}
		gapLo := math.Max(events[i-1].timestamp, lo)
		gapHi := math.Min(events[i].timestamp, hi)
		if gapHi > gapLo {
			idle += gapHi - gapLo
		}
	}
	return idle
}

// === beacons-latest ===

type latestArguments struct {
	sessionID    string
	projectsRoot string
	nowUnix      float64
	nowSet       bool
}

func parseLatestArguments(args []string) (latestArguments, error) {
	var parsed latestArguments
	i := 0
	for i < len(args) {
		flag := args[i]
		switch flag {
		case "--session-id":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--session-id needs a value")
			}
			parsed.sessionID = args[i+1]
			i += 2
		case "--projects-root":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--projects-root needs a value")
			}
			parsed.projectsRoot = args[i+1]
			i += 2
		case "--now":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--now needs a value")
			}
			value, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return parsed, fmt.Errorf("--now: %v", err)
			}
			parsed.nowUnix = value
			parsed.nowSet = true
			i += 2
		default:
			return parsed, fmt.Errorf("unknown flag: %s", flag)
		}
	}
	if parsed.sessionID == "" {
		return parsed, fmt.Errorf("--session-id is required")
	}
	return parsed, nil
}

func runBeaconsLatest(args []string) {
	started := time.Now()
	parsed, err := parseLatestArguments(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: beacons-latest: %v\n", err)
		os.Exit(2)
	}
	root := parsed.projectsRoot
	if root == "" {
		root = defaultProjectsRoot()
	}
	nowUnix := parsed.nowUnix
	if !parsed.nowSet {
		nowUnix = float64(time.Now().UnixNano()) / 1e9
	}

	parentPattern := filepath.Join(root, "*", parsed.sessionID+".jsonl")
	subPattern := filepath.Join(root, "*", "*", "subagents", "agent-"+parsed.sessionID+".jsonl")
	var paths []string
	for _, pattern := range []string{parentPattern, subPattern} {
		matches, err := filepath.Glob(pattern)
		if err == nil {
			paths = append(paths, matches...)
		}
	}

	var best *beaconWithTimestamp
	for _, path := range paths {
		if found, ok := findLatestInPath(path); ok {
			if best == nil || found.timestamp > best.timestamp {
				best = found
			}
		}
	}

	elapsedMS := uint64(time.Since(started).Milliseconds())

	if best == nil {
		fmt.Printf(
			"{\"beacon\":null,\"emitted_at\":null,\"age_seconds\":null,\"elapsed_ms\":%d}\n",
			elapsedMS,
		)
		return
	}
	beaconJSON, err := sonic.Marshal(best.beacon)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: beacons-latest: marshal: %v\n", err)
		os.Exit(2)
	}
	age := nowUnix - best.timestamp
	fmt.Printf(
		"{\"beacon\":%s,\"emitted_at\":%s,\"age_seconds\":%s,\"elapsed_ms\":%d}\n",
		string(beaconJSON),
		formatFloat(best.timestamp),
		formatFloat(age),
		elapsedMS,
	)
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', -1, 64)
}

// === beacons-history ===

type historyArguments struct {
	periodSeconds uint64
	winStartUnix  float64
	projectsRoot  string
	nowUnix       float64
	nowSet        bool
}

func parseHistoryArguments(args []string) (historyArguments, error) {
	var parsed historyArguments
	periodSet := false
	i := 0
	for i < len(args) {
		flag := args[i]
		switch flag {
		case "--period":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--period needs a value")
			}
			value, err := strconv.ParseUint(args[i+1], 10, 64)
			if err != nil {
				return parsed, fmt.Errorf("--period: %v", err)
			}
			parsed.periodSeconds = value
			periodSet = true
			i += 2
		case "--win-start":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--win-start needs a value")
			}
			value, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return parsed, fmt.Errorf("--win-start: %v", err)
			}
			parsed.winStartUnix = value
			i += 2
		case "--projects-root":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--projects-root needs a value")
			}
			parsed.projectsRoot = args[i+1]
			i += 2
		case "--now":
			if i+1 >= len(args) {
				return parsed, fmt.Errorf("--now needs a value")
			}
			value, err := strconv.ParseFloat(args[i+1], 64)
			if err != nil {
				return parsed, fmt.Errorf("--now: %v", err)
			}
			parsed.nowUnix = value
			parsed.nowSet = true
			i += 2
		default:
			return parsed, fmt.Errorf("unknown flag: %s", flag)
		}
	}
	if !periodSet {
		return parsed, fmt.Errorf("--period is required")
	}
	return parsed, nil
}

// discoverHistoryGroups groups transcripts by (slug, session_id) without
// the mtime filter -- beacon entries can sit deep in a long transcript.
func discoverHistoryGroups(root string) map[groupKey][]string {
	groups := make(map[groupKey][]string)

	parentGlob := filepath.Join(root, "*", "*.jsonl")
	parentMatches, err := filepath.Glob(parentGlob)
	if err == nil {
		for _, path := range parentMatches {
			slug := filepath.Base(filepath.Dir(path))
			sessionID := strings.TrimSuffix(filepath.Base(path), ".jsonl")
			key := groupKey{slug: slug, sessionID: sessionID}
			groups[key] = append(groups[key], path)
		}
	}

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
					continue
				}
				for _, subEntry := range subEntries {
					name := subEntry.Name()
					if subEntry.IsDir() || !strings.HasPrefix(name, "agent-") || !strings.HasSuffix(name, ".jsonl") {
						continue
					}
					path := filepath.Join(subagentsDir, name)
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

// historyPair carries the four elapsed values per begin/end pair.
type historyPair struct {
	beginEta      float64
	actualElapsed float64
	idleExcluded  float64
	activeElapsed float64
}

// biasFactor returns the median of (active/eta) ratios. Pairs with eta<=0
// are excluded. Returns (0, false) when no usable pairs exist.
func biasFactor(pairs []historyPair) (float64, bool) {
	if len(pairs) == 0 {
		return 0, false
	}
	ratios := make([]float64, 0, len(pairs))
	for _, p := range pairs {
		if p.beginEta > 0 {
			ratios = append(ratios, p.activeElapsed/p.beginEta)
		}
	}
	if len(ratios) == 0 {
		return 0, false
	}
	sort.Float64s(ratios)
	n := len(ratios)
	if n%2 == 1 {
		return ratios[n/2], true
	}
	return (ratios[n/2-1] + ratios[n/2]) / 2.0, true
}

func runBeaconsHistory(args []string) {
	started := time.Now()
	parsed, err := parseHistoryArguments(args)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: beacons-history: %v\n", err)
		os.Exit(2)
	}
	nowUnix := parsed.nowUnix
	if !parsed.nowSet {
		nowUnix = float64(time.Now().UnixNano()) / 1e9
	}
	periodCutoff := nowUnix - float64(parsed.periodSeconds)
	windowLo := periodCutoff
	if parsed.winStartUnix > windowLo {
		windowLo = parsed.winStartUnix
	}
	root := parsed.projectsRoot
	if root == "" {
		root = defaultProjectsRoot()
	}

	groups := discoverHistoryGroups(root)
	sessionCount := uint64(len(groups))

	var pairs []historyPair
	for _, paths := range groups {
		var beaconsAll []beaconWithTimestamp
		var eventsAll []event
		for _, path := range paths {
			se := collectSessionEventsInPath(path)
			beaconsAll = append(beaconsAll, se.beacons...)
			eventsAll = append(eventsAll, se.events...)
		}
		sort.Slice(eventsAll, func(i, j int) bool {
			return eventsAll[i].timestamp < eventsAll[j].timestamp
		})
		var inside []beaconWithTimestamp
		for _, b := range beaconsAll {
			if b.timestamp >= windowLo {
				inside = append(inside, b)
			}
		}

		var begin, end *beaconWithTimestamp
		for index := range inside {
			b := &inside[index]
			switch b.beacon.Kind {
			case "begin":
				if begin == nil || b.timestamp < begin.timestamp {
					begin = b
				}
			case "end":
				if end == nil || b.timestamp > end.timestamp {
					end = b
				}
			}
		}
		if begin != nil && end != nil && end.timestamp > begin.timestamp {
			wall := end.timestamp - begin.timestamp
			idle := computeIdleInWindow(eventsAll, begin.timestamp, end.timestamp)
			active := wall - idle
			if active < 0 {
				active = 0
			}
			pairs = append(pairs, historyPair{
				beginEta:      begin.beacon.EtaSeconds,
				actualElapsed: wall,
				idleExcluded:  idle,
				activeElapsed: active,
			})
		}
	}

	bias, biasOK := biasFactor(pairs)
	elapsedMS := uint64(time.Since(started).Milliseconds())

	var pairsBuilder strings.Builder
	pairsBuilder.WriteString("[")
	for index, p := range pairs {
		if index > 0 {
			pairsBuilder.WriteString(",")
		}
		fmt.Fprintf(&pairsBuilder,
			"{\"begin_eta\":%s,\"actual_elapsed\":%s,\"idle_excluded\":%s,\"active_elapsed\":%s}",
			formatFloat(p.beginEta),
			formatFloat(p.actualElapsed),
			formatFloat(p.idleExcluded),
			formatFloat(p.activeElapsed),
		)
	}
	pairsBuilder.WriteString("]")

	biasJSON := "null"
	if biasOK {
		biasJSON = formatFloat(bias)
	}

	fmt.Printf(
		"{\"pairs\":%s,\"session_count\":%d,\"n_pairs\":%d,\"bias_factor\":%s,\"elapsed_ms\":%d}\n",
		pairsBuilder.String(),
		sessionCount,
		len(pairs),
		biasJSON,
		elapsedMS,
	)
}
