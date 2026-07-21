## AEOS Production Environment
## Full HA: 3 AZs, on-demand nodes, Multi-AZ RDS, 3-node Redis, read replicas.
## Private API endpoint. All traffic through VPN/bastion.

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
  # trap (it silently targets a nonexistent bucket). Supply the real bucket at
  # init time, once the state bucket + lock table are bootstrapped per account:
  #   terraform init -backend-config="bucket=aeos-tfstate-<ACCOUNT_ID>"
  # See infrastructure/README.md → "Bootstrap Terraform remote state".
  backend "s3" {
    key            = "production/terraform.tfstate"
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
  environment = "production"
  prefix      = "aeos-${local.environment}"

  tags = {
    Project     = "aeos"
    Environment = local.environment
    ManagedBy   = "terraform"
    Team        = "platform"
    CostCenter  = "engineering"
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

## ─── EKS — private endpoint, on-demand, 3 AZs ────────────────────────────

module "eks" {
  source = "../../modules/eks"

  cluster_name       = local.prefix
  kubernetes_version = "1.29"
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids

  endpoint_public_access = false   # No public endpoint in production
  public_access_cidrs    = []

  general_instance_types = ["m6i.2xlarge", "m6a.2xlarge", "m7i.2xlarge"]
  general_desired        = 6
  general_min            = 3
  general_max            = 20
  use_spot               = false   # On-demand only in production

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

## ─── ElastiCache — 3-node cluster, encrypted, auto-failover ──────────────

module "elasticache" {
  source = "../../modules/elasticache"

  name                       = local.prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.intra_subnet_ids
  node_type                  = "cache.r7g.xlarge"
  num_node_groups            = 3   # 3 shards across 3 AZs
  replicas_per_node_group    = 2   # 2 replicas per shard for HA
  allowed_security_group_ids = [module.eks.node_security_group_id]
  auth_token                 = var.redis_auth_token
  sns_topic_arn              = aws_sns_topic.alerts.arn
  tags                       = local.tags
}

## ─── RDS — Multi-AZ with read replica ───────────────────────────────────

module "rds" {
  source = "../../modules/rds"

  name                       = local.prefix
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.intra_subnet_ids
  instance_class             = "db.r8g.2xlarge"
  master_password            = var.db_password
  allowed_security_group_ids = [module.eks.node_security_group_id]
  multi_az                   = true
  deletion_protection        = true
  backup_retention_days      = 14
  allocated_storage          = 500
  max_allocated_storage      = 5000
  create_replica             = true
  tags                       = local.tags
}

## ─── S3 ───────────────────────────────────────────────────────────────────

module "s3" {
  source = "../../modules/s3"

  prefix                = local.prefix
  aws_account_id        = data.aws_caller_identity.current.account_id
  backup_retention_days = 365   # 1-year retention in production
  create_tfstate_bucket = false
  force_destroy         = false
  tags                  = local.tags
}

## ─── Alerts SNS Topic ─────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name              = "${local.prefix}-alerts"
  kms_master_key_id = "alias/aws/sns"
  tags              = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_email_addresses)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

## ─── CloudWatch ───────────────────────────────────────────────────────────

module "cloudwatch" {
  source = "../../modules/cloudwatch"

  prefix      = local.prefix
  environment = local.environment
  aws_region  = var.aws_region

  log_retention_days         = 365
  sns_topic_arn              = aws_sns_topic.alerts.arn
  rds_instance_id            = module.rds.instance_id
  redis_replication_group_id = module.elasticache.replication_group_id
  tags                       = local.tags
}

data "aws_caller_identity" "current" {}

## ─── Outputs ──────────────────────────────────────────────────────────────

output "cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = module.eks.cluster_endpoint
  sensitive   = true
}

output "ecr_registry" {
  description = "ECR registry base URL"
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

output "redis_endpoint" {
  description = "ElastiCache Redis Cluster configuration endpoint (cluster-mode aware clients use this)"
  value       = module.elasticache.configuration_endpoint
  sensitive   = true
}

output "rds_endpoint" {
  description = "RDS primary endpoint"
  value       = module.rds.endpoint
  sensitive   = true
}

output "api_role_arn" {
  description = "IRSA role ARN for the API service account"
  value       = module.iam.api_role_arn
}

output "worker_role_arn" {
  description = "IRSA role ARN for the worker service account"
  value       = module.iam.worker_role_arn
}
