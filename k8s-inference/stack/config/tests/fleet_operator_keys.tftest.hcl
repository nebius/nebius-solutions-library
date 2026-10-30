run "default_no_fleet_operator" {
  command = plan
  assert {
    condition     = length(output.fleet.fleet_operator_keys) == 0
    error_message = "Fleet operators must be explicit, never default tenant-admin elevation."
  }
}

run "explicit_operator_preserves_tenants" {
  command = plan
  variables {
    fleet = merge(var.fleet, { fleet_operator_keys = ["rene"] })
  }
  assert {
    condition     = output.fleet.fleet_operator_keys == toset(["rene"]) && keys(output.fleet.tenants) == keys(var.fleet.tenants)
    error_message = "An operator must not create a tenant or change tenant definitions."
  }
}

run "invalid_operator_name" {
  command = plan
  variables {
    fleet = merge(var.fleet, { fleet_operator_keys = ["not/a/name"] })
  }
  expect_failures = [var.fleet]
}

run "operator_alias_collision" {
  command = plan
  variables {
    fleet = merge(var.fleet, {
      fleet_operator_keys = ["rene"]
      tenants = merge(var.fleet.tenants, {
        platform = { keys = { operator-rene = {} } }
      })
    })
  }
  expect_failures = [var.fleet]
}
