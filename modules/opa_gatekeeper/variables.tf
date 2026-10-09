variable "configs" {
  description = "A YAML file representing a config manifest for Gatekeeper"
  type        = string
  default     = ""
}

variable "gk_version" {
  description = "A gatekeeper version string"
  type        = string
  default     = "v3.21.0"
}

