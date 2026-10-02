package main

import (
	"strings"
	"testing"
)

const (
	testNamespace = "bionemo-faststart"
	testContainer = "boltz2"
	testImageID   = "nvcr.io/nim/mit/boltz2@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)

func validArguments() []string {
	containerID := strings.Repeat("a", 64)
	return []string{
		"restore",
		"--target-namespace", testNamespace,
		"--target-name", "boltz-target-ut-a1b2c3",
		"--target-uid", "11111111-1111-4111-8111-111111111111",
		"--target-container", testContainer,
		"--target-container-id", "containerd://" + containerID,
		"--target-cgroup", "/kubepods.slice/cri-containerd-" + containerID + ".scope",
		"--target-pod-ip", "10.50.42.7",
		"--target-node", "gpu-node-a.example.invalid",
		"--expected-image-id", testImageID,
		"--target-pod-spec-sha256", strings.Repeat("d", 64),
		"--run-id", "ut-a1b2c3",
		"--checkpoint-id", "boltz2-native-v1",
		"--artifact-version", "1",
		"--artifact-manifest-sha256", strings.Repeat("b", 64),
		"--tool-bundle-sha256", strings.Repeat("c", 64),
		"--container-runtime-socket", runtimeSocketPath,
		"--host-proc", hostProcPath,
		"--host-cgroup", hostCgroupPath,
		"--pod-resources-socket", podResourcesPath,
	}
}

func TestParseOptionsAcceptsExactContract(t *testing.T) {
	opts, err := parseOptions(validArguments())
	if err != nil {
		t.Fatalf("parseOptions: %v", err)
	}
	if opts.TargetNamespace != testNamespace || opts.ExpectedImageID != testImageID || opts.TargetContainer != testContainer {
		t.Fatalf("unexpected parsed options: %#v", opts)
	}
}

func TestParseOptionsRejectsDuplicateAndUnknownFlags(t *testing.T) {
	for name, args := range map[string][]string{
		"duplicate": append(append([]string{}, validArguments()...), "--run-id", "other"),
		"unknown":   append(append([]string{}, validArguments()...), "--extra", "value"),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseOptions(args); err == nil {
				t.Fatal("expected fail-closed parse error")
			}
		})
	}
}

func TestParseOptionsAcceptsModelIndependentBindings(t *testing.T) {
	for name, replacements := range map[string]map[string]string{
		"openfold3": {
			"--target-namespace": "protein-folding",
			"--target-name":      "openfold3.restore-7",
			"--target-container": "openfold3",
			"--target-node":      "gpu-node-17.example.internal",
			"--expected-image-id": "nvcr.io/nim/openfold/openfold3@sha256:" +
				strings.Repeat("e", 64),
		},
		"evo2": {
			"--target-namespace": "genomics",
			"--target-name":      "evo2-40b-target",
			"--target-container": "evo2",
			"--target-node":      "gpu-node-22",
			"--expected-image-id": "registry.example.test/bionemo/evo2@sha256:" +
				strings.Repeat("f", 64),
		},
	} {
		t.Run(name, func(t *testing.T) {
			args := validArguments()
			for index := range args {
				if value, ok := replacements[args[index]]; ok && index+1 < len(args) {
					args[index+1] = value
				}
			}
			if _, err := parseOptions(args); err != nil {
				t.Fatalf("model-independent binding rejected: %v", err)
			}
		})
	}
}

func TestParseOptionsRejectsInvalidGenericIdentity(t *testing.T) {
	for name, replacement := range map[string]struct {
		flag  string
		value string
	}{
		"namespace": {"--target-namespace", "UPPERCASE"},
		"node":      {"--target-node", "node..invalid"},
		"image":     {"--expected-image-id", "registry.example/image:latest"},
		"spec-sha":  {"--target-pod-spec-sha256", strings.Repeat("A", 64)},
	} {
		t.Run(name, func(t *testing.T) {
			args := validArguments()
			for index := range args {
				if args[index] == replacement.flag {
					args[index+1] = replacement.value
				}
			}
			if _, err := parseOptions(args); err == nil {
				t.Fatal("expected invalid generic identity rejection")
			}
		})
	}
}
