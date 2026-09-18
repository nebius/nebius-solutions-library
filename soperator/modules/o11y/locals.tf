locals {
  o11y_resources_name = join(
    "-",
    [
      var.company_name,
      var.iam_project_id,
    ]
  )

  logs_public_endpoint = format("dns:///write.logging.%s.nebius.cloud:443", var.region)
}
