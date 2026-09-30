# ssh_user_name = "bastion"
# ssh_public_key = {
#   key  = "put your public ssh key here"
#   path = "put path to public ssh key here"
# }

# Set this to allow SSH ingress through the managed security group.
# When unset or empty, no managed SSH ingress rule is created.
# bastion_allowed_ssh_cidrs = ["203.0.113.10/32"]

# Optional. Defaults to bastion_allowed_ssh_cidrs when unset.
# bastion_allowed_wireguard_cidrs = ["203.0.113.10/32"]
# bastion_allowed_wireguard_ui_cidrs = ["203.0.113.10/32"]

# Set true only for test environments where you intentionally need unrestricted ingress.
# bastion_allow_unrestricted_ingress_rules = false
