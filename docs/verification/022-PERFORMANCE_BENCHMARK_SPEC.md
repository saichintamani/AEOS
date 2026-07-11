# AEOS Phase 9 DRP — Performance Benchmark Specification

**Document:** `022-PERFORMANCE_BENCHMARK_SPEC.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## Purpose

This document defines the performance targets, benchmark methodology, and acceptance thresholds for AEOS Phase 9. All benchmarks MUST be run against a production-equivalent environment. Results that fail acceptance thresholds block release.

---

## Performance Targets (NFRs from §3.2)

| Metric | Target | Hard Limit |
|--------|--------|-----------|
| Task submission → execution start | P99 < 500ms | < 2s |
| Step execution latency (excl. LLM) | P99 < 100ms | < 500ms |
| Governance token issuance | P99 < 200ms | < 1s |
| Redis checkpoint write (Phase 1) | P99 < 20ms | < 100ms |
| Cluster membership update propagation | < 5s | < 15s |
| RBAC revocation propagation | < 1s (P99) | < 5s |
| Kafka consumer lag (steady state) | < 100 messages/worker | < 500 messages/worker |
| Max cluster size | 100 workers | — |
| Throughput at 100 workers | 10,000 steps/minute | — |
| Worker CPU utilization (steady state) | < 70% | < 85% |
| Worker memory utilization (steady state) | < 60% | < 80% |
| Raft leader election time | < 600ms | < 2s |

---

## Benchmark Suite

### PB-001 — Task Submission Latency

**Objective:** Measure end-to-end latency from API submission to task entering EXECUTING state.

**Setup:**
- 10-worker cluster (minimum for meaningful benchmark)
- Policy Service healthy (no circuit breaker)
- Redis Cluster healthy
- Kafka healthy, 0 consumer lag at start

**Workload:**
```
Duration: 5 minutes
Concurrency: 50 concurrent clients
Request rate: Ramp 10 → 100 → 500 requests/second
Task type: aeos.tasks.normal (standard priority)
```

**Measurement points:**
- T1: API request received (gateway timestamp)
- T2: Governance token issued
- T3: Kafka message produced
- T4: Worker lease acquired
- T5: Step execution begins

**Target latencies:**
| Percentile | T1→T5 | T1→T2 (governance) | T3→T4 (dispatch) |
|-----------|-------|-------------------|-----------------|
| P50 | < 150ms | < 50ms | < 50ms |
| P95 | < 300ms | < 100ms | < 150ms |
| P99 | < 500ms | < 200ms | < 300ms |
| P99.9 | < 2s | < 500ms | < 1s |

**Acceptance criteria:**
- P99 end-to-end < 500ms at all load levels
- Zero governance bypass (100% of tasks have valid tokens)
- Error rate < 0.1% (excluding expected 503s when governance circuit opens)

---

### PB-002 — Checkpoint Throughput

**Objective:** Measure Phase 1 checkpoint throughput and latency at scale.

**Setup:**
- 20 workers executing steps concurrently
- Redis Cluster: 3 primaries
- Steps are artificially fast (mock executors; 1ms execution time)
- Measure pure checkpoint protocol overhead

**Workload:**
```
Duration: 2 minutes
Workflows: 1000 concurrent
Steps per workflow: 10
Execution: Mock (immediate)
```

**Target metrics:**
| Metric | Target |
|--------|--------|
| Phase 1 checkpoint latency P50 | < 5ms |
| Phase 1 checkpoint latency P99 | < 20ms |
| Phase 2 (Kafka produce) P99 | < 50ms |
| Total checkpoint throughput | > 5,000 checkpoints/second |
| CROSSSLOT errors | 0 |

**Measurement method:**
```python
with prometheus_client.Histogram(
    "aeos_checkpoint_duration_seconds",
    "Checkpoint duration",
    buckets=[0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.5, 1.0]
) as h:
    for phase in [1, 2]:
        with h.labels(phase=phase).time():
            await checkpoint.run_phase(phase)
```

---

### PB-003 — Consumer Group Throughput

**Objective:** Verify Kafka consumer throughput scales linearly with worker count.

**Setup:**
- Variable worker count: 5, 10, 25, 50, 100
- 200 Kafka partitions
- Task messages: 1KB average
- No LLM calls (mock executors)

**Measurement:**
```
For each worker count N:
  1. Pre-fill topic with 10,000 tasks
  2. Start N workers
  3. Measure time to drain queue to 0 lag
  4. Calculate throughput = 10000 / drain_time (tasks/second)
```

**Expected scaling:**
| Workers | Min Throughput | Expected Throughput |
|---------|---------------|-------------------|
| 5 | 500 tasks/s | ~1,000 tasks/s |
| 10 | 1,000 tasks/s | ~2,000 tasks/s |
| 25 | 2,500 tasks/s | ~5,000 tasks/s |
| 50 | 5,000 tasks/s | ~8,000 tasks/s |
| 100 | 8,000 tasks/s | ~10,000+ tasks/s |

**Acceptance criteria:**
- Linear scaling from 5 to 50 workers (R² > 0.95)
- No single worker becomes a bottleneck
- At 100 workers: throughput > 8,000 tasks/second
- Partition count (200) confirmed as non-limiting factor (worker count < partitions)

---

### PB-004 — Raft Leader Election Time

**Objective:** Measure time from leader failure to new leader heartbeat.

**Setup:**
- 3-node Cluster Manager cluster
- 10 workers connected
- Leader: CM-A

**Method:**
```python
async def benchmark_leader_election():
    results = []
    
    for trial in range(20):
        # Record time of leader kill
        t_kill = time.monotonic_ns()
        
        # Kill current leader (force-delete pod)
        await k8s.delete_pod("aeos-cluster-manager-0")
        
        # Poll until new leader heartbeat observed
        while True:
            leader = await cm_client.get_current_leader()
            if leader is not None and leader != "aeos-cluster-manager-0":
                t_leader = time.monotonic_ns()
                break
            await asyncio.sleep(0.01)
        
        results.append((t_leader - t_kill) / 1e6)  # milliseconds
        
        # Wait for pod to restart before next trial
        await wait_for_pod("aeos-cluster-manager-0")
    
    return results
```

**Target:**
| Percentile | Target |
|-----------|--------|
| P50 | < 300ms |
| P95 | < 500ms |
| P99 | < 600ms |

**Acceptance criteria:**
- P99 < 600ms (2× max election timeout of 300ms)
- New leader always elected (no permanent split-vote in 20 trials)
- Workers reconnect to new leader within 2 seconds of election

---

### PB-005 — Redis Key Operation Latency

**Objective:** Verify Redis Cluster operations meet latency targets under production load.

**Setup:**
- Redis Cluster: 3 primaries, 3 replicas
- 1000 concurrent workflows, each using `{wf:<id>}` hashtag keys

**Workload per second:**
```
SETNX (lease acquisition): 1,000/s
MULTI/EXEC (checkpoint): 1,000/s  → 3,000 individual ops
SET (next_published): 1,000/s
GET (idempotency check): 1,000/s
DEL (lease release): 1,000/s
EXPIRE (lease renewal): ~200/s (long-running steps)
Total: ~8,200 ops/second
```

**Targets:**
| Operation | P50 | P99 | P99.9 |
|-----------|-----|-----|-------|
| SETNX | < 1ms | < 5ms | < 20ms |
| MULTI/EXEC (3 keys) | < 3ms | < 10ms | < 50ms |
| GET | < 0.5ms | < 3ms | < 10ms |
| SET | < 1ms | < 5ms | < 20ms |
| DEL | < 0.5ms | < 3ms | < 10ms |
| EXPIRE | < 0.5ms | < 3ms | < 10ms |

**Acceptance criteria:**
- CROSSSLOT errors: 0 (invariant INV-MEM-001)
- P99 MULTI/EXEC < 10ms
- Redis CPU utilization < 60% at benchmark load

---

### PB-006 — KEDA Autoscaling Reaction Time

**Objective:** Measure time from Kafka lag increase to additional worker pod Ready.

**Setup:**
- 3 workers (minReplicaCount=3)
- KEDA polling interval: 15 seconds
- Lag threshold: 500/partition

**Workload:**
```
T=0: Inject 10,000 tasks into aeos.tasks.normal
     (creates ~50 lag/partition with 3 workers, well above 500 threshold per 3 workers)
Measure: Time until new workers appear in Ready state
```

**Expected timeline:**
```
T=0:    Tasks injected
T=15s:  KEDA polls lag (worst case: just missed previous poll)
T=20s:  KEDA issues scale-up request to Kubernetes
T=30s:  New pods scheduled and starting
T=60s:  New pods reach Ready state (kernel boot + cluster join)
```

**Targets:**
| Event | Target | Max |
|-------|--------|-----|
| KEDA detects lag | < 30s | < 45s |
| Pod scheduled | < 45s | < 60s |
| Pod Ready (kernel + join) | < 90s | < 120s |
| Consumer lag starts decreasing | < 120s | < 150s |

**Acceptance criteria:**
- Additional workers ready within 120 seconds
- No tasks lost during scale-out
- After scale-out: throughput increases proportionally

---

### PB-007 — Governance Evaluation Throughput

**Objective:** Measure Policy Service throughput under concurrent evaluation load.

**Setup:**
- 2 Policy Service replicas
- Postgres with 100 policies per task type
- Task types: 10 unique types, 50/50 approve/reject split

**Workload:**
```
Duration: 3 minutes
Concurrency: ramp 10 → 100 concurrent evaluations
Measurement: evaluations/second, latency histogram
```

**Targets:**
| Metric | Target |
|--------|--------|
| Throughput | > 500 evaluations/second per instance |
| P99 latency | < 200ms |
| Timeout rate | 0% at < 400 concurrent |
| Audit log write lag | < 1 second |

---

### PB-008 — Memory Tier Latency

**Objective:** Verify each memory tier meets latency SLA.

**Workload:**
```
For each tier:
  1000 concurrent reads, 100 concurrent writes
  Duration: 2 minutes
  Measure P50, P95, P99 latency
```

**Targets:**
| Tier | P50 Read | P99 Read | P50 Write | P99 Write |
|------|----------|----------|-----------|-----------|
| Sensory (in-process) | < 0.01ms | < 0.1ms | < 0.01ms | < 0.1ms |
| Working (Redis) | < 1ms | < 5ms | < 1ms | < 10ms |
| Long-Term (Postgres) | < 5ms | < 50ms | < 10ms | < 100ms |
| Episodic (Weaviate vector) | < 50ms | < 200ms | < 100ms | < 500ms |

---

### PB-009 — Full Cluster Throughput (100 Workers)

**Objective:** Demonstrate the 100-node NFR at full scale.

**Setup:**
- 100 workers (3 on-demand, 97 Spot)
- Full production topology (Redis Cluster, Kafka, Postgres, Weaviate, Vault)
- Realistic workload mix: 70% research/analysis, 20% planning, 10% execution

**Workload:**
```
Duration: 30 minutes
Task mix:
  - 40% aeos.tasks.normal
  - 30% aeos.tasks.high
  - 20% aeos.tasks.low
  - 10% aeos.tasks.critical
Task complexity: mix of 3, 5, and 10-step workflows
Step execution: real LLM calls (GPT-4o mock at production latency profile)
```

**Key measurements:**
- Peak throughput (steps/minute)
- Sustained throughput (steps/minute at 30 minutes)
- P99 workflow end-to-end latency (submission → final step completed)
- Resource utilization: CPU, memory, Kafka lag, Redis ops/sec

**Targets:**
| Metric | Target |
|--------|--------|
| Throughput (peak) | > 12,000 steps/minute |
| Throughput (sustained) | > 10,000 steps/minute |
| P99 workflow latency (5 steps) | < 30 seconds (excl. LLM) |
| Worker CPU avg | < 70% |
| Kafka lag (steady state) | < 100/worker |
| Zero data loss | Required |
| Zero governance bypasses | Required |

---

## Benchmark Execution Requirements

### BER-001 — Environment Parity
All benchmarks MUST run on infrastructure equivalent to production:
- Same instance types (CPU, memory, network)
- Same storage types (provisioned IOPS for Redis, Postgres)
- Same Kafka broker count and instance types
- Network latency within AZ < 1ms (benchmark results are invalid if cross-AZ latency differs significantly)

### BER-002 — Warm-Up Period
All benchmarks MUST include a 2-minute warm-up period before measurements begin:
- Allows JIT compilation, connection pool warm-up, cache population
- Ensures steady-state measurements (not cold-start artifacts)

### BER-003 — Results Reporting
Benchmark results MUST include:
- Environment description (instance types, counts, Kubernetes version)
- Timestamp and duration
- Prometheus metrics dump (raw data, not summaries)
- P50, P95, P99, P99.9, P100 for all latency metrics
- Throughput as time series (not single average)
- Error rate breakdown by error type
- Resource utilization time series

### BER-004 — Regression Threshold
A benchmark result is a regression if any P99 latency metric increases by > 20% compared to the previous release. Regressions block release.

---

*End of Performance Benchmark Specification — `022-PERFORMANCE_BENCHMARK_SPEC.md`*
