variable "prefix" {
  description = "Resource name prefix (e.g. aeos-prod)"
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID (used in bucket names to ensure global uniqueness)"
  type        = string
}

variable "backup_retention_days" {
  description = "Number of days to retain backup files in S3"
  type        = number
  default     = 90
}

variable "create_tfstate_bucket" {
  description = "Create S3 bucket and DynamoDB table for Terraform state"
  type        = bool
  default     = false
}

variable "force_destroy" {
  description = "Allow bucket destruction even if non-empty (dev only)"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
