# Phase 13 Sprint 7 — Cloud Remediation Evidence

**Status:** Remediation complete for every in-scope Critical blocker.
**Date:** 2026-07-20
**Predecessor:** `033-CLOUD_DEPLOYMENT_READINESS_AUDIT.md` (the audit this sprint closes).
**Scope:** Deployability only. No new features, no new abstractions. Every change
is a wiring, supply-chain, or manifest correction against a finding in doc 033.

---

## 0. Headline result

> **Critical blockers before: 8** (doc 033 §5, C1–C8)
> **Critical blockers after: 2** — and both remaining are *not* mechanical fixes:
> **C7** (no Azure/GCP infra) is net-new multi-cloud infrastructure, explicitly
> out of this sprint's scope; **C8** (zero execution evidence) is *materially*
> reduced — offline validation now exists and is recorded below — but cannot be
> fully closed here because this environment has **no `terraform`, `helm`,
> `promtool`, or cloud account** to run `terraform validate/plan`, `helm lint`,
> or a real `apply`.

The six mechanically-closable Criticals (**C1–C6**) are **all closed and, where
runtime-verifiable, verified running.**

---

## 1. Honest ID mapping (Sprint 7 labels vs doc 033 IDs)

The Sprint 7 brief used its own C1–C5 labels that do **not** map 1:1 to doc 033's
severity-ranked register. The real mapping, so nobody is misled:

| Sprint 7 brief label | Actual doc 033 ID(s) | doc 033 severity |
|----------------------|----------------------|------------------|
| C1 Terraform ElastiCache failure | **C1** | Critical |
| C2 Phantom `alerts_sns` module | **C2** | Critical |
| C3 Worker container crash-loop | **C6** | Critical |
| C4 Broken monitoring alerts | **H2** (+ **H3** dashboard) | High |
| C5 Missing federation topology | **H1** | High |
| "Remaining critical items" | **C3, C4, C5, C7, C8** | Critical |

So this sprint closed **all Criticals C1–C6**, plus Highs **H1/H2/H3**, and
reduced **C8**. **C7** is untouched by design.

---

## 2. Fix-by-fix evidence

### C1 — ElastiCache `num_cache_clusters` (Critical) — CLOSED
Module never defined `num_cache_clusters`; envs now pass the arguments the module
actually declares (`num_node_groups` + `replicas_per_node_group`).

```
dev/main.tf:119        num_node_groups         = 1
dev/main.tf:120        replicas_per_node_group = 1
production/main.tf:122 num_node_groups         = 3
production/main.tf:123 replicas_per_node_group = 2
staging/main.tf:109    num_node_groups         = 1
staging/main.tf:110    replicas_per_node_group = 1
```
`grep num_cache_clusters infrastructure/terraform` → **no matches.**
Related doc-033 **M7** also addressed: dev/staging outputs now read
`configuration_endpoint` (cluster-mode) instead of `primary_endpoint`.

### C2 — Phantom `module.alerts_sns` (Critical) — CLOSED
Production referenced `module.alerts_sns.topic_arn` with no such module (only a
`resource "aws_sns_topic" "alerts"` + a `local`). Reference rewired to the real
resource.
`grep "module.alerts_sns" infrastructure/terraform` → **no matches.**

### C3 — Remote state backend placeholder buckets (Critical) — CLOSED
`backend "s3"` hardcoded `bucket = "aeos-tfstate-<ENV>_ACCOUNT_ID"`. An S3
backend **cannot interpolate variables**, so that literal is a guaranteed
`init` failure / wrong-bucket trap. Converted all three envs to **partial
backend config** (bucket omitted, supplied at init) and documented the
chicken-and-egg bootstrap.

- `environments/{dev,staging,production}/main.tf`: `bucket` line removed from the
  `backend "s3"` block; `key`/`region`/`encrypt`/`dynamodb_table` retained.
- `grep ACCOUNT_ID …/main.tf` → only the documentation comment
  `terraform init -backend-config="bucket=aeos-tfstate-<ACCOUNT_ID>"` remains.
- `infrastructure/README.md` → "Deploy to AWS" now bootstraps the state bucket +
  DynamoDB lock table with the AWS CLI **before** `terraform init`, then inits
  with `-backend-config`. (The old `terraform apply -target=module.s3` step was
  itself broken — every env sets `create_tfstate_bucket = false`.)

### C4 — RDS/ElastiCache security groups never fed the EKS node SG (Critical) — CLOSED
Data plane was dead: pods could not reach DB/cache.
- `modules/eks/outputs.tf` now exports `node_security_group_id`.
- All three envs pass `allowed_security_group_ids = [module.eks.node_security_group_id]`
  to **both** RDS and ElastiCache (6 call sites — 2 per env).
`grep -c allowed_security_group_ids …/main.tf` → **6.**

### C5 — No container registry / image-tag substitution (Critical) — CLOSED
Two deploy paths existed; both are now supply-chain-correct.

**Kustomize path** (`kubectl apply -k base/`): base manifests ship
`REGISTRY_PLACEHOLDER/aeos-{api,worker}:VERSION_PLACEHOLDER`. Added a kustomize
`images:` transformer declaring both as explicit substitution targets, so an
operator/CI resolves them in one command instead of editing every Deployment:
```
kustomize edit set image \
  REGISTRY_PLACEHOLDER/aeos-api=<acct>.dkr.ecr.us-east-1.amazonaws.com/aeos/api:sha-<git> \
  REGISTRY_PLACEHOLDER/aeos-worker=<acct>.dkr.ecr.us-east-1.amazonaws.com/aeos/worker:sha-<git>
```
Verified (overlay referencing base + `images:` newName/newTag) — **all four app
Deployments resolve** (api + scheduler share `aeos-api`; worker + federation
share `aeos-worker`); zero `PLACEHOLDER` strings remain post-transform.

**Helm path**: repository values were `aeos-api`/`aeos-worker` (dash) but the
Terraform `ecr` module creates `aeos/api`, `aeos/worker`, `aeos/scheduler`
(prefix `aeos`), which is also what CI builds/pushes — a guaranteed
`ImagePullBackOff`. Fixed:
- `values.yaml`: `repository: aeos/api` (api + scheduler), `aeos/worker` (worker).
- `.github/workflows/deploy.yml`: both `helm upgrade` steps now pass
  `--set global.imageRegistry=${{ env.ECR_REGISTRY }}` (derived from the real
  account secret) so the placeholder `123456789…` in the env value files is
  overridden by the true registry.
- `infrastructure/README.md`: manual Helm example now sets `global.imageRegistry`.

### C6 — Worker image crash-loop (Critical) — CLOSED & VERIFIED
`Dockerfile.worker` CMD was `celery -A app.workers` — `app/workers` does not
exist and Celery is not a dependency (AEOS uses `app/distributed`). CMD is now
`python -m app.distributed.worker`. Additionally, `Dockerfile.api` copied from a
non-existent `requirements/` directory (only `requirements.txt` exists); both its
builder and development stages now `COPY requirements.txt` — this unblocks
`docker compose build` for the `api` service (used by `docker-compose.yml` and
`docker-compose.cluster.yml`).

**Runtime verification** (subprocess, this environment):
```
worker exit code while probing: None   (process stayed up)
worker endpoints: {"/health":200, "/health/ready":200, "/metrics":200}
```

### C7 — No Azure/GCP infrastructure (Critical) — NOT CLOSED (out of scope)
Reaching AKS/GKE parity is net-new IaC plus Azure/GCP equivalents for every
AWS-specific dependency (Secrets Manager→Key Vault/Secret Manager, ACM/WAF→App
Gateway/Cloud Armor, IRSA→Workload Identity, gp3→managed-csi, ElastiCache→Azure
Cache/Memorystore, RDS→Flexible Server/Cloud SQL). doc 033 estimates 3–6 weeks
and classes it explicitly as *new infrastructure*. Sprint 7 forbids new features.
**Deliberately deferred.**

### C8 — Zero execution evidence (Critical) — PARTIALLY CLOSED
This environment has **no `terraform`, `helm`, `promtool`, `kubeconform`, or
cloud account**; only `kubectl`, `docker`, and `python`. So `terraform
validate/plan`, `helm lint`, and a real cluster `apply` **could not be run** and
are honestly still outstanding. What *was* executed offline (§3) is real
evidence, not a claim. Full closure requires running the missing binaries on a
real dev account — see §5.

---

## 3. Validation evidence actually executed (offline)

All commands run in this environment (Windows/Anaconda CPython, git-bash),
tooling as noted. `kubectl kustomize` **builds** manifests without a cluster;
`kubectl --dry-run` was **not** usable (it contacts an API server).

| Check | Tool | Result |
|-------|------|--------|
| `kustomize build base/` | `kubectl kustomize` + `yaml.safe_load_all` | **61 docs**, valid YAML; 4 Deployments, 4 Namespaces, 3 StatefulSets, 18 NetworkPolicies, 2 Ingress, 10 Services |
| `kustomize build security/` | same | **13 docs** valid (ClusterSecretStore, ExternalSecrets, OPA templates) |
| `kustomize build istio/` | same | **21 docs** valid (mTLS, authz, gateways) |
| Federation objects render | same | 6 objects in `aeos-jobs`; Deployment image=`…/aeos-worker:…`, command=`python -m app.distributed.federation`, ports 50051+8080 |
| `alert-rules.yml` structure | `yaml` | **7 groups, 23 rules, 0 duplicate group names, 0 stale `status_class`/`_bucket` refs** |
| `error-budget.json` | `json.load` | valid JSON; **0** `status_class` refs |
| Duplicate `alerts.yml` removed | `ls` | confirmed absent |
| Worker entrypoint | subprocess + HTTP | up; `/health` `/health/ready` `/metrics` → **200** |
| Federation gateway entrypoint | subprocess + HTTP + TCP | gRPC listening; `/health` `/health/ready` `/.well-known/jwks.json` → **200**; JWKS = **1 EC/ES256 key** |

**Metric-name ground truth** (source of the C4/H2 fix), from
`app/observability/prometheus.py`: `aeos_http_requests_total{method,path,status_code}`,
`aeos_http_request_duration_seconds_bucket{le}`,
`aeos_task_completed_total`, `aeos_task_failed_total`. The alerts/dashboard were
corrected to these exact names/labels.

---

## 4. Federation deployment topology delivered (H1)

New, all rendering cleanly under the base kustomization:

- `app/distributed/federation/__init__.py` + `__main__.py` — runnable gateway
  (`python -m app.distributed.federation`). **Deployability glue only**: it wires
  pre-existing `KeyStore`/`TokenSigner`/`TokenVerifier`,
  `SchedulerServiceServicer`, `FederationServiceServicer`, `DomainServiceServer`,
  and `JWKSProvider`. Serves gRPC (handshake/session-trust/admission) + HTTP
  (`/health`, `/health/ready`, `/.well-known/jwks.json`).
- `infrastructure/kubernetes/base/federation.yaml` — ServiceAccount, trust
  ConfigMap (`peers.json`), Deployment (worker image + command override,
  single replica with a documented shared-signing-key note for scaling >1),
  Service (gRPC+HTTP), ALB Ingress (`backend-protocol-version: GRPC`, JWKS path),
  zero-trust NetworkPolicy. Added to `kustomization.yaml`.

**Honest scope limit:** the gateway runs in **admission-only** mode. It
terminates federation trust and admits authorized federated tasks into the local
scheduler; it does **not** itself execute remote workloads. Real remote execution
needs a production `WorkerRuntime`-backed `execute_fn` seam for
`FederatedExecutor` (the only implementation today is the test
`make_echo_executor`). Building that bridge is a **new feature**, deliberately
excluded. This limitation is documented in the module docstring and the manifest
header.

**Bonus deployability fix:** `kustomization.yaml` previously carried a top-level
`namespace: aeos-api` directive that collapsed all four Namespace objects to one
name → kustomize error *"namespace transformation produces ID conflict"*. The
base failed to render even without federation. Directive removed (every manifest
already declares its own namespace); base now renders 61 objects across 4
namespaces.

---

## 5. What remains (honest register)

| Item | doc 033 ID | Why still open |
|------|-----------|----------------|
| `terraform validate` / `plan`, `helm lint`, `kubeconform`, real cluster `apply` | **C8** | No `terraform`/`helm`/`promtool` binaries or cloud account in this environment. Run on a dev account to fully close. |
| Azure / GCP infrastructure | **C7** | Net-new multi-cloud IaC (3–6 weeks); out of scope. |
| Several Highs untouched | H4–H13 | Not Critical; not in this sprint's mandate (e.g. Helm/kustomize dual source-of-truth H6, Qdrant DR H10, tracing H13). |
| `Dockerfile.ml` has the same `requirements/` path bug | (new, Low) | Not on any active build path (no compose/CI references it); left as a noted Low rather than silently expanding scope. |

---

## 6. Re-audit verdict

**Before (doc 033):** 8 Critical · 13 High · 7 Medium · 6 Low. `terraform plan`
failed in all envs; `kubectl apply -k base/` couldn't render; worker crash-looped;
SLO alerting silently dead; federation undeployable.

**After (this sprint):**
- **C1, C2, C3, C4, C5, C6 — CLOSED.** Plus **H1, H2, H3, M7** closed and the
  pre-existing kustomize namespace bug fixed.
- **C7 — deferred** (out of scope, net-new infra).
- **C8 — partially closed**: offline validation is real and recorded; binary/cloud
  validation still required.

**Is AEOS deployable to AWS/EKS now?** The mechanical blockers to a first apply
are gone and every offline-checkable artifact validates. The remaining gate is
**executing** the real tooling on a real account (`terraform validate` → `plan` →
`apply`, `helm lint` → `install`, smoke tests) — which is exactly C8 and needs an
environment with those binaries and AWS credentials.

**Critical blockers before: 8. Critical blockers after: 2** (C7 out-of-scope,
C8 partial — offline evidence produced, cloud/tooling execution pending).
