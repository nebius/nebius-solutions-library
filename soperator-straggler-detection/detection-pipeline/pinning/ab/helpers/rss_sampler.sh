#!/bin/bash
# 60s RSS time series of the pipeline's long-lived processes. Usage: rss_sampler.sh OUT SECONDS
OUT=$1; END=$(( $(date +%s) + $2 ))
while [ $(date +%s) -lt $END ]; do
  t=$(date -u +%FT%TZ)
  for p in $(pgrep -f "alerting/alert_engine.py"); do echo "$t alert_engine $(ps -o rss= -p $p)" >> $OUT; done
  for p in $(pgrep -f "vm-standalone/victoria-metrics-prod"); do echo "$t victoria $(ps -o rss= -p $p)" >> $OUT; done
  for w in worker-0 worker-1; do ssh -o BatchMode=yes $w 'for p in $(pgrep -f "python3.*node_aggregator_ref.py"); do echo "'$t' aggregator@$(hostname) $(ps -o rss= -p $p)"; done' >> $OUT 2>/dev/null; done
  sleep 60
done
