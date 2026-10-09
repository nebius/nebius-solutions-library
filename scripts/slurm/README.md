# Slurm GPU Hours Report

Generates a table of **total GPU hours per user** on a Soperator cluster login node for a configurable date range.

## Requirements

- Slurm with accounting enabled (`sacct` available)
- GRES/GPU configured (jobs request GPUs via `--gres=gpu:N` or similar so `AllocTRES` is populated)
- Run on a cluster login node (or where `sacct` can reach the Slurm DB)
- **Python 3** (`python3`) recommended: converts job `Start` times to epoch in one pass for cluster busy/idle (fast). If `python3` is missing, the script falls back to one `date -d` per job, which can be very slow on large date ranges.

## Usage

```bash
# Date range (MM/DD/YYYY)
./calculate_usage.sh 01/01/2026 02/02/2026

# From a start date through now
./calculate_usage.sh 01/01/2026

# Last 365 days through now (default; no args)
./calculate_usage.sh
```

Dates are passed through to `sacct --starttime` / `--endtime`; other formats Slurm accepts (e.g. `YYYY-MM-DD`, `now`, `today`) also work.

## Output

Example:

```
USER                        GPU_HOURS
----                        --------
aaron                         6795.99
soperatorchecks               2133.26
root                           793.07
----                        --------
TOTAL                         9722.32

Cluster period (hours):        xxx.xx
Cluster busy (hours):          xxx.xx
Cluster idle (hours):          xxx.xx
GPU capacity (GPU-h):          YYY.YY
Used GPU-hours:                YYY.YY
Idle GPU-hours:                YYY.YY
GPU util (% of cap):            ZZ.Z
```

- **Cluster period**: first job start to last job end in the queried range.
- **Cluster busy**: sum of wall-clock time when at least one job was running (overlapping intervals merged).
- **Cluster idle**: period minus busy = time when no job was running.
- **GPU capacity**: `Cluster period × total GPUs`, where total GPUs are derived from `sinfo -N -o "%G"` by summing GPU counts across nodes.
- **Used GPU-hours**: same value as the `TOTAL` row in the table.
- **Idle GPU-hours**: capacity minus used (clamped at 0).
- **GPU util**: used / capacity, as a percentage.

## How it works

- Calls `sacct -a -X` for the given (or default) time window with `User`, `Elapsed`, and `AllocTRES`.
- Parses GPU count from `AllocTRES` (e.g. `gres/gpu=2` or `gres/gpu:2`).
- Converts `Elapsed` to hours and sums **GPU hours** = (GPUs × elapsed hours) per user.

## Getting more data from Slurm

If the report only shows recent GPU hours (e.g. a few days) even when you query a long range, Slurm is likely **purging old job records**. That is controlled on the **slurmdbd** host.

### Retain more job history (slurmdbd)

Edit **`slurmdbd.conf`** (often `/etc/slurm/slurmdbd.conf` or `/etc/slurmdbd.conf`) as root or Slurm admin:

| Parameter        | Meaning                    | Example to keep more data      |
|-----------------|----------------------------|--------------------------------|
| `PurgeJobAfter` | Age after which jobs are removed | `PurgeJobAfter=24month` or `365days`; omit to never purge |
| `PurgeStepAfter`| Same for job steps         | Same style as above            |

- **Increase retention:** set a longer value, e.g. `PurgeJobAfter=24month` or `PurgeJobAfter=365days`.
- **Keep everything:** comment out or remove `PurgeJobAfter` (and `PurgeStepAfter` if present); default is no purge.
- **Archive before purge:** set `ArchiveJobs=yes` (and `ArchiveDir=...`) so purged jobs are written to files instead of dropped; you can still lose them from the DB, but they’re stored for later analysis.

Restart **slurmdbd** after changes. Existing data already purged is gone unless you have archives or DB backups.

### File-based accounting

If the cluster uses **file** accounting (`AccountingStorageType=accounting_storage/filetxt` in `slurm.conf`), there is no slurmdbd purge; history is whatever is in the accounting file(s). If you still see short history, check that the file path is correct and that log rotation or other tools aren’t truncating it.

### Useful sacct checks

- See what date range has data:  
  `sacct -a -X --starttime=2026-02-01 --endtime=2026-02-25 --format=User,Start,End,Elapsed,AllocTRES | head -30`
- List all format fields:  
  `sacct -e`

## Notes

- The script counts `gres/gpu`, `gpu`, or `billing=` in AllocTRES. If your cluster uses another TRES name for GPUs, add it to `parse_gpu_count` in `calculate_usage.sh`.
- You may need to run as a user with permission to see all users’ jobs (e.g. Slurm admin or use `sacct -a` as allowed by your `slurmdbd`/PrivateData settings).
