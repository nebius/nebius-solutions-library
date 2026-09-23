#!/bin/bash
# Generate a shareable markdown report from a unified-runner results directory.
#
# The unified runner writes one raw NCCL log per test as "<test>-<hosts>.log"
# (e.g. all_reduce-2.log). This parses each of those into a combined report —
# it does NOT talk to the cluster, so it works after pods are long gone.
#
# Usage: ./generate-report.sh [results/nccl-<timestamp>]
#        (defaults to the most recent results/nccl-* directory)

set -eo pipefail

RESULTS_DIR="${1:-$(ls -dt results/nccl-* 2>/dev/null | head -1)}"
if [ -z "$RESULTS_DIR" ] || [ ! -d "$RESULTS_DIR" ]; then
  echo "ERROR: no results directory given and none found under results/nccl-*"
  echo "Usage: $0 [results/nccl-<timestamp>]"
  exit 1
fi

REPORT="$RESULTS_DIR/report.md"

# awk program shared by the summary and per-test sections: emits genuine NCCL
# data rows only (13 fields, field 3 a real dtype), so NCCL_DEBUG noise is dropped.
DATA_ROWS='NF==13 && $3 ~ /^(float|double|half|bfloat16|int|int8|uint8|char)$/'

# Collect the log files, sorted by host count then test name for a stable order.
# Built with a read loop rather than `mapfile`, which is bash 4+ (macOS ships 3.2).
LOGS=()
while IFS= read -r logfile; do
  [ -n "$logfile" ] && LOGS+=("$logfile")
done < <(ls "$RESULTS_DIR"/*.log 2>/dev/null | sort)
if [ "${#LOGS[@]}" -eq 0 ]; then
  echo "ERROR: no *.log files in $RESULTS_DIR"
  exit 1
fi

{
  echo "# NCCL Test Report"
  echo ""
  echo "**Results directory:** \`$RESULTS_DIR\`"
  echo "**Generated:** $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo ""
  echo "> \`busbw\` (bus bandwidth) is the hardware-utilization figure to compare against"
  echo "> reference numbers. Single-host runs exercise intra-node NVLink; multi-host runs"
  echo "> cross the InfiniBand fabric. Peak is the max across the size sweep (some collectives"
  echo "> — notably alltoall — peak mid-sweep and decline, so the last row is not the peak)."
  echo ""

  # ---- Summary table ----
  echo "## Summary"
  echo ""
  echo "| Test | Hosts | Scope | Transport | Peak busbw (oop) | @ size | Errors |"
  echo "|---|---|---|---|---|---|---|"
  for f in "${LOGS[@]}"; do
    base=$(basename "$f" .log)
    hosts="${base##*-}"
    test="${base%-*}"
    if [ "$hosts" -gt 1 ] 2>/dev/null; then scope="cross-node"; else scope="single-node"; fi
    if grep -q "Using network IB" "$f" 2>/dev/null; then
      transport=$([ "$hosts" -gt 1 ] 2>/dev/null && echo "InfiniBand" || echo "NVLink (IB init'd)")
    else
      transport="?"
    fi
    # Guard against an empty/failed log: with no NCCL data rows the awk aggregates
    # below return peak 0 and zero errors, which would render as a green
    # "0.0 GB/s | none ✓" even for a run that only produced a launcher failure.
    # Require at least one parsed data row before reporting any result.
    nrows=$(awk "$DATA_ROWS"' {c++} END{print c+0}' "$f")
    if [ "$nrows" -eq 0 ]; then
      echo "| $test | $hosts | $scope | $transport | — | — | ✗ no data (incomplete/failed) |"
      continue
    fi
    # peak busbw (field 8) and its size (field 1); errors if any field 9/13 nonzero (N/A ok)
    read -r peak psize < <(awk "$DATA_ROWS"' {if($8+0>max){max=$8+0;sz=$1}} END{printf "%.1f %s\n", max, sz}' "$f")
    errs=$(awk "$DATA_ROWS"' (($9!="N/A" && $9+0!=0)||($13!="N/A" && $13+0!=0)){c++} END{print c+0}' "$f")
    errflag=$([ "$errs" -eq 0 ] && echo "none ✓" || echo "$errs ⚠️")
    echo "| $test | $hosts | $scope | $transport | ${peak} GB/s | ${psize}B | $errflag |"
  done
  echo ""

  # ---- Per-test detail ----
  HEADER="| Size (B) | Count | Type | Redop | Root | Time(us) oop | Algbw oop | Busbw oop | Err oop | Time(us) ip | Algbw ip | Busbw ip | Err ip |"
  DIVIDER="|---|---|---|---|---|---|---|---|---|---|---|---|---|"
  for f in "${LOGS[@]}"; do
    base=$(basename "$f" .log)
    hosts="${base##*-}"
    test="${base%-*}"
    echo "---"
    echo ""
    echo "## $test — $hosts host(s)"
    echo ""

    ROWS=$(awk "$DATA_ROWS" "$f")
    if [ -z "$ROWS" ]; then
      echo "_No data rows found — the test may not have completed._"
      echo ""
      continue
    fi

    # Peak row (max busbw oop) and largest-size row.
    PEAK_ROW=$(echo "$ROWS" | sort -k8,8 -g | tail -1)
    LAST_ROW=$(echo "$ROWS" | tail -1)
    read -r p_size _ _ _ _ _ _ p_oop _ _ _ p_ip _ <<<"$PEAK_ROW"
    read -r l_size _ _ _ _ _ _ l_oop _ _ _ l_ip _ <<<"$LAST_ROW"

    echo "**Peak busbw @ ${p_size}B:** ${p_oop} GB/s (oop) / ${p_ip} GB/s (ip)"
    echo ""
    if [ "$p_size" != "$l_size" ]; then
      echo "**Note:** peak is mid-sweep; at the largest size (${l_size}B) busbw is ${l_oop} GB/s (oop) / ${l_ip} GB/s (ip)."
      echo ""
    fi

    ERRORS=$(echo "$ROWS" | awk '($9!="N/A" && $9+0!=0)||($13!="N/A" && $13+0!=0)')
    if [ -n "$ERRORS" ]; then
      echo "**⚠️ Non-zero error column(s) — possible data corruption:**"
      echo ""; echo '```'; echo "$ERRORS"; echo '```'
    else
      echo "**Correctness:** error columns all zero — no data corruption."
    fi
    echo ""

    echo "$HEADER"; echo "$DIVIDER"
    echo "$ROWS" | awk '{printf "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n",$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13}'
    echo ""
  done
} > "$REPORT"

echo "Report written to: $REPORT"
