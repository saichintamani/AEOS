# AEOS Capacity Planning Guide

## Current Baseline (Production)

| Component | Current Size | Headroom | Scale Trigger |
|-----------|-------------|----------|---------------|
| API pods | 3–20 (HPA) | 6.7x burst | CPU > 70% |
| Worker pods | 2–50 (HPA) | 25x burst | CPU > 65% |
| Redis | cache.r7g.xlarge ×3 | ~80% memory headroom | Memory > 75% |
| RDS | db.r8g.2xlarge | ~60% CPU headroom | CPU > 70% |
| EKS nodes | m6i.2xlarge ×6–20 | Autoscales | Pod pending > 1min |

---

## Capacity Model

### API Tier

Each API pod handles ~200 concurrent requests at 500m CPU request.

```
Max concurrent requests = replicas × 200
Max throughput (simple queries) ≈ replicas × 1000 req/min
```

At 20 replicas (HPA max): **20,000 concurrent**, **200k req/min**.

**Grow when**: Sustained CPU > 70% across all pods for > 5 minutes at max replica count.
**Action**: Increase `api.autoscaling.maxReplicas` in Helm values and right-size EC2.

### Worker Tier

Each worker handles 1 long-running task at a time (LLM calls, code execution).

```
Max concurrent tasks = replicas × 1 (CPU-bound tasks)
Max task throughput ≈ replicas × (3600 / avg_task_duration_seconds)
```

Example: 50 workers × (3600 / 30s avg) = **6,000 tasks/hour = 100 tasks/min**.

**Grow when**: Queue depth sustained > 500 for > 5 minutes.
**Action**: Increase `worker.autoscaling.maxReplicas` up to node capacity.

### Redis

Memory usage drives Redis capacity:

- Each active task: ~2KB in Redis (checkpoint + metadata)
- Each active Raft log entry: ~500 bytes
- Each worker heartbeat: ~100 bytes × TTL

At 50 concurrent workers, 1000 queued tasks:
```
~50 × 2KB + 1000 × 2KB + overhead ≈ 3MB active
```

Redis is sized for metadata, not bulk data. Memory pressure indicates a leak or unbounded growth.

**Grow when**: `redis_memory_used_bytes / redis_memory_max_bytes > 0.75` for > 10 minutes.
**Action**: Scale ElastiCache node type up (cache.r7g.2xlarge).

### RDS

AEOS writes task metadata, audit logs, and validation results to Postgres.

```
Write throughput: ~10 ops/task × task_rate tasks/min
Storage growth: ~1KB/task (compressed)
```

At 100 tasks/min: ~1000 writes/min, ~144MB/day.

**Grow when**: CPU > 70% sustained, or storage utilization > 80%.
**Action**: RDS autoscaling handles storage automatically (up to 5TB). Upgrade instance class for CPU.

---

## Node Capacity Planning

### EKS Node Sizing

Current: `m6i.2xlarge` (8 vCPU, 32 GB RAM)

Schedulable per node (after system overhead):
- CPU: ~7.5 vCPU
- Memory: ~28 GB

API pod resource profile: 0.5 vCPU, 1 GB request → ~15 API pods per node
Worker pod resource profile: 1 vCPU, 2 GB request → ~7 workers per node

**Node scale trigger**: Any pod in Pending state > 60 seconds (Cluster Autoscaler).

**Right-sizing review cadence**: Monthly. If average node CPU < 30% → consider smaller instance type.

---

## Growth Projections

| Metric | Current | 3 Months | 6 Months | Action Required |
|--------|---------|----------|----------|-----------------|
| API req/min | 500 | 2,000 | 10,000 | HPA handles up to 200k |
| Tasks/day | 1,000 | 10,000 | 100,000 | Scale workers + Kafka partitions |
| DB storage | 10 GB | 50 GB | 500 GB | RDS autoscaling handles |
| Redis memory | 512 MB | 1 GB | 4 GB | Upgrade at 75% of max |
| EKS nodes | 6 | 10 | 20 | Autoscaler handles |

---

## Runbook: Emergency Scale-Up

If a sudden traffic spike exceeds HPA capacity:

```bash
# 1. Temporarily increase HPA max replicas
kubectl patch hpa aeos-api -n aeos-api \
  -p '{"spec":{"maxReplicas":50}}'

kubectl patch hpa aeos-worker -n aeos-jobs \
  -p '{"spec":{"maxReplicas":100}}'

# 2. Pre-warm new nodes (if CA is slow)
aws autoscaling set-desired-capacity \
  --auto-scaling-group-name aeos-production-general \
  --desired-capacity 15

# 3. Monitor until traffic stabilizes
watch kubectl top pods -n aeos-api

# 4. Restore normal HPA limits after traffic normalizes
kubectl patch hpa aeos-api -n aeos-api \
  -p '{"spec":{"maxReplicas":20}}'
```
