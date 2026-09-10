package main

import "testing"

func minimalWorkerEnvironment() []string {
	return []string{
		"PATH=/usr/local/bin:/usr/bin:/bin",
		"KUBERNETES_SERVICE_HOST=10.96.0.1",
		"KUBERNETES_SERVICE_PORT=443",
		"KUBERNETES_SERVICE_PORT_HTTPS=443",
		"CUDA_VERSION=13.0.3",
	}
}

func TestSanitizeEnvironmentReturnsMinimalSafeEnvironment(t *testing.T) {
	clean, err := sanitizeEnvironment(minimalWorkerEnvironment())
	if err != nil {
		t.Fatalf("sanitizeEnvironment: %v", err)
	}
	if len(clean) != 7 {
		t.Fatalf("clean environment has %d entries, want 7", len(clean))
	}
}

func TestSanitizeEnvironmentAcceptsStandardKubernetesInjectedEnvironment(t *testing.T) {
	environ := append(minimalWorkerEnvironment(),
		"HOME=/root",
		"HOSTNAME=openfold2-restore-worker",
		"KUBERNETES_PORT=tcp://10.96.0.1:443",
		"KUBERNETES_PORT_443_TCP=tcp://10.96.0.1:443",
		"KUBERNETES_PORT_443_TCP_ADDR=10.96.0.1",
		"KUBERNETES_PORT_443_TCP_PORT=443",
		"KUBERNETES_PORT_443_TCP_PROTO=tcp",
		"LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
		"NVARCH=x86_64",
		"NVIDIA_DRIVER_CAPABILITIES=compute,utility",
		"NVIDIA_CTK_LIBCUDA_DIR=/usr/local/nvidia/lib64",
		"NVIDIA_REQUIRE_CUDA=cuda>=13.0",
		"NVIDIA_VISIBLE_DEVICES=all",
		"NV_CUDA_CUDART_VERSION=13.0.96-1",
	)

	clean, err := sanitizeEnvironment(environ)
	if err != nil {
		t.Fatalf("sanitizeEnvironment: %v", err)
	}
	if len(clean) != 7 {
		t.Fatalf("clean environment has %d entries, want 7", len(clean))
	}
}

func TestSanitizeEnvironmentRejectsGenericCredentialCarrier(t *testing.T) {
	environ := append(minimalWorkerEnvironment(), "CONFIG=nvapi-not-a-real-key")
	if _, err := sanitizeEnvironment(environ); err == nil {
		t.Fatal("expected unapproved environment name/value to be rejected")
	}
}

func TestSanitizeEnvironmentRejectsInnocuousUnknownName(t *testing.T) {
	environ := append(minimalWorkerEnvironment(), "CONFIG=benign")
	if _, err := sanitizeEnvironment(environ); err == nil {
		t.Fatal("expected exact environment allowlist enforcement")
	}
}
