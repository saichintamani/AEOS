variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.10.0.0/16"
}

variable "developer_cidrs" {
  description = "CIDRs allowed to reach the K8s public API endpoint"
  type        = list(string)
  default     = []
}

variable "cicd_role_arn" {
  description = "IAM role ARN for CI/CD pipeline"
  type        = string
}

variable "db_password" {
  description = "RDS master password"
  type        = string
  sensitive   = true
}

variable "redis_auth_token" {
  description = "ElastiCache Redis AUTH token"
  type        = string
  sensitive   = true
}
