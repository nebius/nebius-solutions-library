#!/bin/bash

export TEST_DIR='/opt/slurm-test'

export TEST_QUICKCHECK_DIR="${TEST_DIR}/quickcheck"
export TEST_MLPERF_SD_DIR="${TEST_DIR}/mlperf-sd"
export TEST_MLPERF_GPT3_DIR="${TEST_DIR}/mlperf-gpt3"

TEST_RESULTS_DIR_NAME='results'
export TEST_QUICKCHECK_RESULTS_DIR="${TEST_QUICKCHECK_DIR}/${TEST_RESULTS_DIR_NAME}"
export TEST_MLPERF_SD_RESULTS_DIR="${TEST_MLPERF_SD_DIR}/${TEST_RESULTS_DIR_NAME}"
export TEST_MLPERF_GPT3_RESULTS_DIR="${TEST_MLPERF_GPT3_DIR}/${TEST_RESULTS_DIR_NAME}"

export NEBIUS_S3_ENDPOINT='storage.eu-north1.nebius.cloud'
export NEBIUS_S3_BUCKET_NAME='slurm-mlperf-training'

export NEBIUS_CR_ENDPOINT='cr.eu-north1.nebius.cloud'
export NEBIUS_CR_REGISTRY='slurm-mlperf-training'
export NEBIUS_CR_USER='iam'
export NEBIUS_CR_PASSWORD='cafebabyfordocker'

export RCLONE_PROFILE_NEBIUS_S3='nebius-s3'
