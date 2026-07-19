## Redis Cluster outputs
## NOTE: Cluster mode exposes a configuration endpoint (not primary/reader)
## All clients must use the configuration_endpoint_address for cluster-aware routing.

output "configuration_endpoint" {
  description = "Redis Cluster configuration endpoint (use this for all client connections)"
  value       = aws_elasticache_replication_group.main.configuration_endpoint_address
}

output "port" {
  description = "Redis port"
  value       = 6379
}

output "security_group_id" {
  description = "Security group ID of the Redis Cluster"
  value       = aws_security_group.redis.id
}

output "replication_group_id" {
  description = "ElastiCache replication group ID"
  value       = aws_elasticache_replication_group.main.id
}

output "num_shards" {
  description = "Number of shards (node groups)"
  value       = var.num_node_groups
}

output "cluster_mode_enabled" {
  description = "Always true — this module enforces Cluster mode"
  value       = true
}
