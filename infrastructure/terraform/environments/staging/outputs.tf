output "cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = module.eks.cluster_endpoint
  sensitive   = true
}

output "cluster_ca_data" {
  description = "Base64-encoded cluster CA certificate"
  value       = module.eks.cluster_ca_data
  sensitive   = true
}

output "ecr_registry" {
  description = "ECR registry base URL"
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

output "ecr_api_url" {
  value = module.ecr.repository_urls["api"]
}

output "ecr_worker_url" {
  value = module.ecr.repository_urls["worker"]
}

output "redis_endpoint" {
  value     = module.elasticache.primary_endpoint
  sensitive = true
}

output "rds_endpoint" {
  value     = module.rds.endpoint
  sensitive = true
}

output "artifacts_bucket" {
  value = module.s3.artifacts_bucket
}

output "api_role_arn" {
  value = module.iam.api_role_arn
}

output "worker_role_arn" {
  value = module.iam.worker_role_arn
}

output "eso_role_arn" {
  value = module.iam.eso_role_arn
}

output "vpc_id" {
  value = module.vpc.vpc_id
}
