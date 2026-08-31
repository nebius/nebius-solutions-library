rule "terraform_required_version" {
  enabled = false
}

rule "terraform_required_providers" {
  enabled = true

  source = true
  version = false
}
