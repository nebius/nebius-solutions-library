# k8s-rbac-bindings

Manages Kubernetes RBAC bindings for Kubernetes clusters.

Use this module when Nebius IAM groups or other Kubernetes subjects need a
reviewable mapping to cluster-wide or namespace-scoped access, and you want
Terraform to own the RBAC resources instead of applying ad hoc `kubectl`
manifests.

## What this module manages

- Optional target namespaces
- Cluster-wide `ClusterRoleBinding` resources
- Namespace-scoped `RoleBinding` resources

## What this module does not manage

- Nebius IAM group membership
- Kubeconfig generation or distribution
- Bastion, VPN, or private endpoint tunneling
- Time-based expiry for temporary elevated access

## Inputs

- `default_labels`
- `namespaces`
- `cluster_role_bindings`
- `namespace_role_bindings`

## Outputs

- `namespaces`
- `cluster_role_bindings`
- `namespace_role_bindings`

## Example: cluster-admin access

```hcl
module "k8s_rbac_bindings" {
  source = "../../modules/k8s-rbac-bindings"

  cluster_role_bindings = {
    nebius_viewer_cluster_admin = {
      name      = "nebius-cluster-admin"
      role_name = "cluster-admin"
      subjects = [
        {
          kind      = "Group"
          name      = "nebius:viewer"
          api_group = "rbac.authorization.k8s.io"
        }
      ]
    }
  }

  providers = {
    kubernetes = kubernetes
  }
}
```

## Example: namespace-only access

```hcl
module "k8s_rbac_bindings" {
  source = "../../modules/k8s-rbac-bindings"

  namespaces = {
    workload = {
      name = "workload"
    }
  }

  namespace_role_bindings = {
    workload_admin = {
      name      = "workload-admin"
      namespace = "workload"
      role_kind = "ClusterRole"
      role_name = "admin"
      subjects = [
        {
          kind      = "Group"
          name      = "nebius:viewer"
          api_group = "rbac.authorization.k8s.io"
        }
      ]
    }
  }

  providers = {
    kubernetes = kubernetes
  }
}
```

## Example: read-only cluster visibility

```hcl
module "k8s_rbac_bindings" {
  source = "../../modules/k8s-rbac-bindings"

  cluster_role_bindings = {
    nebius_viewer_read_only = {
      name      = "nebius-viewer-read-only"
      role_name = "view"
      subjects = [
        {
          kind      = "Group"
          name      = "nebius:viewer"
          api_group = "rbac.authorization.k8s.io"
        }
      ]
    }
  }

  providers = {
    kubernetes = kubernetes
  }
}
```

## Temporary elevated access

Use the same `cluster_role_bindings` or `namespace_role_bindings` shape, but
keep the access grant explicit in the cluster Terraform configuration and
remove it when validation is complete. Terraform will then destroy the binding
on the next apply.

## Security notes

- Keep `cluster-admin` bindings opt-in and explicitly approved.
- Prefer namespace-scoped `admin`, `edit`, or `view` bindings when full cluster
  administration is not required.
- Keep Nebius IAM group membership changes outside this module so Kubernetes
  RBAC and identity lifecycle stay reviewable separately.
