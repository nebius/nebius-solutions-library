output "controller_spool" {
  description = "Controller spool filestore."
  value       = local.controller_spool
}

output "jail" {
  description = "Jail filestore."
  value       = local.jail
}

output "jail_submounts" {
  description = "Jail submount filestores."
  value = { for submount in var.jail_submounts :
    submount.name => local.jail_submount[submount.name]
  }
}

output "accounting" {
  description = "Accounting storage filestore."
  value       = local.accounting
}
