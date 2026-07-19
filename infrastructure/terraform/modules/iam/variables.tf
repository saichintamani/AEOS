variable "prefix" {
  description = "Resource name prefix (e.g. aeos-prod)"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID"
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the EKS OIDC provider"
  type        = string
}

variable "oidc_provider_url" {
  description = "URL of the EKS OIDC provider"
  type        = string
}

variable "service_accounts" {
  description = "Map of service account key → {name, namespace} for IRSA"
  type = map(object({
    name      = string
    namespace = string
  }))
  default = {
    api = {
      name      = "aeos-api-sa"
      namespace = "aeos-api"
    }
    worker = {
      name      = "aeos-worker-sa"
      namespace = "aeos-jobs"
    }
    eso = {
      name      = "external-secrets-sa"
      namespace = "external-secrets"
    }
  }
}

variable "artifacts_bucket" {
  description = "S3 bucket name for worker artifacts"
  type        = string
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
