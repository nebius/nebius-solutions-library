package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func makeToolFixture(t *testing.T) (string, string, string) {
	t.Helper()
	root := t.TempDir()
	bundle := filepath.Join(root, "bundle")
	if err := os.MkdirAll(filepath.Join(bundle, "criu-plugins"), 0o755); err != nil {
		t.Fatal(err)
	}
	paths := []string{"criu", "nsrestore", "cuda-checkpoint", "cuda-checkpoint-helper", "ip", "tar", "criu-plugins/snapshot_inet_remap.so"}
	sort.Strings(paths)
	var lines strings.Builder
	for _, rel := range paths {
		path := filepath.Join(bundle, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("fixture:"+rel), 0o755); err != nil {
			t.Fatal(err)
		}
		digest, err := hashRegularFile(path)
		if err != nil {
			t.Fatal(err)
		}
		fmt.Fprintf(&lines, "%s  ./%s\n", digest, rel)
	}
	manifest := filepath.Join(root, "bundle.manifest")
	if err := os.WriteFile(manifest, []byte(lines.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	manifestSHA, err := hashRegularFile(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return manifest, bundle, manifestSHA
}

func TestVerifyToolManifestAtAcceptsExactInventory(t *testing.T) {
	manifest, bundle, digest := makeToolFixture(t)
	if err := verifyToolManifestAt(manifest, bundle, digest); err != nil {
		t.Fatalf("verifyToolManifestAt: %v", err)
	}
}

func TestVerifyToolManifestAtRejectsUnlistedFile(t *testing.T) {
	manifest, bundle, digest := makeToolFixture(t)
	if err := os.WriteFile(filepath.Join(bundle, "surprise"), []byte("unexpected"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := verifyToolManifestAt(manifest, bundle, digest); err == nil {
		t.Fatal("expected unlisted bundle file rejection")
	}
}

func TestVerifyToolManifestAtRejectsSymlink(t *testing.T) {
	manifest, bundle, digest := makeToolFixture(t)
	if err := os.Symlink("criu", filepath.Join(bundle, "alias")); err != nil {
		t.Fatal(err)
	}
	if err := verifyToolManifestAt(manifest, bundle, digest); err == nil {
		t.Fatal("expected bundle symlink rejection")
	}
}
