package main

import (
	"fmt"
	"net/netip"
	"strconv"
	"strings"
)

// These are the only names inherited from the pinned CUDA base or injected by
// Kubernetes. The Job contract forbids env/envFrom, so any additional name is
// evidence that the runtime manifest diverged from the reviewed shape.
var allowedWorkerEnvironment = map[string]struct{}{
	"PATH": {}, "HOME": {}, "HOSTNAME": {}, "KUBERNETES_SERVICE_HOST": {},
	"KUBERNETES_SERVICE_PORT": {}, "KUBERNETES_SERVICE_PORT_HTTPS": {},
	"KUBERNETES_PORT": {}, "KUBERNETES_PORT_443_TCP": {},
	"KUBERNETES_PORT_443_TCP_ADDR": {}, "KUBERNETES_PORT_443_TCP_PORT": {},
	"KUBERNETES_PORT_443_TCP_PROTO": {},
	"NVARCH":                        {}, "NVIDIA_REQUIRE_CUDA": {}, "NV_CUDA_CUDART_VERSION": {},
	"CUDA_VERSION": {}, "LD_LIBRARY_PATH": {}, "NVIDIA_VISIBLE_DEVICES": {},
	"NVIDIA_DRIVER_CAPABILITIES": {}, "NVIDIA_CTK_LIBCUDA_DIR": {}, "NV_CUDA_LIB_VERSION": {},
	"NV_NVTX_VERSION": {}, "NV_LIBNPP_VERSION": {}, "NV_LIBNPP_PACKAGE": {},
	"NV_LIBCUSPARSE_VERSION": {}, "NV_LIBCUBLAS_PACKAGE_NAME": {},
	"NV_LIBCUBLAS_VERSION": {}, "NV_LIBCUBLAS_PACKAGE": {},
	"NV_LIBNCCL_PACKAGE_NAME": {}, "NV_LIBNCCL_PACKAGE_VERSION": {},
	"NCCL_VERSION": {}, "NV_LIBNCCL_PACKAGE": {}, "NVIDIA_PRODUCT_NAME": {},
	"NV_CUDA_CUDART_DEV_VERSION": {}, "NV_NVML_DEV_VERSION": {},
	"NV_LIBCUSPARSE_DEV_VERSION": {}, "NV_LIBNPP_DEV_VERSION": {},
	"NV_LIBNPP_DEV_PACKAGE": {}, "NV_LIBCUBLAS_DEV_VERSION": {},
	"NV_LIBCUBLAS_DEV_PACKAGE_NAME": {}, "NV_LIBCUBLAS_DEV_PACKAGE": {},
	"NV_CUDA_NSIGHT_COMPUTE_VERSION": {}, "NV_CUDA_NSIGHT_COMPUTE_DEV_PACKAGE": {},
	"NV_LIBNCCL_DEV_PACKAGE_NAME": {}, "NV_LIBNCCL_DEV_PACKAGE_VERSION": {},
	"NV_LIBNCCL_DEV_PACKAGE": {}, "LIBRARY_PATH": {},
}

var secretValueMarkers = []string{
	"nvapi-", "ngc_api_key", "nvidia_api_key", "api_key=", "password=",
	"authorization:", "bearer ", "aws_secret_access_key", "-----begin private key-----",
	"ghp_", "github_pat_", "xoxb-", "xoxp-",
}

// sanitizeEnvironment validates the complete inherited environment and returns
// the minimal subset that may be propagated into the Kubernetes client and
// restore child. The caller clears the process environment before installing
// this result, so credential-bearing values cannot leak into CRIU diagnostics.
func sanitizeEnvironment(environ []string) ([]string, error) {
	if len(environ) > 128 {
		return nil, fmt.Errorf("worker environment exceeds the bound")
	}
	seen := make(map[string]struct{}, len(environ))
	values := make(map[string]string, len(environ))
	total := 0
	for _, entry := range environ {
		total += len(entry)
		if total > 32768 {
			return nil, fmt.Errorf("worker environment exceeds the byte bound")
		}
		name, value, ok := strings.Cut(entry, "=")
		if !ok || name == "" {
			return nil, fmt.Errorf("worker environment has a malformed entry")
		}
		if _, duplicate := seen[name]; duplicate {
			return nil, fmt.Errorf("worker environment repeats a name")
		}
		seen[name] = struct{}{}
		if _, allowed := allowedWorkerEnvironment[name]; !allowed {
			return nil, fmt.Errorf("worker environment contains an unapproved name")
		}
		upperName := strings.ToUpper(name)
		if strings.Contains(upperName, "SECRET") || strings.Contains(upperName, "PASSWORD") || strings.Contains(upperName, "API_KEY") || strings.Contains(upperName, "CREDENTIAL") || strings.Contains(upperName, "TOKEN") {
			return nil, fmt.Errorf("worker environment contains a secret-like name")
		}
		lowerValue := strings.ToLower(value)
		for _, marker := range secretValueMarkers {
			if strings.Contains(lowerValue, marker) {
				return nil, fmt.Errorf("worker environment contains a credential marker")
			}
		}
		values[name] = value
	}

	host, hostOK := values["KUBERNETES_SERVICE_HOST"]
	port, portOK := values["KUBERNETES_SERVICE_PORT"]
	if !hostOK || !portOK {
		return nil, fmt.Errorf("in-cluster Kubernetes endpoint is absent")
	}
	if address, err := netip.ParseAddr(host); err != nil || !address.IsValid() || address.IsUnspecified() || address.IsLoopback() {
		return nil, fmt.Errorf("in-cluster Kubernetes host is invalid")
	}
	parsedPort, err := strconv.Atoi(port)
	if err != nil || parsedPort < 1 || parsedPort > 65535 {
		return nil, fmt.Errorf("in-cluster Kubernetes port is invalid")
	}
	if httpsPort, ok := values["KUBERNETES_SERVICE_PORT_HTTPS"]; ok && httpsPort != port {
		return nil, fmt.Errorf("in-cluster Kubernetes HTTPS port is inconsistent")
	}

	// The child gets only deterministic tool lookup plus the validated API
	// endpoint. CUDA helpers obtain devices from the target namespace and do not
	// need the CUDA image's package metadata environment.
	clean := []string{
		"PATH=/usr/local/nvidia/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
		"NVIDIA_VISIBLE_DEVICES=all",
		"NVIDIA_DRIVER_CAPABILITIES=compute,utility",
		"KUBERNETES_SERVICE_HOST=" + host,
		"KUBERNETES_SERVICE_PORT=" + port,
	}
	if _, ok := values["KUBERNETES_SERVICE_PORT_HTTPS"]; ok {
		clean = append(clean, "KUBERNETES_SERVICE_PORT_HTTPS="+port)
	}
	return clean, nil
}
