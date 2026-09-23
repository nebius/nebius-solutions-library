moved {
  from = kubernetes_deployment_v1.openfold3
  to   = kubernetes_deployment_v1.nims["openfold3"]
}

moved {
  from = kubernetes_deployment_v1.boltz2
  to   = kubernetes_deployment_v1.nims["boltz2"]
}

moved {
  from = kubernetes_deployment_v1.evo2_40b
  to   = kubernetes_deployment_v1.nims["evo2_40b"]
}

moved {
  from = kubernetes_deployment_v1.msa_search
  to   = kubernetes_deployment_v1.nims["msa_search"]
}

moved {
  from = kubernetes_deployment_v1.openfold2
  to   = kubernetes_deployment_v1.nims["openfold2"]
}

moved {
  from = kubernetes_deployment_v1.genmol
  to   = kubernetes_deployment_v1.nims["genmol"]
}

moved {
  from = kubernetes_deployment_v1.molmim
  to   = kubernetes_deployment_v1.nims["molmim"]
}

moved {
  from = kubernetes_deployment_v1.diffdock
  to   = kubernetes_deployment_v1.nims["diffdock"]
}

moved {
  from = kubernetes_deployment_v1.qwen3-next-80b-a3b-instruct
  to   = kubernetes_deployment_v1.nims["qwen3-next-80b-a3b-instruct"]
}

moved {
  from = kubernetes_deployment_v1.proteinmpnn
  to   = kubernetes_deployment_v1.nims["proteinmpnn"]
}

moved {
  from = kubernetes_deployment_v1.rfdiffusion
  to   = kubernetes_deployment_v1.nims["rfdiffusion"]
}

moved {
  from = kubernetes_deployment_v1.cosmos_reason1_7b
  to   = kubernetes_deployment_v1.nims["cosmos_reason1_7b"]
}

moved {
  from = kubernetes_deployment_v1.cosmos_reason2_8b
  to   = kubernetes_deployment_v1.nims["cosmos_reason2_8b"]
}

moved {
  from = kubernetes_deployment_v1.cosmos_reason2_2b
  to   = kubernetes_deployment_v1.nims["cosmos_reason2_2b"]
}

moved {
  from = kubernetes_deployment_v1.cosmos_embed1
  to   = kubernetes_deployment_v1.nims["cosmos_embed1"]
}

moved {
  from = kubernetes_deployment_v1.nemotron_nano_12b_v2_vl
  to   = kubernetes_deployment_v1.nims["nemotron_nano_12b_v2_vl"]
}

moved {
  from = kubernetes_service_v1.openfold3
  to   = kubernetes_service_v1.nims["openfold3"]
}

moved {
  from = kubernetes_service_v1.boltz2
  to   = kubernetes_service_v1.nims["boltz2"]
}

moved {
  from = kubernetes_service_v1.evo2_40b
  to   = kubernetes_service_v1.nims["evo2_40b"]
}

moved {
  from = kubernetes_service_v1.msa_search
  to   = kubernetes_service_v1.nims["msa_search"]
}

moved {
  from = kubernetes_service_v1.openfold2
  to   = kubernetes_service_v1.nims["openfold2"]
}

moved {
  from = kubernetes_service_v1.genmol
  to   = kubernetes_service_v1.nims["genmol"]
}

moved {
  from = kubernetes_service_v1.molmim
  to   = kubernetes_service_v1.nims["molmim"]
}

moved {
  from = kubernetes_service_v1.diffdock
  to   = kubernetes_service_v1.nims["diffdock"]
}

moved {
  from = kubernetes_service_v1.qwen3
  to   = kubernetes_service_v1.nims["qwen3-next-80b-a3b-instruct"]
}

moved {
  from = kubernetes_service_v1.proteinmpnn
  to   = kubernetes_service_v1.nims["proteinmpnn"]
}

moved {
  from = kubernetes_service_v1.rfdiffusion
  to   = kubernetes_service_v1.nims["rfdiffusion"]
}

moved {
  from = kubernetes_service_v1.cosmos_reason1_7b
  to   = kubernetes_service_v1.nims["cosmos_reason1_7b"]
}

moved {
  from = kubernetes_service_v1.cosmos_reason2_8b
  to   = kubernetes_service_v1.nims["cosmos_reason2_8b"]
}

moved {
  from = kubernetes_service_v1.cosmos_reason2_2b
  to   = kubernetes_service_v1.nims["cosmos_reason2_2b"]
}

moved {
  from = kubernetes_service_v1.cosmos_embed1
  to   = kubernetes_service_v1.nims["cosmos_embed1"]
}

moved {
  from = kubernetes_service_v1.nemotron_nano_12b_v2_vl
  to   = kubernetes_service_v1.nims["nemotron_nano_12b_v2_vl"]
}

moved {
  from = kubernetes_config_map_v1.nginx_tcp_proxy
  to   = kubernetes_config_map_v1.tcp_proxy["protein-apps"]
}

moved {
  from = kubernetes_deployment_v1.nginx_tcp_proxy
  to   = kubernetes_deployment_v1.tcp_proxy["protein-apps"]
}

moved {
  from = kubernetes_service_v1.nims_lb
  to   = kubernetes_service_v1.model_lbs["protein-apps"]
}

moved {
  from = kubernetes_config_map_v1.cosmos_tcp_proxy
  to   = kubernetes_config_map_v1.tcp_proxy["cosmos"]
}

moved {
  from = kubernetes_deployment_v1.cosmos_tcp_proxy
  to   = kubernetes_deployment_v1.tcp_proxy["cosmos"]
}

moved {
  from = kubernetes_service_v1.cosmos_lb
  to   = kubernetes_service_v1.model_lbs["cosmos"]
}

moved {
  from = kubernetes_deployment_v1.bionemo_notebook[0]
  to   = kubernetes_deployment_v1.bionemo_notebook["0"]
}

moved {
  from = kubernetes_service_v1.bionemo_public[0]
  to   = kubernetes_service_v1.bionemo_public["0"]
}
