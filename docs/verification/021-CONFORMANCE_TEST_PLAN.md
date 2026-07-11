# AEOS Phase 9 DRP — Conformance Test Plan

**Document:** `021-CONFORMANCE_TEST_PLAN.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## Purpose

This document defines the automated conformance tests that verify every Phase 9 implementation conforms to the Architecture Contract (`015`), invariants (`019`), protocols (`016`), and state machines (`017`). Tests are organized by conformance tier. All tests MUST pass before Phase 9B implementation is considered complete.

---

## Conformance Tiers

| Tier | Name | Description | When run |
|------|------|-------------|---------|
| T1 | Unit Contract Tests | Test individual components against contract | Every commit |
| T2 | Integration Protocol Tests | Test protocol sequences end-to-end | Every PR |
| T3 | State Machine Tests | Verify state machine transitions | Every PR |
| T4 | Distributed Correctness Tests | Multi-node invariant verification | Pre-merge to main |
| T5 | API Conformance Tests | External API contract tests | Pre-release |
| T6 | Security Conformance Tests | Security contract verification | Pre-release |
| T7 | Cloud Deployment Tests | Infrastructure contract verification | Pre-deployment |

---

## T1 — Unit Contract Tests

### CT-T1-001 — Execution Lease Acquisition
**Contract:** AC-EXEC-001  
**Invariant:** INV-EXEC-004

```python
# Test: Worker MUST NOT execute step if lease acquisition fails
def test_lease_acquisition_failure_prevents_execution():
    redis_mock = MockRedis()
    redis_mock.setnx_returns = 0  # Lease already held
    
    worker = Worker(redis=redis_mock)
    result = worker.try_execute_step(step_id="step-1", wf_id="wf-abc")
    
    assert result == StepResult.SKIPPED_LEASE_HELD
    assert worker.execute_count("step-1") == 0

# Test: Worker proceeds when lease acquired
def test_lease_acquisition_success_permits_execution():
    redis_mock = MockRedis()
    redis_mock.setnx_returns = 1  # Lease acquired
    
    worker = Worker(redis=redis_mock)
    result = worker.try_execute_step(step_id="step-1", wf_id="wf-abc")
    
    assert result != StepResult.SKIPPED_LEASE_HELD
    assert worker.execute_count("step-1") == 1
```

### CT-T1-002 — Idempotency Key Check
**Contract:** AC-EXEC-003  
**Invariant:** INV-EXEC-001

```python
def test_idempotency_key_prevents_re_execution():
    redis_mock = MockRedis()
    redis_mock.set("{wf:abc}:step:1:idem", "1")  # Already executed
    
    executor = StepExecutor(redis=redis_mock)
    result = executor.execute(step=step, wf_id="abc")
    
    assert result.from_cache == True
    assert executor.llm_calls == 0  # LLM was NOT called

def test_missing_idempotency_key_allows_execution():
    redis_mock = MockRedis()
    # No idem key — first execution
    
    executor = StepExecutor(redis=redis_mock)
    result = executor.execute(step=step, wf_id="abc")
    
    assert result.from_cache == False
    assert executor.llm_calls == 1
```

### CT-T1-003 — Fail-Closed Governance
**Contract:** AC-GOV-001, AC-GOV-002  
**Invariant:** INV-EXEC-005

```python
def test_governance_no_matching_policy_returns_rejected():
    evaluator = PolicyEvaluator(policies=[])  # Empty policy DB
    result = evaluator.evaluate("novel.unknown_task_type", submitter_id="user-1")
    
    assert result.decision == Decision.REJECTED
    assert result.reason == "no_policy_matched"

def test_governance_timeout_returns_rejected():
    slow_db = SlowDatabase(delay_s=6)  # Exceeds 5s timeout
    evaluator = PolicyEvaluator(db=slow_db, timeout_s=5)
    result = evaluator.evaluate("research.web_search", submitter_id="user-1")
    
    assert result.decision == Decision.REJECTED
    assert result.reason == "policy_evaluation_timeout"

def test_governance_fail_open_env_triggers_warning(caplog):
    with patch.dict(os.environ, {"AEOS_GOVERNANCE_FAIL_OPEN": "true"}):
        service = PolicyService()
    
    assert "CRITICAL" in caplog.text
    assert "AEOS_GOVERNANCE_FAIL_OPEN" in caplog.text
```

### CT-T1-004 — State Machine Invalid Transition
**Contract:** SMR-002, SMR-005  
**Invariant:** SM-TASK

```python
def test_task_completed_to_executing_is_invalid():
    task = Task(state=TaskState.COMPLETED)
    
    with pytest.raises(StateMachineViolation) as exc:
        task.transition(TaskEvent.EXECUTION_STARTED)
    
    assert exc.value.machine == "SM-TASK"
    assert exc.value.from_state == TaskState.COMPLETED
    assert exc.value.event == TaskEvent.EXECUTION_STARTED

def test_task_failed_can_transition_to_queued_for_retry():
    task = Task(state=TaskState.FAILED, retry_count=0, max_retries=3)
    task.transition(TaskEvent.RETRY_SCHEDULED)
    
    assert task.state == TaskState.QUEUED
```

### CT-T1-005 — Redis Key Hashtag Schema
**Contract:** AC-MEM-001  
**Invariant:** INV-MEM-001

```python
def test_wf_key_uses_hashtag():
    key = _wf_key(workflow_id="abc123", suffix="step:1:result")
    assert key == "{wf:abc123}:step:1:result"
    assert key.startswith("{wf:")

def test_multi_exec_keys_same_hashtag():
    # Verify that Phase 1 checkpoint uses keys with same hashtag
    checkpoint = TwoPhaseCheckpoint(wf_id="abc123", step_id="1")
    keys = checkpoint.get_phase1_keys()
    
    hashtags = {extract_hashtag(k) for k in keys}
    assert len(hashtags) == 1  # All keys share the same hashtag

def test_cross_workflow_transaction_raises():
    pipeline = redis.pipeline(transaction=True)
    pipeline.set(_wf_key("wf1", "step:1:result"), "x")
    pipeline.set(_wf_key("wf2", "step:1:result"), "y")  # Different workflow!
    
    with pytest.raises(CrossWorkflowTransactionError):
        validate_pipeline_keys(pipeline)
```

### CT-T1-006 — Consumer Group Assignment
**Contract:** AC-COMP-003 (indirectly), INV-DIST-001  
**Protocol:** PROTO-006

```python
def test_task_consumer_uses_shared_group():
    consumer_factory = KafkaConsumerFactory(node_id="worker-042")
    task_consumer = consumer_factory.create_task_consumer()
    
    assert task_consumer.group_id == "aeos-workers"

def test_event_consumer_uses_per_worker_group():
    consumer_factory = KafkaConsumerFactory(node_id="worker-042")
    event_consumer = consumer_factory.create_event_consumer()
    
    assert event_consumer.group_id == "aeos-worker-worker-042"
    assert "worker-042" in event_consumer.group_id

def test_event_consumer_group_ids_differ_across_workers():
    factory_a = KafkaConsumerFactory(node_id="worker-001")
    factory_b = KafkaConsumerFactory(node_id="worker-002")
    
    assert factory_a.create_event_consumer().group_id != \
           factory_b.create_event_consumer().group_id
```

### CT-T1-007 — LLM Cache Opt-In
**Contract:** AC-MEM-005  
**Invariant:** INV-MEM-001 (indirectly)

```python
def test_llm_cache_default_is_disabled():
    cap = CapabilityNode(name="research.web_search")
    assert cap.cacheable == False

def test_llm_call_without_cache_flag_does_not_cache():
    cache = LLMResponseCache(redis=MockRedis())
    capability = ResearchCapability(cacheable=False)
    
    result = capability.call(prompt="What is AEOS?")
    
    assert cache.get_call_count() == 0  # Cache never checked or written

def test_llm_call_with_cache_flag_uses_cache():
    cache = LLMResponseCache(redis=MockRedis())
    capability = ResearchCapability(cacheable=True, cache=cache)
    
    result1 = capability.call(prompt="What is AEOS?")
    result2 = capability.call(prompt="What is AEOS?")  # Same prompt
    
    assert cache.hit_count == 1
    assert result1 == result2
```

### CT-T1-008 — Circuit Breaker States
**Contract:** SM-CIRCUIT-BREAKER  
**ADR Reference:** ADR-009

```python
def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=5, reset_timeout_s=30)
    
    for _ in range(5):
        cb.record_failure()
    
    assert cb.state == CircuitBreakerState.OPEN

def test_circuit_breaker_rejects_in_open_state():
    cb = CircuitBreaker(state=CircuitBreakerState.OPEN)
    
    with pytest.raises(CircuitBreakerOpenError):
        cb.call(lambda: "ok")

def test_circuit_breaker_half_open_after_reset_timeout():
    cb = CircuitBreaker(state=CircuitBreakerState.OPEN, reset_timeout_s=0.1)
    time.sleep(0.2)
    
    assert cb.state == CircuitBreakerState.HALF_OPEN

def test_circuit_breaker_state_names_are_correct():
    # Verify no "SHUT" or "TESTING" aliases exist
    valid_states = {CircuitBreakerState.CLOSED, CircuitBreakerState.OPEN, CircuitBreakerState.HALF_OPEN}
    assert set(CircuitBreakerState) == valid_states
```

---

## T2 — Integration Protocol Tests

### CT-T2-001 — Two-Phase Checkpoint (Success Path)
**Protocol:** PROTO-008  
**Invariant:** INV-EXEC-002

```python
async def test_two_phase_checkpoint_success_path():
    redis = get_test_redis()
    kafka = get_test_kafka()
    
    worker = Worker(redis=redis, kafka=kafka)
    
    # Execute step and verify checkpoint protocol
    result = await worker.execute_and_checkpoint(
        wf_id="wf-test-001",
        step_id="step-1",
        executor=MockExecutor(result="success")
    )
    
    # Verify Phase 1: all three keys written atomically
    assert redis.get("{wf:wf-test-001}:step:1:result") == b"success"
    assert redis.get("{wf:wf-test-001}:step:1:status") == b"COMPLETED"
    assert redis.exists("{wf:wf-test-001}:step:1:idem") == 1
    
    # Verify Phase 2: next_published set
    assert redis.get("{wf:wf-test-001}:step:1:next_published") == b"1"
    
    # Verify offset committed
    committed_offset = kafka.get_committed_offset("aeos-workers", 0)
    assert committed_offset > 0
    
    # Verify lease released
    assert redis.exists("{wf:wf-test-001}:step:1:lease") == 0
```

### CT-T2-002 — Checkpoint Recovery (Phase 2 Missing)
**Protocol:** PROTO-009  
**Invariant:** INV-CHKPT-001

```python
async def test_orphan_scanner_recovers_missing_next_published():
    redis = get_test_redis()
    kafka = get_test_kafka()
    
    # Simulate Phase 1 complete, Phase 2 missing
    redis.set("{wf:wf-test-002}:step:5:result", serialize({"answer": 42}))
    redis.set("{wf:wf-test-002}:step:5:status", "COMPLETED")
    redis.setex("{wf:wf-test-002}:step:5:idem", 86400, "1")
    # next_published NOT set — simulates crash after Phase 1

    scanner = OrphanScanner(redis=redis, kafka=kafka, execution_plan=mock_plan)
    await scanner.scan_once()
    
    # Verify next task published to Kafka
    published = kafka.get_produced_messages("aeos.tasks.normal")
    assert len(published) == 1
    assert published[0].step_id == "step-6"
    
    # Verify next_published set
    assert redis.get("{wf:wf-test-002}:step:5:next_published") == b"1"
```

### CT-T2-003 — Node Join Protocol
**Protocol:** PROTO-001  
**Contract:** AC-KERN-003

```python
async def test_node_join_registers_capabilities_before_partitions():
    """Capabilities MUST be registered before Kafka partition assignment."""
    
    cr = CapabilityRegistryTestDouble()
    kafka_admin = KafkaAdminTestDouble()
    cm = ClusterManager(capability_registry=cr, kafka_admin=kafka_admin)
    
    call_order = []
    cr.on_register = lambda: call_order.append("register_caps")
    kafka_admin.on_assign = lambda: call_order.append("assign_partitions")
    
    await cm.handle_join_request(JoinRequest(
        node_id="worker-test",
        capabilities=["research.web_search"]
    ))
    
    assert call_order.index("register_caps") < call_order.index("assign_partitions")
```

### CT-T2-004 — Governance Token Validation Chain
**Protocol:** PROTO-013  
**Contract:** AC-EXEC-005

```python
async def test_worker_validates_token_before_execution():
    """Worker must reject execution on any token validation failure."""
    
    # Test 1: Invalid signature
    bad_sig_token = create_token(tamper_signature=True)
    with pytest.raises(TokenValidationError, match="invalid_signature"):
        await worker.execute_step(token=bad_sig_token, step=mock_step)
    
    # Test 2: Expired token
    expired_token = create_token(expires_at=int(time.time()) - 3600)
    with pytest.raises(TokenValidationError, match="token_expired"):
        await worker.execute_step(token=expired_token, step=mock_step)
    
    # Test 3: Revoked token
    revoked_token = create_token()
    worker.revocation_cache[revoked_token.token_id] = "REVOKED"
    with pytest.raises(TokenValidationError, match="token_revoked"):
        await worker.execute_step(token=revoked_token, step=mock_step)
    
    # Test 4: Wrong task type
    wrong_type_token = create_token(allowed_task_types=["analysis.summarize"])
    with pytest.raises(TokenValidationError, match="task_type_not_authorized"):
        await worker.execute_step(token=wrong_type_token, step=research_step)
```

---

## T3 — State Machine Tests

### CT-T3-001 — Complete Task State Machine Transition Coverage

```python
@pytest.mark.parametrize("from_state,event,expected_to", [
    (TaskState.SUBMITTED, TaskEvent.TOKEN_ISSUED, TaskState.QUEUED),
    (TaskState.SUBMITTED, TaskEvent.GOVERNANCE_REJECTED, TaskState.FAILED),
    (TaskState.QUEUED, TaskEvent.LEASE_ACQUIRED, TaskState.ACCEPTED),
    (TaskState.QUEUED, TaskEvent.DEADLINE_EXCEEDED, TaskState.TIMEOUT),
    (TaskState.ACCEPTED, TaskEvent.EXECUTION_STARTED, TaskState.EXECUTING),
    (TaskState.EXECUTING, TaskEvent.STEP_COMPLETE, TaskState.COMPLETED),
    (TaskState.EXECUTING, TaskEvent.STEP_FAILED, TaskState.FAILED),
    (TaskState.EXECUTING, TaskEvent.CANCEL_REQUESTED, TaskState.CANCELLED),
    (TaskState.FAILED, TaskEvent.RETRY_SCHEDULED, TaskState.QUEUED),
])
def test_valid_task_transitions(from_state, event, expected_to):
    task = Task(state=from_state)
    task.transition(event)
    assert task.state == expected_to

@pytest.mark.parametrize("from_state,event", [
    (TaskState.COMPLETED, TaskEvent.EXECUTION_STARTED),
    (TaskState.COMPLETED, TaskEvent.RETRY_SCHEDULED),
    (TaskState.CANCELLED, TaskEvent.TOKEN_ISSUED),
    (TaskState.FAILED, TaskEvent.EXECUTION_STARTED),  # Must go through QUEUED
    (TaskState.TIMEOUT, TaskEvent.LEASE_ACQUIRED),
])
def test_invalid_task_transitions_raise(from_state, event):
    task = Task(state=from_state)
    with pytest.raises(StateMachineViolation):
        task.transition(event)
```

### CT-T3-002 — HyperKernel Boot Phase Ordering

```python
async def test_kernel_phases_execute_in_order():
    phase_log = []
    
    kernel = HyperKernel()
    kernel.on_phase_transition = lambda p: phase_log.append(p)
    
    await kernel.startup()
    
    assert phase_log == [
        KernelPhase.INITIALIZING,
        KernelPhase.LOADING,
        KernelPhase.CONFIGURING,
        KernelPhase.STARTING,
        KernelPhase.JOINING,
        KernelPhase.RUNNING,
    ]

async def test_kernel_cannot_skip_joining_phase():
    kernel = HyperKernel()
    
    # Verify that JOINING is present — capabilities not advertised before JOINING
    advertising_calls = []
    registry_mock = CapabilityRegistryMock()
    registry_mock.on_advertise = lambda: advertising_calls.append(kernel.current_phase)
    
    kernel.capability_registry = registry_mock
    await kernel.startup()
    
    for phase in advertising_calls:
        assert phase >= KernelPhase.JOINING, \
            f"Capability advertised during phase {phase} (must be JOINING or later)"
```

---

## T4 — Distributed Correctness Tests

### CT-T4-001 — Split-Brain Step Execution Prevention

```python
async def test_split_brain_prevents_duplicate_execution():
    """Two workers receiving the same task must not both execute it."""
    redis = get_shared_test_redis()
    
    worker_a = Worker(node_id="worker-a", redis=redis)
    worker_b = Worker(node_id="worker-b", redis=redis)
    
    task_message = TaskMessage(wf_id="wf-split", step_id="step-1", ...)
    
    # Both workers receive the same task simultaneously
    results = await asyncio.gather(
        worker_a.process_task(task_message),
        worker_b.process_task(task_message),
        return_exceptions=True
    )
    
    # Exactly one should succeed, one should skip
    successes = [r for r in results if r == ProcessResult.EXECUTED]
    skips = [r for r in results if r == ProcessResult.SKIPPED_LEASE_HELD]
    
    assert len(successes) == 1
    assert len(skips) == 1
    
    # Verify step executed exactly once
    assert get_execution_count(redis, "wf-split", "step-1") == 1
```

### CT-T4-002 — Consumer Group Fan-Out Verification

```python
async def test_governance_event_delivered_to_all_workers():
    """A governance event must be received by every worker."""
    
    kafka = get_test_kafka()
    workers = [Worker(node_id=f"worker-{i}", kafka=kafka) for i in range(5)]
    
    # Start all workers consuming
    await asyncio.gather(*[w.start_event_consumer() for w in workers])
    
    # Publish governance event
    await kafka.produce("aeos.events.governance", 
                        RevocationEvent(entity_id="key-xyz"))
    
    # Verify all workers received it within 2 seconds
    await asyncio.sleep(2)
    
    for worker in workers:
        assert worker.revocation_cache.get("key-xyz") == "REVOKED", \
            f"Worker {worker.node_id} did not receive revocation event"
```

### CT-T4-003 — Checkpoint Protocol Under Concurrent Load

```python
async def test_checkpoint_correctness_under_concurrent_workflows():
    """100 concurrent workflows must all checkpoint correctly."""
    redis = get_test_redis()
    kafka = get_test_kafka()
    
    async def run_workflow(wf_id: str):
        for step_id in range(10):
            worker = Worker(redis=redis, kafka=kafka)
            await worker.execute_and_checkpoint(wf_id, str(step_id), ...)
    
    # Run 100 concurrent workflows
    workflows = [run_workflow(f"wf-{i:04d}") for i in range(100)]
    await asyncio.gather(*workflows)
    
    # Verify all 1000 steps checkpointed correctly
    for wf_id in [f"wf-{i:04d}" for i in range(100)]:
        for step_id in range(10):
            assert redis.exists(f"{{wf:{wf_id}}}:step:{step_id}:idem") == 1
            assert redis.get(f"{{wf:{wf_id}}}:step:{step_id}:next_published") == b"1"
```

---

## T5 — API Conformance Tests

### CT-T5-001 — Health Endpoints
**Contract:** AC-IFACE-001

```python
@pytest.mark.parametrize("service_url", [
    "http://aeos-worker:8000",
    "http://aeos-cluster-manager:9090",
    "http://aeos-policy-service:8080",
    "http://aeos-capability-registry:9091",
])
def test_health_endpoints_exist(service_url):
    assert requests.get(f"{service_url}/healthz").status_code == 200
    assert requests.get(f"{service_url}/readyz").status_code == 200
    metrics = requests.get(f"{service_url}/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["Content-Type"]
```

### CT-T5-002 — Required Prometheus Metrics
**Contract:** AC-OBS-002

```python
def test_worker_emits_required_metrics():
    metrics_text = requests.get("http://aeos-worker:8000/metrics").text
    
    required_metrics = [
        "aeos_task_execution_duration_seconds_bucket",
        "aeos_task_queue_depth",
        "aeos_step_execution_duration_seconds_bucket",
        "aeos_governance_evaluation_duration_seconds_bucket",
        "aeos_redis_operation_duration_seconds_bucket",
        "aeos_kafka_consumer_lag",
        "aeos_worker_in_flight_tasks",
    ]
    
    for metric in required_metrics:
        assert metric in metrics_text, f"Required metric {metric} not found"
```

### CT-T5-003 — Histogram Format (not Summary)
**Contract:** AC-OBS-001  
**ADR Reference:** ADR-013

```python
def test_latency_metrics_use_histogram_not_summary():
    metrics_text = requests.get("http://aeos-worker:8000/metrics").text
    
    # Verify histogram format present
    assert "aeos_task_execution_duration_seconds_bucket" in metrics_text
    assert "aeos_task_execution_duration_seconds_sum" in metrics_text
    assert "aeos_task_execution_duration_seconds_count" in metrics_text
    
    # Verify no Summary quantile format
    assert "aeos_task_execution_duration_seconds{quantile=" not in metrics_text
```

---

## T6 — Security Conformance Tests

### CT-T6-001 — mTLS Enforcement

```python
def test_service_rejects_plaintext_connection():
    """Services must not accept plaintext (non-TLS) connections."""
    
    for service_port in [8000, 9090, 8080, 9091]:
        try:
            # Attempt plaintext HTTP connection
            response = requests.get(f"http://localhost:{service_port}/healthz", timeout=2)
            pytest.fail(f"Port {service_port} accepted plaintext connection")
        except (ConnectionRefusedError, requests.exceptions.ConnectionError):
            pass  # Expected: plaintext rejected

def test_service_rejects_untrusted_client_cert():
    """Services using mTLS must reject connections with self-signed client certs."""
    
    # Create a self-signed cert (not from AEOS CA)
    self_signed_cert = generate_self_signed_cert()
    
    try:
        response = requests.get(
            "https://aeos-worker:8000/healthz",
            cert=self_signed_cert,
            verify=AEOS_CA_CERT
        )
        pytest.fail("mTLS accepted untrusted client certificate")
    except requests.exceptions.SSLError:
        pass  # Expected: cert rejected
```

### CT-T6-002 — Container Security
**Contract:** AC-SEC-004

```python
def test_worker_pod_runs_as_non_root():
    pod = get_pod_spec("aeos-worker")
    assert pod.spec.security_context.run_as_non_root == True
    assert pod.spec.security_context.run_as_user >= 1000

def test_worker_pod_has_readonly_root_filesystem():
    pod = get_pod_spec("aeos-worker")
    for container in pod.spec.containers:
        assert container.security_context.read_only_root_filesystem == True

def test_worker_pod_drops_capabilities():
    pod = get_pod_spec("aeos-worker")
    for container in pod.spec.containers:
        assert "ALL" in container.security_context.capabilities.drop
```

---

## T7 — Cloud Deployment Tests

### CT-T7-001 — Kafka Partition Count
**Contract:** AC-CLOUD-005  
**Invariant:** INV-DIST-002

```python
def test_kafka_task_topics_have_200_partitions():
    admin = KafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP)
    
    for topic in TASK_TOPICS:
        metadata = admin.describe_topics([topic])
        partition_count = len(metadata[topic]["partitions"])
        assert partition_count == 200, \
            f"Topic {topic} has {partition_count} partitions, expected 200"
```

### CT-T7-002 — PodDisruptionBudgets Present
**Contract:** AC-CLOUD-001

```python
def test_pod_disruption_budgets_deployed():
    k8s = get_k8s_client()
    
    required_pdbs = {
        "aeos-cluster-manager-pdb": {"minAvailable": 2},
        "aeos-capability-registry-pdb": {"minAvailable": 2},
        "aeos-worker-pdb": {"minAvailable": 3},
    }
    
    for pdb_name, spec in required_pdbs.items():
        pdb = k8s.read_namespaced_pod_disruption_budget(pdb_name, "aeos")
        assert pdb.spec.min_available == spec["minAvailable"]
```

### CT-T7-003 — KEDA ScaledObject Present
**Contract:** AC-SCHED-003  
**ADR Reference:** ADR-008

```python
def test_keda_scaled_object_deployed():
    k8s = get_k8s_client()
    
    # Check KEDA ScaledObject exists
    scaled_objects = k8s.list_namespaced_custom_object(
        group="keda.sh", version="v1alpha1",
        namespace="aeos", plural="scaledobjects"
    )
    
    worker_so = next(
        (so for so in scaled_objects["items"] 
         if so["metadata"]["name"] == "aeos-worker-scaledobject"),
        None
    )
    
    assert worker_so is not None
    assert worker_so["spec"]["minReplicaCount"] == 3
    assert worker_so["spec"]["maxReplicaCount"] == 100
    
    # Verify HPA does NOT exist (conflict with KEDA)
    hpas = k8s.list_namespaced_horizontal_pod_autoscaler("aeos")
    worker_hpas = [h for h in hpas.items if h.metadata.name.startswith("aeos-worker")]
    assert len(worker_hpas) == 0, "HPA must not coexist with KEDA ScaledObject"
```

### CT-T7-004 — NetworkPolicy Enforcement
**Contract:** AC-NET-004

```python
def test_worker_network_policy_blocks_direct_worker_connections():
    """Worker pods must not be reachable from other worker pods."""
    
    # Attempt connection from worker-a to worker-b
    result = kubectl_exec(
        pod="aeos-worker-0",
        cmd=f"nc -zv {WORKER_1_POD_IP} 8000 -w 2"
    )
    assert result.returncode != 0, "Worker-to-worker connection should be blocked"

def test_worker_network_policy_allows_kafka():
    """Worker pods must be able to reach Kafka."""
    result = kubectl_exec(
        pod="aeos-worker-0",
        cmd=f"nc -zv {KAFKA_IP} 9092 -w 2"
    )
    assert result.returncode == 0
```

---

## Conformance Test Execution Matrix

| Test ID | Tier | Auto? | Gate | Pass Criteria |
|---------|------|-------|------|--------------|
| CT-T1-001 through CT-T1-008 | T1 | Yes | Commit | 100% pass |
| CT-T2-001 through CT-T2-004 | T2 | Yes | PR | 100% pass |
| CT-T3-001 through CT-T3-002 | T3 | Yes | PR | 100% pass |
| CT-T4-001 through CT-T4-003 | T4 | Yes | Pre-merge | 100% pass |
| CT-T5-001 through CT-T5-003 | T5 | Yes | Pre-release | 100% pass |
| CT-T6-001 through CT-T6-002 | T6 | Yes | Pre-release | 100% pass |
| CT-T7-001 through CT-T7-004 | T7 | Yes | Pre-deploy | 100% pass |

**Definition of Conformance:** A Phase 9 implementation is considered conformant when all tests in tiers T1–T7 pass with 100% success rate.

---

*End of Conformance Test Plan — `021-CONFORMANCE_TEST_PLAN.md`*
