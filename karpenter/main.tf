module "karpenter" {
  source = "./module"

  iam_project_id = "project-e02yjrzten1056mwwf7gvc"
  vpc_subnet_id = "vpcsubnet-e02f26c1f493zyb2s8"

  providers = {
    nebius = nebius
  }
}

