# Standalone Grafana — real, enforced-auth launch

**Use this. Do not reuse an existing Grafana instance whose auth posture
you haven't confirmed.** This project's own Stage 3 validation found a
real, live example of exactly why: the instance tested there had
anonymous Admin access enabled — a drifted, pre-existing dev artifact
dated three weeks before this packaging effort even started (confirmed
via its own file mtime), not a reflection of anything install.sh/run.sh
ever configured. Neither script has ever launched or managed a Grafana
instance's own config — they only detect a reachable instance and
generate ready-to-use provisioning/auth config for one, matching this
project's own already-established VictoriaMetrics precedent (see
`../vm-standalone/README.md`): a real, official binary, launched as a
plain process, not committed into this repo.

## What this is

A single, official upstream Grafana binary (confirmed version: `grafana
version 11.5.1`, via `./bin/grafana --version` — download the matching or
newer release directly from
[Grafana's own releases](https://grafana.com/grafana/download); **do not
commit a prebuilt binary into this repo**, same reasoning as
`vm-standalone/README.md`'s own note). No install, no daemon, no systemd
unit required — launched directly, reading/writing real files under its
own `-homepath`.

## Real, confirmed launch command

`install.sh` generates everything this launch needs but does not run
Grafana itself (a real deployment/lifecycle decision, left to the
operator — same scope boundary as VictoriaMetrics):

- `var/grafana_auth_generated.ini` — real, enforced auth: anonymous access
  explicitly disabled, a real random admin password (never a hardcoded
  default; see `var/grafana_admin_credentials.txt` to retrieve it).
- `var/grafana_provisioning_generated/` — real datasource/dashboard
  provisioning pointed at this cluster's own real `VM_URL` and this
  package's own real installed path.

**On a genuinely fresh instance** (a NEW, empty data directory — Grafana
only applies `[security] admin_user`/`admin_password` on a data
directory's own first start; on an already-initialized one, use
`grafana-cli admin reset-admin-password <new password>` instead), combine
these into one config and launch:

```bash
mkdir -p /path/to/grafana-standalone/data
cat > /path/to/grafana-standalone/custom.ini <<'BASE'
[server]
http_port = 3000
[paths]
data = /path/to/grafana-standalone/data
BASE
cat /path/to/detection-pipeline/var/grafana_auth_generated.ini >> /path/to/grafana-standalone/custom.ini

/path/to/grafana-11.5.1/bin/grafana server \
  --config=/path/to/grafana-standalone/custom.ini \
  --homepath=/path/to/grafana-11.5.1 &
```

Then point that instance's own provisioning config at
`var/grafana_provisioning_generated/` (or copy those files into place —
see `install.sh`'s own Step 4.5 comment for why this script doesn't
assume a path there either).

## Verifying real auth is enforced (do this every time, not just once)

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:3000/api/org
# 200 == anonymous access is enabled (WRONG -- do not accept this)
# 401/302 == real auth is enforced (correct)
```

`install.sh` performs exactly this check live against whatever instance
`GRAFANA_URL` in `cluster.env` resolves to, and refuses to claim
"auth enforced" without it — see its own `GRAFANA_ANON_ENABLED` field.

## Retrieving the real admin password

```bash
cat /path/to/detection-pipeline/var/grafana_admin_credentials.txt
```

Restricted to this file's own owner (`chmod 600`, applied by
`install.sh`). Never printed in plaintext to a shared log — `run.sh`'s own
printed access instructions only ever reference this file's path, never
its contents.
