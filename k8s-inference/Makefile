# Short names for the everyday commands (README.md). Everything here is also a script you can call directly.
.PHONY: help venv format lint openapi check preflight plan apply destroy images

help:         ## this list
	@grep -E '^[a-z]+: .*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t 12

venv:         ## one-time: a Python virtualenv with the test dependencies (.venv-api, used by `make check`)
	python3 -m venv .venv-api && .venv-api/bin/pip install -q -r services/api/requirements.txt -r services/api/requirements-dev.txt -r services/dispatcher/requirements-dev.txt pyyaml

format:       ## format Python/imports and Terraform; needs `make venv` once
	.venv-api/bin/python -m ruff check --select I --fix services stack
	.venv-api/bin/python -m ruff format services stack
	terraform fmt -recursive stack

lint:         ## Python formatting/lint, ShellCheck and Terraform formatting (also part of `make check`)
	tools/check.sh lint

openapi:      ## export the generated API contract as JSON to stdout; needs `make venv` once
	@PYTHONPATH=services/api .venv-api/bin/python -c 'import json; from app import app; print(json.dumps(app.openapi(), indent=2))'

check:        ## lint, render everything and run the tests, no cloud access (the CI gate); needs `make venv` once
	tools/check.sh

preflight:    ## terraform.tfvars against the Nebius compatibility matrix and quotas
	./stack.sh preflight

plan:         ## terraform plan of every stage in order
	./stack.sh plan

apply:        ## bring the fleet up (or update it): every stage in order
	./stack.sh apply

destroy:      ## tear the fleet down: every stage in reverse
	./stack.sh destroy

images:       ## build and push the four platform images to the fleet's registry
	tools/images.sh build
