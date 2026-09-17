# Cloud-init regression tests

Run `python3 soperator/test/cloud-init/test_cloud_init.py` from the repository root.
The same tests run via `make terraform-test-cloud-init` from `soperator/`, and
are included in `make terraform-check` in CI.

Requires Python 3 and Terraform; no Python packages, providers, or cloud credentials.

The tests render the real Terraform template, decode its YAML, check combinations
of AppArmor/SSH/NVIDIA settings, and exercise its shell loader with a fake parser
and kernel-interface file. Named subtests cover missing prerequisites, parser
errors, profile verification, and repeated boots. These tests do not validate
Linux kernel enforcement or node boot ordering.
