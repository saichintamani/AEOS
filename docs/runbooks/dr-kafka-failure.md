# DR Runbook: Kafka Failure

**RTO**: 5 minutes (pod restart) / 20 minutes (broker loss and ISR recovery)
**RPO**: 0 for replicated topics (RF=3, min.insync.replicas=2) / up to last committed offset for in-flight messages

## Symptoms

- `AEOSWorkerDown` or `AEOSWorkerQueueDepthCritical` alert fires
- `NoBrokersAvailable` in API or worker logs
- Kafka consumer lag metric absent (consumers disconnected)
- Tasks queuing in Redis but not being dispatched

## Triage

### Check broker status
```bash
kubectl get pods -n aeos-data -l app=kafka
kubectl describe pod kafka-0 -n aeos-data
kubectl logs kafka-0 -n aeos-data --tail=100
```

### Check ISR (in-sync replicas) from a running broker
```bash
kubectl exec -n aeos-data kafka-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --describe --topic aeos.tasks | grep -E "LeaderEpoch|Isr|Leader"
```

Healthy output: `Isr: 1,2,3`  
Degraded output: `Isr: 1,2` (one broker out — **acceptable, can still write**)  
Critical output: `Isr: 1` (only 1 in-sync — **writes will block**, min.insync.replicas=2)

### Check consumer lag
```bash
kubectl exec -n aeos-data kafka-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group aeos-workers
```

## Scenario A: Single Broker Crash (pod restart)

K8s will restart the pod automatically. Expected recovery: 60-90 seconds.

```bash
# Watch recovery
kubectl get pods -n aeos-data -l app=kafka -w

# After pod Running, verify it rejoins ISR
kubectl exec -n aeos-data kafka-0 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --describe --topic aeos.tasks | grep Isr
# Should return Isr: 1,2,3 within ~30s
```

## Scenario B: Leader Partition Lost

If kafka-0 was leader for critical partitions and crashes:

```bash
# Find new leader for aeos.tasks
kubectl exec -n aeos-data kafka-1 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --describe --topic aeos.tasks

# Leader election is automatic in KRaft — no manual intervention needed
# Verify within 30s that a new Leader is elected
```

## Scenario C: 2+ Brokers Down (min-ISR breach)

**This is critical — producers will block until ISR recovers.**

```bash
# Identify which pods are down
kubectl get pods -n aeos-data -l app=kafka

# Force restart failed pods
kubectl delete pods -n aeos-data -l app=kafka --field-selector=status.phase!=Running

# Watch recovery
kubectl rollout status statefulset/kafka -n aeos-data --timeout=5m
```

If pods are stuck in `Pending` (node failure):
```bash
# Check node availability
kubectl get nodes

# If nodes are gone, trigger ASG replacement (see dr-cluster-failure.md)
# Kafka will self-heal once nodes are available
```

## Scenario D: Corrupt Log (broker refuses to start)

Symptoms: `IOException` or `OffsetOutOfRange` in kafka pod logs.

```bash
# Identify the corrupt partition
kubectl logs kafka-0 -n aeos-data | grep -E "ERROR|IOException|corrupt"

# Delete the corrupt log segment (data loss for that segment only — accept it)
kubectl exec -n aeos-data kafka-0 -- \
  rm /bitnami/kafka/data/aeos.tasks-0/*.log

# Restart pod
kubectl delete pod kafka-0 -n aeos-data

# Kafka will fetch missing segments from replicas
```

## Topic Re-creation (last resort)

If a critical topic is unrecoverable:
```bash
# Delete topic
kubectl exec -n aeos-data kafka-1 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --delete --topic aeos.tasks

# Recreate with correct config
kubectl exec -n aeos-data kafka-1 -- \
  kafka-topics.sh --bootstrap-server localhost:9092 \
  --create --topic aeos.tasks \
  --partitions 12 \
  --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=604800000
```

**Note**: Messages in the deleted topic are lost. Workers will resume from empty offset.

## Post-Recovery Checklist

- [ ] All 3 Kafka brokers Running: `kubectl get pods -n aeos-data -l app=kafka`
- [ ] ISR=3 for all AEOS topics: `kafka-topics.sh --describe`
- [ ] Consumer lag draining: Grafana "Kafka Consumer Lag" panel
- [ ] Task throughput recovered: Grafana "Task Throughput" panel
- [ ] No `NoBrokersAvailable` errors in API logs for 5 minutes
- [ ] Invariant engine healthy: `GET /api/v1/validation/status`
