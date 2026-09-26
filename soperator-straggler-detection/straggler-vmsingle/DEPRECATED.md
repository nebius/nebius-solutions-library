# DEPRECATED — use `../vm_standalone/` instead

This Helm chart's own `README.md` ("Validation performed" section)
already honestly discloses that it was never actually installed against
a real Kubernetes API — zero in-pod RBAC (`SelfSubjectRulesReview`
returned no permissions) on the cluster it was built for.

That same blocker was **re-checked and re-confirmed on the next cluster
this project migrated to** (the 6-node "soperator" cluster,
`migration_readiness.md` §3): identical result, zero resource
permissions, needs a real kubeconfig from a human operator — not
something obtainable from inside the pod, and not something to work
around (see `migration_readiness.md`'s own "Permanent note" on why
host-level login shells are never a legitimate substitute for a blocked
Kubernetes token).

Two clusters in a row confirming the same block is a real pattern, not
bad luck. This project's actual, live-validated detection backend for
every real test since is `../vm_standalone/` — a plain binary launched as
an ordinary Slurm job, needing no Kubernetes access at all. See its own
`README.md` for the exact working launch command.

**Keep this chart in the package** in case a future session gets real
kubeconfig access and wants to pursue the "shared/centralized Grafana"
integration this chart was originally built for (see
`straggler_dectection_history.md`, Phase 12) — but do not spend more
setup time on it as the default path. Start with `vm_standalone/`.
