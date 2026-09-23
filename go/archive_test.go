package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/klauspost/compress/zstd"
)

func TestParentSessionIDAcceptsBothSuffixes(t *testing.T) {
	cases := []struct {
		name   string
		want   string
		wantOK bool
	}{
		{"abc.jsonl", "abc", true},
		{"abc.jsonl.zst", "abc", true},
		{"abc.jsonl.gz", "", false},
		{"abc.txt", "", false},
	}
	for _, testCase := range cases {
		got, ok := parentSessionID(testCase.name)
		if got != testCase.want || ok != testCase.wantOK {
			t.Fatalf("parentSessionID(%q) = (%q, %v), want (%q, %v)",
				testCase.name, got, ok, testCase.want, testCase.wantOK)
		}
	}
}

func TestSubagentAgentIDRequiresPrefix(t *testing.T) {
	if id, ok := subagentAgentID("agent-aaa.jsonl.zst"); !ok || id != "aaa" {
		t.Fatalf("subagentAgentID = (%q, %v), want (aaa, true)", id, ok)
	}
	if _, ok := subagentAgentID("aaa.jsonl"); ok {
		t.Fatal("subagentAgentID accepted a name without the agent- prefix")
	}
}

func TestExpandArchiveRootSortsHosts(t *testing.T) {
	root := t.TempDir()
	for _, host := range []string{"llamabox", "chonkers"} {
		if err := os.MkdirAll(filepath.Join(root, host), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "README.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	want := []string{filepath.Join(root, "chonkers"), filepath.Join(root, "llamabox")}
	if got := expandArchiveRoot(root); !reflect.DeepEqual(got, want) {
		t.Fatalf("expandArchiveRoot = %v, want %v", got, want)
	}
	if got := expandArchiveRoot(filepath.Join(root, "missing")); len(got) != 0 {
		t.Fatalf("expandArchiveRoot(missing) = %v, want empty", got)
	}
}

func TestReadTranscriptInflatesAndRejects(t *testing.T) {
	root := t.TempDir()
	plainPath := filepath.Join(root, "session.jsonl")
	if err := os.WriteFile(plainPath, []byte("{\"a\":1}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, err := readTranscript(plainPath); err != nil || string(got) != "{\"a\":1}\n" {
		t.Fatalf("readTranscript(plain) = (%q, %v)", got, err)
	}

	encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedBetterCompression))
	if err != nil {
		t.Fatal(err)
	}
	compressedPath := filepath.Join(root, "session.jsonl.zst")
	if err := os.WriteFile(compressedPath,
		encoder.EncodeAll([]byte("{\"b\":2}\n"), nil), 0o644); err != nil {
		t.Fatal(err)
	}
	if got, err := readTranscript(compressedPath); err != nil || string(got) != "{\"b\":2}\n" {
		t.Fatalf("readTranscript(compressed) = (%q, %v)", got, err)
	}

	brokenPath := filepath.Join(root, "broken.jsonl.zst")
	if err := os.WriteFile(brokenPath, []byte("not a zstd frame"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readTranscript(brokenPath); err == nil {
		t.Fatal("readTranscript accepted a corrupt frame")
	}

	if _, err := readTranscript(filepath.Join(root, "missing.jsonl")); err == nil {
		t.Fatal("readTranscript accepted a nonexistent file")
	}
}

func TestDiscoverHistoryGroupsDanglingSymlink(t *testing.T) {
	root := t.TempDir()
	slugDir := filepath.Join(root, "slug-a")
	if err := os.MkdirAll(slugDir, 0o755); err != nil {
		t.Fatal(err)
	}
	danglingParent := filepath.Join(slugDir, "dangling.jsonl")
	_ = os.Symlink("/no/such/target.jsonl", danglingParent)

	subDir := filepath.Join(slugDir, "sess", "subagents")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	danglingAgent := filepath.Join(subDir, "agent-dang.jsonl")
	_ = os.Symlink("/no/such/agent.jsonl", danglingAgent)

	groups := discoverHistoryGroups([]resolvedRoot{{Path: root, FromArchive: false}})
	if len(groups) != 0 {
		t.Fatalf("expected empty groups, got %v", groups)
	}
}
