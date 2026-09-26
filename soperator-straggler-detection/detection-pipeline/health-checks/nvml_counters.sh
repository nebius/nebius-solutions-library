#!/bin/bash
# Per-GPU NVML counters for one host. Usage: nvml_counters.sh <host>
set -u
HOST=$1
ssh -o BatchMode=yes "$HOST" '
for i in 0 1 2 3 4 5 6 7; do
  temp=$(nvidia-smi -i $i --query-gpu=temperature.gpu --format=csv,noheader,nounits)
  power=$(nvidia-smi -i $i --query-gpu=power.draw --format=csv,noheader,nounits)
  appclk=$(nvidia-smi -i $i --query-gpu=clocks.applications.graphics --format=csv,noheader,nounits)
  maxclk=$(nvidia-smi -i $i --query-gpu=clocks.max.sm --format=csv,noheader,nounits)
  perf=$(nvidia-smi -i $i -q -d PERFORMANCE)
  sw_thermal=$(echo "$perf" | grep -A5 "Clocks Event Reasons Counters" | grep "SW Thermal Slowdown" | grep -oE "[0-9]+" | head -1)
  hw_thermal=$(echo "$perf" | grep -A5 "Clocks Event Reasons Counters" | grep "HW Thermal Slowdown" | grep -oE "[0-9]+" | head -1)
  hw_power=$(echo "$perf" | grep -A5 "Clocks Event Reasons Counters" | grep "HW Power Braking" | grep -oE "[0-9]+" | head -1)
  sw_power=$(echo "$perf" | grep -A5 "Clocks Event Reasons Counters" | grep "SW Power Capping" | grep -oE "[0-9]+" | head -1)
  echo "$i,$temp,$power,$appclk,$maxclk,${sw_thermal:-0},${hw_thermal:-0},${hw_power:-0},${sw_power:-0}"
done
'
