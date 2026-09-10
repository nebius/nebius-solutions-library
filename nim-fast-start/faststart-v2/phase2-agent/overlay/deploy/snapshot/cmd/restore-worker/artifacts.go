package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/ai-dynamo/dynamo/deploy/snapshot/internal/types"
)

var requiredToolFiles = map[string]struct{}{
	"criu":                   {},
	"nsrestore":              {},
	"cuda-checkpoint":        {},
	"cuda-checkpoint-helper": {},
	"ip":                     {},
	"tar":                    {},
}

func verifyCheckpointManifest(o options) (string, error) {
	checkpointPath := o.checkpointPath()
	manifestPath := filepath.Join(checkpointPath, "manifest.yaml")
	if err := rejectSymlinkComponents(checkpointBasePath, manifestPath); err != nil {
		return "", fmt.Errorf("checkpoint path failed confinement validation: %w", err)
	}
	actual, err := hashBoundedRegularFile(manifestPath, maxManifestBytes)
	if err != nil {
		return "", fmt.Errorf("checkpoint manifest is not a bounded regular file: %w", err)
	}
	if actual != o.ArtifactManifestSHA {
		return "", fmt.Errorf("checkpoint manifest digest does not match the binding")
	}
	manifest, err := types.ReadManifest(checkpointPath)
	if err != nil {
		return "", fmt.Errorf("checkpoint manifest cannot be parsed: %w", err)
	}
	if manifest.CheckpointID != o.CheckpointID {
		return "", fmt.Errorf("checkpoint manifest ID does not match the binding")
	}
	if manifest.CreatedAt.IsZero() {
		return "", fmt.Errorf("checkpoint manifest has no creation time")
	}
	return checkpointPath, nil
}

// verifyToolManifest treats --tool-bundle-sha256 as the SHA-256 of the exact
// sha256sum-format inventory at toolManifestPath. It verifies every listed
// file, rejects symlinks/special files, and rejects unlisted bundle files.
func verifyToolManifest(expectedManifestSHA string) error {
	return verifyToolManifestAt(toolManifestPath, toolBundlePath, expectedManifestSHA)
}

func verifyToolManifestAt(manifestPath, bundlePath, expectedManifestSHA string) error {
	actualManifestSHA, err := hashBoundedRegularFile(manifestPath, maxManifestBytes)
	if err != nil {
		return fmt.Errorf("tool manifest is not a bounded regular file: %w", err)
	}
	if actualManifestSHA != expectedManifestSHA {
		return fmt.Errorf("tool manifest digest does not match the binding")
	}

	file, err := os.Open(manifestPath)
	if err != nil {
		return fmt.Errorf("open tool manifest: %w", err)
	}
	defer file.Close()

	want := map[string]string{}
	previous := ""
	scanner := bufio.NewScanner(io.LimitReader(file, maxManifestBytes+1))
	scanner.Buffer(make([]byte, 4096), maxManifestBytes)
	for scanner.Scan() {
		line := scanner.Text()
		if len(line) < 68 || line[64:68] != "  ./" {
			return fmt.Errorf("tool manifest has a malformed record")
		}
		digest := line[:64]
		rel := strings.TrimPrefix(line[66:], "./")
		if !digestPattern.MatchString(digest) || !validToolRelativePath(rel) {
			return fmt.Errorf("tool manifest has an invalid digest or path")
		}
		if previous != "" && rel <= previous {
			return fmt.Errorf("tool manifest paths are not unique and sorted")
		}
		previous = rel
		want[rel] = digest
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("read tool manifest: %w", err)
	}
	if len(want) == 0 {
		return fmt.Errorf("tool manifest is empty")
	}
	for required := range requiredToolFiles {
		if _, ok := want[required]; !ok {
			return fmt.Errorf("tool manifest omits a required executable")
		}
	}
	pluginFound := false
	for rel := range want {
		if strings.HasPrefix(rel, "criu-plugins/") && strings.HasSuffix(rel, ".so") {
			pluginFound = true
		}
	}
	if !pluginFound {
		return fmt.Errorf("tool manifest omits the CRIU plugin")
	}

	seen := map[string]struct{}{}
	err = filepath.WalkDir(bundlePath, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == bundlePath {
			return nil
		}
		rel, err := filepath.Rel(bundlePath, path)
		if err != nil || !validToolRelativePath(rel) {
			return fmt.Errorf("tool bundle has an invalid path")
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || (!info.Mode().IsRegular() && !info.IsDir()) {
			return fmt.Errorf("tool bundle contains a symlink or special file")
		}
		if info.IsDir() {
			return nil
		}
		expected, ok := want[rel]
		if !ok {
			return fmt.Errorf("tool bundle contains an unlisted file")
		}
		actual, err := hashRegularFile(path)
		if err != nil {
			return err
		}
		if actual != expected {
			return fmt.Errorf("tool bundle file digest mismatch")
		}
		seen[rel] = struct{}{}
		return nil
	})
	if err != nil {
		return fmt.Errorf("verify tool bundle: %w", err)
	}
	if len(seen) != len(want) {
		missing := make([]string, 0, len(want)-len(seen))
		for rel := range want {
			if _, ok := seen[rel]; !ok {
				missing = append(missing, rel)
			}
		}
		sort.Strings(missing)
		return fmt.Errorf("tool bundle is missing %d manifest file(s)", len(missing))
	}
	return nil
}

func validToolRelativePath(path string) bool {
	if path == "" || filepath.IsAbs(path) || filepath.Clean(path) != path || strings.Contains(path, `\`) {
		return false
	}
	for _, part := range strings.Split(path, string(os.PathSeparator)) {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return strings.IndexFunc(path, func(r rune) bool { return r < 0x20 || r == 0x7f }) < 0
}

func rejectSymlinkComponents(root, target string) error {
	root = filepath.Clean(root)
	target = filepath.Clean(target)
	if target != root && !strings.HasPrefix(target, root+string(os.PathSeparator)) {
		return fmt.Errorf("target escapes the approved root")
	}
	rel, err := filepath.Rel(root, target)
	if err != nil {
		return err
	}
	current := root
	rootInfo, err := os.Lstat(root)
	if err != nil || rootInfo.Mode()&os.ModeSymlink != 0 || !rootInfo.IsDir() {
		return fmt.Errorf("approved root is absent, not a directory, or a symlink")
	}
	for _, component := range strings.Split(rel, string(os.PathSeparator)) {
		current = filepath.Join(current, component)
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("path contains a symlink")
		}
	}
	return nil
}

func hashBoundedRegularFile(path string, maximum int64) (string, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Size() > maximum {
		return "", fmt.Errorf("file type or size is not allowed")
	}
	return hashRegularFile(path)
}

func hashRegularFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
