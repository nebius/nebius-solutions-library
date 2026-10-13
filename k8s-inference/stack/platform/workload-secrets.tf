# Reuse the Solutions Library's maintained ESO and Nebius MysteryBox integrations.
module "external_secrets" {
  count  = length(local.f.secrets.workload_secrets) > 0 ? 1 : 0
  source = "../../../modules/external-secrets-operator"
}

module "workload_secret" {
  for_each                                = local.f.secrets.workload_secrets
  source                                  = "../../../modules/external-secret-mysterybox"
  namespace                               = each.value.namespace
  create_namespace                        = each.value.create_namespace
  secret_store_name                       = "serverless-${each.key}"
  service_account_credentials_secret_name = local.f.secrets.mysterybox_credentials_secret
  service_account_credentials_secret_key  = "subject-credentials.json"
  target_secret_name                      = each.value.name
  mysterybox_secret_id                    = each.value.secret_id
  mysterybox_secret_version               = each.value.version
  depends_on                              = [module.external_secrets, kubernetes_namespace_v1.ns]
}
