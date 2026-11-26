locals {
  otel_collectors = {
    logs = {
      suffix          = "logs"
      sizing_key      = "logs_collector"
      exporter        = "otlp_http/victoriametrics"
      pipeline        = "logs"
      state_directory = "/var/lib/otelcol"
    }
    jailLogs = {
      suffix          = "jail-logs"
      sizing_key      = "jail_logs_collector"
      exporter        = "otlp_http/victorialogs"
      pipeline        = "logs"
      state_directory = "/var/lib/otelcol-jail"
    }
    events = {
      suffix          = "events"
      sizing_key      = "events_collector"
      exporter        = "otlp_http/victoriametrics"
      pipeline        = "logs/events"
      state_directory = "/var/lib/otelcol-events"
    }
  }

  otel_cpu_requests = {
    for key in ["logs_collector", "jail_logs_collector", "events_collector", "nccl_profiles_collector"] :
    key => tostring(local.selected_preset[key].cpu)
  }
  otel_gomaxprocs = {
    for key, cpu in local.otel_cpu_requests :
    key => tostring(max(1, ceil(endswith(cpu, "m") ? tonumber(trimsuffix(cpu, "m")) / 1000 : tonumber(cpu))))
  }

  otel_batch_min_size = coalesce(try(var.opentelemetry_batch.send_batch_size, null), 2000)
  otel_queue_size     = coalesce(try(var.opentelemetry_sending_queue.size, null), 30000)
  otel_local_queue = {
    enabled       = true
    sizer         = "items"
    num_consumers = coalesce(try(var.opentelemetry_sending_queue.num_consumers, null), 10)
    queue_size    = max(local.otel_queue_size, local.otel_batch_min_size)
    batch = {
      sizer         = "items"
      flush_timeout = coalesce(try(var.opentelemetry_batch.timeout, null), "1s")
      min_size      = local.otel_batch_min_size
      max_size      = coalesce(try(var.opentelemetry_batch.send_batch_max_size, null), 5000)
    }
  }
  # Cloud Logging accepts at most 1000 records per request, independently of the local exporter batch.
  otel_public_queue = merge(local.otel_local_queue, {
    queue_size = max(local.otel_queue_size, 500)
    batch      = { sizer = "items", flush_timeout = "1s", min_size = 500, max_size = 1000 }
  })

  otel_collectors_yaml = templatefile("${path.module}/templates/helm_values/opentelemetry.yaml.tftpl", {
    enabled           = var.telemetry_enabled
    pod_monitor       = local.prometheus_crds_enabled
    public_enabled    = var.public_o11y_enabled
    version           = var.opentelemetry_collector_version
    cluster_name      = var.cluster_name
    region            = var.region
    collectors        = local.otel_collectors
    sizes             = local.selected_preset
    gomaxprocs        = local.otel_gomaxprocs
    local_queue       = local.otel_local_queue
    public_queue      = local.otel_public_queue
    delete_after_read = var.opentelemetry_delete_jail_logs_after_read
    delete_min_age    = var.opentelemetry_delete_jail_logs_min_age
  })
}
