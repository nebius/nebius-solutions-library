locals {
  o11y_resources_name = join(
    "-",
    [
      var.company_name,
      var.iam_project_id,
    ]
  )
}
