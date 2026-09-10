resource "nebius_vpc_v1_network" "network" {
  name      = var.vpc_name
  parent_id = var.parent_id
}

#This subnet is used to house the gateway server
resource "nebius_vpc_v1_subnet" "gateway_subnet" {
  name          = "${var.vpc_name}-subnet-gateway"
  parent_id     = var.parent_id
  network_id    = nebius_vpc_v1_network.network.id

  #ipv4_private_pools = {
  #  pools = [
  #    {
  #      cidrs = [
  #        { cidr = local.gateway_subnet_cidr }
  #      ]
  #    }
  #  ]
  #}
}

#This is the private subnet
resource "nebius_vpc_v1_subnet" "private_subnet" {
  name          = "${var.vpc_name}-subnet-private"
  parent_id     = var.parent_id
  network_id    = nebius_vpc_v1_network.network.id

  route_table_id  = nebius_vpc_v1_route_table.gateway_route_table.id

  #ipv4_private_pools = {
  #  pools = [
  #    {
  #      cidrs = [
  #        { cidr = local.private_subnet_cidr }
  #      ]
  #    }
  #  ]
  #}

  ipv4_public_pools = {}
}

#Private ip allocation for gateway
resource "nebius_vpc_v1_allocation" "gateway_allocation" {
  parent_id = var.parent_id
  name = "${var.vpc_name}-gateway-allocation"
  ipv4_private = {
    cidr      = "/32"
    subnet_id = nebius_vpc_v1_subnet.gateway_subnet.id
  }
}

#Route table
resource "nebius_vpc_v1_route_table" "gateway_route_table" {
  parent_id   = var.parent_id
  name        = "${var.vpc_name}-gateway-route-table"
  network_id  = nebius_vpc_v1_network.network.id
}

#Route to gateway
resource "nebius_vpc_v1_route" "gateway_route" {
  parent_id   = nebius_vpc_v1_route_table.gateway_route_table.id
  name        = "route-to-gw"

  destination = {
    cidr  = "0.0.0.0/0"
  }

  next_hop  = {
    allocation = {
      id  = nebius_vpc_v1_allocation.gateway_allocation.id
    }
  }
}