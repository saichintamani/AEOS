# DR Runbook: Full Cluster / Region Failure

**RTO**: 2 hours (region failover, pre-provisioned standby) / 4 hours (cold restore)
**RPO**: 15 minutes (S3 backup interval) for Redis / 5 minutes (RDS automated backup)

## Failure Scenarios

| Scenario | RTO | RPO | Action |
|----------|-----|-----|--------|
| EKS control plane unavailable | 15 min | 0 | Wait for AWS recovery |
| All nodes terminated (ASG failure) | 20 min | 0 | Force ASG refresh |
| VPC/networking failure | 30 min | 0 | Contact AWS support |
| Full region outage | 2 hours | 15 min | Activate DR region |

## Scenario 1: EKS Control Plane Unavailable

```bash
# Check AWS status
aws eks describe-cluster --name aeos-production --query 'cluster.status'

# If DEGRADED or UPDATING, check service health dashboard
# https://health.aws.amazon.com/health/status
```

Actions:
1. Wait up to 15 minutes — AWS SLA for control plane is 99.95%
2. If no recovery after 15 min, open AWS support case (production tier)
3. Notify users via status page

Workers continue processing tasks from Kafka as long as Redis/Kafka are available.

## Scenario 2: All Worker Nodes Terminated

```bash
# Check ASG status
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names aeos-production-general

# Force refresh (terminates and replaces all nodes)
aws autoscaling start-instance-refresh \
  --auto-scaling-group-name aeos-production-general \
  --preferences '{"MinHealthyPercentage": 0}'
```

New nodes will bootstrap in ~5 minutes. DaemonSets and deployments schedule automatically.

## Scenario 3: Full Region Failover

### Pre-conditions (must be set up in advance):
- RDS: cross-region automated backups enabled to `us-west-2`
- ElastiCache: daily backup snapshots copied to `us-west-2`
- S3: cross-region replication enabled
- ECR: images replicated to `us-west-2`
- Terraform: DR environment pre-provisioned in `us-west-2` (standby)

### Step 1: Declare incident (ETA > 30 minutes to recovery)
```bash
# Page oncall
# Update status page: https://status.aeos.example.com
# Notify: Slack #incidents, stakeholders
```

### Step 2: Activate DR EKS cluster (pre-provisioned in us-west-2)
```bash
export AWS_REGION=us-west-2
aws eks update-kubeconfig --name aeos-dr --region us-west-2

# Verify DR cluster is healthy
kubectl get nodes
```

### Step 3: Promote RDS read replica to primary
```bash
aws rds promote-read-replica \
  --db-instance-identifier aeos-production-replica-us-west-2 \
  --region us-west-2

# Wait for status: available
aws rds wait db-instance-available \
  --db-instance-identifier aeos-production-replica-us-west-2 \
  --region us-west-2
```

### Step 4: Restore ElastiCache from latest snapshot
```bash
# List available snapshots
aws elasticache describe-snapshots \
  --replication-group-id aeos-production \
  --region us-west-2

# Restore
aws elasticache create-replication-group \
  --replication-group-id aeos-production-dr \
  --replication-group-description "DR restore" \
  --snapshot-name <LATEST_SNAPSHOT> \
  --region us-west-2
```

### Step 5: Update secrets/config for DR region endpoints
```bash
# Update DR configmap with new endpoints
kubectl apply -f infrastructure/kubernetes/dr/configmap-us-west-2.yaml

# Apply full AEOS deployment to DR cluster
helm upgrade --install aeos ./infrastructure/helm/aeos \
  -f infrastructure/helm/aeos/values-production.yaml \
  -f infrastructure/helm/aeos/values-dr-us-west-2.yaml \
  --namespace aeos-api
```

### Step 6: Cut DNS over to DR region
```bash
# Update Route53 record: api.aeos.example.com → DR ALB
aws route53 change-resource-record-sets \
  --hosted-zone-id <ZONE_ID> \
  --change-batch file://infrastructure/dr/route53-failover.json
```

### Step 7: Verify DR is serving traffic
```bash
curl https://api.aeos.example.com/health/ready
curl https://api.aeos.example.com/api/v1/validation/status
```

### Step 8: Assess data loss window
```bash
# Check RDS: last backup timestamp
aws rds describe-db-instances \
  --db-instance-identifier aeos-production-replica-us-west-2 \
  --query 'DBInstances[0].LatestRestorableTime'

# Check Redis: latest snapshot time
aws elasticache describe-snapshots \
  --snapshot-name <USED_SNAPSHOT> \
  --query 'Snapshots[0].SnapshotCreateTime'
```

## Post-Failover Checklist

- [ ] DR cluster serving traffic verified via health endpoint
- [ ] RDS writes succeeding (check API task creation)
- [ ] Redis connected and accepting writes
- [ ] Kafka consumer lag < 1000 messages
- [ ] Grafana DR dashboard green
- [ ] DNS TTL reduced to 60s before failover (should have been done proactively)
- [ ] Status page updated to "operational (DR mode)"
- [ ] Primary region recovery plan documented
- [ ] Failback window scheduled (next maintenance window)

## Failback Procedure

After primary region recovers:
1. Sync RDS from DR back to primary (pg_dump / DMS)
2. Sync Redis keys from DR to primary
3. Cut DNS back to primary
4. Shut down DR resources to stop billing
