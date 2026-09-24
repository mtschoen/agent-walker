// Compressed archive support: zstd inflate at the transcript-read chokepoint,
// transcript file-name classification shared by every discovery path, and
// expansion of a claude-archive root into one claude-code layout root per
// immediate <archive>/<hostname> subdirectory.
//
// See ../SPEC.md sections "Roots", "Discovery", and "Filters".

package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/klauspost/compress/zstd"
)

const (
	archiveDirectoryName = "claude-archive"
	transcriptSuffix     = ".jsonl"
	compressedSuffix     = ".jsonl.zst"
	subagentPrefix       = "agent-"
)

// One shared stateless decoder. zstd.Decoder created with a nil reader is
// safe for concurrent DecodeAll calls, which matters because discovery fans
// files out across workers.
var sharedZstdDecoder = func() *zstd.Decoder {
	decoder, _ := zstd.NewReader(nil)
	return decoder
}()

func defaultArchiveRoot() string {
	if home := homeDirectory(); home != "" {
		return filepath.Join(home, archiveDirectoryName)
	}
	return archiveDirectoryName
}

func parentSessionID(name string) (string, bool) {
	if trimmed, found := strings.CutSuffix(name, compressedSuffix); found {
		return trimmed, true
	}
	if trimmed, found := strings.CutSuffix(name, transcriptSuffix); found {
		return trimmed, true
	}
	return "", false
}

func subagentAgentID(name string) (string, bool) {
	trimmed, found := strings.CutPrefix(name, subagentPrefix)
	if !found {
		return "", false
	}
	return parentSessionID(trimmed)
}

func isCompressedTranscript(path string) bool {
	return strings.HasSuffix(path, compressedSuffix)
}

// Immediate subdirectories, sorted ascending by name so the effective root
// order is deterministic. A missing or unreadable path yields nil; the caller
// emits the diagnostic.
func expandArchiveRoot(path string) []string {
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil
	}
	var hosts []string
	for _, entry := range entries {
		if entry.IsDir() {
			hosts = append(hosts, filepath.Join(path, entry.Name()))
		}
	}
	sort.Strings(hosts)
	return hosts
}

// Whole-file read, inflating a .zst in memory. A decode failure emits the one
// stderr line SPEC "Filters" requires and returns the error; callers skip the
// file and continue.
func readTranscript(path string) ([]byte, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if !isCompressedTranscript(path) {
		return raw, nil
	}
	plain, err := sharedZstdDecoder.DecodeAll(raw, nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "walker: unreadable archive file, skipping: %s\n", path)
		return nil, err
	}
	return plain, nil
}

// Line-oriented reader for the bufio.Scanner call sites. A live .jsonl keeps
// its os.File so the per-line hot path is unchanged; a .jsonl.zst is inflated
// once and served from memory.
func openTranscript(path string) (io.ReadCloser, error) {
	if !isCompressedTranscript(path) {
		return os.Open(path)
	}
	plain, err := readTranscript(path)
	if err != nil {
		return nil, err
	}
	return io.NopCloser(bytes.NewReader(plain)), nil
}
