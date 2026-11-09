# The fleet's router key (control cluster). The API registers every OpenAI endpoint it defines as a LiteLLM
# model group whose deployment is the endpoint's gateway hostname, and the edge on those hostnames accepts
# only a key of the fleet: this key is that caller. Role `router` in the fleet database (services/api/keys.py
# `ensure_router`, at every API start): every model, no budget, never billed, accepted by the gateway's
# authorize hook only; LiteLLM applies the real caller's key (budget, rate limit, spend) before it routes to
# the group (docs/API.md "Model groups"). Value generated here, Secret api/router-key (ROUTER_KEY, stage.tf);
# rotation: `-replace=random_password.router_key` and a restart of the API (docs/OPERATIONS.md).
resource "random_password" "router_key" {
  count   = local.role.control ? 1 : 0
  length  = 40
  special = false
}

resource "kubernetes_secret_v1" "router_key" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "router-key"
    namespace = "api"
  }
  data       = { key = "sk-${random_password.router_key[0].result}" }
  depends_on = [kubernetes_namespace_v1.ns]
}
