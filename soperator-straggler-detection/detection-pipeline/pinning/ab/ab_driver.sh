#!/bin/bash
# ab_driver.sh -- interleaved A/B driver (NEW). Usage: ab_driver.sh TAG ROUNDS ITERS NCCL(host|2323) ARMS...
# ARMS from: off | on_nopin | on_dump | on_full. Order is rotated each round (round r starts at arm r mod N).
# Per run: 1 Hz per-CPU sampler on each worker; Inspector dumps deleted afterwards. Raw logs: runs/<TAG>_r<R>_<arm>/
TAG=$1; ROUNDS=$2; ITERS=$3; NCCL=$4; shift 4; ARMS=("$@"); N=${#ARMS[@]}
source /root/inspector-audit/run_cfg.sh
PKGP=$PKG/pinning
for ((r=1; r<=ROUNDS; r++)); do
  for ((i=0; i<N; i++)); do
    arm=${ARMS[$(( (i + r - 1) % N ))]}
    case $arm in off) insp=off; pin=off;; on_nopin) insp=on; pin=off;; on_dump) insp=on; pin=dump;; on_full) insp=on; pin=full;; esac
    name=${TAG}_r${r}_${arm}
    echo "$(date -u +%FT%TZ) START $name"
    # sampler start happens after the queue-wait inside run_one would be ideal; start just before and trim by t_start in analysis
    ( run_one $name $ITERS $insp $pin $NCCL ) &
    RP=$!
    while [ ! -f /root/inspector-audit/runs/$name/t_start ]; do sleep 1; done
    for w in worker-0 worker-1; do ssh -o BatchMode=yes $w "nohup python3 -I $PKGP/cpu_sampler.py /root/inspector-audit/runs/$name/cpu_$w.jsonl >/dev/null 2>&1 & echo \$! > /tmp/cpusamp_$name.pid"; done
    wait $RP
    for w in worker-0 worker-1; do ssh -o BatchMode=yes $w "kill \$(cat /tmp/cpusamp_$name.pid) 2>/dev/null; rm -rf /tmp/cpusamp_$name.pid /tmp/insp_dump_$name"; done
    echo "$(date -u +%FT%TZ) END $name"
  done
done
echo ALL_DONE
