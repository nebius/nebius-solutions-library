variable "number_of_hosts" {
  type        = number
  description = "Number of GPU hosts to test"
  default     = 0
}

variable "image" {
  type        = string
  description = "NCCL test container image. Override this for a platform-specific CUDA/NCCL combination."
  default     = "cr.eu-north1.nebius.cloud/nebius-benchmarks/nccl-tests:2.19.4-ubu22.04-cu12.2"

  validation {
    condition     = length(trimspace(var.image)) > 0
    error_message = "image must not be empty."
  }
}
