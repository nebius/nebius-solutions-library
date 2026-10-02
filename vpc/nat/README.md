# VPC configuration with NAT gateway and a private subnet

This Terraform solution deploys a VPC with two subnets. One of these is deployed without access to the public-ip-pool and as such can't connect to the internet directly. The other subnet houses a gateway instance to which all traffic from the private subnet is routed to, thus enabling the private subnet to access public internet via the gateway.

The file `test-instance.tf` contains configuration for deploying a test instance to the private subnet just to verify the configuration is working accordingly. You can remove this file or comment the contents to disable deploying the test instance.

With the default configuration, both the private and public subnets use the host network CIDR. If you want to enable specific CIDRs for these subnets, remove the comments from the subnet `ipv4_private_pools` configurations in `vpc.tf`. This will then default to using CIDRs configured in `locals.tf`. Feel free to edit the CIDRs found there, but make sure to keep the region specific CIDR blocks of `x.x.0.0/13`in place.

The recipe follows the manual configuration described here: https://docs.nebius.com/vpc/routing/custom-nat-gateway 