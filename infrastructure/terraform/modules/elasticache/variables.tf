variable "name" {
  description = "ElastiCache replication group ID (e.g. aeos-prod)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "subnet_ids" {
  description = "Intra subnet IDs for ElastiCache"
  type        = list(string)
}

variable "allowed_security_group_ids" {
  description = "Security group IDs allowed to connect to Redis"
  type        = list(string)
  default     = []
}

variable "engine_version" {
  description = "Redis engine version"
  type        = string
  default     = "7.1"
}

variable "node_type" {
  description = "ElastiCache node type (cluster mode; each shard uses this type)"
  type        = string
  default     = "cache.r7g.large"
}

# Cluster mode: replaces num_cache_clusters
variable "num_node_groups" {
  description = "Number of shards (node groups) in Redis Cluster. Minimum 1; production: 3+"
  type        = number
  default     = 3
}

variable "replicas_per_node_group" {
  description = "Number of read replicas per shard. Minimum 1 for HA; production: 2"
  type        = number
  default     = 1
}

variable "auth_token" {
  description = "Redis AUTH token (password)"
  type        = string
  sensitive   = true
}

variable "sns_topic_arn" {
  description = "SNS topic ARN for failover notifications (optional)"
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
