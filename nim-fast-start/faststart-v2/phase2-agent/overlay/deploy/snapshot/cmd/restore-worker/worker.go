package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-logr/logr"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/ai-dynamo/dynamo/deploy/snapshot/internal/executor"
	"github.com/ai-dynamo/dynamo/deploy/snapshot/internal/nsmount"
	snapshotruntime "github.com/ai-dynamo/dynamo/deploy/snapshot/internal/runtime"
	snapshotprotocol "github.com/ai-dynamo/dynamo/deploy/snapshot/protocol"
)

const receiptSchema = "archvteams.nebius.ai/dynamo-one-shot-restore-receipt/v1"

type restoreReceipt struct {
	Schema                   string `json:"schema"`
	Status                   string `json:"status"`
	Code                     string `json:"code,omitempty"`
	CompletedAt              string `json:"completed_at,omitempty"`
	DurationMilliseconds     int64  `json:"duration_ms,omitempty"`
	RunID                    string `json:"run_id,omitempty"`
	TargetNamespace          string `json:"target_namespace,omitempty"`
	TargetName               string `json:"target_name,omitempty"`
	TargetUID                string `json:"target_uid,omitempty"`
	TargetContainerID        string `json:"target_container_id,omitempty"`
	TargetImageID            string `json:"target_image_id,omitempty"`
	TargetNode               string `json:"target_node,omitempty"`
	TargetPodIP              string `json:"target_pod_ip,omitempty"`
	TargetPodSpecSHA256      string `json:"target_pod_spec_sha256,omitempty"`
	CheckpointID             string `json:"checkpoint_id,omitempty"`
	ArtifactVersion          string `json:"artifact_version,omitempty"`
	CheckpointManifestSHA256 string `json:"checkpoint_manifest_sha256,omitempty"`
	ToolBundleManifestSHA256 string `json:"tool_bundle_manifest_sha256,omitempty"`
}

type restoreCall func(context.Context, snapshotruntime.Runtime, logr.Logger, executor.RestoreRequest, executor.Mounter) (int, error)

type workerDependencies struct {
	verifyCheckpoint func(options) (string, error)
	verifyTools      func(string) error
	restore          restoreCall
	writeSentinel    func(int, string) error
	now              func() time.Time
}

func productionDependencies() workerDependencies {
	return workerDependencies{
		verifyCheckpoint: verifyCheckpointManifest,
		verifyTools:      verifyToolManifest,
		restore:          executor.Restore,
		writeSentinel:    snapshotruntime.WriteControlSentinel,
		now:              time.Now,
	}
}

func main() {
	os.Exit(realMain(os.Args[1:], os.Environ(), os.Stdout))
}

func realMain(args, environ []string, stdout io.Writer) int {
	opts, err := parseOptions(args)
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "invalid_binding"})
		return 2
	}
	cleanEnvironment, err := sanitizeEnvironment(environ)
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "environment_rejected"})
		return 1
	}
	os.Clearenv()
	for _, entry := range cleanEnvironment {
		name, value, _ := stringsCut(entry, '=')
		if err := os.Setenv(name, value); err != nil {
			_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "environment_isolation_failed"})
			return 1
		}
	}

	rootCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(rootCtx, workerTimeoutSecond*time.Second)
	defer cancel()

	config, err := rest.InClusterConfig()
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "kubernetes_config_failed"})
		return 1
	}
	client, err := kubernetes.NewForConfig(config)
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "kubernetes_client_failed"})
		return 1
	}
	runtimeClient, err := snapshotruntime.New(snapshotruntime.RuntimeContainerd, opts.RuntimeSocket)
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "runtime_client_failed"})
		return 1
	}
	defer runtimeClient.Close()
	injector, err := nsmount.New(nsmount.SnapshotBinSrc, nsmount.SnapshotBinDst, logr.Discard())
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "bundle_injector_failed"})
		return 1
	}

	receipt, err := runWorker(ctx, opts, client, runtimeClient, injector, productionDependencies())
	if err != nil {
		_ = writeReceipt(stdout, restoreReceipt{Schema: receiptSchema, Status: "failed", Code: "restore_failed", RunID: opts.RunID})
		return 1
	}
	if err := writeReceipt(stdout, receipt); err != nil {
		return 1
	}
	return 0
}

func runWorker(
	ctx context.Context,
	opts options,
	client kubernetes.Interface,
	runtimeClient snapshotruntime.Runtime,
	injector executor.Mounter,
	deps workerDependencies,
) (restoreReceipt, error) {
	if deps.verifyCheckpoint == nil || deps.verifyTools == nil || deps.restore == nil || deps.writeSentinel == nil || deps.now == nil {
		return restoreReceipt{}, fmt.Errorf("worker dependencies are incomplete")
	}
	checkpointPath, err := deps.verifyCheckpoint(opts)
	if err != nil {
		return restoreReceipt{}, err
	}
	if err := deps.verifyTools(opts.ToolBundleManifestSHA); err != nil {
		return restoreReceipt{}, err
	}
	verifier := &identityVerifier{client: client, runtime: runtimeClient, opts: opts}
	if _, err := verifier.verify(ctx, 0); err != nil {
		return restoreReceipt{}, fmt.Errorf("initial identity verification failed: %w", err)
	}
	boundInjector := &boundMounter{delegate: injector, verify: verifier}
	started := deps.now()
	restoreRequest := executor.RestoreRequest{
		CheckpointID:                opts.CheckpointID,
		CheckpointLocation:          checkpointPath,
		ContainerCheckpointLocation: checkpointPath,
		ContainerID:                 snapshotruntime.StripCRIScheme(opts.TargetContainerID),
		StartedAt:                   started,
		PodName:                     opts.TargetName,
		PodNamespace:                opts.TargetNamespace,
		TargetPodIP:                 opts.TargetPodIP,
		ContainerName:               opts.TargetContainer,
		Clientset:                   client,
		RequireCleanUnmount:         true,
	}
	_, err = performOneRestore(ctx, runtimeClient, restoreRequest, boundInjector, deps)
	if err != nil {
		return restoreReceipt{}, err
	}
	completed := deps.now()
	return restoreReceipt{
		Schema:                   receiptSchema,
		Status:                   "succeeded",
		CompletedAt:              completed.UTC().Format(time.RFC3339Nano),
		DurationMilliseconds:     completed.Sub(started).Milliseconds(),
		RunID:                    opts.RunID,
		TargetNamespace:          opts.TargetNamespace,
		TargetName:               opts.TargetName,
		TargetUID:                opts.TargetUID,
		TargetContainerID:        opts.TargetContainerID,
		TargetImageID:            opts.ExpectedImageID,
		TargetNode:               opts.TargetNode,
		TargetPodIP:              opts.TargetPodIP,
		TargetPodSpecSHA256:      opts.TargetPodSpecSHA,
		CheckpointID:             opts.CheckpointID,
		ArtifactVersion:          opts.ArtifactVersion,
		CheckpointManifestSHA256: opts.ArtifactManifestSHA,
		ToolBundleManifestSHA256: opts.ToolBundleManifestSHA,
	}, nil
}

func performOneRestore(ctx context.Context, runtimeClient snapshotruntime.Runtime, request executor.RestoreRequest, injector executor.Mounter, deps workerDependencies) (int, error) {
	placeholderPID, err := deps.restore(ctx, runtimeClient, logr.Discard(), request, injector)
	if err != nil {
		return 0, err
	}
	if placeholderPID <= 0 {
		return 0, fmt.Errorf("restore returned an invalid placeholder PID")
	}
	if err := deps.writeSentinel(placeholderPID, snapshotprotocol.RestoreCompleteFile); err != nil {
		return 0, fmt.Errorf("restore completed but the release sentinel failed: %w", err)
	}
	return placeholderPID, nil
}

func writeReceipt(output io.Writer, receipt restoreReceipt) error {
	data, err := json.Marshal(receipt)
	if err != nil {
		return err
	}
	if len(data)+1 > maxReceiptBytes {
		return fmt.Errorf("receipt exceeds the byte bound")
	}
	data = append(data, '\n')
	_, err = output.Write(data)
	return err
}

var stringsCut = func(value string, separator byte) (string, string, bool) {
	for index := range value {
		if value[index] == separator {
			return value[:index], value[index+1:], true
		}
	}
	return value, "", false
}
