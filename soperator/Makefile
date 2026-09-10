SHELL = /usr/bin/env bash -o pipefail
.SHELLFLAGS = -ec

# Provider downloads flake on slow networks: the registry client gives up after
# 10s / 2 attempts by default and `terraform init` fails on the checksum fetch.
export TF_REGISTRY_DISCOVERY_RETRY ?= 5
export TF_REGISTRY_CLIENT_TIMEOUT ?= 60

SOPERATOR_VERSION		= $(shell cat VERSION)
SUBVERSION				= $(shell cat SUBVERSION)
VERSION					= $(SOPERATOR_VERSION)-$(SUBVERSION)

ifeq ($(shell uname), Darwin)
    SED_COMMAND = sed -i ''
else
    SED_COMMAND = sed -i
endif

.PHONY: sync-version
sync-version: ## Sync Soperator version from file
	@echo 'Soperator version is - $(SOPERATOR_VERSION)'

	@# region installations/example/terraform.tfvars
	@echo 'Syncing installations/example/terraform.tfvars'
	@$(SED_COMMAND) -E 's/slurm_operator_version *= *"[0-9]+.[0-9]+.[0-9]+[^ ]*"/slurm_operator_version = "$(SOPERATOR_VERSION)"/' installations/example/terraform.tfvars
	@terraform fmt installations/example/terraform.tfvars
	@# endregion installations/example/terraform.tfvars

.PHONY: release
release: ## Create a zipped tarball with release TF recipe
	@echo "Packing terraform tarball with version - ${IMAGE_TAG}"
	VERSION=${VERSION} ./release_terraform.sh -f

.PHONY: terraform-fmt-check
terraform-fmt-check:
	@echo 'Checking Terraform formatting...'
	@terraform fmt -check -recursive .

.PHONY: terraform-fmt
terraform-fmt:
	@echo 'Formatting Terraform files...'
	@terraform fmt -recursive .

.PHONY: terraform-validate-modules
terraform-validate-modules:
	@echo 'Validating Terraform modules...'
	@for module_dir in modules/*/; do \
		if [ -d "$$module_dir" ]; then \
			echo "Validating module: $$module_dir"; \
			cd "$$module_dir"; \
			terraform init -upgrade -backend=false; \
			terraform validate; \
			cd - > /dev/null; \
		fi; \
	done

.PHONY: terraform-validate-installations
terraform-validate-installations:
	@echo 'Validating Terraform installations...'
	@for install_dir in installations/example/; do \
		if [ -d "$$install_dir" ]; then \
			echo "Validating installation: $$install_dir"; \
			cd "$$install_dir"; \
			terraform init -upgrade -backend=false; \
			terraform validate; \
			cd - > /dev/null; \
		fi; \
	done

.PHONY: terraform-test-modules
terraform-test-modules:
	@echo 'Running Terraform module tests...'
	@for module_dir in modules/*/; do \
		if [ -d "$$module_dir/tests" ]; then \
			echo "Testing module: $$module_dir"; \
			cd "$$module_dir"; \
			terraform init -upgrade -backend=false; \
			terraform test; \
			cd - > /dev/null; \
		fi; \
	done

.PHONY: terraform-test-installations
terraform-test-installations:
	@echo 'Running Terraform installation module tests...'
	@for installation_dir in installations/*/; do \
		if [ -d "$$installation_dir/tests" ]; then \
			echo "Testing module: $$installation_dir"; \
			pushd "$$installation_dir" > /dev/null; \
			terraform test -var-file ./tests/vars.hcl; \
			popd > /dev/null; \
		fi; \
	done

.PHONY: terraform-check
terraform-check: terraform-fmt-check terraform-validate-modules terraform-validate-installations terraform-test-modules
	@echo 'All Terraform checks passed successfully!'
