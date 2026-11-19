output "public_ip_cidrs" {
  description = "Entire stable public IP pool to allowlist, including the upgrade spare. Individual node assignments may change."
  value = sort(distinct(flatten([
    for pool in nebius_vpc_v1_subnet.gateway.status.ipv4_public_pools : pool.cidrs
  ])))
}

output "worker_route_table_id" {
  description = "Prepared route table. Attach ONLY after the allocation is bound to a working LB and NAT forwarding is verified."
  value       = nebius_vpc_v1_route_table.workers.id
  depends_on  = [nebius_vpc_v1_route.workers]
}
