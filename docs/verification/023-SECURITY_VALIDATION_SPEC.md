# AEOS Phase 9 DRP — Security Validation Specification

**Document:** `023-SECURITY_VALIDATION_SPEC.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## 1. Threat Model

### 1.1 Assets

| Asset | Sensitivity | Owner |
|-------|-------------|-------|
| Governance tokens (JWT) | High — authorize task execution | Policy Service |
| Agent task payloads | Medium — may contain user data or prompts | Submitting system |
| LLM responses (cached) | High — may contain sensitive analysis | Capability layer |
| Cluster Raft log | Critical — cluster membership state | Cluster Manager |
| mTLS private keys | Critical — service identity | Vault Agent |
| API credentials (api-key-xyz) | High — external system access | Secrets Management |
| Episodic memory | Medium — historical workflow data | Memory subsystem |

### 1.2 Threat Actors

| Actor | Capability | Primary Goal |
|-------|-----------|-------------|
| External attacker (no cluster access) | Network-level; may intercept unencrypted traffic | Task injection, credential theft |
| Malicious task submitter | Valid API key; can submit tasks | Governance bypass, capability abuse |
| Compromised worker pod | Code execution inside cluster | Lateral movement, data exfiltration |
| Compromised base image | Supply chain | Backdoor in all pods |
| Malicious plugin | Plugin registration rights | Kernel API abuse |

### 1.3 Threat Scenarios

| ID | Scenario | Category | Mitigating Control |
|----|----------|----------|------------------|
| T-01 | Replay of captured governance token | Auth | Token expiry + token_id revocation |
| T-02 | Governance bypass via fail-open mode | Logic | Fail-closed default + CRITICAL log |
| T-03 | Cross-workflow data leakage via LLM cache | Logic | Opt-in cache; cache key = prompt hash |
| T-04 | Worker impersonation (fake node_id) | Identity | mTLS cert required for join |
| T-05 | MITM on internal gRPC | Network | mTLS required for all internal services |
| T-06 | Raft log injection (unauthorized write) | Integrity | Raft leader validates AppendEntries source cert |
| T-07 | Secret exfiltration via pod environment | Supply chain | Secrets from Vault agent; no env var secrets |
| T-08 | Redis key collision across workflows | Isolation | Hashtag routing; per-workflow key namespace |
| T-09 | Unauthorized capability invocation | Authorization | Governance token lists allowed capabilities |
| T-10 | Supply chain: malicious base image | Supply chain | Pinned digests; image signing; OPA admission |

---

## 2. Zero Trust Validation

### ZT-001 — Every Service-to-Service Call is Authenticated

**Validation procedure:**
1. Deploy Wireshark/tcpdump on pod network interface
2. Capture 5 minutes of internal traffic
3. Verify: ALL connections use TLS (no plaintext)
4. Verify: Certificate subject matches expected service identity

**Pass criteria:** Zero plaintext connections observed in 5-minute capture window.

### ZT-002 — No Implicit Trust Based on Network Location

**Validation procedure:**
1. Create a test pod in the `aeos` namespace with no AEOS identity cert
2. Attempt to call each AEOS service endpoint
3. Verify: All calls rejected (TLS handshake failure or 401)

**Pass criteria:** Test pod with no cert cannot reach any AEOS service.

### ZT-003 — Lateral Movement Prevention

**Validation procedure:**
1. Deploy test pod simulating a compromised worker
2. Attempt to reach other worker pods directly (not via Kafka or CM)
3. Verify: NetworkPolicy blocks all worker-to-worker direct connections

```bash
# From compromised-worker pod:
kubectl exec compromised-worker -- nc -zv $OTHER_WORKER_IP 8000 -w 2
# Expected: Connection refused or timeout
```

**Pass criteria:** Worker-to-worker direct connections blocked by NetworkPolicy.

---

## 3. RBAC Verification

### RBAC-001 — Permission Scope Enforcement

**Test cases:**
```python
def test_task_submitter_cannot_read_other_user_results():
    """A user can only read results for their own task submissions."""
    
    token_user_a = get_api_token(user="user-a")
    token_user_b = get_api_token(user="user-b")
    
    # User A submits a task
    task_id = submit_task(token=token_user_a, payload="test")
    
    # User B attempts to read User A's task result
    response = get_task_result(task_id=task_id, token=token_user_b)
    assert response.status_code == 403

def test_reader_role_cannot_submit_tasks():
    reader_token = get_api_token(user="user-reader", role="reader")
    response = submit_task(token=reader_token, payload="test")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_PERMISSIONS"

def test_admin_can_revoke_any_token():
    admin_token = get_api_token(user="admin", role="admin")
    user_token_id = "some-token-id"
    
    response = revoke_token(token_id=user_token_id, auth_token=admin_token)
    assert response.status_code == 200
```

### RBAC-002 — Role Boundary Verification

Roles and their allowed operations (from governance policies):

| Role | submit_task | read_own_results | read_all_results | admin_operations |
|------|------------|-----------------|-----------------|----------------|
| `reader` | No | Yes | No | No |
| `submitter` | Yes | Yes | No | No |
| `operator` | Yes | Yes | Yes | No |
| `admin` | Yes | Yes | Yes | Yes |

Each row MUST have conformance tests verifying both allowed and denied operations.

### RBAC-003 — Revocation Propagation Timing

```python
async def test_revocation_propagates_to_all_workers_within_1_second():
    """INV-SEC-002: RBAC revocation < 1 second propagation."""
    
    # Create token for user-x
    token = issue_token(user="user-x")
    
    # Verify token accepted by all workers
    for worker in get_all_workers():
        assert worker.validate_token(token).valid == True
    
    # Revoke token
    revoke_time = time.monotonic()
    revoke_token(token.token_id)
    
    # Wait 1 second
    await asyncio.sleep(1.0)
    
    # Verify ALL workers reject token
    for worker in get_all_workers():
        result = worker.validate_token(token)
        assert result.valid == False
        assert result.reason == "token_revoked"
        
        propagation_time = result.invalidated_at - revoke_time
        assert propagation_time <= 1.0, \
            f"Worker {worker.node_id} received revocation after {propagation_time:.3f}s"
```

---

## 4. JWT Verification

### JWT-001 — Algorithm Whitelist

**Validation:**
```python
def test_jwt_rejects_none_algorithm():
    """CVE classic: JWT with alg=none must be rejected."""
    
    # Craft JWT with alg=none (no signature)
    header = base64url_encode(json.dumps({"alg": "none", "typ": "JWT"}))
    payload = base64url_encode(json.dumps({"task_type": "research.web_search", 
                                            "exp": future_time()}))
    malicious_token = f"{header}.{payload}."
    
    with pytest.raises(TokenValidationError, match="invalid_algorithm"):
        validator.validate(malicious_token)

def test_jwt_rejects_rs256_when_hs256_expected():
    """Algorithm confusion: server configured for HS256 must reject RS256."""
    rs256_token = create_token_with_algorithm("RS256")
    
    with pytest.raises(TokenValidationError, match="invalid_algorithm"):
        validator.validate(rs256_token)

def test_jwt_only_accepts_hs256():
    hs256_token = create_valid_token()  # Uses HS256
    result = validator.validate(hs256_token)
    assert result.valid == True
```

### JWT-002 — Expiry Enforcement

```python
def test_expired_token_rejected():
    expired_token = create_token(expires_at=int(time.time()) - 1)
    
    with pytest.raises(TokenValidationError, match="token_expired"):
        validator.validate(expired_token)

def test_token_expiring_in_future_accepted():
    future_token = create_token(expires_at=int(time.time()) + 3600)
    result = validator.validate(future_token)
    assert result.valid == True
```

### JWT-003 — Claim Validation

```python
def test_token_task_type_must_match_execution_context():
    token = create_token(allowed_task_types=["analysis.summarize"])
    
    with pytest.raises(TokenValidationError, match="task_type_not_authorized"):
        validator.validate_for_task(token, task_type="research.web_search")

def test_token_required_claims_present():
    """All required claims must be present."""
    required_claims = ["token_id", "task_id", "task_type", "issued_at", 
                       "expires_at", "decision", "policy_id"]
    
    for missing_claim in required_claims:
        incomplete_token = create_token_missing_claim(missing_claim)
        
        with pytest.raises(TokenValidationError, match="missing_required_claim"):
            validator.validate(incomplete_token)
```

---

## 5. mTLS Verification

### MTLS-001 — Client Certificate Required

```python
def test_service_requires_client_certificate():
    """Services must reject connections without client certs (mTLS)."""
    
    # Connect with TLS but no client cert
    context = ssl.create_default_context()
    context.load_verify_locations(AEOS_CA_CERT)
    # No context.load_cert_chain() — no client cert
    
    with pytest.raises(ssl.SSLError):
        grpc.secure_channel(
            "aeos-worker:9090",
            grpc.ssl_channel_credentials(
                root_certificates=open(AEOS_CA_CERT, "rb").read(),
                # No private_key or certificate_chain
            )
        )
```

### MTLS-002 — Certificate Rotation Without Downtime

```python
async def test_cert_rotation_does_not_drop_connections():
    """Certificate rotation must not cause connection drops (AC-NET-002)."""
    
    # Start background traffic
    traffic_task = asyncio.create_task(send_continuous_requests())
    
    # Trigger cert rotation
    await vault_agent.force_renewal()
    
    # Wait for renewal to complete
    await asyncio.sleep(5)
    
    # Verify no connection errors during rotation
    assert traffic_task.exception() is None
    assert traffic_errors == 0
```

### MTLS-003 — Leaf Certificate Expiry < 24 Hours

```python
def test_leaf_certificate_max_validity():
    """All leaf certificates must have validity <= 24 hours (AC-NET-002)."""
    
    for service_host in ALL_SERVICE_HOSTS:
        cert = get_tls_certificate(service_host)
        validity_seconds = cert.not_valid_after - cert.not_valid_before
        
        assert validity_seconds.total_seconds() <= 86400, \
            f"{service_host} cert validity {validity_seconds} exceeds 24 hours"
```

---

## 6. Audit Log Validation

### AUDIT-001 — Completeness

```python
def test_every_governance_evaluation_has_audit_log():
    """AC-GOV-005: Every governance evaluation must produce an audit log entry."""
    
    task_ids = [submit_task_and_get_id() for _ in range(100)]
    
    for task_id in task_ids:
        audit_entry = db.query(
            "SELECT * FROM governance_audit_log WHERE task_id = %s", 
            [task_id]
        )
        
        assert audit_entry is not None, f"No audit entry for task {task_id}"
        assert audit_entry["task_type"] is not None
        assert audit_entry["policy_id"] is not None
        assert audit_entry["decision"] in ["APPROVED", "REJECTED"]
        assert audit_entry["timestamp_ns"] is not None
        assert audit_entry["worker_node_id"] is not None

def test_rejected_governance_has_synchronous_audit_log():
    """Rejected decisions must have sync audit writes (fire-and-forget not permitted)."""
    
    # Submit task that will be rejected
    task_id = submit_task_expected_to_be_rejected()
    
    # Audit log must exist IMMEDIATELY (not async)
    audit_entry = db.query(
        "SELECT * FROM governance_audit_log WHERE task_id = %s", 
        [task_id]
    )
    assert audit_entry is not None
    assert audit_entry["decision"] == "REJECTED"
```

### AUDIT-002 — Retention

```python
def test_audit_log_retention_90_days():
    """Governance audit logs retained for 90 days minimum (AC-OBS-005)."""
    
    # Verify retention policy set
    retention_policy = db.query(
        "SELECT retention_days FROM audit_log_retention_policies "
        "WHERE log_type = 'governance'"
    )
    assert retention_policy["retention_days"] >= 90

def test_security_event_retention_365_days():
    retention_policy = db.query(
        "SELECT retention_days FROM audit_log_retention_policies "
        "WHERE log_type = 'security'"
    )
    assert retention_policy["retention_days"] >= 365
```

---

## 7. Secret Rotation Validation

### SECROT-001 — Vault-Sourced Secrets Only

```python
def test_no_secrets_in_environment_variables():
    """AC-SEC-003: Secrets must not be in environment variables."""
    
    for pod in get_all_pods():
        env_vars = pod.spec.containers[0].env
        
        for env_var in (env_vars or []):
            # Check for patterns that look like secrets
            suspicious_patterns = [
                re.compile(r"(?i)(password|secret|key|token|credential)"),
            ]
            for pattern in suspicious_patterns:
                if pattern.search(env_var.name):
                    # Value must be from Vault agent (empty/mounted), not literal
                    assert env_var.value is None or env_var.value == "", \
                        f"Pod {pod.metadata.name} has literal secret in {env_var.name}"
                    assert env_var.value_from is not None or \
                           is_vault_agent_volume_mount(pod, env_var.name)

def test_no_secrets_in_configmaps():
    """Secrets must not be stored in Kubernetes ConfigMaps."""
    
    for cm in get_all_configmaps():
        for key, value in (cm.data or {}).items():
            # No values that look like passwords, tokens, or keys
            assert not looks_like_secret(value), \
                f"ConfigMap {cm.metadata.name}.{key} appears to contain a secret"
```

### SECROT-002 — Secret Rotation Without Restart

```python
async def test_database_credential_rotation():
    """Rotating a Postgres credential must not require worker restart."""
    
    # Record current connections
    initial_connections = get_active_db_connections()
    
    # Rotate Postgres credential via Vault
    await vault.rotate_database_credential("aeos-postgres")
    
    # Wait for workers to pick up new credential (Vault agent renewal)
    await asyncio.sleep(10)
    
    # Verify workers still connected with new credential
    post_rotation_connections = get_active_db_connections()
    assert len(post_rotation_connections) >= len(initial_connections) * 0.9
    
    # Verify old credential rejected
    old_credential = get_previous_credential()
    connection_result = test_db_connection(credential=old_credential)
    assert connection_result == ConnectionResult.REJECTED
```

---

## 8. Replay Attack Protection

### REPLAY-001 — Token Replay Prevention

```python
def test_used_token_cannot_be_replayed():
    """A governance token used for one task cannot be used for another."""
    
    # Issue a token for task A
    token = issue_governance_token(task_id="task-A", task_type="research.web_search")
    
    # Use token for task A (legitimate)
    execute_task(task_id="task-A", token=token)
    
    # Attempt to use same token for task B (replay)
    with pytest.raises(TokenValidationError, match="task_id_mismatch"):
        execute_task(task_id="task-B", token=token)

def test_kafka_message_replay_handled_by_idempotency():
    """Replayed Kafka messages must not cause duplicate execution."""
    
    # Execute step once
    initial_result = await execute_step(wf_id="wf-1", step_id="step-1")
    
    # Replay the same Kafka message
    replayed_result = await execute_step(wf_id="wf-1", step_id="step-1")
    
    # Must return same result, not re-execute
    assert replayed_result == initial_result
    assert execution_count("wf-1", "step-1") == 1
```

---

## 9. Supply Chain Validation

### SC-001 — Container Image Signing

```python
def test_all_images_are_signed():
    """AC-SEC-005: All container images must be signed with Cosign."""
    
    for image in get_all_deployed_images():
        verification = cosign.verify(
            image=image,
            certificate_identity=AEOS_SIGNING_IDENTITY,
            certificate_oidc_issuer=AEOS_OIDC_ISSUER
        )
        assert verification.valid, f"Image {image} signature invalid or missing"

def test_admission_controller_rejects_unsigned_images():
    """OPA admission controller must block unsigned images."""
    
    result = kubectl_apply("""
    apiVersion: v1
    kind: Pod
    spec:
      containers:
        - image: unsigned-test-image:latest
    """)
    
    assert result.returncode != 0
    assert "image signature verification failed" in result.stderr
```

### SC-002 — Base Image Digest Pinning

```python
def test_all_dockerfiles_use_pinned_digests():
    """Images must use digest pins, not mutable tags (AC-SEC-005)."""
    
    import re
    
    for dockerfile_path in glob.glob("**/Dockerfile", recursive=True):
        content = open(dockerfile_path).read()
        from_lines = [l for l in content.splitlines() if l.startswith("FROM")]
        
        for from_line in from_lines:
            # Must use @sha256:... digest, not :tag
            assert "@sha256:" in from_line, \
                f"{dockerfile_path}: {from_line} uses tag not digest"
```

---

## 10. Container Hardening Verification

### CH-001 — Pod Security Standards

```python
def test_all_pods_meet_restricted_security_standard():
    """All AEOS pods must pass Kubernetes restricted pod security standard."""
    
    for pod in get_all_pods():
        security_ctx = pod.spec.security_context
        
        # Non-root user
        assert security_ctx.run_as_non_root == True
        assert security_ctx.run_as_user >= 1000
        
        # No privilege escalation
        for container in pod.spec.containers:
            assert container.security_context.allow_privilege_escalation == False
            
            # Read-only root filesystem
            assert container.security_context.read_only_root_filesystem == True
            
            # Capability drops
            assert "ALL" in container.security_context.capabilities.drop
            
            # No privileged
            assert container.security_context.privileged != True

def test_no_host_namespace_access():
    for pod in get_all_pods():
        assert pod.spec.host_network != True
        assert pod.spec.host_pid != True
        assert pod.spec.host_ipc != True
```

---

## Security Validation Gate

A Phase 9 deployment MUST pass all security validations before being considered production-ready:

| Validation Category | Tests | Required Pass Rate |
|--------------------|-------|-------------------|
| Zero Trust | ZT-001–003 | 100% |
| RBAC | RBAC-001–003 | 100% |
| JWT | JWT-001–003 | 100% |
| mTLS | MTLS-001–003 | 100% |
| Audit Logs | AUDIT-001–002 | 100% |
| Secret Rotation | SECROT-001–002 | 100% |
| Replay Protection | REPLAY-001 | 100% |
| Supply Chain | SC-001–002 | 100% |
| Container Hardening | CH-001 | 100% |
| **Total** | **All** | **100%** |

Any security validation failure MUST block production deployment. Security validations do not have "acceptable failure rates."

---

*End of Security Validation Specification — `023-SECURITY_VALIDATION_SPEC.md`*
