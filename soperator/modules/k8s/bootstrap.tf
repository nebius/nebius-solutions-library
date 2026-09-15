variable "node_groups_dependency" {
  description = "Optional bootstrap completion token; node groups start after it is available."
  type        = string
  default     = null
}

resource "terraform_data" "before_node_groups" {
  input = var.node_groups_dependency
}
