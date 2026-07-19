variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.29"
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for node groups"
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "Public subnet IDs (control plane ENIs)"
  type        = list(string)
}

variable "endpoint_public_access" {
  description = "Whether to enable public access to the API server"
  type        = bool
  default     = false
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to access the public endpoint"
  type        = list(string)
  default     = []
}

variable "general_instance_types" {
  description = "EC2 instance types for general node group"
  type        = list(string)
  default     = ["m6i.xlarge", "m6a.xlarge"]
}

variable "general_desired" {
  description = "Desired number of general nodes"
  type        = number
  default     = 3
}

variable "general_min" {
  description = "Minimum number of general nodes"
  type        = number
  default     = 1
}

variable "general_max" {
  description = "Maximum number of general nodes"
  type        = number
  default     = 10
}

variable "use_spot" {
  description = "Use Spot instances for node groups"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
