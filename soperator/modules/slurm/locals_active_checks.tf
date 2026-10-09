locals {
  active_checks_scopes = {
    # Run only checks required for cluster initialization.
    skip_all = {
      ssh-check = {
        k8sJobSpec = {
          jobContainer = {
            env = [{
              name : "NUM_OF_LOGIN_NODES",
              value : tostring(var.node_count.login)
            }]
          }
        }
      }
      dcgmi-diag-r3 = {
        runAfterCreation = false
      }
      gpu-checks = {
        runAfterCreation = false
      }
      manage-jail-state = {
        runAfterCreation = false
      }
    }

    # Skip everything that can be skipped in production.
    essential = {
      ssh-check = {
        k8sJobSpec = {
          jobContainer = {
            env = [{
              name : "NUM_OF_LOGIN_NODES",
              value : tostring(var.node_count.login)
            }]
          }
        }
      }
      dcgmi-diag-r3 = {
        runAfterCreation = false
      }
      gpu-checks = {
        runAfterCreation = false
      }
      manage-jail-state = {
        runAfterCreation = true
      }
    }

    # Run short GPU health checks.
    prod_quick = {
      ssh-check = {
        k8sJobSpec = {
          jobContainer = {
            env = [{
              name : "NUM_OF_LOGIN_NODES",
              value : tostring(var.node_count.login)
            }]
          }
        }
      }
      dcgmi-diag-r3 = {
        runAfterCreation = false
      }
      gpu-checks = {
        runAfterCreation = true
      }
      manage-jail-state = {
        runAfterCreation = true
      }
    }

    # Run all available checks.
    prod_acceptance = {
      ssh-check = {
        k8sJobSpec = {
          jobContainer = {
            env = [{
              name : "NUM_OF_LOGIN_NODES",
              value : tostring(var.node_count.login)
            }]
          }
        }
      }
      dcgmi-diag-r3 = {
        runAfterCreation = true
      }
      gpu-checks = {
        runAfterCreation = true
      }
      manage-jail-state = {
        runAfterCreation = true
      }
    }
  }

  soperator_activechecks_override_yaml = yamlencode(local.active_checks_scopes[var.active_checks_scope])
}
