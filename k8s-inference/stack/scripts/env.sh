#!/usr/bin/env bash
# External data source: the secret inputs from the environment (terraform.tfvars `secrets` names the variables).
# Prints {"ngc": ..., "hf": ..., "registries": ...}; an unset variable is an empty string (feature off).
set -eu
ngc="${!1:-}"; hf="${!2:-}"; regs="${!3:-}"
python3 -c 'import json,sys; print(json.dumps({"ngc": sys.argv[1], "hf": sys.argv[2], "registries": sys.argv[3]}))' "$ngc" "$hf" "$regs"
