# Kubernetes NAT gateway

Creates a highly available NAT gateway for private Kubernetes workers. It routes
IPv4 internet traffic from Pods and nodes, including container image pulls,
through a fixed pool of public IP addresses.

The module creates gateway nodes in a separate subnet, an internal `LoadBalancer`
Service, and a route table for worker traffic.
A Cilium host policy blocks Internet access to gateway host services, including
SSH and the kubelet.

This protection requires Cilium host firewall, enabled by default on Managed
Kubernetes clusters created on or after April 17, 2025. For older clusters, follow
the [host firewall enablement instructions](https://docs.nebius.com/kubernetes/networking/add-ons#host-firewall).

Gateway nodes have the label
`nebius.com/exclude-from-implicit-load-balancer-selection=true`, which keeps them
out of other `LoadBalancer` Services that use default node selection. The NAT
Service uses `nebius.com/load-balancer-target-node-selector` with
`nebius.com/nat-gateway=true` to select only gateway nodes.

## Which gateway should I use?

Both options provide a reserved public IP pool for outbound traffic:

- **Cilium Egress Gateway:** Choose this to select which Pods and destinations
  use the gateway. It does not cover `hostNetwork` Pods, container image pulls
  or other traffic from the nodes themselves.
- **Kubernetes NAT gateway:** Choose this to route IPv4 internet traffic from
  an entire subnet, including regular Pods, `hostNetwork` Pods, worker nodes
  and standalone VMs.

## Usage

Follow the [k8s-training setup instructions](../../k8s-training/README.md). Add
these settings to `k8s-training/terraform.tfvars` before running `terraform apply`:

```hcl
enable_egress_gateway = false
enable_nat_gateway    = true
cpu_nodes_public_ips  = false
gpu_nodes_public_ips  = false
```

By default, the module creates two gateway nodes and reserves three public IPs,
including one spare for rolling upgrades. To change sizing, use the
[`nat_gateway` variable](../../k8s-training/variables.tf). Set
`public_ip_pool_size` to at least `node_count + 1`.

After `terraform apply` completes, run the remaining commands from the
`k8s-training` directory. View the NAT gateway outputs:

```sh
terraform output -json nat_gateway
```

For external services that restrict access by source IP, allowlist every address
in `public_ip_cidrs`, including the spare. Nodes can change addresses within
this reserved pool.

## Enable routing

Attach the new route table to the worker subnet to send IPv4 internet traffic
through the gateway. This applies to every resource in that subnet.

1. With `kubectl` connected to your cluster, wait for the gateway nodes to become
   ready and the internal load balancer to receive an address:

   ```sh
   kubectl wait node -l nebius.com/nat-gateway=true --for=condition=Ready --timeout=10m
   kubectl -n nat-gateway wait service/nat-gateway-service \
     --for=jsonpath='{.status.loadBalancer.ingress}' --timeout=10m
   ```

2. Save the worker subnet's current route table ID so you can undo the routing
   change if needed.

   ```sh
   WORKER_SUBNET_ID="<subnet_id used by k8s-training>"
   nebius vpc subnet get "$WORKER_SUBNET_ID" --format json |
     jq '.spec.route_table_id // ""'
   ```

   An empty string (`""`) means the subnet uses its network's default route table.

3. Get the NAT route table ID:

   ```sh
   NAT_ROUTE_TABLE_ID=$(terraform output -json nat_gateway |
     jq -r '.worker_route_table_id')
   ```

   This table contains the IPv4 default route (`0.0.0.0/0`) through the gateway.
   If the subnet uses other custom routes, add those routes to this table before
   attaching it.

4. Attach the NAT route table to the worker subnet. If another Terraform
   configuration manages this subnet, update its `route_table_id` there and apply
   it. Otherwise, run:

   ```sh
   nebius vpc subnet update "$WORKER_SUBNET_ID" \
     --route-table-id "$NAT_ROUTE_TABLE_ID"
   ```

5. On a private worker, run `curl -4 https://ifconfig.me/ip` and confirm the address
   belongs to `public_ip_cidrs`. Then start a Pod on a private worker using an
   uncached image to verify container image pulls.

## Undo the routing change

Before applying `enable_nat_gateway = false` or running `terraform destroy`,
restore the worker subnet's previous routing using the instructions below.

To restore the worker subnet's previous routing, use the route table ID saved in
step 2. Update the Terraform configuration that manages the subnet, or repeat the
subnet-update command with that ID. If step 2 returned `""`, use
`--route-table-id ""` to restore the network's default routing.

## References

- [Fixed public IP pools](https://docs.nebius.com/kubernetes/networking/defined-ips)
- [Custom NAT gateway and subnet routing](https://docs.nebius.com/vpc/routing/custom-nat-gateway)
- [Managed Kubernetes load balancers](https://docs.nebius.com/kubernetes/clusters/load-balancer)
