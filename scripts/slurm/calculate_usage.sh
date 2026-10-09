#!/usr/bin/env bash
#
# Slurm GPU hours report: total GPU hours per user for a date range.
# Usage: ./calculate_usage.sh [START_DATE [END_DATE]]
#   Dates in MM/DD/YYYY (e.g. 01/01/2026 02/02/2026).
#   If omitted, uses last 365 days.
#

set -euo pipefail

# Convert elapsed string [DD-[HH:]]MM:SS to decimal hours
elapsed_to_hours() {
  local e="$1"
  local days=0 hours=0 mins=0 secs=0
  if [[ "$e" =~ ^([0-9]+)- ]]; then
    days="${BASH_REMATCH[1]}"
    e="${e#*-}"
  fi
  if [[ "$e" =~ ^([0-9]+):([0-9]+):([0-9]+)$ ]]; then
    hours="${BASH_REMATCH[1]}"
    mins="${BASH_REMATCH[2]}"
    secs="${BASH_REMATCH[3]}"
  elif [[ "$e" =~ ^([0-9]+):([0-9]+)$ ]]; then
    mins="${BASH_REMATCH[1]}"
    secs="${BASH_REMATCH[2]}"
  else
    echo "0"
    return
  fi
  awk -v d="$days" -v h="$hours" -v m="$mins" -v s="$secs" \
    'BEGIN { printf "%.4f", d*24 + h + m/60 + s/3600 }'
}

# Convert MM/DD/YYYY (or M/D/YYYY) to YYYY-MM-DD for sacct; pass through "now", "now-365days", etc.
normalize_date() {
  local d="$1"
  if [[ -z "$d" ]]; then echo ""; return; fi
  if [[ "$d" == now* ]] || [[ "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2} ]]; then echo "$d"; return; fi
  if [[ "$d" =~ ^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$ ]]; then
    printf "%s-%02d-%02d" "${BASH_REMATCH[3]}" "${BASH_REMATCH[1]#0}" "${BASH_REMATCH[2]#0}"
    return
  fi
  echo "$d"
}

# Derive total GPU count from sinfo by summing GPUs across all nodes.
get_total_gpus() {
  if ! command -v sinfo >/dev/null 2>&1; then
    echo ""
    return
  fi

  # Use %G (GRES) per node; handle forms like gpu:4, gpu:tesla:4 (POSIX awk, no match third arg)
  local total
  total=$(sinfo -N -h -o "%G" 2>/dev/null | awk '
    {
      n = split($0, a, ",");
      for (i = 1; i <= n; i++) {
        if (a[i] ~ /gpu/) {
          if (match(a[i], /[:=]([0-9]+)$/)) {
            total += substr(a[i], RSTART+1, RLENGTH-1) + 0
          }
        }
      }
    }
    END {
      if (total > 0) printf "%d\n", total;
    }')

  echo "${total:-}"
}

# Parse GPU/billing count from AllocTRES (gres/gpu, gpu, or billing= used by some clusters)
parse_gpu_count() {
  local tres="$1"
  if [[ "$tres" =~ gres/gpu[:=]([0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "$tres" =~ gpu[:=]([0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  # Some clusters track GPUs (or billable units) as billing=N or billing=N+
  if [[ "$tres" =~ billing[:=]([0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  echo "0"
}

main() {
  local start_arg="${1:-}"
  local end_arg="${2:-}"

  local sacct_start="" sacct_end=""
  if [[ -n "$start_arg" ]]; then
    sacct_start=$(normalize_date "$start_arg")
    sacct_end=$(normalize_date "${end_arg:-now}")
  else
    # No dates: last 365 days (avoids Slurm/slurmdbd limits on huge ranges that can truncate results)
    sacct_start="now-365days"
    sacct_end="now"
  fi

  echo "Date range: $sacct_start — $sacct_end" >&2

  local tmp="" intervals_file="" tmp_cluster="" mixed=""
  tmp=$(mktemp) || { echo "mktemp failed" >&2; exit 1; }
  intervals_file=$(mktemp) || true
  tmp_cluster=$(mktemp) || true
  mixed=$(mktemp) || true
  trap 'rm -f "${tmp:-}" "${intervals_file:-}" "${tmp_cluster:-}" "${mixed:-}"' EXIT

  # GPU hours + cluster busy/idle: awk writes INT lines + user table; convert Start→epoch in one pass (not one date per job)
  sacct -a -X -n -P --format=User,Elapsed,AllocTRES%400,Start \
      --starttime="$sacct_start" --endtime="$sacct_end" | awk -F'|' '
      function parse_tres(s,   n) {
        n = 0
        if (match(s, /billing=[0-9]+/)) { n = substr(s, RSTART+8, RLENGTH-8)+0 }
        else if (match(s, /gres\/gpu[:=][0-9]+/)) { n = substr(s, RSTART+9, RLENGTH-9)+0 }
        else if (match(s, /gpu[:=][0-9]+/) && s !~ /billing/) { n = substr(s, RSTART+4, RLENGTH-4)+0 }
        return n
      }
      function elapsed_hours(e,   d, t, n) {
        d = 0
        if (match(e, /^[0-9]+-/)) { d = substr(e, 1, RLENGTH-1)+0; e = substr(e, RLENGTH+1) }
        n = split(e, t, ":")
        if (n == 3) return d*24 + t[1]+0 + t[2]/60 + t[3]/3600
        if (n == 2) return d*24 + t[1]/60 + t[2]/3600
        return 0
      }
      NF >= 4 && $1 != "" && $1 != "User" && $4 ~ /^[0-9]{4}-/ {
        eh = elapsed_hours($2)
        if (eh >= 0) printf "INT\t%s\t%.0f\n", $4, eh * 3600
        n = parse_tres($3)
        if (n > 0) { h = elapsed_hours($2); gpu_hrs[$1] += n * h }
      }
      END { for (u in gpu_hrs) printf "%s\t%.2f\n", u, gpu_hrs[u] }
    ' > "$mixed"

  # User rows are exactly two tab fields (user, GPU hours). INT rows are three fields (INT, Start, sec)—exclude those.
  awk -F'\t' 'NF == 2 && $2 ~ /^-?[0-9]+\.?[0-9]*$/ { print }' "$mixed" > "$tmp"
  if grep -q '^INT\t' "$mixed" 2>/dev/null; then
    if command -v python3 &>/dev/null; then
      grep '^INT\t' "$mixed" | python3 -c '
import sys
from datetime import datetime, timezone
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 3:
        continue
    start, es = parts[1], parts[2]
    s = start.strip()
    if s.endswith(".UTC"):
        s = s[:-4].strip()
    s = s.replace("T", " ")
    dt = None
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S.%f", 26)):
        if len(s) >= n:
            try:
                dt = datetime.strptime(s[:n], fmt)
                break
            except ValueError:
                pass
    if dt is None and len(s) >= 10:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            continue
    if dt is None:
        continue
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = int(dt.timestamp())
    end_e = epoch + int(float(es))
    print(epoch, end_e)
' > "${intervals_file}"
    else
      while IFS= read -r line; do
        [[ "$line" != INT$'\t'* ]] && continue
        start="${line#INT$'\t'}"
        start="${start%%$'\t'*}"
        elapsed_sec="${line#*$'\t'}"
        elapsed_sec="${elapsed_sec#*$'\t'}"
        start_clean="${start%.*}"
        start_epoch=$(date -d "$start_clean" +%s 2>/dev/null) || start_epoch=""
        if [[ -n "$start_epoch" && -n "$elapsed_sec" ]]; then
          end_epoch=$((start_epoch + elapsed_sec))
          printf "%s %s\n" "$start_epoch" "$end_epoch" >> "${intervals_file}"
        fi
      done < <(grep '^INT\t' "$mixed")
    fi
  fi
  rm -f "$mixed"

  sort -t$'\t' -k2 -nr "$tmp" -o "$tmp"
    if [[ -s "${intervals_file:-}" ]] && [[ -n "$tmp_cluster" ]]; then
      sort -n "${intervals_file}" | awk '
        { starts[NR]=$1; ends[NR]=$2 }
        END {
          n = NR
          for (i=1;i<=n;i++) ord[i]=i
          for (i=1;i<=n;i++) for (j=i+1;j<=n;j++) if (starts[ord[j]]<starts[ord[i]]) { t=ord[i]; ord[i]=ord[j]; ord[j]=t }
          busy_sec=0; cur_end=0
          for (i=1;i<=n;i++) {
            s=starts[ord[i]]; e=ends[ord[i]]
            if (s>cur_end) { busy_sec+=e-s; cur_end=e }
            else if (e>cur_end) { busy_sec+=e-cur_end; cur_end=e }
          }
          period_sec = cur_end - starts[ord[1]]
          idle_sec = period_sec - busy_sec
          if (idle_sec<0) idle_sec=0
          printf "\nCLUSTER_TIME\t%.2f\n", period_sec/3600
          printf "CLUSTER_BUSY\t%.2f\n", busy_sec/3600
          printf "CLUSTER_IDLE\t%.2f\n", idle_sec/3600
        }
      ' > "$tmp_cluster"
      cat "$tmp" "$tmp_cluster" > "${tmp}.full" && mv "${tmp}.full" "$tmp"
    fi

  # Split table vs cluster summary (lines with CLUSTER_ prefix)
  local table_lines=""
  local cluster_busy="" cluster_idle="" cluster_time=""
  while IFS=$'\t' read -r k v; do
    case "$k" in
      CLUSTER_TIME) cluster_time="$v" ;;
      CLUSTER_BUSY) cluster_busy="$v" ;;
      CLUSTER_IDLE) cluster_idle="$v" ;;
      *) [[ -n "$k" ]] && table_lines="${table_lines}${k}\t${v}\n" ;;
    esac
  done < "$tmp"

  printf "%-24s %12s\n" "USER" "GPU_HOURS"
  printf "%-24s %12s\n" "----" "--------"
  total=0
  echo -e "$table_lines" | while IFS=$'\t' read -r u gh; do
    [[ -z "$u" ]] && continue
    [[ "$gh" =~ ^-?[0-9]+\.?[0-9]*$ ]] || continue
    printf "%-24s %12.2f\n" "$u" "$gh"
    total=$(awk -v t="$total" -v g="$gh" 'BEGIN { printf "%.2f", t + g }')
  done
  printf "%-24s %12s\n" "----" "--------"
  total=$(echo -e "$table_lines" | awk -F'\t' '{ sum += $2 } END { printf "%.2f", sum }')
  printf "%-24s %12.2f\n" "TOTAL" "$total"

  if [[ -n "$cluster_time" ]]; then
    printf "\n%-24s %12s\n" "Cluster period (hours):" "$cluster_time"
    printf "%-24s %12s\n" "Cluster busy (hours):" "$cluster_busy"
    printf "%-24s %12s\n" "Cluster idle (hours):" "$cluster_idle"

    # Optional: idle GPU-hours based on total GPU capacity (derived from sinfo).
    local total_gpus
    total_gpus=$(get_total_gpus || true)
    if [[ -n "$total_gpus" && "$total_gpus" =~ ^[0-9]+$ && "$total_gpus" -gt 0 ]]; then
      gpu_capacity=$(awk -v g="$total_gpus" -v h="$cluster_time" 'BEGIN { printf "%.2f", g * h }')
      idle_gpu=$(awk -v cap="$gpu_capacity" -v used="$total" 'BEGIN { x = cap - used; if (x < 0) x = 0; printf "%.2f", x }')
      util_pct=$(awk -v cap="$gpu_capacity" -v used="$total" 'BEGIN { if (cap <= 0) { printf "0.0" } else { printf "%.1f", (used / cap) * 100 } }')
      printf "\n%-24s %12s\n" "GPU capacity (GPU-h):" "$gpu_capacity"
      printf "%-24s %12s\n" "Used GPU-hours:" "$total"
      printf "%-24s %12s\n" "Idle GPU-hours:" "$idle_gpu"
      printf "%-24s %12s\n" "GPU util (% of cap):" "$util_pct"
    fi
  fi
}

main "$@"
