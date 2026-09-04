package main

import (
	"flag"
	"fmt"
	"io"
	"net/netip"
	"path/filepath"
	"regexp"
	"strings"
)

const (
	runLabelKey         = "archvteams.nebius.ai/run-id"
	targetSpecSHAKey    = "archvteams.nebius.ai/target-pod-spec-sha256"
	checkpointBasePath  = "/checkpoints"
	toolBundlePath      = "/snapshot-binaries"
	toolManifestPath    = "/snapshot-binaries.manifest"
	runtimeSocketPath   = "/run/containerd/containerd.sock"
	hostProcPath        = "/host/proc"
	hostCgroupPath      = "/sys/fs/cgroup"
	podResourcesPath    = "/var/lib/kubelet/pod-resources/kubelet.sock"
	maxReceiptBytes     = 4096
	maxManifestBytes    = 1 << 20
	workerTimeoutSecond = 840
)

var (
	dnsLabelPattern = regexp.MustCompile(`^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$`)
	uuidPattern     = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	digestPattern   = regexp.MustCompile(`^[0-9a-f]{64}$`)
	versionPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
)

type options struct {
	TargetNamespace       string
	TargetName            string
	TargetUID             string
	TargetContainer       string
	TargetContainerID     string
	TargetCgroup          string
	TargetPodIP           string
	TargetNode            string
	ExpectedImageID       string
	TargetPodSpecSHA      string
	RunID                 string
	CheckpointID          string
	ArtifactVersion       string
	ArtifactManifestSHA   string
	ToolBundleManifestSHA string
	RuntimeSocket         string
	HostProc              string
	HostCgroup            string
	PodResourcesSocket    string
}

type requiredValue struct {
	name  string
	value string
	seen  bool
}

func (v *requiredValue) String() string { return v.value }

func (v *requiredValue) Set(value string) error {
	if v.seen {
		return fmt.Errorf("duplicate --%s", v.name)
	}
	v.seen = true
	v.value = value
	return nil
}

func parseOptions(args []string) (options, error) {
	if len(args) == 0 || args[0] != "restore" {
		return options{}, fmt.Errorf("the only supported operation is restore")
	}

	fs := flag.NewFlagSet("restore-worker restore", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	values := map[string]*requiredValue{}
	for _, name := range []string{
		"target-namespace", "target-name", "target-uid", "target-container",
		"target-container-id", "target-cgroup", "target-pod-ip", "target-node",
		"expected-image-id", "target-pod-spec-sha256", "run-id", "checkpoint-id", "artifact-version",
		"artifact-manifest-sha256", "tool-bundle-sha256",
		"container-runtime-socket", "host-proc", "host-cgroup", "pod-resources-socket",
	} {
		value := &requiredValue{name: name}
		values[name] = value
		fs.Var(value, name, "required bound value")
	}
	if err := fs.Parse(args[1:]); err != nil {
		return options{}, fmt.Errorf("invalid restore arguments")
	}
	if fs.NArg() != 0 {
		return options{}, fmt.Errorf("positional arguments are forbidden")
	}
	for name, value := range values {
		if !value.seen || value.value == "" {
			return options{}, fmt.Errorf("missing required --%s", name)
		}
		if len(value.value) > 4096 || strings.IndexFunc(value.value, func(r rune) bool { return r < 0x20 || r == 0x7f }) >= 0 {
			return options{}, fmt.Errorf("invalid --%s", name)
		}
	}

	opts := options{
		TargetNamespace:       values["target-namespace"].value,
		TargetName:            values["target-name"].value,
		TargetUID:             values["target-uid"].value,
		TargetContainer:       values["target-container"].value,
		TargetContainerID:     values["target-container-id"].value,
		TargetCgroup:          values["target-cgroup"].value,
		TargetPodIP:           values["target-pod-ip"].value,
		TargetNode:            values["target-node"].value,
		ExpectedImageID:       values["expected-image-id"].value,
		TargetPodSpecSHA:      values["target-pod-spec-sha256"].value,
		RunID:                 values["run-id"].value,
		CheckpointID:          values["checkpoint-id"].value,
		ArtifactVersion:       values["artifact-version"].value,
		ArtifactManifestSHA:   values["artifact-manifest-sha256"].value,
		ToolBundleManifestSHA: values["tool-bundle-sha256"].value,
		RuntimeSocket:         values["container-runtime-socket"].value,
		HostProc:              values["host-proc"].value,
		HostCgroup:            values["host-cgroup"].value,
		PodResourcesSocket:    values["pod-resources-socket"].value,
	}
	if err := opts.validate(); err != nil {
		return options{}, err
	}
	return opts, nil
}

func (o options) validate() error {
	if !validDNSLabel(o.TargetNamespace, 63) {
		return fmt.Errorf("target namespace must be a lowercase DNS label")
	}
	if !validDNSSubdomain(o.TargetName, 253) {
		return fmt.Errorf("target name must be a lowercase DNS subdomain")
	}
	if !validDNSLabel(o.TargetContainer, 63) {
		return fmt.Errorf("target container must be a lowercase DNS label")
	}
	if !validDNSLabel(o.RunID, 63) {
		return fmt.Errorf("run ID must be a lowercase DNS label")
	}
	if !uuidPattern.MatchString(o.TargetUID) {
		return fmt.Errorf("target UID must be a canonical lowercase UUID")
	}
	containerHex := strings.TrimPrefix(o.TargetContainerID, "containerd://")
	if len(containerHex) != 64 || !digestPattern.MatchString(containerHex) || o.TargetContainerID != "containerd://"+containerHex {
		return fmt.Errorf("target container ID must be a full containerd ID")
	}
	if !filepath.IsAbs(o.TargetCgroup) || filepath.Clean(o.TargetCgroup) != o.TargetCgroup || !strings.HasPrefix(o.TargetCgroup, "/kubepods") || !strings.Contains(o.TargetCgroup, containerHex) {
		return fmt.Errorf("target cgroup must be the exact normalized kubepods path for the container ID")
	}
	address, err := netip.ParseAddr(o.TargetPodIP)
	if err != nil || !address.IsValid() || address.IsUnspecified() || address.IsLoopback() || address.IsMulticast() || address.String() != o.TargetPodIP {
		return fmt.Errorf("target Pod IP must be a canonical unicast address")
	}
	if !validDNSSubdomain(o.TargetNode, 253) {
		return fmt.Errorf("target node must be a lowercase DNS subdomain")
	}
	if !validImmutableImageID(o.ExpectedImageID) {
		return fmt.Errorf("expected image ID must be an immutable sha256 image reference")
	}
	if !validDNSLabel(o.CheckpointID, 63) {
		return fmt.Errorf("checkpoint ID must be a lowercase DNS label")
	}
	if !versionPattern.MatchString(o.ArtifactVersion) {
		return fmt.Errorf("artifact version is invalid")
	}
	if !digestPattern.MatchString(o.TargetPodSpecSHA) || !digestPattern.MatchString(o.ArtifactManifestSHA) || !digestPattern.MatchString(o.ToolBundleManifestSHA) {
		return fmt.Errorf("manifest hashes must be lowercase SHA-256 values")
	}
	if o.RuntimeSocket != runtimeSocketPath || o.HostProc != hostProcPath || o.HostCgroup != hostCgroupPath || o.PodResourcesSocket != podResourcesPath {
		return fmt.Errorf("host path arguments do not match the compiled interface")
	}
	return nil
}

func validDNSLabel(value string, maximum int) bool {
	return value != "" && len(value) <= maximum && dnsLabelPattern.MatchString(value)
}

func validDNSSubdomain(value string, maximum int) bool {
	if value == "" || len(value) > maximum {
		return false
	}
	for _, label := range strings.Split(value, ".") {
		if !validDNSLabel(label, 63) {
			return false
		}
	}
	return true
}

func validImmutableImageID(value string) bool {
	const algorithm = "@sha256:"
	separator := strings.LastIndex(value, algorithm)
	if separator <= 0 || separator+len(algorithm)+64 != len(value) {
		return false
	}
	repository := value[:separator]
	digest := value[separator+len(algorithm):]
	if strings.Contains(repository, "@") || strings.IndexFunc(repository, func(r rune) bool {
		return r <= 0x20 || r == 0x7f
	}) >= 0 {
		return false
	}
	return digestPattern.MatchString(digest)
}

func (o options) checkpointPath() string {
	return filepath.Join(checkpointBasePath, o.CheckpointID, "versions", o.ArtifactVersion)
}
