# Global
variable "tenant_id" {
  description = "Tenant ID."
  type        = string
}

variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID."
  type        = string
}

variable "region" {
  description = "The current region."
  type        = string
}

# Helm
variable "iam_token" {
  description = "Token for Helm provider authentication. (source environment.sh)"
  type        = string
}
