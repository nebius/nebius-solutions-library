#!/bin/bash

set -e

usage() {
  echo "Usage: ${0} <REQUIRED_FLAGS> [FLAGS] [-h]" >&2
  echo 'Required flags:' >&2
  echo '  -t  [str ]  Test type. One of:' >&2
  echo '                quickcheck' >&2
  echo '                checkpointing' >&2
  echo '                mlperf-sd' >&2
  echo '                mlperf-gpt3' >&2
  echo '  -u  [str ]  SSH username' >&2
  echo '  -k  [path]  Path to private SSH key' >&2
  echo '  -a  [str ]  Address of login node (IP or domain name)' >&2
  echo '' >&2
  echo 'Flags:' >&2
  echo '  -p  [int ]  SSH port of login node.' >&2
  echo '              By default, 22' >&2
  echo '' >&2
  echo '  -h  Print help and exit' >&2
  exit 1
}

while getopts t:u:k:a:p:n:h flag
do
  case "${flag}" in
    t) TEST_TYPE=${OPTARG};;
    u) USER=${OPTARG};;
    k) KEY=${OPTARG};;
    a) ADDRESS=${OPTARG};;
    p) PORT=${OPTARG};;
    h) usage;;
    *) usage;;
  esac
done

if [ -z "${TEST_TYPE}" ] || [ -z "${USER}" ] || [ -z "${KEY}" ] || [ -z "${ADDRESS}" ]; then
  usage
fi

if [ "${TEST_TYPE}" != "quickcheck" ] && [ "${TEST_TYPE}" != "checkpointing" ] && [ "${TEST_TYPE}" != "mlperf-sd" ] && [ "${TEST_TYPE}" != "mlperf-gpt3" ]; then
  usage
fi

if [ -z "${PORT}" ]; then
  PORT=22
fi

# shellcheck disable=SC1091 # resolved from the documented soperator/test cwd
source common/env.sh
# shellcheck disable=SC1091 # resolved from the documented soperator/test cwd
source common/printer.sh

h1 "Creating directory for tests on ${ADDRESS}..."
ssh \
  -i "${KEY}" \
  -p "${PORT}" \
  "${USER}@${ADDRESS}" \
  "sudo mkdir -p '${TEST_DIR}'; sudo chown ${USER}:${USER} '${TEST_DIR}'"
hdone

# The checkpointing bundle has multiple files in both source directories.  scp
# requires the corresponding remote destinations to exist when copying more
# than one source.  Keep this workaround scoped to the checkpointing bundle;
# the other delivery workflows retain their existing behavior.
if [ "${TEST_TYPE}" = "checkpointing" ]; then
  ssh \
    -i "${KEY}" \
    -p "${PORT}" \
    "${USER}@${ADDRESS}" \
    "mkdir -p '${TEST_DIR}/common' '${TEST_DIR}/checkpointing'"
fi

h1 "Transferring common files as user '${USER}' with key '${KEY}' to ${ADDRESS}:${PORT}..."
scp \
  -i "${KEY}" \
  -P "${PORT}" \
  -r \
  ./common/* \
  "${USER}@${ADDRESS}":"${TEST_DIR}/common"
hdone

h1 "Transferring files of ${TEST_TYPE} as user '${USER}' with key '${KEY}' to ${ADDRESS}:${PORT}..."
scp \
  -i "${KEY}" \
  -P "${PORT}" \
  -r \
  "./${TEST_TYPE}"/* \
  "${USER}@${ADDRESS}":"${TEST_DIR}/${TEST_TYPE}"
hdone
