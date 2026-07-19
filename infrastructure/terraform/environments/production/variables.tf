variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16"
}

variable "cicd_role_arn" {
  description = "IAM role ARN for CI/CD pipeline (push to ECR, update EKS)"
  type        = string
}

variable "alert_email_addresses" {
  description = "Email addresses to receive CloudWatch alarm notifications"
  type        = list(string)
  default     = []
}

variable "db_password" {
  description = "RDS master password — inject via CI/CD secrets, never commit"
  type        = string
  sensitive   = true
}

variable "redis_auth_token" {
  description = "ElastiCache AUTH token — inject via CI/CD secrets, never commit"
  type        = string
  sensitive   = true
}
