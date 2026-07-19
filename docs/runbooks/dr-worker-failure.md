# DR Runbook: Worker Failure

**RTO**: 2 minutes (K8s pod restart) / 10 minutes (node failure with rescheduling)
**RPO**: 0 (tasks checkpointed to Redis before processing)

## Symptoms

- `AEOS_WORKER_DOWN` or `AEOS_TASK_FAILURE_RATE_HIGH` alert fires
- Kafka consumer lag growing in `aeos.tasks` topic
- Worker pods in `CrashLoopBackOff` or `Error` state
- Task queue depth metric climbing

## Diagnosis

### Check pod status
```bash
kubectl get pods -n aeos-jobs -l app=aeos-worker
kubectl describe pod <failed-pod> -n aeos-jobs
kubectl logs <failed-pod> -n aeos-jobs --previous
```

### Check Kafka consumer lag
```bash
kubectl exec -n aeos-data kafka-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group aeos-workers
```

## Recovery Procedures

### Scenario A: Pod crash (OOMKill or application exception)

K8s will restart the pod automatically (RestartPolicy: Always). Verify:
```bash
kubectl rollout status deployment/aeos-worker -n aeos-jobs
```

If pods keep crashing, examine the last N logs:
```bash
kubectl logs -l app=aeos-worker -n aeos-jobs --previous --tail=200
```

Common causes and fixes:
| Error | Fix |
|-------|-----|
| `OOMKilled` | Increase memory limit in deployment or reduce batch size |
| `ConnectionRefusedError` (Redis) | Verify Redis is healthy (see redis runbook) |
| `NoBrokersAvailable` (Kafka) | Verify Kafka is healthy |
| `InvariantViolation` | Check `/api/v1/validation/status` for critical violations |

### Scenario B: Node failure (EC2 instance terminated)

EKS node group will replace the node via ASG. Workers reschedule automatically.
```bash
# Check node status
kubectl get nodes
# Watch pods reschedule
kubectl get pods -n aeos-jobs -w
```

Force reschedule if pods are stuck in Pending:
```bash
kubectl delete pods -n aeos-jobs -l app=aeos-worker --field-selector=status.phase=Pending
```

### Scenario C: Deployment image bad (CrashLoopBackOff after new deploy)

Rollback:
```bash
kubectl rollout undo deployment/aeos-worker -n aeos-jobs
kubectl rollout status deployment/aeos-worker -n aeos-jobs
```

### Scenario D: In-flight task recovery

Tasks checkpointed to Redis before processing. After worker recovery:
```bash
# Check for stuck in-flight tasks (older than 10 minutes)
kubectl exec -n aeos-api deployment/aeos-api -- \
  python -c "
import asyncio
from app.distributed.coordination.redis_coordinator import RedisCoordinator
async def check():
    rc = RedisCoordinator()
    tasks = await rc.get_stale_tasks(timeout_seconds=600)
    print(f'Stale tasks: {len(tasks)}')
asyncio.run(check())
"
```

Requeue stale tasks via API:
```bash
curl -X POST https://api.aeos.example.com/api/v1/tasks/requeue-stale \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## Scaling Up (queue depth > SLO)

If task queue depth exceeds 1000 and workers can't keep up:
```bash
# Scale worker replicas temporarily
kubectl scale deployment/aeos-worker -n aeos-jobs --replicas=20

# Or if HPA max is too low, temporarily increase:
kubectl patch hpa aeos-worker -n aeos-jobs -p '{"spec":{"maxReplicas":50}}'
```

## Post-Recovery Checklist

- [ ] All worker pods Running: `kubectl get pods -n aeos-jobs`
- [ ] Kafka consumer lag < 100: check Grafana consumer lag panel
- [ ] Task failure rate < 1%: check Grafana "Task Failure Rate" panel
- [ ] No stale tasks > 10min in Redis
- [ ] HPA restored to normal max if temporarily increased
