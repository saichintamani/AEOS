# DR Runbook: RDS / Database Failure

**RTO**: 5 minutes (Multi-AZ automatic failover) / 30 minutes (manual restore from backup)
**RPO**: ~30 seconds (Multi-AZ synchronous replication) / up to 5 minutes (backup window)

## Symptoms

- `AEOS_RDS_CPU_HIGH` alarm fires, or API returning `500` for write operations
- `OperationalError: could not connect to server` in API logs
- `/health/ready` returning `503` (DB connectivity check fails)
- RDS `DBInstanceNotAvailable` in AWS Console

## Triage

```bash
# Check RDS instance status
aws rds describe-db-instances \
  --db-instance-identifier aeos-production \
  --query 'DBInstances[0].{Status:DBInstanceStatus,AZ:AvailabilityZone,MF:MultiAZ}'

# Check recent events
aws rds describe-events \
  --source-identifier aeos-production \
  --source-type db-instance \
  --duration 60

# Check API connectivity
kubectl exec -n aeos-api deployment/aeos-api -- \
  python -c "
import asyncpg, asyncio
async def check():
    conn = await asyncpg.connect(dsn='$DATABASE_URL')
    print(await conn.fetchval('SELECT 1'))
asyncio.run(check())
"
```

## Scenario A: Multi-AZ Automatic Failover

AWS triggers automatic failover in ~1-2 minutes when primary becomes unreachable.

```bash
# Monitor failover event
aws rds describe-events \
  --source-identifier aeos-production \
  --source-type db-instance \
  --query 'Events[?contains(Message,`failover`) || contains(Message,`Multi-AZ`)]'

# Verify new primary AZ
aws rds describe-db-instances \
  --db-instance-identifier aeos-production \
  --query 'DBInstances[0].{AZ:AvailabilityZone,Status:DBInstanceStatus}'
```

The RDS endpoint DNS (`aeos-production.xxx.rds.amazonaws.com`) updates automatically.
API pods should reconnect via connection pool retry — no restart needed.

Verify recovery:
```bash
curl https://api.aeos.example.com/health/ready
```

## Scenario B: Performance Degradation (High CPU / Slow Queries)

```bash
# Check Performance Insights for top queries
aws pi get-resource-metrics \
  --service-type RDS \
  --identifier db-INSTANCE_ID \
  --metric-queries '[{"Metric":"db.load.avg","GroupBy":{"Group":"db.sql","Dimensions":["db.sql.statement"],"Limit":10}}]' \
  --start-time $(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period-in-seconds 60
```

### Actions:
1. **Kill long-running queries** (> 5 minutes):
```bash
kubectl exec -n aeos-api deployment/aeos-api -- psql $DATABASE_URL \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='active' AND now()-query_start > interval '5 minutes' AND pid <> pg_backend_pid();"
```

2. **Enable read replica routing** for read traffic:
```bash
kubectl set env deployment/aeos-api \
  DATABASE_READ_URL="postgresql://aeos_admin:$DB_PASS@$(aws rds describe-db-instances --db-instance-identifier aeos-production-replica --query 'DBInstances[0].Endpoint.Address' --output text)/aeos" \
  -n aeos-api
```

3. **Scale up instance** (if CPU sustained > 80%):
```bash
# Must modify in maintenance window unless urgent
aws rds modify-db-instance \
  --db-instance-identifier aeos-production \
  --db-instance-class db.r8g.4xlarge \
  --apply-immediately
```

## Scenario C: Data Corruption / Accidental Drop

**STOP: do not write to the database until you assess the scope of damage.**

```bash
# Identify latest restorable time
aws rds describe-db-instances \
  --db-instance-identifier aeos-production \
  --query 'DBInstances[0].LatestRestorableTime'
```

### Point-in-time restore (new instance):
```bash
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier aeos-production \
  --target-db-instance-identifier aeos-production-pitr-$(date +%Y%m%d%H%M) \
  --restore-time "2026-07-12T10:00:00Z"   # Time BEFORE corruption

# Wait for instance to be available
aws rds wait db-instance-available \
  --db-instance-identifier aeos-production-pitr-$(date +%Y%m%d%H%M)
```

### Extract and replay missing data:
```bash
# Dump tables from PITR instance
pg_dump -h $PITR_ENDPOINT -U aeos_admin -d aeos -t tasks -t workflows \
  --data-only > recovery_$(date +%Y%m%d%H%M).sql

# Review before applying
psql $DATABASE_URL < recovery_$(date +%Y%m%d%H%M).sql
```

## Post-Recovery Checklist

- [ ] RDS status: `available` in AWS Console
- [ ] API health ready: `curl https://api.aeos.example.com/health/ready`
- [ ] No DB connection errors in API logs for 5 minutes
- [ ] Task creation working: `POST /api/v1/tasks`
- [ ] RDS Multi-AZ re-enabled (check if single-AZ after failover)
- [ ] Performance Insights shows normal query load
- [ ] Runbook reviewed: was this a preventable failure?
