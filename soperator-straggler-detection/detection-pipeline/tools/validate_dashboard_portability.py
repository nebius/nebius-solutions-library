#!/usr/bin/env python3
"""Replicates validateDashboardPortability's checks from
soperator/helm/soperator-monitoring-dashboards/templates/_helpers.tpl,
line by line, against a local dashboard JSON -- since real `helm template`
against the actual chart isn't available from here (no cluster access).
Exits non-zero and prints every failure found, exactly mirroring the
helm `fail()` calls in the template."""
import json
import sys

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


def validate_cluster_filtered_expressions(path, value):
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = f"{path}.{key}"
            if key == "expr":
                if 'cluster=~"$cluster"' not in str(nested):
                    errors.append(f'{path}: {nested_path} must filter metrics with cluster=~"$cluster"')
            else:
                validate_cluster_filtered_expressions(nested_path, nested)
    elif isinstance(value, list):
        for i, nested in enumerate(value):
            validate_cluster_filtered_expressions(f"{path}[{i}]", nested)


def validate(path, dashboard):
    variables = dashboard.get("templating", {}).get("list", [])
    check(len(variables) >= 3, f"{path}: expected $datasource, $o11y, and $cluster variables")
    if len(variables) < 3:
        return
    datasource, o11y, cluster = variables[0], variables[1], variables[2]

    check(datasource.get("name") == "datasource", f"{path}: first variable must be $datasource")
    check(datasource.get("type") == "datasource" and str(datasource.get("hide", 0)) == "2",
          f"{path}: $datasource must be a hidden datasource variable")
    check(datasource.get("query") == "prometheus", f"{path}: $datasource must select prometheus datasources")

    check(o11y.get("name") == "o11y", f"{path}: second variable must be $o11y")
    check(o11y.get("type") == "adhoc" and str(o11y.get("hide", 0)) == "2",
          f"{path}: $o11y must be a hidden adhoc variable")
    check(len(o11y.get("filters", [])) == 0 and len(o11y.get("baseFilters", [])) == 0,
          f"{path}: $o11y must have no in-cluster filters")
    check(o11y.get("datasource", {}).get("uid") == "${datasource}", f"{path}: $o11y must use ${{datasource}}")

    check(cluster.get("name") == "cluster", f"{path}: third variable must be $cluster")
    check(cluster.get("type") == "query" and str(cluster.get("hide", 0)) == "2",
          f"{path}: $cluster must be a hidden query variable")
    # Go template: `not (dig "includeAll" false $cluster)` -- sprig `not` is
    # generic truthiness (falsy: false/nil/0/""/empty collection), not a
    # strict bool-identity check. Matched here with Python truthiness
    # rather than `is True`, which would wrongly reject a truthy non-bool
    # value (e.g. a stray string "true") that the real template accepts.
    check(bool(cluster.get("includeAll", False)) and cluster.get("allValue") == ".*",
          f"{path}: $cluster must include All with the value .*")
    check(cluster.get("current", {}).get("value") == "$__all", f"{path}: $cluster must default to All")
    check(cluster.get("datasource", {}).get("uid") == "${datasource}", f"{path}: $cluster must use ${{datasource}}")

    for variable in variables:
        if variable.get("type") == "query" and variable.get("name") != "cluster":
            name = variable.get("name")
            check('cluster=~"$cluster"' in variable.get("definition", ""),
                  f'{path}: ${name} definition must filter metrics with cluster=~"$cluster"')
            check('cluster=~"$cluster"' in variable.get("query", {}).get("query", ""),
                  f'{path}: ${name} query must filter metrics with cluster=~"$cluster"')

    validate_cluster_filtered_expressions(path, dashboard)


if __name__ == "__main__":
    fp = sys.argv[1] if len(sys.argv) > 1 else "straggler_detection_metrics.json"
    d = json.load(open(fp))
    validate(fp, d)
    if errors:
        print(f"FAILED: {len(errors)} violation(s)")
        for e in errors:
            print(" -", e)
        sys.exit(1)
    else:
        print(f"PASSED: {fp} satisfies validateDashboardPortability's exact checks")
