# Unit tests for the cluster-size dispatch.
# Pure module, no providers: run with `terraform test` from this module dir:
#   cd soperator/modules/sizing_tier && terraform init && terraform test

# Derivation boundaries.

run "zero_workers_is_xs" {
  command = apply
  variables { worker_count = 0 }
  assert {
    condition     = output.sizing_tier == "XS"
    error_message = "0 workers must derive XS, got ${output.sizing_tier}"
  }
}

run "one_worker_is_xs" {
  command = apply
  variables { worker_count = 1 }
  assert {
    condition     = output.sizing_tier == "XS"
    error_message = "1 worker must derive XS, got ${output.sizing_tier}"
  }
}

run "nine_workers_is_xs" {
  command = apply
  variables { worker_count = 9 }
  assert {
    condition     = output.sizing_tier == "XS"
    error_message = "9 workers must derive XS, got ${output.sizing_tier}"
  }
}

run "ten_workers_is_s" {
  command = apply
  variables { worker_count = 10 }
  assert {
    condition     = output.sizing_tier == "S"
    error_message = "10 workers must derive S, got ${output.sizing_tier}"
  }
}

run "ninety_nine_workers_is_s" {
  command = apply
  variables { worker_count = 99 }
  assert {
    condition     = output.sizing_tier == "S"
    error_message = "99 workers must derive S, got ${output.sizing_tier}"
  }
}

run "hundred_workers_is_m" {
  command = apply
  variables { worker_count = 100 }
  assert {
    condition     = output.sizing_tier == "M"
    error_message = "100 workers must derive M, got ${output.sizing_tier}"
  }
  assert {
    condition     = output.node_preset.controller == "16vcpu-64gb" && output.node_preset.accounting == "16vcpu-64gb" && output.node_preset.nfs == "32vcpu-128gb"
    error_message = "M node presets must be the mid-size presets"
  }
}

run "four_ninety_nine_workers_is_m" {
  command = apply
  variables { worker_count = 499 }
  assert {
    condition     = output.sizing_tier == "M"
    error_message = "499 workers must derive M, got ${output.sizing_tier}"
  }
}

run "five_hundred_workers_is_l" {
  command = apply
  variables { worker_count = 500 }
  assert {
    condition     = output.sizing_tier == "L"
    error_message = "500 workers must derive L, got ${output.sizing_tier}"
  }
  assert {
    condition     = output.preset.soperator_main_controller.limits.memory == 3 && output.preset.soperator_checks_controller.limits.memory == 4
    error_message = "L must expose 3Gi main / 4Gi checks controller memory"
  }
  assert {
    condition     = output.preset.vm_single.size == "1023Gi"
    error_message = "L vm_single size must be 1023Gi (93x11, IO M3 granularity)"
  }
}

run "nineteen_ninety_nine_workers_is_l" {
  command = apply
  variables { worker_count = 1999 }
  assert {
    condition     = output.sizing_tier == "L"
    error_message = "1999 workers must derive L, got ${output.sizing_tier}"
  }
}

run "two_thousand_workers_is_xl" {
  command = apply
  variables { worker_count = 2000 }
  assert {
    condition     = output.sizing_tier == "XL"
    error_message = "2000 workers must derive XL, got ${output.sizing_tier}"
  }
}

run "xl_is_open_ended" {
  command = apply
  variables { worker_count = 5000 }
  assert {
    condition     = output.sizing_tier == "XL"
    error_message = "5000 workers must derive XL (top tier, no upper bound), got ${output.sizing_tier}"
  }
}

# Override precedence.

run "override_forces_tier" {
  command = apply
  variables {
    worker_count         = 6 # would derive XS
    sizing_tier_override = "XL"
  }
  assert {
    condition     = output.sizing_tier == "XL"
    error_message = "sizing_tier_override must win over the derived tier"
  }
  assert {
    condition     = output.preset.rest.cpu == 20
    error_message = "forced XL must expose the XL rest preset"
  }
  assert {
    condition     = output.node_preset.system == "64vcpu-256gb" && output.node_preset.nfs == "128vcpu-512gb"
    error_message = "forced XL must expose the XL node presets"
  }
  assert {
    condition     = output.preset.soperator_main_controller.limits.memory == 8 && output.preset.soperator_checks_controller.limits.memory == 10
    error_message = "forced XL must expose 8Gi main / 10Gi checks controller memory (the previous 4Gi OOM-ed on big clusters)"
  }
}

run "component_override_wins_over_tier" {
  command = apply
  variables {
    worker_count = 5 # derives XS
    component_overrides = {
      rest                        = { cpu = 20, memory = 120, ephemeral_storage = 5 }
      vm_single                   = { cpu = "25000m", memory = "24Gi", size = "558Gi", gomaxprocs = 25 }
      logs_collector              = { memory = "1Gi", cpu = "500m" }
      jail_logs_collector         = { memory = "1Gi", cpu = "1000m" }
      kube_state_metrics          = { requests = { cpu = "300m", memory = "2048Mi" }, limits = { memory = "4096Mi" } }
      soperator_checks_controller = { requests = { cpu = 2, memory = 6 }, limits = { memory = 6 } }
    }
  }
  assert {
    condition     = output.preset.rest.cpu == 20 && output.preset.rest.memory == 120
    error_message = "component_overrides.rest must replace the XS tier value"
  }
  assert {
    condition     = output.preset.vm_single.cpu == "25000m" && output.preset.vm_single.gomaxprocs == 25
    error_message = "component_overrides.vm_single must replace the XS tier value"
  }
  assert {
    condition     = output.preset.mariadb.memory == 12
    error_message = "components without an override must keep their tier (XS) values"
  }
  assert {
    condition     = output.preset.logs_collector.memory == "1Gi"
    error_message = "component_overrides.logs_collector must replace the constant"
  }
  assert {
    condition     = output.preset.jail_logs_collector.memory == "1Gi" && output.preset.jail_logs_collector.cpu == "1000m"
    error_message = "component_overrides.jail_logs_collector must replace the constant"
  }
  assert {
    condition     = output.preset.kube_state_metrics.requests.memory == "2048Mi" && output.preset.kube_state_metrics.limits.memory == "4096Mi"
    error_message = "component_overrides.kube_state_metrics must replace the XS tier value"
  }
  assert {
    condition     = output.preset.kruise_daemon.memory == 0.128
    error_message = "constant components without an override must keep their constant values"
  }
  assert {
    condition     = output.preset.node_configurator.requests.memory == 0.25
    error_message = "node_configurator without an override must keep its constant value"
  }
  assert {
    condition     = output.preset.soperator_checks_controller.requests.cpu == 2 && output.preset.soperator_checks_controller.limits.memory == 6
    error_message = "component_overrides.soperator_checks_controller must replace the XS tier value"
  }
  assert {
    condition     = output.preset.soperator_main_controller.limits.memory == 1
    error_message = "soperator_main_controller without an override must keep its tier (XS) value"
  }
}

run "vm_single_size_override_must_be_io_m3_multiple" {
  command = plan
  variables {
    worker_count = 5
    component_overrides = {
      vm_single = { cpu = "6000m", memory = "24Gi", size = "512Gi", gomaxprocs = 6 } # not a multiple of 93
    }
  }
  expect_failures = [var.component_overrides]
}

run "constants_do_not_scale_with_tier" {
  command = apply
  variables { worker_count = 6000 } # XL
  assert {
    condition     = output.preset.kruise_daemon.cpu == 0.05 && output.preset.kruise_daemon.memory == 0.128
    error_message = "kruise_daemon must stay constant at XL"
  }
  assert {
    condition     = output.preset.logs_collector.memory == "200Mi" && output.preset.logs_collector.cpu == "50m"
    error_message = "logs_collector must stay constant at XL"
  }
  assert {
    condition     = output.preset.jail_logs_collector.memory == "512Mi" && output.preset.jail_logs_collector.cpu == "200m"
    error_message = "jail_logs_collector must keep the shared 512Mi memory and 200m CPU preset at XL"
  }
  assert {
    condition     = output.preset.node_configurator.requests.cpu == 0.5 && output.preset.node_configurator.requests.memory == 0.25 && output.preset.node_configurator.limits.memory == 0.25
    error_message = "node_configurator must stay constant at XL"
  }
}

# Back-compat: XS == legacy defaults.

run "xs_matches_legacy_defaults" {
  command = apply
  variables { worker_count = 5 }
  assert {
    condition     = output.sizing_tier == "XS"
    error_message = "5 workers must derive XS"
  }
  assert {
    condition     = output.preset.rest.cpu == 2 && output.preset.rest.memory == 8
    error_message = "XS rest must equal the legacy default 2cpu/8Gi"
  }
  assert {
    condition     = output.preset.mariadb.memory == 12
    error_message = "XS mariadb memory must equal the legacy default 12Gi"
  }
  assert {
    condition     = output.preset.vm_single.cpu == "6000m" && output.preset.vm_single.gomaxprocs == 6
    error_message = "XS vm_single must equal the legacy default 6000m / gomaxprocs 6"
  }
  assert {
    condition     = output.preset.vm_single.size == "558Gi"
    error_message = "XS vm_single size must be 558Gi (93x6, IO M3 granularity)"
  }
  assert {
    condition     = output.preset.vm_agent.memory == "10Gi" && output.preset.vm_agent.cpu == "5000m"
    error_message = "XS vm_agent must equal the legacy default 10Gi / 5000m"
  }
  assert {
    condition     = output.preset.events_collector.memory == "128Mi"
    error_message = "XS events_collector must equal the legacy default 128Mi"
  }
  assert {
    condition     = output.node_preset.controller == "16vcpu-64gb" && output.node_preset.accounting == "8vcpu-32gb"
    error_message = "XS node presets must be the small-cluster presets"
  }
}

# Scrape-size caps follow the tier.

run "small_tiers_keep_global_scrape_guard" {
  command = apply
  variables { worker_count = 99 } # S
  assert {
    condition     = output.kube_state_metrics_max_scrape_size == null
    error_message = "S must keep the global 32MiB scrape guard (null cap)"
  }
}

run "m_raises_ksm_scrape_cap" {
  command = apply
  variables { worker_count = 100 } # M
  assert {
    condition     = output.kube_state_metrics_max_scrape_size == 134217728
    error_message = "M must raise the kube-state-metrics scrape cap to 128MiB"
  }
  assert {
    condition     = output.preset.kube_state_metrics.requests.memory == "1024Mi" && output.preset.kube_state_metrics.requests.cpu == "200m"
    error_message = "M must expose the 1Gi/200m kube-state-metrics preset"
  }
}

run "l_raises_ksm_scrape_cap" {
  command = apply
  variables { worker_count = 500 } # L
  assert {
    condition     = output.kube_state_metrics_max_scrape_size == 268435456
    error_message = "L must raise the kube-state-metrics scrape cap to 256MiB"
  }
}

run "forced_xl_raises_ksm_scrape_cap" {
  command = apply
  variables {
    worker_count         = 6 # would derive XS
    sizing_tier_override = "XL"
  }
  assert {
    condition     = output.kube_state_metrics_max_scrape_size == 536870912
    error_message = "forced XL must expose the 512MiB XL kube-state-metrics scrape cap"
  }
  assert {
    condition     = output.preset.kube_state_metrics.requests.memory == "6144Mi" && output.preset.kube_state_metrics.limits.memory == "12288Mi"
    error_message = "forced XL must expose the 6Gi/12Gi kube-state-metrics preset"
  }
}

# Capacity: presets must fit the nodes they are placed on, in every tier.

run "capacity_fits_all_tiers" {
  command = apply
  variables { worker_count = 5 }
  assert {
    condition     = length(output.capacity_violations) == 0
    error_message = "presets no longer fit their nodes: ${join("; ", output.capacity_violations)}"
  }
  assert {
    condition     = output.system_nodes_needed >= 1
    error_message = "system_nodes_needed must be at least 1"
  }
}

run "xl_system_pods_need_few_nodes" {
  command = apply
  variables { worker_count = 6000 }
  assert {
    # The XL column is the fattest: its system-bound singletons must still pack
    # into a handful of system nodes (the example ships min_size = 3).
    condition     = output.system_nodes_needed <= 3
    error_message = "XL system-bound components need ${output.system_nodes_needed} system nodes; expected <= 3 - retune the table or the system node preset"
  }
}

# Top-end PoC anchors.

run "xl_matches_poc_numbers" {
  command = apply
  variables { worker_count = 6000 }
  assert {
    condition     = output.preset.rest.cpu == 20 && output.preset.rest.memory == 120
    error_message = "XL rest must be the 20cpu/120Gi PoC value"
  }
  assert {
    condition     = output.preset.vm_single.cpu == "25000m" && output.preset.vm_single.gomaxprocs == 25
    error_message = "XL vm_single must be 25000m / gomaxprocs 25"
  }
  assert {
    condition     = output.preset.vm_single.size == "2046Gi"
    error_message = "XL vm_single size must be 2046Gi (93x22, IO M3 granularity)"
  }
  assert {
    condition     = output.preset.vm_agent.memory == "25Gi" && output.preset.vm_agent.cpu == "12000m"
    error_message = "XL vm_agent must be 25Gi / 12000m"
  }
  assert {
    condition     = output.preset.mariadb.memory == 48
    error_message = "XL mariadb memory must be 48Gi"
  }
  assert {
    # The controller intentionally does NOT grow with the tier: the PoC showed
    # no benefit from larger controller presets even at 5k workers.
    condition     = output.node_preset.controller == "32vcpu-128gb" && output.node_preset.accounting == "32vcpu-128gb" && output.node_preset.nfs == "128vcpu-512gb"
    error_message = "XL node presets must be the big-cluster presets (controller stays 16vcpu-64gb)"
  }
}
