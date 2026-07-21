# Phase 13 Sprint 6 — Cloud Deployment Readiness Audit

**Status:** Audit complete. **Verdict: NOT deployable to cloud today** without
remediation. AWS/EKS is ~3–5 engineer-days away; AKS/GKE do not exist as
infrastructure and are weeks away.
**Date:** 2026-07-20
**Scope:** A brutally honest audit of every infrastructure artifact in the repo,
testing the Phase 11 "PRODUCTION READY" claim against reality. No new features,
no new architecture — evidence, validation, and cloud-execution planning only.
**Method:** Full read of `infrastructure/terraform`, `infrastructure/helm`,
`infrastructure/kubernetes` (base/istio/security), `infrastructure/monitoring`,
`infrastructure/docker`, `docs/runbooks`, `infra/`; every headline finding
cross-checked against the actual application code and re-verified by hand
(§7 verification log).

---

## 1. Executive verdict

AEOS ships a **well-engineered but never-executed** infrastructure layer. The
artifacts reflect genuine cloud expertise — real VPC/EKS/IRSA/KMS Terraform,
STRICT-mTLS Istio, default-deny NetworkPolicies, OPA Gatekeeper, External Secrets
against AWS Secrets Manager, encrypted gp3 storage, PDBs, anti-affinity, and
command-rich DR runbooks. This is **not** a stub.

But it has **never been `terraform validate`d, `helm lint`ed, or applied**, and a
cross-check against the running application exposes multiple **phantom
dependencies** — infrastructure that manages components the app does not have, and
containers that launch code that does not exist. In its current committed state:

- `terraform plan` **fails immediately in all three environments** (hard argument
  and reference errors — verified).
- `helm install` and `kubectl apply -k base/` **cannot pull images** (placeholder
  registry) and the **worker container crash-loops** on a missing Celery module
  (verified).
- The **headline multi-cluster federation** (Sprints 3–4) has **zero deployment
  manifests** — it cannot be deployed at all.
- The **SLO / error-budget alerting is silently dead** (wrong metric label —
  verified), so an operator would get a false all-clear during an incident.
- There is **no Azure or GCP infrastructure whatsoever** — the "run on EKS, AKS,
  GKE" objective is only partially satisfiable.

The unqualified "PRODUCTION READY" label from Phase 11 is **not substantiated by
execution evidence.** It is closer to "production-*shaped*": ~70% of a real infra
layer, with the hard security scaffolding done and the last-mile wiring, image
supply chain, federation topology, and any validation run still missing.

---

## 2. What was audited (artifact inventory)

| Domain | Artifacts | Real? | One-line assessment |
|--------|-----------|-------|---------------------|
| Terraform | 8 modules (vpc, eks, ecr, elasticache, rds, s3, iam, cloudwatch) + 3 envs + versions.tf (33 `.tf`) | Real, AWS-only | Genuinely well-built modules; **never validated**; hard plan errors in all envs; no Azure/GCP |
| Helm | `aeos` chart: 15 templates + 4 value files | Real skeleton | Renders with errors (scheduler image helper), subcharts not vendored |
| K8s base | 17 manifests (deployments, statefulsets, netpol, rbac, hpa, ingress) | Real | Security-strong; placeholder images/certs; conflicts with Helm |
| Istio | mTLS, authz, destinationrules, virtualservices, canary | Real | STRICT mTLS + default-deny; solid |
| Security | ESO ClusterSecretStore, OPA constraints | Real | Store-name mismatch breaks secret sync |
| Monitoring | Prometheus (+rules), Alertmanager, Grafana (2 dashboards) | Real, partly broken | Alertmanager excellent; SLO alerts dead; one dashboard invalid JSON |
| DR runbooks | 6 DR + secret-rotation + README | Mostly real prose | Command-rich but some target components the app lacks; Qdrant uncovered |
| Docker | Dockerfile.api / .worker / .ml | 2 of 3 real | api production-grade; **worker broken**; ml pinned by tag |

---

## 3. Requirement-2 verification matrix

The Sprint 6 directive required verifying seven specific completeness dimensions.
Honest status of each:

| Dimension | Verdict | Evidence |
|-----------|---------|----------|
| **Terraform completeness** | ⚠️ Modules complete, envs broken | 8 real modules; but `plan` fails on `num_cache_clusters` (all envs) + phantom `module.alerts_sns` (prod). Never validated. |
| **Helm completeness** | ⚠️ Renders with errors | Scheduler image helper miswired; Bitnami subcharts unvendored/unpinned; two sources of truth vs base kustomize. |
| **K8s manifest completeness** | ⚠️ Structurally complete, not deployable | Placeholder images/certs; ESO store-name mismatch; HPAs need an absent metrics adapter. |
| **Monitoring stack completeness** | ❌ Silently non-functional in key paths | `/metrics` is real, but 5xx/latency alerts query labels/metrics that are never emitted → false all-clear; error-budget dashboard is invalid JSON. |
| **DR readiness** | ⚠️ Good prose, unverified + gaps | node/redis/kafka/cluster runbooks genuinely concrete; **no Qdrant DR/backup**; RDS/Postgres runbooks target a DB the app never connects to; no drill dates. |
| **Security posture** | ✅ Strong on paper, unproven | STRICT mTLS, default-deny, OPA, IRSA, ESO, KMS everywhere, no hardcoded secrets. Best part of the stack — but log-group KMS never wired and never applied. |
| **Federation deployment readiness** | ❌ Absent | Zero manifests/values/ports/gateways for cross-cluster gRPC or JWKS exposure. The headline capability is undeployable. |

---

## 4. Cloud coverage — the multi-cloud reality

**AWS-only. Definitively.** The entire repository contains exactly 33 `.tf`
files, all under `infrastructure/terraform/`, all `hashicorp/aws` (+ `tls`,
`random`). A repo-wide search for `azurerm`, `google_`, `aks`, `gke` returns only
false positives in Python/Markdown ("task", "makes", prose). There is:

- **No AKS** (Azure) Terraform, Bicep, or ARM.
- **No GKE** (GCP) Terraform or Deployment Manager.
- **No cloud-agnostic abstraction** — RDS, ElastiCache, ECR, S3, CloudWatch,
  IRSA, and ACM/WAF ingress annotations are all AWS-specific.

**Consequence for Requirement 5 (EKS + AKS + GKE commands):** only EKS is backed
by real IaC. The AKS/GKE command sets in the playbook (§Playbook) provision a
*cluster* via cloud CLIs, but there is **no AEOS-on-Azure / AEOS-on-GCP
infrastructure** to deploy onto them — the Helm chart's AWS assumptions (ESO→AWS
Secrets Manager, ACM/WAF ingress, gp3 storageClass, IRSA service accounts) would
all need Azure/GCP equivalents that do not exist. Presenting AKS/GKE as "ready"
would be dishonest; they are documented as *net-new work*, not *ready commands*.

---

## 5. Blocker register (severity-ranked)

Severity model: **Critical** = blocks a real deploy, causes silent data loss, or
breaks security. **High** = deploys but a core capability/safety property is
broken. **Medium** = degraded/operational-risk. **Low** = cosmetic/hygiene.
Findings marked ✓verified were re-confirmed by hand (§7).

### Critical

| ID | Blocker | Impact | Evidence |
|----|---------|--------|----------|
| C1 | Terraform `elasticache` call uses **`num_cache_clusters`**, an argument the module does not define | `terraform plan` fails in **all 3 envs** | ✓ `production/main.tf:117` vs `modules/elasticache/variables.tf:35,41` |
| C2 | Production references **`module.alerts_sns`** which does not exist (only a resource + local do) | `plan` fails in **production** | ✓ `production/main.tf:119` (no matching `module` block) |
| C3 | Remote state backend points at **placeholder buckets** (`aeos-tfstate-PROD_ACCOUNT_ID`), never bootstrapped (s3 tfstate module gated off) — chicken-and-egg | `terraform init` fails everywhere | `production/main.tf:18-24`; `modules/s3` `create_tfstate_bucket=false` |
| C4 | RDS + ElastiCache security groups are **never fed the EKS node SG** (envs don't pass `allowed_security_group_ids`; eks exports no node SG) | Even if applied, **pods cannot reach DB/cache** — dead data plane | `modules/rds/main.tf`, `modules/elasticache/main.tf` ingress `[]` |
| C5 | **No container registry / image-tag substitution.** Helm `imageRegistry: ""`; base manifests ship literal `REGISTRY_PLACEHOLDER`/`VERSION_PLACEHOLDER`; no build/push pipeline | Every pod **ImagePullBackOff** | `helm/aeos/values.yaml:7`; `kubernetes/base/api-deployment.yaml:61` |
| C6 | **Worker image is broken** — CMD `celery -A app.workers` but `app/workers` does not exist and Celery is not a dependency (AEOS uses `app/distributed`) | **Entire worker tier crash-loops** | ✓ `Dockerfile.worker:59,63`; `ls app/workers` → MISSING; no `import celery` in `app/` |
| C7 | **No Azure/GCP infrastructure at all** | Requirement 5 (AKS+GKE) only partly satisfiable; multi-cloud claim unsupported | Repo-wide: 33 `.tf`, all AWS |
| C8 | **Zero execution evidence** — nothing has been `terraform validate`d, `helm lint`ed, `kubeconform`ed, or applied | The "PRODUCTION READY" claim rests on unrun code | Absence of CI infra-validation passing on these paths; C1/C2/C6 prove it was never run |

### High

| ID | Blocker | Impact | Evidence |
|----|---------|--------|----------|
| H1 | **Federation undeployable** — no manifest/port/gateway/JWKS-exposure for cross-cluster gRPC | Headline Sprint 3–4 capability cannot run in a cluster | grep `infrastructure/` for federation/jwks → none |
| H2 | **SLO/error alerting silently dead** — exporter emits `status_code`, alerts query `status_class="5xx"`; latency alerts query `..._bucket` not `..._seconds_bucket` | **False all-clear** during incidents | ✓ `prometheus.py:134` emits `status_code`; `alert-rules.yml:36,47` |
| H3 | `error-budget.json` has `##` comment lines before the opening `{` → **invalid JSON** | SLO dashboard fails to provision | `grafana/dashboards/error-budget.json:1-3` |
| H4 | Helm **scheduler image helper miswired** (`include "aeos.image"` gets `global` dict, helper reads `.Values.imageRegistry`) | Scheduler renders wrong/errors | `scheduler-deployment.yaml:45` vs `_helpers.tpl:57-66` |
| H5 | **Bitnami subcharts (redis/kafka/qdrant) not vendored**, no `Chart.lock`; qdrant repo `0.x.x` unpinned | `helm install` fails until deps resolve against live repos | `Chart.yaml:23-43` |
| H6 | **Two conflicting sources of truth** (base kustomize vs Helm) — incompatible selectors, SA names, service DNS; neither declared authoritative | Applying both corrupts routing | base `app:` labels vs Helm `app.kubernetes.io/*` |
| H7 | **ESO ClusterSecretStore name mismatch** (`aws-secrets-manager` vs values `aws-secretsmanager`) | ExternalSecrets never sync → secret-dependent pods crash | `security/cluster-secret-store.yaml:6` vs `values.yaml:202` |
| H8 | **No TLS certificate automation** — ACM ARNs and Istio `aeos-tls-cert` are placeholders; no cert-manager Certificate | HTTPS ingress non-functional | `base/ingress.yaml:15`; `istio/virtual-services.yaml:74` |
| H9 | **EKS 1.29 + hardcoded addon eksbuild pins** are stale for 2026 | `apply` may fail on unavailable addon versions / forced upgrade | `eks/main.tf:196-219`; `production/main.tf:65` |
| H10 | **Qdrant has no DR runbook and no backup/restore** despite holding RAG vector state (the shipped product slice) | Data-loss risk with no recovery path | `docs/runbooks/` — no qdrant file |
| H11 | **RDS/Postgres DR + secret-rotation target a DB the app never connects to** (no `asyncpg`/`psycopg`/`sqlalchemy` in `app/`) | Runbooks are aspirational; real state stores under-covered | `dr-rds-failure.md`; grep `app/` → no PG client |
| H12 | **`/health/ready` referenced across DR docs + Dockerfile comment but not implemented** (only `/health` exists) | Documented readiness gating is fictional | ✓ `app/main.py:435` only `/health` |
| H13 | **Distributed tracing not emitted on request path** — middleware sets only `X-Trace-Id`; no spans, no OTLP export; Jaeger datasource unfed | No real tracing despite the datasource | `main.py:287-293`; `tracing.py:38` |

### Medium

| ID | Blocker | Evidence |
|----|---------|----------|
| M1 | CloudWatch log-group KMS never wired (module accepts `kms_key_arn`, no env passes it) → logs unencrypted | `modules/cloudwatch` default null |
| M2 | Helm worker probe is cosmetic (`python -c exit(0)` liveness, no readiness/startup) — a hung worker is never detected | `helm .../worker-deployment.yaml:81-85` |
| M3 | Base HPAs depend on custom/external metrics with no Prometheus-adapter/KEDA manifest provided → won't scale | `base/hpa.yaml:33,74` |
| M4 | Two Prometheus rule files both load; `alerts.yml` uses unprefixed metrics matching nothing → dead + duplicate alerts | `prometheus.yml:15-16` |
| M5 | Runbook `runbook_url`s are placeholders (`YOUR_ORG`, `wiki.example.com`); README alert names don't match real alert names | `alert-rules.yml:19`; runbook README |
| M6 | No image build-and-push pipeline in scope; default image `VERSION=unknown` | Dockerfiles `ARG VERSION=unknown` |
| M7 | dev/staging output `module.elasticache.primary_endpoint` (cluster mode exports `configuration_endpoint`) → plan error for dev/staging | `dev/outputs.tf:31` |

### Low

| ID | Blocker | Evidence |
|----|---------|----------|
| L1 | Root `versions.tf` is effectively dead (envs redeclare providers; not in any root module) | `versions.tf` |
| L2 | ML base image pinned by tag, not digest (4GB image, reproducibility gap) | `Dockerfile.ml` |
| L3 | `redis-client` service selects `role: primary` but the StatefulSet never sets that label → selects zero endpoints | `base/services.yaml:86` |
| L4 | `Chart.yaml` `home`/`sources` are `your-org` placeholders; `namespace.yaml` hardcodes `environment: production` regardless of overlay | `Chart.yaml:16-18` |
| L5 | `canary-rollout.yaml` excluded from kustomization; DestinationRule subset `v2` has no backing deployment | `istio/kustomization.yaml:7-11` |
| L6 | No "last drilled" date on any DR runbook — RTO/RPO stated but unverified | all `dr-*.md` |

**Totals:** 8 Critical · 13 High · 7 Medium · 6 Low.

---

## 6. Final verdict — is AEOS genuinely deployable to cloud today?

**No.**

**To AWS / EKS:** Not today, but close. The IaC and Helm chart are genuinely
competent and ~3–5 engineer-days of mechanical remediation from a first
successful apply: fix the three Terraform plan errors (C1, C2, M7), bootstrap
remote state (C3), wire the EKS node SG into RDS/ElastiCache (C4), stand up a real
registry + build/push and substitute image refs (C5), fix the worker container
(C6), refresh the EKS/addon version pins (H9), reconcile the base-vs-Helm split
(H6), and correct the ESO store name (H7). None of these require new architecture
— they are wiring and supply-chain corrections. After that, a genuine dev/staging
apply + smoke test is the real proof (still missing).

**To Azure / AKS and GCP / GKE:** No, and not close. There is **no Azure or GCP
infrastructure of any kind.** Reaching parity means net-new IaC plus Azure/GCP
equivalents for every AWS-specific dependency (Secrets Manager→Key Vault/Secret
Manager, ACM/WAF→App Gateway/Cloud Armor, IRSA→Workload Identity, gp3→managed-csi,
ElastiCache→Azure Cache/Memorystore, RDS→Flexible Server/Cloud SQL). That is
**multiple weeks** and explicitly *new infrastructure*, which is out of scope for
this sprint ("no new features, no new architecture"). Honest status: **aspirational,
not ready.**

**For the headline federation capability:** Not deployable on any cloud today —
there are no cross-cluster manifests at all (H1). The application-layer federation
proven in Sprints 3–4 (loopback/in-process) has **no production topology**;
building one is ~1–2 weeks of net-new manifests and mesh configuration.

**What is genuinely strong and should be preserved:** the security posture
(STRICT mTLS, default-deny, OPA, IRSA, KMS, ESO, no hardcoded secrets), the
Terraform *module* quality, the node/redis/kafka/cluster DR runbooks, the
Alertmanager routing, and the api Dockerfile. The gap is not competence — it is
**execution and last-mile integration**: nothing here has ever been run, and the
monitoring/worker/federation paths break the moment it is.

### Effort estimate to close

| Goal | Effort | Scope note |
|------|--------|------------|
| First successful **EKS** dev apply + smoke test | 3–5 eng-days | wiring/state/registry/worker/version fixes (C1–C6, H4–H9) |
| Trustworthy monitoring + DR (fix H2/H3/H10–H13, drill) | 2–3 eng-days | metric-label fix, Qdrant backup, reconcile Postgres story, run a drill |
| **Federation** production topology | 1–2 weeks | net-new manifests — *new infra*, out of this sprint's scope |
| **AKS + GKE** parity | 3–6 weeks | net-new multi-cloud IaC — *new infra*, out of scope |

---

## 7. Verification log (hand-confirmed, not taken on faith)

Because this document tests an unverified claim, the highest-impact findings were
re-confirmed directly against the repo rather than trusted from the sub-audits:

1. **C6** — `ls app/workers` → **MISSING**; `grep -rl "import celery" app/` →
   none. Worker Dockerfile CMD/HEALTHCHECK target a module that does not exist. ✓
2. **C1** — `production/main.tf:117` passes `num_cache_clusters = 3`; the module
   only defines `num_node_groups` + `replicas_per_node_group`
   (`variables.tf:35,41`). Hard `plan` error. ✓
3. **C2** — `production/main.tf:119` references `module.alerts_sns.topic_arn`; the
   file defines only `resource "aws_sns_topic" "alerts"` and
   `local.alerts_sns` — no such module. ✓
4. **H2** — exporter labels are `("method","path","status_code")`
   (`prometheus.py:134`); alerts query `status_class="5xx"` → the label is never
   emitted, so 5xx alerts evaluate empty. ✓
5. **H12** — `app/main.py:435` defines only `@app.get("/health", ...)`; no
   `/health/ready`. ✓

All five confirmed the sub-audit findings exactly. The audits are accurate.

---

## 8. Position in Phase 13

Sprint 6 closes the *evidence* gap on the infrastructure claim: we now know
precisely, with citations, what "PRODUCTION READY" does and does not mean for
AEOS. It does **not** close the *deployment* gap — that requires the ~3–5-day EKS
remediation above plus a real cloud apply. The companion
`docs/runbooks/CLOUD_VALIDATION_PLAYBOOK.md` provides the exact migration and
certification procedures (Local→Linux→Kubernetes→Cloud) and the EKS/AKS/GKE
command sets, with the honest caveats recorded here. Recommended next step:
execute the EKS remediation on a real dev account, run `scripts/certify.py` with
`--allow-full-scale` on that cluster for genuine Bronze/Silver evidence, then
decide whether federation-on-cloud or multi-cloud parity is the higher priority.
