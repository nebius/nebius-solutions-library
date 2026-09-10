package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func boolPointer(value bool) *bool    { return &value }
func int64Pointer(value int64) *int64 { return &value }

func validOptionsForPod() options {
	containerID := strings.Repeat("a", 64)
	return options{
		TargetNamespace:       testNamespace,
		TargetName:            "boltz-target-ut-a1b2c3",
		TargetUID:             "11111111-1111-4111-8111-111111111111",
		TargetContainer:       testContainer,
		TargetContainerID:     "containerd://" + containerID,
		TargetCgroup:          "/kubepods.slice/cri-containerd-" + containerID + ".scope",
		TargetPodIP:           "10.50.42.7",
		TargetNode:            "gpu-node-a.example.invalid",
		ExpectedImageID:       testImageID,
		TargetPodSpecSHA:      strings.Repeat("d", 64),
		RunID:                 "ut-a1b2c3",
		CheckpointID:          "boltz2-native-v1",
		ArtifactVersion:       "1",
		ArtifactManifestSHA:   strings.Repeat("b", 64),
		ToolBundleManifestSHA: strings.Repeat("c", 64),
		RuntimeSocket:         runtimeSocketPath,
		HostProc:              hostProcPath,
		HostCgroup:            hostCgroupPath,
		PodResourcesSocket:    podResourcesPath,
	}
}

func emptyDirVolume(name, size string, medium corev1.StorageMedium) corev1.Volume {
	quantity := resource.MustParse(size)
	return corev1.Volume{
		Name:         name,
		VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: medium, SizeLimit: &quantity}},
	}
}

func validTargetPod() (*corev1.Pod, options) {
	o := validOptionsForPod()
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: o.TargetName, Namespace: o.TargetNamespace, UID: types.UID(o.TargetUID),
			Labels: map[string]string{
				runLabelKey:                             o.RunID,
				"nvidia.com/snapshot-is-restore-target": "true",
				"nvidia.com/snapshot-checkpoint-id":     o.CheckpointID,
			},
			Annotations: map[string]string{
				"nvidia.com/snapshot-target-containers":         o.TargetContainer,
				"nvidia.com/snapshot-artifact-version":          o.ArtifactVersion,
				"nvidia.com/snapshot-storage-type":              "pvc",
				"nvidia.com/snapshot-storage-base-path":         checkpointBasePath,
				targetSpecSHAKey:                                "pending",
				"archvteams.nebius.ai/artifact-manifest-sha256": o.ArtifactManifestSHA,
				"archvteams.nebius.ai/tool-bundle-sha256":       o.ToolBundleManifestSHA,
			},
		},
		Spec: corev1.PodSpec{
			NodeName:                     o.TargetNode,
			SchedulerName:                corev1.DefaultSchedulerName,
			AutomountServiceAccountToken: boolPointer(false),
			EnableServiceLinks:           boolPointer(false),
			NodeSelector:                 map[string]string{"nvidia.com/gpu.product": "h100"},
			Containers: []corev1.Container{{
				Name: o.TargetContainer, Image: o.ExpectedImageID,
				Command: []string{"/bin/sleep"}, Args: []string{"2147483647"},
				Env: []corev1.EnvVar{
					{Name: "DYN_SNAPSHOT_RESTORE_STANDBY", Value: "1"},
					{Name: "DYN_SNAPSHOT_CONTROL_DIR", Value: "/snapshot-control"},
					{Name: "NIM_CACHE_PATH", Value: "/models/boltz-cache"},
					{Name: "MODEL_VARIANT", Value: "boltz2"},
				},
				SecurityContext: &corev1.SecurityContext{
					Privileged: boolPointer(false), AllowPrivilegeEscalation: boolPointer(false),
					RunAsUser: int64Pointer(0), RunAsGroup: int64Pointer(0),
					Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
				},
				VolumeMounts: []corev1.VolumeMount{
					{Name: "shared-memory", MountPath: "/dev/shm"},
					{Name: "model-cache", MountPath: "/models/boltz-cache"},
					{Name: "scratch-space", MountPath: "/work"},
					{Name: "restore-control", MountPath: "/snapshot-control", SubPath: "boltz2"},
					{Name: "artifact-store", MountPath: "/checkpoints", ReadOnly: true},
					{Name: "tool-store", MountPath: "/approved-dynamo-tools", ReadOnly: true},
					{Name: "tool-store", MountPath: "/usr/local/bin/cuda-checkpoint-helper", SubPath: "bin/cuda-checkpoint-helper", ReadOnly: true},
				},
			}},
			Volumes: []corev1.Volume{
				emptyDirVolume("shared-memory", "96Gi", corev1.StorageMediumMemory),
				emptyDirVolume("model-cache", "40Gi", ""),
				emptyDirVolume("scratch-space", "12Gi", ""),
				emptyDirVolume("restore-control", "32Mi", ""),
				{Name: "artifact-store", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "boltz-artifact", ReadOnly: true}}},
				{Name: "tool-store", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "dynamo-tools", ReadOnly: true}}},
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning, PodIP: o.TargetPodIP, PodIPs: []corev1.PodIP{{IP: o.TargetPodIP}},
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: o.TargetContainer, ContainerID: o.TargetContainerID, ImageID: o.ExpectedImageID,
				State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: metav1.NewTime(time.Now())}},
			}},
		},
	}
	digest, err := canonicalPodSpecSHA256(pod.Spec)
	if err != nil {
		panic(err)
	}
	o.TargetPodSpecSHA = digest
	pod.Annotations[targetSpecSHAKey] = digest
	return pod, o
}

func TestValidateTargetPodAcceptsExactBinding(t *testing.T) {
	pod, opts := validTargetPod()
	if err := validateTargetPod(pod, opts); err != nil {
		t.Fatalf("validateTargetPod: %v", err)
	}
}

func TestValidateTargetPodRejectsIdentityAndSecretDrift(t *testing.T) {
	type mutation struct {
		change     func(*corev1.Pod)
		rebindSpec bool
	}
	mutations := map[string]mutation{
		"uid":      {change: func(p *corev1.Pod) { p.UID = types.UID("22222222-2222-4222-8222-222222222222") }},
		"image-id": {change: func(p *corev1.Pod) { p.Status.ContainerStatuses[0].ImageID = "different" }},
		"generic-secret-env": {rebindSpec: true, change: func(p *corev1.Pod) {
			p.Spec.Containers[0].Env = append(p.Spec.Containers[0].Env, corev1.EnvVar{Name: "CONFIG", Value: "nvapi-not-real"})
		}},
		"scheduler": {rebindSpec: true, change: func(p *corev1.Pod) { p.Spec.SchedulerName = "other-scheduler" }},
		"host-path": {rebindSpec: true, change: func(p *corev1.Pod) {
			p.Spec.Volumes = append(p.Spec.Volumes, corev1.Volume{
				Name: "host", VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: "/etc"}},
			})
		}},
		"writable-pvc-mount": {rebindSpec: true, change: func(p *corev1.Pod) {
			for index := range p.Spec.Containers[0].VolumeMounts {
				if p.Spec.Containers[0].VolumeMounts[index].MountPath == checkpointBasePath {
					p.Spec.Containers[0].VolumeMounts[index].ReadOnly = false
				}
			}
		}},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			pod, opts := validTargetPod()
			mutate.change(pod)
			if mutate.rebindSpec {
				rebindTargetPodSpec(t, pod, &opts)
			}
			if err := validateTargetPod(pod, opts); err == nil {
				t.Fatal("expected fail-closed target validation error")
			}
		})
	}
}

func TestValidateTargetPodAcceptsAdditionalModelVolumesAndEnvironment(t *testing.T) {
	pod, opts := validTargetPod()
	pod.Spec.Containers[0].SecurityContext.RunAsUser = int64Pointer(1000)
	pod.Spec.Containers[0].SecurityContext.RunAsGroup = int64Pointer(1000)
	filteredVolumes := pod.Spec.Volumes[:0]
	for _, volume := range pod.Spec.Volumes {
		if volume.Name != "tool-store" {
			filteredVolumes = append(filteredVolumes, volume)
		}
	}
	pod.Spec.Volumes = filteredVolumes
	filteredMounts := pod.Spec.Containers[0].VolumeMounts[:0]
	for _, mount := range pod.Spec.Containers[0].VolumeMounts {
		if mount.Name != "tool-store" {
			filteredMounts = append(filteredMounts, mount)
		}
	}
	pod.Spec.Containers[0].VolumeMounts = filteredMounts
	pod.Spec.Volumes = append(pod.Spec.Volumes, emptyDirVolume("model-output", "2Gi", ""))
	pod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{
		Name: "model-state", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "boltz-state"}},
	})
	pod.Spec.Containers[0].VolumeMounts = append(pod.Spec.Containers[0].VolumeMounts, corev1.VolumeMount{
		Name: "model-output", MountPath: "/models/output",
	})
	pod.Spec.Containers[0].VolumeMounts = append(pod.Spec.Containers[0].VolumeMounts, corev1.VolumeMount{
		Name: "model-state", MountPath: "/models/state",
	})
	pod.Spec.Containers[0].Env = append(pod.Spec.Containers[0].Env,
		corev1.EnvVar{Name: "BATCH_SIZE", Value: "4"},
		corev1.EnvVar{Name: "TOKENIZERS_PARALLELISM", Value: "false"},
	)
	rebindTargetPodSpec(t, pod, &opts)
	if err := validateTargetPod(pod, opts); err != nil {
		t.Fatalf("generic model-specific settings rejected: %v", err)
	}
}

func TestValidateTargetPodRejectsSpecDigestDrift(t *testing.T) {
	pod, opts := validTargetPod()
	pod.Spec.Containers[0].Env = append(pod.Spec.Containers[0].Env, corev1.EnvVar{Name: "BATCH_SIZE", Value: "8"})
	if err := validateTargetPod(pod, opts); err == nil {
		t.Fatal("expected target Pod spec digest mismatch")
	}
}

func TestCanonicalPodSpecSHA256IgnoresMapInsertionOrder(t *testing.T) {
	first := corev1.PodSpec{NodeSelector: map[string]string{"z": "last", "a": "first"}}
	second := corev1.PodSpec{NodeSelector: map[string]string{"a": "first", "z": "last"}}
	firstSHA, err := canonicalPodSpecSHA256(first)
	if err != nil {
		t.Fatal(err)
	}
	secondSHA, err := canonicalPodSpecSHA256(second)
	if err != nil {
		t.Fatal(err)
	}
	if firstSHA != secondSHA || len(firstSHA) != 64 {
		t.Fatalf("canonical hashes differ: %q != %q", firstSHA, secondSHA)
	}
}

func TestValidateTargetEnvironmentAllowsTokenizerTuningButRejectsCredentialsAndDynamicSources(t *testing.T) {
	valid := &corev1.Container{Env: []corev1.EnvVar{
		{Name: "DYN_SNAPSHOT_RESTORE_STANDBY", Value: "1"},
		{Name: "DYN_SNAPSHOT_CONTROL_DIR", Value: "/snapshot-control"},
		{Name: "TOKENIZERS_PARALLELISM", Value: "false"},
	}}
	if err := validateTargetEnvironment(valid); err != nil {
		t.Fatalf("safe tokenizer tuning was rejected: %v", err)
	}

	for name, mutate := range map[string]func(*corev1.Container){
		"credential-name": func(container *corev1.Container) {
			container.Env = append(container.Env, corev1.EnvVar{Name: "HF_TOKEN", Value: "not-a-real-token"})
		},
		"dynamic-source": func(container *corev1.Container) {
			container.Env = append(container.Env, corev1.EnvVar{
				Name: "MODEL_NAME", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"}},
			})
		},
		"missing-control": func(container *corev1.Container) {
			container.Env = container.Env[1:]
		},
	} {
		t.Run(name, func(t *testing.T) {
			candidate := valid.DeepCopy()
			mutate(candidate)
			if err := validateTargetEnvironment(candidate); err == nil {
				t.Fatal("expected target environment rejection")
			}
		})
	}
}

func rebindTargetPodSpec(t *testing.T, pod *corev1.Pod, opts *options) {
	t.Helper()
	digest, err := canonicalPodSpecSHA256(pod.Spec)
	if err != nil {
		t.Fatal(err)
	}
	opts.TargetPodSpecSHA = digest
	pod.Annotations[targetSpecSHAKey] = digest
}

func TestVerifyPinnedMountNamespace(t *testing.T) {
	procRoot := t.TempDir()
	nsDir := filepath.Join(procRoot, "123", "ns")
	if err := os.MkdirAll(nsDir, 0o755); err != nil {
		t.Fatal(err)
	}
	mountPath := filepath.Join(nsDir, "mnt")
	if err := os.WriteFile(mountPath, []byte("namespace"), 0o644); err != nil {
		t.Fatal(err)
	}
	pinned, err := os.Open(mountPath)
	if err != nil {
		t.Fatal(err)
	}
	defer pinned.Close()
	if err := verifyPinnedMountNamespace(pinned, procRoot, 123); err != nil {
		t.Fatalf("verifyPinnedMountNamespace: %v", err)
	}
	other := filepath.Join(t.TempDir(), "other")
	if err := os.WriteFile(other, []byte("other"), 0o644); err != nil {
		t.Fatal(err)
	}
	different, err := os.Open(other)
	if err != nil {
		t.Fatal(err)
	}
	defer different.Close()
	if err := verifyPinnedMountNamespace(different, procRoot, 123); err == nil {
		t.Fatal("expected mismatched namespace fd rejection")
	}
}
