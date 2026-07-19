# AEOS Service Level Objectives

## Overview

SLOs define the reliability targets that AEOS commits to. Error budgets are the allowed amount of unreliability per rolling 30-day window.

| SLO | Target | Error Budget (30 days) |
|-----|--------|----------------------|
| API Availability | 99.9% | 43.2 minutes |
| API Latency P99 | < 1s | Burn rate monitored |
| Task Success Rate | 99.5% | 0.5% failure allowed |
| Task Throughput | > 100 tasks/min (p50 load) | — |

---

## SLO 1: API Availability

**Definition**: Percentage of HTTP requests that return a non-5xx response.

**Target**: 99.9% over a rolling 30-day window.

**Measurement window**: 1-minute buckets, 30-day rolling average.

**PromQL**:
```promql
# Availability rate (last 30 days)
sum(rate(aeos_http_requests_total{status_class!="5xx"}[30d]))
/
sum(rate(aeos_http_requests_total[30d]))
```

**Error budget remaining**:
```promql
# Minutes of downtime budget remaining
(
  sum(rate(aeos_http_requests_total{status_class="5xx"}[30d])) /
  sum(rate(aeos_http_requests_total[30d]))
- 0.001
) * 43200 * -1
```

**Alert thresholds**:
- Burn rate 14.4x → Critical page (exhausts budget in 2 hours)
- Burn rate 6x → Warning (exhausts budget in 5 hours)
- Burn rate 1x → Informational

---

## SLO 2: API Latency

**Definition**: P99 response time for all non-health-check endpoints.

**Target**: P99 < 1s, P50 < 200ms over a rolling 5-minute window.

**PromQL**:
```promql
# P99 latency
histogram_quantile(0.99,
  sum(rate(aeos_http_request_duration_bucket{path!="/health"}[5m])) by (le)
)

# P50 latency
histogram_quantile(0.50,
  sum(rate(aeos_http_request_duration_bucket{path!="/health"}[5m])) by (le)
)
```

**Error budget**: Latency SLO doesn't have a fixed budget — instead, sustained P99 > 1s triggers error budget burn alerts.

---

## SLO 3: Task Success Rate

**Definition**: Percentage of submitted tasks that complete successfully (vs. failing, timing out, or being DLQ'd).

**Target**: 99.5% over a rolling 24-hour window.

**PromQL**:
```promql
sum(rate(aeos_task_executions_total{status="success"}[24h]))
/
sum(rate(aeos_task_executions_total[24h]))
```

**Exclusions**: Tasks explicitly cancelled by user are excluded from denominator.

---

## SLO 4: Task Throughput

**Definition**: System can sustain ≥ 100 completed tasks/minute during normal operating load.

**Target**: p50 throughput ≥ 100 tasks/min, p99 ≥ 50 tasks/min.

**PromQL**:
```promql
# Current throughput
sum(rate(aeos_task_executions_total{status="success"}[1m])) * 60
```

---

## Error Budget Policy

| Remaining Budget | Action |
|-----------------|--------|
| > 50% | Normal operations, feature deployments allowed |
| 25–50% | Heightened scrutiny on deployments; risk review required |
| 10–25% | No non-critical deployments; focus on reliability |
| < 10% | Feature freeze; all hands on reliability; executive escalation |
| 0% (exhausted) | Incident retrospective required before any new deployment |

## SLO Review Cadence

- **Weekly**: Error budget burn rate review in SRE sync
- **Monthly**: SLO target review — adjust targets based on actual user impact
- **Quarterly**: SLO definition review — add new SLOs as system matures
