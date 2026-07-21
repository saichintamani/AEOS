## AEOS Staging Environment
## Multi-AZ, on-demand nodes, mirrors production topology at reduced scale.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # Partial backend config. `bucket` is intentionally omitted: an S3 backend
  # cannot interpolate variables, so a hardcoded account-scoped name is a deploy
  # trap. Supply the real bucket at init time:
  #   terraform init -backend-config="bucket=aeos-tfstate-<ACCOUNT_ID>"
  # See infrastructure/README.md → "Bootstrap Terraform remote state".
  backend "s3" {
    key            = "staging/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "aeos-tflock"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

locals {
  environment = "staging"
  prefix      = "aeos-${local.environment}"

  tags = {
    Project     = "aeos"
    Environment = local.environment
    ManagedBy   = "terraform"
    Team        = "platform"
  }
}

module "vpc" {
  source = "../../modules/vpc"

  name         = local.prefix
  vpc_cidr     = var.vpc_cidr
  cluster_name = local.prefix
  tags         = local.tags
}

module "eks" {
  source = "../../modules/eks"

  cluster_name       = local.prefix
  kubernetes_version = "1.29"
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids

  endpoint_public_access = false   # Private endpoint only
  public_access_cidrs    = []

  general_instance_types = ["m6i.xlarge", "m6a.xlarge"]
  general_desired        = 3
  general_min            = 2
  general_max            = 8
  use_spot               = false

  tags = local.tags
}

module "iam" {
  source = "../../modules/iam"

  prefix            = local.prefix
  aws_region        = var.aws_region
  aws_account_id    = data.aws_caller_identity.current.account_id
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  artifacts_bucket  = module.s3.artifacts_bucket
  tags              = local.tags
}

module "ecr" {
  source = "../../modules/ecr"

  prefix        = "aeos"
  repositories  = ["api", "worker", "scheduler"]
  node_role_arn = module.eks.node_group_role_arn
  cicd_role_arn = var.cicd_role_arn
  tags          = local.tags
}

module "elasticache" {
  source = "../../modules/elasticache"

  name                       = local.prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.intra_subnet_ids
  node_type                  = "cache.r7g.large"
  num_node_groups            = 1   # single shard, mirrors prod topology at reduced scale
  replicas_per_node_group    = 1   # 1 replica for HA
  allowed_security_group_ids = [module.eks.node_security_group_id]
  auth_token                 = var.redis_auth_token
  sns_topic_arn              = var.alerts_sns_topic_arn
  tags                       = local.tags
}

module "rds" {
  source = "../../modules/rds"

  name                       = local.prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.intra_subnet_ids
  instance_class             = "db.r8g.large"
  master_password            = var.db_password
  allowed_security_group_ids = [module.eks.node_security_group_id]
  multi_az                   = true
  deletion_protection        = true
  backup_retention_days      = 7
  create_replica             = false
  tags                       = local.tags
}

module "s3" {
  source = "../../modules/s3"

  prefix                = local.prefix
  aws_account_id        = data.aws_caller_identity.current.account_id
  backup_retention_days = 30
  create_tfstate_bucket = false
  force_destroy         = false
  tags                  = local.tags
}

module "cloudwatch" {
  source = "../../modules/cloudwatch"

  prefix      = local.prefix
  environment = local.environment
  aws_region  = var.aws_region

  log_retention_days         = 30
  sns_topic_arn              = var.alerts_sns_topic_arn
  rds_instance_id            = module.rds.instance_id
  redis_replication_group_id = module.elasticache.replication_group_id
  tags                       = local.tags
}

data "aws_caller_identity" "current" {}
