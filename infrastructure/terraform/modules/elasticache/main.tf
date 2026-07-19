## ElastiCache Module — Redis 7.x CLUSTER MODE (replaces Sentinel/replication-group)
##
## Migration from DRIFT-001:
##   BEFORE: aws_elasticache_replication_group (Sentinel/primary+replicas)
##   AFTER:  aws_elasticache_replication_group with cluster_mode enabled
##           (num_node_groups + replicas_per_node_group)
##
## Why Cluster mode:
##   - Horizontal sharding: data partitioned across node groups (shards)
##   - No single-node memory ceiling; scales to hundreds of TB
##   - Automatic slot migration during resharding
##   - Multi-primary writes: each shard has its own primary
##   - Meets AC-COMP-003 requirement for Redis Cluster (not Sentinel)
##
## Hash slot allocation (16384 total):
##   3 shards: [0–5460] [5461–10922] [10923–16383]
##   AEOS key prefixes → hash tags:
##     {aeos:lease:*}     → consistent shard
##     {aeos:checkpoint:*} → consistent shard
##     {aeos:cluster:*}   → consistent shard

resource "aws_elasticache_subnet_group" "main" {
  name        = "${var.name}-redis-subnet-group"
  description = "ElastiCache subnet group for ${var.name}"
  subnet_ids  = var.subnet_ids
  tags        = var.tags
}

resource "aws_security_group" "redis" {
  name        = "${var.name}-redis-sg"
  description = "ElastiCache Redis Cluster security group"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Redis from EKS nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids
  }

  # Redis Cluster bus port (inter-node communication)
  ingress {
    description     = "Redis Cluster bus"
    from_port       = 16379
    to_port         = 16379
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name}-redis-sg" })
}

resource "aws_kms_key" "redis" {
  description             = "ElastiCache Redis Cluster encryption key for ${var.name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = var.tags
}

## Redis Cluster Mode — replaces the non-cluster replication group
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = var.name
  description          = "AEOS Redis Cluster (cluster mode) for ${var.name}"

  engine         = "redis"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = 6379

  # ── CLUSTER MODE (DRIFT-001 fix) ──────────────────────────────────────
  # cluster_mode block enables Redis Cluster Protocol (not Sentinel)
  # num_node_groups: number of shards (primaries)
  # replicas_per_node_group: replicas per shard (for HA within each shard)
  num_node_groups         = var.num_node_groups
  replicas_per_node_group = var.replicas_per_node_group

  # Cluster mode requires this to be enabled
  automatic_failover_enabled = true
  multi_az_enabled           = true

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  # Encryption at rest and in transit
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.redis.arn
  auth_token                 = var.auth_token

  # Maintenance and backup windows
  maintenance_window       = "sun:03:00-sun:04:00"
  snapshot_window          = "01:00-02:00"
  snapshot_retention_limit = 7

  parameter_group_name   = aws_elasticache_parameter_group.main.name
  notification_topic_arn = var.sns_topic_arn

  apply_immediately = false
  tags              = var.tags
}

resource "aws_elasticache_parameter_group" "main" {
  name        = "${var.name}-redis-cluster-params"
  # cluster.redis7 family enables cluster mode
  family      = "redis7.cluster.on"
  description = "AEOS Redis Cluster parameter group"

  # Memory policy: evict LRU keys (preserves hot data)
  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  # Active defragmentation (reduces memory fragmentation)
  parameter {
    name  = "activedefrag"
    value = "yes"
  }

  # Lazy eviction for non-blocking performance
  parameter {
    name  = "lazyfree-lazy-eviction"
    value = "yes"
  }

  # Keyspace notifications: expired + generic commands
  # Used by AEOS for lease TTL events and workflow triggers
  parameter {
    name  = "notify-keyspace-events"
    value = "Ex"
  }

  # Cluster node timeout — fail a node after 15s of unreachability
  # (default 15000ms = 15s; reduces split-brain window)
  parameter {
    name  = "cluster-node-timeout"
    value = "15000"
  }

  # Allow reads from replica nodes (READONLY commands)
  # AEOS worker lease reads can use replicas; writes go to primary
  parameter {
    name  = "cluster-allow-reads-when-down"
    value = "yes"
  }

  tags = var.tags
}
