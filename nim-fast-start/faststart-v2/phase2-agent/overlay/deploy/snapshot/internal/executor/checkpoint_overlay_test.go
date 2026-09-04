package executor

import (
	"errors"
	"strings"
	"testing"

	"github.com/ai-dynamo/dynamo/deploy/snapshot/internal/types"
)

func TestCaptureOverlayStateFailsClosedOnRootfsDiffError(t *testing.T) {
	deletedCalls := 0
	err := captureOverlayState(
		"/upper", "/checkpoint", types.OverlaySettings{}, nil,
		func(string, string, types.OverlaySettings, []string) (string, error) {
			return "", errors.New("tar failed")
		},
		func(string, string) (bool, error) {
			deletedCalls++
			return false, nil
		},
	)
	if err == nil || !strings.Contains(err.Error(), "rootfs diff capture failed") {
		t.Fatalf("expected terminal rootfs diff error, got %v", err)
	}
	if deletedCalls != 0 {
		t.Fatal("deleted-file capture must not run after rootfs diff failure")
	}
}

func TestCaptureOverlayStateFailsClosedOnDeletedFileError(t *testing.T) {
	err := captureOverlayState(
		"/upper", "/checkpoint", types.OverlaySettings{}, []string{"/models"},
		func(upperDir, checkpointDir string, _ types.OverlaySettings, bindMounts []string) (string, error) {
			if upperDir != "/upper" || checkpointDir != "/checkpoint" || len(bindMounts) != 1 || bindMounts[0] != "/models" {
				t.Fatal("overlay capture arguments changed")
			}
			return "/checkpoint/rootfs-diff.tar", nil
		},
		func(string, string) (bool, error) {
			return false, errors.New("whiteout walk failed")
		},
	)
	if err == nil || !strings.Contains(err.Error(), "deleted-file capture failed") {
		t.Fatalf("expected terminal deleted-file error, got %v", err)
	}
}

func TestCaptureOverlayStateSucceedsOnlyAfterBothArtifacts(t *testing.T) {
	calls := []string{}
	err := captureOverlayState(
		"/upper", "/checkpoint", types.OverlaySettings{}, nil,
		func(string, string, types.OverlaySettings, []string) (string, error) {
			calls = append(calls, "rootfs")
			return "/checkpoint/rootfs-diff.tar", nil
		},
		func(string, string) (bool, error) {
			calls = append(calls, "deleted")
			return true, nil
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(calls, ",") != "rootfs,deleted" {
		t.Fatalf("unexpected capture order: %v", calls)
	}
}
