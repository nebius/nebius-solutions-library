# Module `config`

The one input of the solution and its derived view. `variables.tf` is the schema of `fleet`
(every key of `terraform.tfvars`, with defaults and validations), `locals.tf` derives what the stages
need from it (the cluster list and roles, the hub, the state bucket, the fleet document the charts read,
the model catalog per cluster, the apply order), and `outputs.tf` hands all of that back.

It is used in two ways:

- as a module: every stage calls it (`stack/<stage>/config.tf`) and reads `module.config.*`, so the
  schema exists once and no stage carries a copy;
- as a root for `terraform console`: `stack.sh` and `tools/check.sh` evaluate `local.plan`,
  `local.chart_fleet` and `local.catalog` over a tfvars file without any provider or state.

There are no resources in here.
