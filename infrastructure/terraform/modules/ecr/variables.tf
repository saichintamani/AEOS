variable "prefix" {
  description = "ECR repository prefix (e.g. aeos)"
  type        = string
}

variable "repositories" {
  description = "List of repository names (without prefix)"
  type        = list(string)
  default     = ["api", "worker", "scheduler"]
}

variable "node_role_arn" {
  description = "IAM role ARN of EKS node group (for pull access)"
  type        = string
}

variable "cicd_role_arn" {
  description = "IAM role ARN used by CI/CD pipeline (for push access)"
  type        = string
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
