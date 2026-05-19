// Search subcommand: substring/regex match across transcript content.
// See ../SPEC.md "Subcommands" for the contract.
//
// Algorithm mirrors rust/src/search.rs:
//   - Parse flags: pattern (positional), --regex, --case-sensitive, --role,
//     --since/--until (time), --cwd/--any-cwd, --context, --limit,
//     --count-only, --include-tool-blocks, --format (pretty|jsonl),
//     --snippet-chars, --projects-root, --now.
//   - For each jsonl file under --projects-root/<slug>/*.jsonl:
//     - Scan each line as an assistant entry.
//     - Extract text from message.content (text blocks; tool_use/tool_result
//       blocks only when --include-tool-blocks).
//     - Skip entries where content is ONLY tool_use/tool_result blocks
//       (unless --include-tool-blocks is set).
//     - Filter by role (--role user|assistant|both).
//     - Filter by time window (--since/--until).
//     - Find pattern matches in the extracted text.
//     - Build snippet around first match with configurable width.
//     - Collect context_before/context_after turns.
//   - Sort hits newest-first by timestamp, tiebreak (session_id, line_number).
//   - Count distinct (slug, session_id) pairs BEFORE truncation.
//   - Truncate to --limit.
//   - Output hits + summary as JSONL or pretty text.

package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bytedance/sonic"
)

// searchExtractText concatenates text blocks from an array-shaped content.
// With include_tool_blocks, also pulls tool_use.input and tool_result.content.
func searchExtractText(content json.RawMessage, include_tool_blocks bool) string {
	if firstNonSpaceByte(content) != '[' {
		// Bare string content
		var s string
		if err := sonic.Unmarshal(content, &s); err == nil {
			return s
		}
		return ""
	}
	type block struct {
		Type    string          `json:"type"`
		Text    string          `json:"text"`
		Content json.RawMessage `json:"content"`
		Input   json.RawMessage `json:"input"`
	}
	var blocks []block
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return ""
	}
	var parts []string
	for _, b := range blocks {
		switch b.Type {
		case "text":
			parts = append(parts, b.Text)
		case "tool_use":
			if include_tool_blocks && len(b.Input) > 0 {
				parts = append(parts, string(b.Input))
			}
		case "tool_result":
			if include_tool_blocks && len(b.Content) > 0 {
				if firstNonSpaceByte(b.Content) != '[' {
					var s string
					if err := sonic.Unmarshal(b.Content, &s); err == nil {
						parts = append(parts, s)
					}
				} else {
					type innerBlock struct {
						Type string `json:"type"`
						Text string `json:"text"`
					}
					var inner []innerBlock
					if err := sonic.Unmarshal(b.Content, &inner); err == nil {
						for _, ib := range inner {
							if ib.Type == "text" {
								parts = append(parts, ib.Text)
							}
						}
					}
				}
			}
		}
	}
	return strings.Join(parts, "\n")
}

// searchIsOnlyToolBlocks returns true when content is a JSON array of only tool_use/tool_result blocks.
func searchIsOnlyToolBlocks(content json.RawMessage) bool {
	if firstNonSpaceByte(content) != '[' {
		return false
	}
	type simpleBlock struct {
		Type string `json:"type"`
	}
	var blocks []simpleBlock
	if err := sonic.Unmarshal(content, &blocks); err != nil {
		return false
	}
	if len(blocks) == 0 {
		return false
	}
	for _, b := range blocks {
		if b.Type != "tool_use" && b.Type != "tool_result" {
			return false
		}
	}
	return true
}

// === Scan ===

type searchMsg struct {
	LineNumber       uint32
	Timestamp        float64
	HasTimestamp     bool
	TimestampStr     string
	Role             string
	TextDefault      string
	TextWithTools    string
	IsOnlyToolBlocks bool
}

func searchScanFile(path string) []searchMsg {
	file, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer file.Close()
	var out []searchMsg
	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 4*1024*1024)
	idx := 0
	for scanner.Scan() {
		idx++
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var root map[string]json.RawMessage
		if err := sonic.Unmarshal([]byte(line), &root); err != nil {
			continue
		}
		msgRaw, ok := root["message"]
		if !ok {
			continue
		}
		var msg struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		}
		if err := sonic.Unmarshal(msgRaw, &msg); err != nil || msg.Role == "" {
			continue
		}
		// Include ALL roles (user, assistant, etc.) — context needs all messages
		// processFile will filter by role as needed
		var tsStr string
		if raw, ok := root["timestamp"]; ok {
			json.Unmarshal(raw, &tsStr)
		}
		var ts float64
		hasTs := false
		if tsStr != "" {
			t, ok := parseISO8601(tsStr)
			if ok {
				ts = t
				hasTs = true
			}
		}
		content := msg.Content
		if content == nil {
			continue
		}
		sm := searchMsg{
			LineNumber:       uint32(idx),
			Timestamp:        ts,
			HasTimestamp:     hasTs,
			TimestampStr:     tsStr,
			Role:             msg.Role,
			TextDefault:      searchExtractText(content, false),
			TextWithTools:    searchExtractText(content, true),
			IsOnlyToolBlocks: searchIsOnlyToolBlocks(content),
		}
		out = append(out, sm)
	}
	return out
}

// === Discovery ===

type searchFileInfo struct {
	Path      string
	Slug      string
	SessionID string
}

func searchDiscoverFiles(root string, since *float64, cwdSlug *string) []searchFileInfo {
	var out []searchFileInfo
	entries, err := os.ReadDir(root)
	if err != nil {
		return out
	}
	earliestTime := time.Time{}
	if since != nil {
		earliestTime = time.Unix(0, int64(*since*1e9))
	}
	for _, slugEnt := range entries {
		if !slugEnt.IsDir() {
			continue
		}
		slug := slugEnt.Name()
		if cwdSlug != nil && slug != *cwdSlug {
			continue
		}
		slugPath := filepath.Join(root, slug)
		dirEntries, err := os.ReadDir(slugPath)
		if err != nil {
			continue
		}
		for _, fEnt := range dirEntries {
			if fEnt.IsDir() {
				continue
			}
			if !strings.HasSuffix(fEnt.Name(), ".jsonl") {
				continue
			}
			if since != nil {
				info, err := fEnt.Info()
				if err == nil && info.ModTime().Before(earliestTime) {
					continue
				}
			}
			df := searchFileInfo{
				Path:      filepath.Join(slugPath, fEnt.Name()),
				Slug:      slug,
				SessionID: strings.TrimSuffix(fEnt.Name(), ".jsonl"),
			}
			out = append(out, df)
		}
	}
	return out
}

// === Snippet ===

func searchNudgeWS(text string, cut int, direction int, maxNudge int) int {
	if cut <= 0 || cut >= len(text) {
		return cut
	}
	if direction < 0 {
		lo := cut - maxNudge
		if lo < 0 {
			lo = 0
		}
		for i := cut; i > lo; i-- {
			if i > 0 && (text[i-1] == ' ' || text[i-1] == '\t' || text[i-1] == '\n' || text[i-1] == '\r') {
				return i
			}
		}
	} else {
		hi := cut + maxNudge
		if hi > len(text) {
			hi = len(text)
		}
		for i := cut; i < hi; i++ {
			if text[i] == ' ' || text[i] == '\t' || text[i] == '\n' || text[i] == '\r' {
				return i
			}
		}
	}
	return cut
}

func searchMakeSnippet(text string, firstMatch [2]uint32, snippetChars uint32) string {
	halfInt := int(snippetChars / 2)
	mstart := int(firstMatch[0])
	mend := int(firstMatch[1])
	lo := mstart - halfInt
	if lo < 0 {
		lo = 0
	}
	hi := mend + halfInt
	if hi > len(text) {
		hi = len(text)
	}
	if lo > 0 {
		lo = searchNudgeWS(text, lo, -1, 20)
	}
	if hi < len(text) {
		hi = searchNudgeWS(text, hi, 1, 20)
	}
	return text[lo:hi]
}

// === Context ===

type searchCtx struct {
	Role      string `json:"role"`
	Text      string `json:"text"`
	Timestamp string `json:"timestamp"`
}

func searchBuildCtx(msgs []searchMsg, hitIdx int, ctxN uint32) ([]searchCtx, []searchCtx) {
	if ctxN == 0 {
		return nil, nil
	}
	n := int(ctxN)
	var before, after []searchCtx
	start := hitIdx - n
	if start < 0 {
		start = 0
	}
	for i := start; i < hitIdx; i++ {
		before = append(before, searchCtx{msgs[i].Role, msgs[i].TextDefault, msgs[i].TimestampStr})
	}
	end := hitIdx + 1 + n
	if end > len(msgs) {
		end = len(msgs)
	}
	for i := hitIdx + 1; i < end; i++ {
		after = append(after, searchCtx{msgs[i].Role, msgs[i].TextDefault, msgs[i].TimestampStr})
	}
	return before, after
}

// === Hit ===

type searchHit struct {
	Timestamp      float64       `json:"-"`
	TimestampStr   string        `json:"timestamp"`
	SessionID      string        `json:"session_id"`
	CwdSlug        string        `json:"cwd_slug"`
	HostRoot       string        `json:"host_root"`
	FilePath       string        `json:"file_path"`
	LineNumber     uint32        `json:"line_number"`
	Role           string        `json:"role"`
	Snippet        string        `json:"snippet"`
	MatchOffsets   [][2]uint32   `json:"match_offsets"`
	ContextBefore  []searchCtx   `json:"context_before"`
	ContextAfter   []searchCtx   `json:"context_after"`
}

// === Args ===

type searchArgs struct {
	Pattern             string
	Regex               bool
	CaseSensitive       bool
	Role                string
	Since               *float64
	Until               *float64
	Cwd                 string
	AnyCwd              bool
	Context             uint32
	Limit               uint32
	CountOnly           bool
	IncludeToolBlocks   bool
	Format              string
	SnippetChars        uint32
	ProjectsRoot        string
	Now                 float64
}

func parseSearchArgs(raw []string) (searchArgs, error) {
	var args searchArgs
	args.Role = "both"
	args.Context = 1
	args.Limit = 50
	args.Format = "pretty"
	args.SnippetChars = 240
	args.ProjectsRoot = defaultProjectsRoot()
	args.Now = float64(time.Now().UnixNano()) / 1e9

	var sinceRaw, untilRaw *string

	for i := 0; i < len(raw); i++ {
		switch raw[i] {
		case "--regex":
			args.Regex = true
		case "--case-sensitive":
			args.CaseSensitive = true
		case "--role":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--role needs a value")
			}
			i++
			args.Role = raw[i]
			if args.Role != "user" && args.Role != "assistant" && args.Role != "both" {
				return args, fmt.Errorf("--role: invalid value %s; expected user|assistant|both", args.Role)
			}
		case "--since":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--since needs a value")
			}
			i++
			sinceRaw = &raw[i]
		case "--until":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--until needs a value")
			}
			i++
			untilRaw = &raw[i]
		case "--cwd":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--cwd needs a value")
			}
			i++
			args.Cwd = raw[i]
		case "--any-cwd":
			args.AnyCwd = true
		case "--context":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--context needs a value")
			}
			i++
			v, err := strconv.ParseUint(raw[i], 10, 32)
			if err != nil {
				return args, fmt.Errorf("--context: %v", err)
			}
			args.Context = uint32(v)
		case "--limit":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--limit needs a value")
			}
			i++
			v, err := strconv.ParseUint(raw[i], 10, 32)
			if err != nil {
				return args, fmt.Errorf("--limit: %v", err)
			}
			args.Limit = uint32(v)
		case "--count-only":
			args.CountOnly = true
		case "--include-tool-blocks":
			args.IncludeToolBlocks = true
		case "--format":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--format needs a value")
			}
			i++
			args.Format = raw[i]
			if args.Format != "pretty" && args.Format != "jsonl" {
				return args, fmt.Errorf("--format: invalid value %s; expected pretty|jsonl", args.Format)
			}
		case "--snippet-chars":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--snippet-chars needs a value")
			}
			i++
			v, err := strconv.ParseUint(raw[i], 10, 32)
			if err != nil {
				return args, fmt.Errorf("--snippet-chars: %v", err)
			}
			args.SnippetChars = uint32(v)
		case "--projects-root":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--projects-root needs a value")
			}
			i++
			args.ProjectsRoot = raw[i]
		case "--now":
			if i+1 >= len(raw) {
				return args, fmt.Errorf("--now needs a value")
			}
			i++
			v, err := strconv.ParseFloat(raw[i], 64)
			if err != nil {
				return args, fmt.Errorf("--now: %v", err)
			}
			args.Now = v
		default:
			if strings.HasPrefix(raw[i], "--") {
				return args, fmt.Errorf("unknown flag: %s", raw[i])
			}
			if args.Pattern != "" {
				return args, fmt.Errorf("unexpected positional argument: %s", raw[i])
			}
			args.Pattern = raw[i]
		}
	}

	if args.Pattern == "" {
		return args, fmt.Errorf("pattern must be non-empty")
	}
	if args.Cwd != "" && args.AnyCwd {
		return args, fmt.Errorf("--cwd and --any-cwd are mutually exclusive")
	}
	if args.ProjectsRoot == "" {
		args.ProjectsRoot = defaultProjectsRoot()
	}

	if sinceRaw != nil {
		v, err := parseSearchTimeArg(*sinceRaw, args.Now)
		if err != nil {
			return args, fmt.Errorf("bad time: --since=%s (%v)", *sinceRaw, err)
		}
		args.Since = &v
	}
	if untilRaw != nil {
		v, err := parseSearchTimeArg(*untilRaw, args.Now)
		if err != nil {
			return args, fmt.Errorf("bad time: --until=%s (%v)", *untilRaw, err)
		}
		args.Until = &v
	}

	return args, nil
}

func parseSearchTimeArg(s string, now float64) (float64, error) {
	trimmed := strings.TrimSpace(s)
	if trimmed == "" {
		return 0, fmt.Errorf("empty value")
	}
	if len(trimmed) > 0 {
		last := trimmed[len(trimmed)-1]
		if last == 'd' || last == 'h' || last == 'm' || last == 's' {
			head := trimmed[:len(trimmed)-1]
			if head != "" && isSearchNumeric(head) {
				n, err := strconv.ParseFloat(head, 64)
				if err != nil {
					return 0, err
				}
				mult := map[byte]float64{'d': 86400, 'h': 3600, 'm': 60, 's': 1}[last]
				return now - n*mult, nil
			}
		}
	}
	ts, ok := parseISO8601(trimmed)
	if !ok {
		return 0, fmt.Errorf("not RFC3339 or relative: %s", trimmed)
	}
	return ts, nil
}

func isSearchNumeric(s string) bool {
	for _, c := range s {
		if c != '.' && (c < '0' || c > '9') {
			return false
		}
	}
	return true
}

// === File processing ===

func searchProcessFile(f searchFileInfo, args searchArgs, re *regexp.Regexp, hostRoot string) []searchHit {
	msgs := searchScanFile(f.Path)
	var hits []searchHit

	for idx, m := range msgs {
		if !searchRoleMatches(args.Role, m.Role) {
			continue
		}
		if !args.IncludeToolBlocks && m.IsOnlyToolBlocks {
			continue
		}
		if (args.Since != nil || args.Until != nil) && !m.HasTimestamp {
			continue
		}
		if args.Since != nil && m.Timestamp < *args.Since {
			continue
		}
		if args.Until != nil && m.Timestamp > *args.Until {
			continue
		}

		text := m.TextDefault
		if args.IncludeToolBlocks {
			text = m.TextWithTools
		}
		if text == "" {
			continue
		}

		matches := re.FindAllStringIndex(text, -1)
		if len(matches) == 0 {
			continue
		}

		firstMatch := matches[0]
		snippet := searchMakeSnippet(text, [2]uint32{uint32(firstMatch[0]), uint32(firstMatch[1])}, args.SnippetChars)

		snippetMatches := re.FindAllStringIndex(snippet, -1)
		var offsets [][2]uint32
		for _, m2 := range snippetMatches {
			offsets = append(offsets, [2]uint32{uint32(m2[0]), uint32(m2[1])})
		}

		ctxBefore, ctxAfter := searchBuildCtx(msgs, idx, args.Context)
		if ctxBefore == nil {
			ctxBefore = []searchCtx{}
		}
		if ctxAfter == nil {
			ctxAfter = []searchCtx{}
		}

		h := searchHit{
			Timestamp:      m.Timestamp,
			TimestampStr:   m.TimestampStr,
			SessionID:      f.SessionID,
			CwdSlug:        f.Slug,
			HostRoot:       hostRoot,
			FilePath:       f.Path,
			LineNumber:     m.LineNumber,
			Role:           m.Role,
			Snippet:        snippet,
			MatchOffsets:   offsets,
			ContextBefore:  ctxBefore,
			ContextAfter:   ctxAfter,
		}
		hits = append(hits, h)
	}
	return hits
}

func searchRoleMatches(filter, role string) bool {
	return filter == "both" || filter == role
}

// === Output ===

type searchHitJSON struct {
	Type          string        `json:"type"`
	SessionID     string        `json:"session_id"`
	CwdSlug       string        `json:"cwd_slug"`
	HostRoot      string        `json:"host_root"`
	FilePath      string        `json:"file_path"`
	LineNumber    uint32        `json:"line_number"`
	Timestamp     string        `json:"timestamp"`
	Role          string        `json:"role"`
	Snippet       string        `json:"snippet"`
	MatchOffsets  [][2]uint32   `json:"match_offsets"`
	ContextBefore []searchCtx   `json:"context_before"`
	ContextAfter  []searchCtx   `json:"context_after"`
}

type searchSummaryJSON struct {
	Type            string `json:"type"`
	Hits            uint64 `json:"hits"`
	SessionsMatched uint64 `json:"sessions_matched"`
	RotsWalked      uint64 `json:"roots_walked"`
	FilesWalked     uint64 `json:"files_walked"`
	Truncated       bool   `json:"truncated"`
	ElapsedMS       uint64 `json:"elapsed_ms"`
}

func searchWriteSummary(out *strings.Builder, hitsCount uint64, sessions uint64, roots uint64, files uint64, truncated bool, elapsedMs uint64) {
	s := searchSummaryJSON{
		Type:            "summary",
		Hits:            hitsCount,
		SessionsMatched: sessions,
		RotsWalked:      roots,
		FilesWalked:     files,
		Truncated:       truncated,
		ElapsedMS:       elapsedMs,
	}
	b, _ := json.Marshal(s)
	out.WriteString(string(b))
}

func searchTruncateStr(s string, max int) string {
	if len(s) <= max {
		return s
	}
	return s[:max] + "…"
}

// === Top-level ===

func runSearch(argv []string) {
	started := time.Now()
	args, err := parseSearchArgs(argv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: search: %v\n", err)
		os.Exit(2)
	}

	// Build regex
	var re *regexp.Regexp
	pattern := args.Pattern
	if !args.Regex {
		pattern = regexp.QuoteMeta(args.Pattern)
	}
	flags := ""
	if !args.CaseSensitive {
		flags = "(?i)"
	}
	re, err = regexp.Compile(flags + pattern)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: search: bad regex: %v\n", err)
		os.Exit(2)
	}

	// Discover files
	var cwdSlug *string
	if args.Cwd != "" {
		cwdSlug = &args.Cwd
	}
	files := searchDiscoverFiles(args.ProjectsRoot, args.Since, cwdSlug)
	filesWalked := uint64(len(files))
	hostRoot := args.ProjectsRoot

	// Process files in parallel. Work unit = one file; each worker owns a
	// local hits slice to avoid contention; merge after all workers exit,
	// then sort. searchScanFile/searchProcessFile use only local state; the
	// shared compiled `re` is safe for concurrent use per regexp.Regexp's
	// docs ("safe for concurrent use by multiple goroutines"). sonic's
	// Unmarshal is documented thread-safe. Mirrors the cost-mode pattern
	// in main.go's runCost (channel + sync.WaitGroup + per-worker accumulator).
	numWorkers := runtime.NumCPU()
	if numWorkers > 8 {
		numWorkers = 8
	}
	if numWorkers < 1 {
		numWorkers = 1
	}

	work := make(chan searchFileInfo, len(files))
	perWorkerHits := make([][]searchHit, numWorkers)

	var wg sync.WaitGroup
	for workerIndex := 0; workerIndex < numWorkers; workerIndex++ {
		wg.Add(1)
		go func(tid int) {
			defer wg.Done()
			local := perWorkerHits[tid][:0]
			for f := range work {
				hs := searchProcessFile(f, args, re, hostRoot)
				if len(hs) > 0 {
					local = append(local, hs...)
				}
			}
			perWorkerHits[tid] = local
		}(workerIndex)
	}
	for _, f := range files {
		work <- f
	}
	close(work)
	wg.Wait()

	// Merge per-worker hits
	total := 0
	for _, v := range perWorkerHits {
		total += len(v)
	}
	hits := make([]searchHit, 0, total)
	for _, v := range perWorkerHits {
		hits = append(hits, v...)
	}

	// Sort newest-first
	sort.Slice(hits, func(i, j int) bool {
		if hits[i].Timestamp != hits[j].Timestamp {
			return hits[i].Timestamp > hits[j].Timestamp
		}
		if hits[i].SessionID != hits[j].SessionID {
			return hits[i].SessionID < hits[j].SessionID
		}
		return hits[i].LineNumber < hits[j].LineNumber
	})

	// Count distinct sessions BEFORE truncation
	sessionSet := make(map[string]bool)
	for _, h := range hits {
		sessionSet[h.CwdSlug+"/"+h.SessionID] = true
	}
	sessionsMatched := uint64(len(sessionSet))
	totalUnfiltered := uint64(len(hits))
	truncated := totalUnfiltered > uint64(args.Limit)
	if truncated {
		hits = hits[:args.Limit]
	}

	elapsedMs := uint64(time.Since(started).Milliseconds())
	hitsOutput := totalUnfiltered
	if !args.CountOnly {
		hitsOutput = uint64(len(hits))
	}

	out := &strings.Builder{}

	if args.Format == "jsonl" {
		if !args.CountOnly {
			for _, h := range hits {
				hj := searchHitJSON{
					Type:          "hit",
					SessionID:     h.SessionID,
					CwdSlug:       h.CwdSlug,
					HostRoot:      h.HostRoot,
					FilePath:      h.FilePath,
					LineNumber:    h.LineNumber,
					Timestamp:     h.TimestampStr,
					Role:          h.Role,
					Snippet:       h.Snippet,
					MatchOffsets:  h.MatchOffsets,
					ContextBefore: h.ContextBefore,
					ContextAfter:  h.ContextAfter,
				}
				b, _ := json.Marshal(hj)
				out.Write(b)
				out.WriteByte('\n')
			}
		}
		searchWriteSummary(out, hitsOutput, sessionsMatched, 1, filesWalked, truncated, elapsedMs)
		out.WriteByte('\n')
		fmt.Print(out.String())
	} else {
		// Pretty format
		if !args.CountOnly {
			for _, h := range hits {
				fmt.Printf("[%s] cwd=%s role=%s session=%s\n", h.TimestampStr, h.CwdSlug, h.Role, h.SessionID)
				fmt.Printf("  %s:%d\n", h.FilePath, h.LineNumber)
				for _, t := range h.ContextBefore {
					fmt.Printf("  before: %s\n", searchTruncateStr(t.Text, 120))
				}
				if len(h.MatchOffsets) > 0 {
					mo := h.MatchOffsets[0]
					pm := int(mo[0])
					pe := int(mo[1])
					if pm > len(h.Snippet) {
						pm = len(h.Snippet)
					}
					if pe > len(h.Snippet) {
						pe = len(h.Snippet)
					}
					fmt.Printf("  >>>%s[%s]<<<\n", h.Snippet[:pm], h.Snippet[pm:pe])
				} else {
					fmt.Printf("  %s\n", h.Snippet)
				}
				for _, t := range h.ContextAfter {
					fmt.Printf("  after:  %s\n", searchTruncateStr(t.Text, 120))
				}
				fmt.Println()
			}
		}
		searchWriteSummary(out, hitsOutput, sessionsMatched, 1, filesWalked, truncated, elapsedMs)
		out.WriteByte('\n')
		fmt.Print(out.String())
	}

	if truncated {
		fmt.Fprintf(os.Stderr, "walker: search: truncated to --limit=%d (had %d total); narrow with --since\n", args.Limit, totalUnfiltered)
	}
}
