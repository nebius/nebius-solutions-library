locals {
  active_checks_scopes = {
    # Scope for dev clusters
    dev = {
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
    }

    # Run what is relevant in E2E
    testing = {
      gpu-checks = {
        suspend          = true
        runAfterCreation = false
      }
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
    }
    # Check the provisioned cluster, but don't run health-checks that take long
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
    }

    # Run all available health-checks
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
    }

    # Skip most of checks and run only essential ones
    essential = {
      dcgmi-diag-r3 = {
        runAfterCreation = false
      }
      manage-jail-state = {
        runAfterCreation = false
      }
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
      gpu-checks = {
        runAfterCreation = false
      }
    }
  }

  soperator_activechecks_override_yaml = yamlencode(local.active_checks_scopes[var.active_checks_scope])
}
