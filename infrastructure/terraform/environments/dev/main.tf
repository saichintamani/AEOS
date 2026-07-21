## AEOS Dev Environment
## Single-AZ, spot instances, minimal replicas, no deletion protection.

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
    key            = "dev/terraform.tfstate"
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
  environment = "dev"
  prefix      = "aeos-${local.environment}"

  tags = {
    Project     = "aeos"
    Environment = local.environment
    ManagedBy   = "terraform"
    Team        = "platform"
  }
}

## ─── Networking ───────────────────────────────────────────────────────────

module "vpc" {
  source = "../../modules/vpc"

  name         = local.prefix
  vpc_cidr     = var.vpc_cidr
  cluster_name = local.prefix
  tags         = local.tags
}

## ─── EKS ──────────────────────────────────────────────────────────────────

module "eks" {
  source = "../../modules/eks"

  cluster_name       = local.prefix
  kubernetes_version = "1.29"
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids

  endpoint_public_access  = true   # OK for dev — no VPN required
  public_access_cidrs     = var.developer_cidrs

  general_instance_types = ["m6i.large", "m6a.large"]
  general_desired        = 2
  general_min            = 1
  general_max            = 5
  use_spot               = true   # Cost savings in dev

  tags = local.tags
}

## ─── IAM / IRSA ───────────────────────────────────────────────────────────

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

## ─── ECR ──────────────────────────────────────────────────────────────────

module "ecr" {
  source = "../../modules/ecr"

  prefix        = "aeos"
  repositories  = ["api", "worker", "scheduler"]
  node_role_arn = module.eks.node_group_role_arn
  cicd_role_arn = var.cicd_role_arn
  tags          = local.tags
}

## ─── ElastiCache (single node in dev) ────────────────────────────────────

module "elasticache" {
  source = "../../modules/elasticache"

  name                       = local.prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.intra_subnet_ids
  node_type                  = "cache.t4g.micro"
  num_node_groups            = 1   # single shard in dev
  replicas_per_node_group    = 1   # 1 replica (module/cluster-mode minimum for HA + Multi-AZ)
  allowed_security_group_ids = [module.eks.node_security_group_id]
  auth_token                 = var.redis_auth_token
  tags                       = local.tags
}

## ─── RDS (single-AZ, no deletion protection) ─────────────────────────────

module "rds" {
  source = "../../modules/rds"

  name                       = local.prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.intra_subnet_ids
  instance_class             = "db.t4g.medium"
  master_password            = var.db_password
  allowed_security_group_ids = [module.eks.node_security_group_id]
  multi_az                   = false
  deletion_protection        = false
  backup_retention_days      = 3
  create_replica             = false
  tags                       = local.tags
}

## ─── S3 ───────────────────────────────────────────────────────────────────

module "s3" {
  source = "../../modules/s3"

  prefix                = local.prefix
  aws_account_id        = data.aws_caller_identity.current.account_id
  backup_retention_days = 14
  create_tfstate_bucket = false
  force_destroy         = true   # OK to destroy buckets in dev
  tags                  = local.tags
}

## ─── CloudWatch ───────────────────────────────────────────────────────────

module "cloudwatch" {
  source = "../../modules/cloudwatch"

  prefix      = local.prefix
  environment = local.environment
  aws_region  = var.aws_region
  log_retention_days         = 14
  rds_instance_id            = module.rds.instance_id
  redis_replication_group_id = module.elasticache.replication_group_id
  tags        = local.tags
}

data "aws_caller_identity" "current" {}
