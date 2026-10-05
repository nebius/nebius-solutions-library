variable "kube_sched_ver" {
  description = "Full kube-scheduler patch version. It should not be newer than the Kubernetes API server version."
  type        = string
}
