# AEOS Cloud Validation Playbook

**Purpose:** The step-by-step procedure to move AEOS from a laptop to a real
cloud, and to run genuine tier certifications at each stage. This is the
operational companion to
`docs/architecture/033-CLOUD_DEPLOYMENT_READINESS_AUDIT.md`.

**Honesty banner:** As of 2026-07-20 the infrastructure has **never been applied**
and has **8 Critical blockers** (see doc 033 §5). This playbook is written so that
the person executing it hits those blockers in a controlled order with the fix
inline, rather than discovering them mid-incident. Steps that will fail against
the *current committed state* are marked **⚠ BLOCKED (fix first)** with the doc-033
blocker ID. Do not treat a green step as certification — certification only comes
from `scripts/certify.py` on production-grade infra (§5).

---

## 0. Prerequisites

| Tool | Min version | Used for |
|------|-------------|----------|
| `terraform` | ≥ 1.7 | provisioning (AWS only today) |
| `kubectl` | ≥ 1.29 | cluster ops |
| `helm` | ≥ 3.14 | app install |
| `aws` CLI v2 | current | EKS/ECR/state |
| `az` CLI | current | **AKS only — no AEOS IaC exists (§4B)** |
| `gcloud` CLI | current | **GKE only — no AEOS IaC exists (§4C)** |
| `docker` / `buildx` | current | image build/push |
| Python | 3.11–3.13 | `scripts/certify.py` |

Repo root referenced below as `$AEOS` (e.g. `D:\My projects\AEOS` on Windows,
`/d/My projects/AEOS` in git-bash).

---

## 1. Stage A — Local (developer workstation)

**Goal:** prove the app and the certification harness run; produce dev-scale
operational validation (never a certification — the harness gates it off, by
design).

```bash
cd "$AEOS"
pip install -r requirements.txt

# 1. App smoke test
python -m app.main &            # or: uvicorn app.main:app --port 8000
curl -s localhost:8000/health   # expect {"status":"healthy",...}
curl -s localhost:8000/metrics | head   # Prometheus text exposition

# 2. Distributed tests (real gRPC / Raft / federation)
pytest tests/integration/distributed -q

# 3. Dev-scale operational validation (NOT a certification on a laptop)
python scripts/certify.py bronze
python scripts/certify.py all      # bronze..platinum, all dev-scale
```

**Expected honest outcome:** every tier reports `NOT CERTIFIED (dev-scale)` with
real measurements. Reports land in `reports/certification/*.{json,md}`. Bronze
thresholds are typically met; Silver typically fails its 200-TPS floor on a
single-process loopback box — that is the harness reporting reality, not a bug.

**Exit criteria for Stage A:** health + metrics OK, distributed suite green, four
dev-scale reports produced.

---

## 2. Stage B — Local → Linux server (single production-grade host)

**Goal:** run the *same* command on a real Linux server so the environment
classifier promotes to `linux-server` (production-grade), making certification
*possible* (still requires `--allow-full-scale`).

```bash
# On the Linux server (bare VM or EC2), as a non-root service user:
git clone <repo> aeos && cd aeos
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Confirm the environment classifier sees a production-grade host:
python - <<'PY'
from app.certification import classify
e = classify()
print(e.environment_class.value, "production_grade=", e.is_production_grade)
PY
# expect: linux-server production_grade= True
```

**Certification on the Linux host** (see §5 for tier procedures):

```bash
# dev-scale (safe, fast) — still NOT certified without --allow-full-scale:
python scripts/certify.py bronze

# genuine attempt — full-scale load, real gate:
python scripts/certify.py bronze --allow-full-scale --require-certified
```

`--require-certified` makes the process exit non-zero unless every requested tier
actually passed — use it in a CI job. A single host can legitimately certify
Bronze/Silver (throughput/latency/failover/recovery/federation are all in-process
or loopback); Gold/Platinum full-scale realistically want a cluster (Stage C/D).

**What differs from Stage A:** *only* the environment classification and the
`--allow-full-scale` opt-in. **No code or command changes.** That portability is
the whole point of the harness.

---

## 3. Stage C — Linux → Kubernetes (self-managed or managed cluster)

**⚠ BLOCKED (fix first):** C5 (no registry/image substitution), C6 (worker image
broken), H4 (scheduler Helm helper), H5 (subcharts unvendored), H6 (base vs Helm
conflict), H7 (ESO store name). Do §3.0 remediation before §3.1.

### 3.0 Pre-flight remediation (from doc 033)

1. **Registry + images (C5, C6, M6):** stand up a registry (ECR for AWS), fix
   `Dockerfile.worker` to launch the real `app.distributed` worker (not
   `celery -A app.workers`), build and push all three images with an immutable
   tag (git SHA), and set that tag in your values overlay.
2. **Helm scheduler helper (H4):** align `templates/scheduler-deployment.yaml`
   image dict with `_helpers.tpl` (match api/worker shape).
3. **Subcharts (H5):** `helm dependency build infrastructure/helm/aeos` and commit
   `Chart.lock`; pin the qdrant chart to a real version.
4. **ESO store name (H7):** make `cluster-secret-store.yaml` name and the values
   `externalSecrets.secretStore` agree.
5. **Choose ONE source of truth (H6):** deploy *either* `kubernetes/base` (kustomize)
   *or* the Helm chart — not both. Recommendation: Helm for app, kustomize
   `istio/` + `security/` overlays for mesh/policy.

### 3.1 Validate before apply (catches C1/C2 class errors early)

```bash
# Manifests
kubectl kustomize infrastructure/kubernetes/base | kubeconform -strict -summary
helm lint infrastructure/helm/aeos
helm template aeos infrastructure/helm/aeos -f infrastructure/helm/aeos/values-staging.yaml \
  | kubeconform -strict -summary
```

### 3.2 Deploy (order matters)

```bash
kubectl apply -f infrastructure/kubernetes/base/namespace.yaml

# Secrets backend (ESO) + policies first, so app pods can resolve secrets:
kubectl apply -k infrastructure/kubernetes/security
# Mesh (STRICT mTLS, default-deny authz):
kubectl apply -k infrastructure/kubernetes/istio

# App:
helm dependency build infrastructure/helm/aeos
helm upgrade --install aeos infrastructure/helm/aeos \
  -n aeos --create-namespace \
  -f infrastructure/helm/aeos/values-staging.yaml \
  --set image.tag="$GIT_SHA" --wait --timeout 10m

kubectl -n aeos rollout status deploy/aeos-api
kubectl -n aeos get pods -o wide
```

### 3.3 In-cluster certification

Run the harness *inside* the cluster so the classifier detects `kubernetes`:

```bash
kubectl -n aeos run certify --rm -it --restart=Never \
  --image="$REGISTRY/aeos-api:$GIT_SHA" -- \
  python scripts/certify.py bronze --allow-full-scale --require-certified
```

The classifier reads the service-account mount / `KUBERNETES_SERVICE_HOST`,
returns `kubernetes` (production-grade), and full-scale + opt-in become legitimate.

**Exit criteria for Stage C:** kubeconform/helm-lint clean, all pods Ready, mTLS
STRICT verified (`istioctl authn tls-check`), in-cluster Bronze certified.

---

## 4. Stage D — Kubernetes → Cloud (EKS / AKS / GKE)

### 4A. AWS EKS — backed by real Terraform (with blockers)

**⚠ BLOCKED (fix first):** C1, C2, C3, C4, H9, M7 — the Terraform will not
`init`/`plan` until these are fixed (doc 033 §5).

```bash
cd "$AEOS/infrastructure/terraform/environments/staging"

# --- one-time state bootstrap (fixes C3) ---
# Create the state bucket + lock table out-of-band, then set the real names in the
# backend "s3" block (replace *_ACCOUNT_ID placeholders):
aws s3api create-bucket --bucket aeos-tfstate-<ACCOUNT_ID> --region us-east-1
aws s3api put-bucket-versioning --bucket aeos-tfstate-<ACCOUNT_ID> \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name aeos-tflock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST

# --- after applying the C1/C2/C4/M7/H9 fixes from doc 033 ---
export TF_VAR_db_password=$(aws secretsmanager get-secret-value \
  --secret-id aeos/rds/password --query SecretString --output text)
export TF_VAR_redis_auth_token=$(aws secretsmanager get-secret-value \
  --secret-id aeos/redis/authtoken --query SecretString --output text)

terraform init
terraform validate            # MUST pass — proves C1/C2/M7 fixed
terraform plan -out tf.plan
terraform apply tf.plan

# Wire kubectl to the new EKS cluster:
aws eks update-kubeconfig --name aeos-staging --region us-east-1
kubectl get nodes

# Build + push images to the ECR repos Terraform created:
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
for svc in api worker ml; do
  docker buildx build -f infrastructure/docker/Dockerfile.$svc \
    -t <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/aeos-$svc:$GIT_SHA --push .
done

# Deploy (as Stage C §3.2) with the ECR registry + tag, then certify in-cluster:
helm upgrade --install aeos infrastructure/helm/aeos -n aeos \
  -f infrastructure/helm/aeos/values-production.yaml \
  --set imageRegistry=<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com \
  --set image.tag=$GIT_SHA --wait
kubectl -n aeos run certify --rm -it --restart=Never \
  --image=<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/aeos-api:$GIT_SHA -- \
  python scripts/certify.py all --allow-full-scale --require-certified
```

### 4B. Azure AKS — NO AEOS INFRASTRUCTURE EXISTS

There is **no Azure Terraform/Bicep** in this repo (doc 033 §4). The commands
below provision a *bare AKS cluster only*; AEOS itself will not run until Azure
equivalents are built for: AWS Secrets Manager→**Key Vault** (+ ESO Azure provider
or CSI Secrets Store), ACM/WAF ingress→**App Gateway/Front Door**, IRSA→**Workload
Identity**, gp3→**managed-csi / azurefile**, ElastiCache→**Azure Cache for Redis**,
RDS→**Postgres Flexible Server**. Treat this as *net-new work*, not a ready path.

```bash
# Cluster provisioning ONLY (does not deploy AEOS):
az group create -n aeos-rg -l eastus
az aks create -g aeos-rg -n aeos-aks \
  --node-count 3 --enable-managed-identity --enable-workload-identity \
  --enable-oidc-issuer --network-plugin azure --generate-ssh-keys
az aks get-credentials -g aeos-rg -n aeos-aks
kubectl get nodes
# ⚠ STOP: Helm chart assumes AWS ESO/ACM/IRSA/gp3 — will not function on AKS
#   until the Azure equivalents above are authored. See doc 033 §4.
```

### 4C. GCP GKE — NO AEOS INFRASTRUCTURE EXISTS

Same situation for GCP (doc 033 §4). GCP equivalents needed: Secrets Manager→
**Secret Manager** (+ CSI), IRSA→**Workload Identity**, ACM/WAF→**Cloud Armor +
managed certs**, gp3→**pd-ssd csi**, ElastiCache→**Memorystore**, RDS→**Cloud SQL**.

```bash
# Cluster provisioning ONLY (does not deploy AEOS):
gcloud container clusters create-auto aeos-gke \
  --region us-central1 --workload-pool=<PROJECT>.svc.id.goog
gcloud container clusters get-credentials aeos-gke --region us-central1
kubectl get nodes
# ⚠ STOP: Helm chart assumes AWS-specific services — not functional on GKE
#   until GCP equivalents are authored. See doc 033 §4.
```

---

## 5. Certification procedures (Bronze / Silver / Gold / Platinum)

The single command is `python scripts/certify.py <tier> [--allow-full-scale]
[--require-certified] [--output-dir]`. The **honesty gate** (`app/certification/
runner.py`) sets `certified=True` **iff**: (1) environment is production-grade
(`linux-server`/`kubernetes`/`cloud`), **and** (2) the run was full-scale, **and**
(3) `--allow-full-scale` was passed, **and** (4) every threshold was met. A laptop
is never certified, regardless of numbers.

| Tier | Full-scale load | Thresholds (floor/ceiling) | Where to certify |
|------|-----------------|----------------------------|------------------|
| **Bronze** | 10k tasks | ≥50 TPS, P99 <2s, failover/recovery <3s | Linux host or K8s |
| **Silver** | 100k tasks | ≥200 TPS, P99 <1s | K8s (multi-core) — single-proc loopback tops ~150 TPS |
| **Gold** | 1M tasks, 128-way | ≥1000 TPS, P99 <500ms | Multi-node cloud cluster |
| **Platinum** | 2M tasks + chaos | ≥2000 TPS + sustained-load/chaos | Multi-node cloud + chaos harness (chaos not yet implemented — doc 032 §7) |

**Procedure (identical at every stage):**

```bash
# 1. Confirm the environment is what you think it is:
python -c "from app.certification import classify; e=classify(); \
print(e.environment_class.value, e.is_production_grade)"

# 2. Dry run (dev-scale, never certified) to confirm the paths work:
python scripts/certify.py <tier>

# 3. Real attempt on production-grade infra:
python scripts/certify.py <tier> --allow-full-scale --require-certified \
  --output-dir reports/certification/<stage>

# 4. Archive reports/certification/<stage>/*.{json,md} as the evidence artifact.
```

**Interpreting the result honestly:**
- `CERTIFIED` requires all four gate conditions — a green threshold on a dev box
  is an *operational validation*, not a certification.
- `thresholds_met: true` + `certified: false` = the numbers were good but the
  environment/scale/opt-in gate was not satisfied (expected off production infra).
- A tier that fails its throughput floor is reported plainly; the harness never
  inflates. Re-run on more capable infra rather than lowering the floor.

**Platinum caveat:** the chaos dimension is defined but not implemented
(doc 032 §7). Platinum on any current environment runs the same measurements as
the others and is (correctly) never certified until a chaos harness exists.

---

## 6. Rollback & safety

- **Terraform:** keep the last known-good state; `terraform plan` before every
  apply; never apply to `production` without a staging apply first. State is in
  S3 with DynamoDB locking (once C3 is fixed).
- **Helm:** `helm rollback aeos <REVISION>`; `helm history aeos`.
- **K8s:** `kubectl rollout undo deploy/aeos-api -n aeos`.
- **Data stores:** follow `docs/runbooks/dr-*.md`. Note the gaps recorded in
  doc 033: **Qdrant has no backup/restore (H10)** — do not put irreplaceable RAG
  state on a cloud cluster until that is addressed; and the **RDS runbooks cover a
  DB the app does not yet connect to (H11)** — confirm the actual persistence
  topology before relying on them.
- **Certification is not a deploy gate for safety** — a certified throughput
  number says nothing about backup coverage. Gate production promotion on DR
  completeness (H10/H11) *and* certification, not certification alone.

---

## 7. Quick reference — stage → command → honest outcome

| Stage | Command | Honest outcome today |
|-------|---------|----------------------|
| A Local | `python scripts/certify.py all` | dev-scale validation, never certified ✅ works |
| B Linux | `certify.py bronze --allow-full-scale --require-certified` | can certify Bronze/Silver ✅ works |
| C K8s | §3.2 helm install + in-cluster certify | ⚠ BLOCKED until §3.0 fixes (C5/C6/H4–H7) |
| D-EKS | §4A terraform apply + deploy + certify | ⚠ BLOCKED until C1–C4/H9/M7 fixes |
| D-AKS | §4B | ❌ cluster only; no AEOS Azure IaC |
| D-GKE | §4C | ❌ cluster only; no AEOS GCP IaC |
