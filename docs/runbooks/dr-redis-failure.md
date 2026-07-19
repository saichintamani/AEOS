# DR Runbook: Redis Failure

**RTO**: 5 minutes (ElastiCache automatic failover) / 30 minutes (manual recovery)
**RPO**: 0 (replica is synchronous) / up to 1 second (AOF everysec)

## Symptoms

- `AEOS_REDIS_UNAVAILABLE` alert fires
- API returning 503 for endpoints that touch rate limiting, leases, or cluster state
- Worker heartbeats timing out
- `redis.exceptions.ConnectionError` in logs

## Automatic Recovery (ElastiCache)

ElastiCache performs automatic primary failover in ~1-2 minutes when the primary becomes unavailable. No action required unless failover does not complete.

**Verify failover occurred:**
```bash
aws elasticache describe-replication-groups \
  --replication-group-id aeos-production \
  --query 'ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint'
```

**Check API connectivity after failover:**
```bash
kubectl exec -n aeos-api deployment/aeos-api -- \
  python -c "import redis; r = redis.from_url('$REDIS_URL'); print(r.ping())"
```

## Manual Recovery (self-managed Redis StatefulSet)

### Step 1: Identify failed node
```bash
kubectl get pods -n aeos-data -l app=redis
# Expected: redis-0 Running, redis-1 Running, redis-2 CrashLoopBackOff (or similar)
```

### Step 2: Check primary
```bash
kubectl exec -n aeos-data redis-0 -- redis-cli -a "$REDIS_PASSWORD" info replication
# Look for: role:master
```

### Step 3: If primary (redis-0) is down — promote replica
```bash
# Connect to a healthy replica
kubectl exec -n aeos-data redis-1 -- redis-cli -a "$REDIS_PASSWORD" replicaof NO ONE
# Verify promotion
kubectl exec -n aeos-data redis-1 -- redis-cli -a "$REDIS_PASSWORD" info replication
```

### Step 4: Update client config to point to new primary
```bash
kubectl set env deployment/aeos-api REDIS_URL="redis://:${REDIS_PASSWORD}@redis-1.redis.aeos-data.svc.cluster.local:6379/0" -n aeos-api
kubectl set env deployment/aeos-worker REDIS_URL="redis://:${REDIS_PASSWORD}@redis-1.redis.aeos-data.svc.cluster.local:6379/0" -n aeos-jobs
```

### Step 5: Restart failed pod
```bash
kubectl delete pod redis-0 -n aeos-data
# StatefulSet will recreate it; it will rejoin as replica
```

### Step 6: Rejoin old primary as replica
```bash
# Wait for redis-0 to be Running again
kubectl exec -n aeos-data redis-0 -- redis-cli -a "$REDIS_PASSWORD" \
  replicaof redis-1.redis.aeos-data.svc.cluster.local 6379
```

## Data Loss Assessment

```bash
# Check AOF status on current primary
kubectl exec -n aeos-data redis-1 -- redis-cli -a "$REDIS_PASSWORD" info persistence
# Check: aof_last_bgrewrite_status:ok, aof_last_write_status:ok
```

If data loss occurred:
1. Check the latest RDB snapshot: `kubectl exec redis-0 -- ls -lh /data/dump.rdb`
2. Restore from S3 backup if necessary (see backup schedule below)

## Backup Schedule

- RDB snapshots: every 15 minutes to `/data/dump.rdb`
- ElastiCache daily snapshots: retained 7 days in S3
- Restore from ElastiCache snapshot: `aws elasticache restore-replication-group-from-s3 ...`

## Post-Recovery Checklist

- [ ] Redis replication lag < 100ms: `redis-cli info replication | grep master_repl_offset`
- [ ] API health check passing: `curl https://api.aeos.example.com/health/ready`
- [ ] Worker heartbeats resumed: check Grafana "Healthy Workers" panel
- [ ] Invariant checks passing: `curl https://api.aeos.example.com/api/v1/validation/status`
- [ ] Alert resolved in PagerDuty
- [ ] Incident ticket filed in Linear
