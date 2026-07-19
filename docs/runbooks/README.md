# AEOS Runbook Index

Quick-reference for on-call engineers. All runbooks live in `docs/runbooks/`.

## 🔴 P0 — Service Down (page immediately)

| Symptom | Runbook | RTO |
|---------|---------|-----|
| API returning 5xx / health failing | [dr-cluster-failure.md](dr-cluster-failure.md) | 2h |
| All workers stopped processing | [dr-worker-failure.md](dr-worker-failure.md) | 10m |
| Redis unavailable | [dr-redis-failure.md](dr-redis-failure.md) | 5m |
| Kafka brokers unreachable | [dr-kafka-failure.md](dr-kafka-failure.md) | 5m |
| Database connection errors | [dr-rds-failure.md](dr-rds-failure.md) | 5m |
| Region outage | [dr-cluster-failure.md#scenario-3](dr-cluster-failure.md) | 2h |

## 🟠 P1 — Degraded Service (respond within 30 minutes)

| Symptom | Runbook |
|---------|---------|
| Task failure rate > 5% | [dr-worker-failure.md](dr-worker-failure.md) |
| Redis memory > 85% | [dr-redis-failure.md](dr-redis-failure.md) |
| Kafka ISR degraded (< 3) | [dr-kafka-failure.md](dr-kafka-failure.md) |
| Node(s) NotReady | [dr-node-failure.md](dr-node-failure.md) |
| API latency P99 > 5s | [dr-cluster-failure.md](dr-cluster-failure.md) |
| Invariant violations detected | [dr-cluster-failure.md](dr-cluster-failure.md) |

## 🟡 P2 — Operational (respond within business hours)

| Task | Runbook |
|------|---------|
| 90-day secret rotation | [secret-rotation.md](secret-rotation.md) |
| Emergency secret rotation (breach) | [secret-rotation.md#emergency](secret-rotation.md) |
| Kafka consumer lag growing | [dr-kafka-failure.md](dr-kafka-failure.md) |
| Scale worker capacity | [dr-worker-failure.md#scaling-up](dr-worker-failure.md) |
| Disk pressure on node | [dr-node-failure.md#disk-pressure](dr-node-failure.md) |

## 📊 SRE References

| Document | Location |
|----------|----------|
| SLO definitions & error budgets | [sre/slo-definitions.md](../sre/slo-definitions.md) |
| Capacity planning guide | [sre/capacity-planning.md](../sre/capacity-planning.md) |
| AlertManager config | `infrastructure/monitoring/alertmanager/alertmanager.yml` |
| Prometheus alert rules | `infrastructure/monitoring/prometheus/alert-rules.yml` |
| Grafana dashboards | `infrastructure/monitoring/grafana/dashboards/` |

## 🛠️ Pre-Deployment Verification

```bash
# Always run before deploying to staging or production
bash scripts/validate.sh [staging|production]
```

See [infra-validate.yml](../../.github/workflows/infra-validate.yml) for CI equivalent.

## 🚀 Deployment Procedures

```bash
# Deploy to staging (auto on merge to main)
git push origin main

# Deploy to production (manual — requires tag)
git tag v0.2.0 && git push origin v0.2.0

# Canary rollout (50/50)
kubectl apply -f infrastructure/kubernetes/istio/canary-rollout.yaml

# Rollback
helm rollback aeos --namespace aeos-api
```

## 📞 Escalation Path

1. **On-call engineer** — PagerDuty rotation (auto-paged on P0/P1)
2. **Platform lead** — Slack `#incidents` + direct message
3. **AWS Support** — https://support.console.aws.amazon.com (Production tier)
4. **Incident retrospective** — Linear ticket, 48h post-incident report required for P0
