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
  description = "ECR repository URL for the API image"
  value       = module.ecr.repository_urls["api"]
}

output "ecr_worker_url" {
  description = "ECR repository URL for the worker image"
  value       = module.ecr.repository_urls["worker"]
}

output "redis_endpoint" {
  description = "ElastiCache Redis Cluster configuration endpoint (cluster-mode aware)"
  value       = module.elasticache.configuration_endpoint
  sensitive   = true
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint"
  value       = module.rds.endpoint
  sensitive   = true
}

output "artifacts_bucket" {
  description = "S3 artifacts bucket name"
  value       = module.s3.artifacts_bucket
}

output "api_role_arn" {
  description = "IRSA IAM role ARN for the API service account"
  value       = module.iam.api_role_arn
}

output "worker_role_arn" {
  description = "IRSA IAM role ARN for the worker service account"
  value       = module.iam.worker_role_arn
}

output "eso_role_arn" {
  description = "IRSA IAM role ARN for External Secrets Operator"
  value       = module.iam.eso_role_arn
}

output "cloudwatch_dashboard_url" {
  description = "CloudWatch dashboard URL"
  value       = module.cloudwatch.dashboard_url
}

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}
