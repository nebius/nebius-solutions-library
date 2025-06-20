# Creating a jump server with WireGuard installed on it

In Nebius AI Cloud, you can create a virtual machine (VM) with a [WireGuard](https://www.wireguard.com) image. This solution allows you to create a jump server between two zones:

* Secure zone that contains your VMs in Nebius AI Cloud
* Demilitarized zone (DMZ) that contains machines outside Nebius AI Cloud

By using a jump server as a virtual machine with WireGuard, DMZ machines can connect to VMs in the secure zone and share data in an encrypted form. Only one public IP address is required for the connection, which enables you to keep the number of available public IP addresses within a [quota](https://docs.nebius.com/compute/resources/quotas-limits#network).

To create the jump server, use Terraform manifests located in the current directory. For the instructions on how to deploy this solution, see the [Compute documentation](https://docs.nebius.com/compute/virtual-machines/wireguard).

