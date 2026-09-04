package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/ai-dynamo/dynamo/deploy/snapshot/internal/nsmount"
	snapshotruntime "github.com/ai-dynamo/dynamo/deploy/snapshot/internal/runtime"
)

var requiredTargetEnvironment = map[string]string{
	"DYN_SNAPSHOT_RESTORE_STANDBY": "1",
	"DYN_SNAPSHOT_CONTROL_DIR":     "/snapshot-control",
}

type identityVerifier struct {
	client  kubernetes.Interface
	runtime snapshotruntime.Runtime
	opts    options
}

func (v *identityVerifier) verify(ctx context.Context, expectedPID int) (int, error) {
	pod, err := v.client.CoreV1().Pods(v.opts.TargetNamespace).Get(ctx, v.opts.TargetName, metav1.GetOptions{})
	if err != nil {
		return 0, fmt.Errorf("get the bound target Pod: %w", err)
	}
	if err := validateTargetPod(pod, v.opts); err != nil {
		return 0, err
	}

	containerID := snapshotruntime.StripCRIScheme(v.opts.TargetContainerID)
	pid, _, err := v.runtime.ResolveContainer(ctx, containerID)
	if err != nil {
		return 0, fmt.Errorf("resolve the bound container: %w", err)
	}
	if pid <= 0 || (expectedPID > 0 && pid != expectedPID) {
		return 0, fmt.Errorf("runtime PID changed across the restore boundary")
	}
	if err := snapshotruntime.ValidateProcessState(v.opts.HostProc, pid); err != nil {
		return 0, fmt.Errorf("bound container process is not live: %w", err)
	}
	cgroup, err := snapshotruntime.ResolveCgroupRootFromHostPID(pid)
	if err != nil {
		return 0, fmt.Errorf("resolve the bound container cgroup: %w", err)
	}
	if cgroup != v.opts.TargetCgroup {
		return 0, fmt.Errorf("runtime cgroup does not match the binding")
	}
	return pid, nil
}

func validateTargetPod(pod *corev1.Pod, o options) error {
	if pod == nil || pod.Namespace != o.TargetNamespace || pod.Name != o.TargetName || string(pod.UID) != o.TargetUID {
		return fmt.Errorf("Pod name, namespace, or UID does not match the binding")
	}
	if pod.DeletionTimestamp != nil || pod.Status.Phase != corev1.PodRunning {
		return fmt.Errorf("bound Pod is deleting or not Running")
	}
	if pod.Labels[runLabelKey] != o.RunID || pod.Labels["nvidia.com/snapshot-is-restore-target"] != "true" || pod.Labels["nvidia.com/snapshot-checkpoint-id"] != o.CheckpointID {
		return fmt.Errorf("Pod run, restore-target, or checkpoint label does not match the binding")
	}
	annotations := pod.Annotations
	if annotations["nvidia.com/snapshot-target-containers"] != o.TargetContainer ||
		annotations["nvidia.com/snapshot-artifact-version"] != o.ArtifactVersion ||
		annotations["nvidia.com/snapshot-storage-type"] != "pvc" ||
		annotations["nvidia.com/snapshot-storage-base-path"] != checkpointBasePath ||
		annotations[targetSpecSHAKey] != o.TargetPodSpecSHA ||
		annotations["archvteams.nebius.ai/artifact-manifest-sha256"] != o.ArtifactManifestSHA ||
		annotations["archvteams.nebius.ai/tool-bundle-sha256"] != o.ToolBundleManifestSHA {
		return fmt.Errorf("Pod restore annotations do not match the exact artifact binding")
	}
	actualSpecSHA, err := canonicalPodSpecSHA256(pod.Spec)
	if err != nil {
		return fmt.Errorf("canonicalize target Pod spec: %w", err)
	}
	if actualSpecSHA != o.TargetPodSpecSHA {
		return fmt.Errorf("target Pod spec digest does not match the binding")
	}
	if pod.Spec.NodeName != o.TargetNode || pod.Status.PodIP != o.TargetPodIP {
		return fmt.Errorf("Pod node or Pod IP does not match the binding")
	}
	if len(pod.Status.PodIPs) != 1 || pod.Status.PodIPs[0].IP != o.TargetPodIP {
		return fmt.Errorf("Pod IP set does not match the single-address binding")
	}
	if pod.Spec.SchedulerName != "" && pod.Spec.SchedulerName != corev1.DefaultSchedulerName {
		return fmt.Errorf("Pod uses a non-default scheduler")
	}
	if pod.Spec.HostPID || pod.Spec.HostIPC || pod.Spec.HostNetwork || pod.Spec.AutomountServiceAccountToken == nil || *pod.Spec.AutomountServiceAccountToken {
		return fmt.Errorf("Pod namespace or service-account isolation diverges from policy")
	}
	if pod.Spec.EnableServiceLinks == nil || *pod.Spec.EnableServiceLinks {
		return fmt.Errorf("Pod service links are not allowed")
	}
	if len(pod.Spec.InitContainers) != 0 || len(pod.Spec.EphemeralContainers) != 0 || len(pod.Spec.Containers) != 1 {
		return fmt.Errorf("Pod must have exactly one ordinary target container")
	}

	container := &pod.Spec.Containers[0]
	if container.Name != o.TargetContainer || container.Image != o.ExpectedImageID || !reflect.DeepEqual(container.Command, []string{"/bin/sleep"}) || !reflect.DeepEqual(container.Args, []string{"2147483647"}) {
		return fmt.Errorf("target container identity or inert command diverges from policy")
	}
	if err := validateTargetEnvironment(container); err != nil {
		return err
	}
	if err := validateTargetSecurity(container.SecurityContext); err != nil {
		return err
	}
	if err := validateTargetVolumes(&pod.Spec, container); err != nil {
		return err
	}

	if len(pod.Status.ContainerStatuses) != 1 {
		return fmt.Errorf("Pod status must contain exactly one target container")
	}
	status := pod.Status.ContainerStatuses[0]
	if status.Name != o.TargetContainer || status.ContainerID != o.TargetContainerID || status.ImageID != o.ExpectedImageID || status.State.Running == nil {
		return fmt.Errorf("live container ID, image ID, or state does not match the binding")
	}
	return nil
}

func validateTargetSecurity(sc *corev1.SecurityContext) error {
	if sc == nil || sc.Privileged == nil || *sc.Privileged || sc.AllowPrivilegeEscalation == nil || *sc.AllowPrivilegeEscalation {
		return fmt.Errorf("target container privilege policy diverges")
	}
	if sc.RunAsUser == nil || *sc.RunAsUser < 0 || sc.RunAsGroup == nil || *sc.RunAsGroup < 0 {
		return fmt.Errorf("target restore user/group must be explicit non-negative IDs")
	}
	if sc.Capabilities == nil || !reflect.DeepEqual(sc.Capabilities.Drop, []corev1.Capability{"ALL"}) || len(sc.Capabilities.Add) != 0 {
		return fmt.Errorf("target capability policy diverges")
	}
	return nil
}

// canonicalPodSpecSHA256 defines a language-independent binding for the exact
// server-defaulted target PodSpec. The typed object is converted to generic
// JSON, whose object keys encoding/json orders lexicographically, and encoded
// without insignificant whitespace or HTML-only escaping. A collector can
// reproduce these bytes with Python json.dumps(spec, sort_keys=True,
// separators=(",", ":"), ensure_ascii=False).
func canonicalPodSpecSHA256(spec corev1.PodSpec) (string, error) {
	typedJSON, err := json.Marshal(spec)
	if err != nil {
		return "", err
	}
	decoder := json.NewDecoder(bytes.NewReader(typedJSON))
	decoder.UseNumber()
	var generic any
	if err := decoder.Decode(&generic); err != nil {
		return "", err
	}
	var canonical bytes.Buffer
	encoder := json.NewEncoder(&canonical)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(generic); err != nil {
		return "", err
	}
	data := bytes.TrimSuffix(canonical.Bytes(), []byte{'\n'})
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:]), nil
}

func validateTargetEnvironment(container *corev1.Container) error {
	if len(container.EnvFrom) != 0 || len(container.Env) > 128 {
		return fmt.Errorf("target environment imports a source or exceeds the bound")
	}
	values := make(map[string]string, len(container.Env))
	total := 0
	for _, variable := range container.Env {
		total += len(variable.Name) + len(variable.Value)
		if total > 32768 || variable.Name == "" || variable.ValueFrom != nil {
			return fmt.Errorf("target environment is dynamic or exceeds the byte bound")
		}
		if _, duplicate := values[variable.Name]; duplicate {
			return fmt.Errorf("target environment repeats a name")
		}
		if secretLikeEnvironmentName(variable.Name) {
			return fmt.Errorf("target environment contains a secret-like name")
		}
		lowerValue := strings.ToLower(variable.Value)
		for _, marker := range secretValueMarkers {
			if strings.Contains(lowerValue, marker) {
				return fmt.Errorf("target environment contains a credential marker")
			}
		}
		values[variable.Name] = variable.Value
	}
	for name, expected := range requiredTargetEnvironment {
		if values[name] != expected {
			return fmt.Errorf("target environment omits or changes required restore variable %s", name)
		}
	}
	return nil
}

func secretLikeEnvironmentName(name string) bool {
	parts := strings.FieldsFunc(strings.ToUpper(name), func(r rune) bool {
		return (r < 'A' || r > 'Z') && (r < '0' || r > '9')
	})
	for index, part := range parts {
		switch part {
		case "SECRET", "PASSWORD", "CREDENTIAL", "TOKEN":
			return true
		case "API":
			if index+1 < len(parts) && parts[index+1] == "KEY" {
				return true
			}
		}
	}
	return false
}

func validateTargetVolumes(spec *corev1.PodSpec, container *corev1.Container) error {
	if len(spec.Volumes) < 2 || len(spec.Volumes) > 32 || len(container.VolumeMounts) < 2 || len(container.VolumeMounts) > 64 || len(container.VolumeDevices) != 0 {
		return fmt.Errorf("target volume or mount count is outside the bound")
	}
	volumes := make(map[string]corev1.Volume, len(spec.Volumes))
	for _, volume := range spec.Volumes {
		if _, duplicate := volumes[volume.Name]; duplicate {
			return fmt.Errorf("target volume set contains a duplicate")
		}
		switch {
		case volume.EmptyDir != nil && reflect.DeepEqual(volume.VolumeSource, corev1.VolumeSource{EmptyDir: volume.EmptyDir}):
			if volume.EmptyDir.SizeLimit == nil || volume.EmptyDir.SizeLimit.Sign() <= 0 {
				return fmt.Errorf("target emptyDir volume %s has no positive size bound", volume.Name)
			}
		case volume.PersistentVolumeClaim != nil && reflect.DeepEqual(volume.VolumeSource, corev1.VolumeSource{PersistentVolumeClaim: volume.PersistentVolumeClaim}):
			if volume.PersistentVolumeClaim.ClaimName == "" {
				return fmt.Errorf("target PVC volume %s has no claim name", volume.Name)
			}
		default:
			return fmt.Errorf("target volume %s is not a bounded emptyDir or PVC", volume.Name)
		}
		volumes[volume.Name] = volume
	}

	mounts := make(map[string]corev1.VolumeMount, len(container.VolumeMounts))
	for _, mount := range container.VolumeMounts {
		volume, ok := volumes[mount.Name]
		if !ok {
			return fmt.Errorf("target mount refers to an unknown volume")
		}
		if mount.SubPathExpr != "" || mount.MountPropagation != nil || !cleanAbsolutePath(mount.MountPath) || !cleanRelativeSubPath(mount.SubPath) {
			return fmt.Errorf("target mount uses an unapproved dynamic field")
		}
		if volume.PersistentVolumeClaim != nil && volume.PersistentVolumeClaim.ReadOnly && !mount.ReadOnly {
			return fmt.Errorf("target mount weakens a read-only PVC declaration")
		}
		if _, duplicate := mounts[mount.MountPath]; duplicate {
			return fmt.Errorf("target volume-mount set repeats a path")
		}
		mounts[mount.MountPath] = mount
	}

	control, ok := mounts["/snapshot-control"]
	if !ok || control.ReadOnly || volumes[control.Name].EmptyDir == nil {
		return fmt.Errorf("target has no writable bounded snapshot-control emptyDir")
	}
	checkpoints, ok := mounts[checkpointBasePath]
	if !ok || !checkpoints.ReadOnly || checkpoints.SubPath != "" || volumes[checkpoints.Name].PersistentVolumeClaim == nil || !volumes[checkpoints.Name].PersistentVolumeClaim.ReadOnly {
		return fmt.Errorf("target has no exact read-only checkpoint PVC mount")
	}
	return nil
}

func cleanAbsolutePath(path string) bool {
	return path != "" && path != string(os.PathSeparator) && strings.IndexFunc(path, func(r rune) bool { return r < 0x20 || r == 0x7f }) < 0 && filepath.IsAbs(path) && filepath.Clean(path) == path
}

func cleanRelativeSubPath(path string) bool {
	if path == "" {
		return true
	}
	return strings.IndexFunc(path, func(r rune) bool { return r < 0x20 || r == 0x7f }) < 0 && !filepath.IsAbs(path) && filepath.Clean(path) == path && path != "." && path != ".." && !strings.HasPrefix(path, ".."+string(os.PathSeparator))
}

type boundMounter struct {
	delegate interface {
		Mount(context.Context, int) (nsmount.MountPoint, error)
	}
	verify *identityVerifier
}

func (m *boundMounter) Mount(ctx context.Context, pid int) (nsmount.MountPoint, error) {
	if _, err := m.verify.verify(ctx, pid); err != nil {
		return nil, fmt.Errorf("pre-mount identity revalidation failed: %w", err)
	}
	mp, err := m.delegate.Mount(ctx, pid)
	if err != nil {
		return nil, err
	}
	fail := func(cause error) (nsmount.MountPoint, error) {
		if cleanupErr := mp.Unmount(context.Background()); cleanupErr != nil {
			return nil, fmt.Errorf("%v; injected bundle cleanup also failed: %w", cause, cleanupErr)
		}
		return nil, cause
	}
	if _, err := m.verify.verify(ctx, pid); err != nil {
		return fail(fmt.Errorf("post-mount identity revalidation failed: %w", err))
	}
	if err := verifyPinnedMountNamespace(mp.NsFd(), m.verify.opts.HostProc, pid); err != nil {
		return fail(err)
	}
	return mp, nil
}

func verifyPinnedMountNamespace(pinned *os.File, procRoot string, pid int) error {
	if pinned == nil {
		return fmt.Errorf("mount namespace was not pinned")
	}
	live, err := os.Open(filepathJoin(procRoot, strconv.Itoa(pid), "ns", "mnt"))
	if err != nil {
		return fmt.Errorf("open current mount namespace: %w", err)
	}
	defer live.Close()
	pinnedInfo, err := pinned.Stat()
	if err != nil {
		return fmt.Errorf("stat pinned mount namespace: %w", err)
	}
	liveInfo, err := live.Stat()
	if err != nil {
		return fmt.Errorf("stat current mount namespace: %w", err)
	}
	if !os.SameFile(pinnedInfo, liveInfo) {
		return fmt.Errorf("pinned mount namespace does not belong to the bound runtime PID")
	}
	return nil
}

// filepathJoin is a test seam for the otherwise fixed host-proc namespace path.
var filepathJoin = func(parts ...string) string { return strings.Join(parts, string(os.PathSeparator)) }
