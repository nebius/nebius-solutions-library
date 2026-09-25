package main

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/go-logr/logr"

	"github.com/ai-dynamo/dynamo/deploy/snapshot/internal/executor"
	snapshotruntime "github.com/ai-dynamo/dynamo/deploy/snapshot/internal/runtime"
)

func TestPerformOneRestoreCallsRestoreOnceThenSentinel(t *testing.T) {
	restoreCalls := 0
	sentinelCalls := 0
	deps := workerDependencies{
		restore: func(context.Context, snapshotruntime.Runtime, logr.Logger, executor.RestoreRequest, executor.Mounter) (int, error) {
			restoreCalls++
			return 321, nil
		},
		writeSentinel: func(pid int, name string) error {
			sentinelCalls++
			if pid != 321 || name != "restore-complete" {
				t.Fatalf("unexpected sentinel %d/%s", pid, name)
			}
			return nil
		},
		now: time.Now,
	}
	pid, err := performOneRestore(context.Background(), nil, executor.RestoreRequest{}, nil, deps)
	if err != nil {
		t.Fatal(err)
	}
	if pid != 321 || restoreCalls != 1 || sentinelCalls != 1 {
		t.Fatalf("pid=%d restoreCalls=%d sentinelCalls=%d", pid, restoreCalls, sentinelCalls)
	}
}

func TestPerformOneRestoreDoesNotReleaseAfterRestoreFailure(t *testing.T) {
	sentinelCalls := 0
	deps := workerDependencies{
		restore: func(context.Context, snapshotruntime.Runtime, logr.Logger, executor.RestoreRequest, executor.Mounter) (int, error) {
			return 0, errors.New("restore failed")
		},
		writeSentinel: func(int, string) error { sentinelCalls++; return nil },
	}
	if _, err := performOneRestore(context.Background(), nil, executor.RestoreRequest{}, nil, deps); err == nil {
		t.Fatal("expected restore failure")
	}
	if sentinelCalls != 0 {
		t.Fatal("release sentinel must not be written after restore failure")
	}
}
