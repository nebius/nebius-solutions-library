package executor

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestOpenPinnedRestoreNamespacesUsesExplicitFDs(t *testing.T) {
	procRoot := t.TempDir()
	pidRoot := filepath.Join(procRoot, "42")
	nsRoot := filepath.Join(pidRoot, "ns")
	if err := os.MkdirAll(nsRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	// Suffix fields begin at stat field 3. Index 19 below is field 22/starttime.
	stat := "42 (worker with ) char) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 424242 20\n"
	if err := os.WriteFile(filepath.Join(pidRoot, "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, name := range append([]string{"mnt"}, restoreNamespaceNames...) {
		if err := os.WriteFile(filepath.Join(nsRoot, name), []byte(name), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mountFD, err := os.Open(filepath.Join(nsRoot, "mnt"))
	if err != nil {
		t.Fatal(err)
	}
	defer mountFD.Close()
	pinned, err := openPinnedRestoreNamespaces(procRoot, 42, mountFD)
	if err != nil {
		t.Fatalf("openPinnedRestoreNamespaces: %v", err)
	}
	defer pinned.close()
	if len(pinned.files) != 4 {
		t.Fatalf("got %d pinned namespaces, want 4", len(pinned.files))
	}
	want := []string{
		"--uts=/proc/self/fd/5",
		"--ipc=/proc/self/fd/6",
		"--net=/proc/self/fd/7",
		"--pid=/proc/self/fd/8",
	}
	if got := pinned.nsenterArgs(5); !reflect.DeepEqual(got, want) {
		t.Fatalf("nsenter args = %#v, want %#v", got, want)
	}
}

func TestReadProcessStartTimeHandlesClosingParenthesis(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "7"), 0o755); err != nil {
		t.Fatal(err)
	}
	stat := "7 (odd ) command) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 999 20\n"
	if err := os.WriteFile(filepath.Join(root, "7", "stat"), []byte(stat), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readProcessStartTime(root, 7)
	if err != nil {
		t.Fatal(err)
	}
	if got != "999" {
		t.Fatalf("starttime = %q, want 999", got)
	}
}
