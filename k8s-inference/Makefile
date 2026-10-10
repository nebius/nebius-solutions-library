# Short names for the everyday commands (README.md). Everything here is also a script you can call directly.
.PHONY: help venv check preflight plan apply destroy images

help:         ## this list
	@grep -E '^[a-z]+: .*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t 12

venv:         ## one-time: a Python virtualenv with the test dependencies (.venv-api, used by `make check`)
	python3 -m venv .venv-api && .venv-api/bin/pip install -q -r services/api/requirements.txt -r services/api/requirements-dev.txt -r services/dispatcher/requirements-dev.txt pyyaml

check:        ## render everything and run the tests, no cloud access (the CI gate); needs `make venv` once
	tools/check.sh

preflight:    ## terraform.tfvars against the Nebius compatibility matrix and quotas
	./stack.sh preflight

plan:         ## terraform plan of every stage in order
	./stack.sh plan

apply:        ## bring the fleet up (or update it): every stage in order
	./stack.sh apply

destroy:      ## tear the fleet down: every stage in reverse
	./stack.sh destroy

images:       ## build and push the five platform images to the fleet's registry
	tools/images.sh build
