run "test_mode_bastion_apply" {
  command = apply

  variables {
    test_mode                                = true
    bastion_allowed_ssh_cidrs                = ["0.0.0.0/0"]
    bastion_allow_unrestricted_ingress_rules = true
    bastion_allowed_wireguard_cidrs          = ["0.0.0.0/0"]
    bastion_allowed_wireguard_ui_cidrs       = ["0.0.0.0/0"]
  }
}
