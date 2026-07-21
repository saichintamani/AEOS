## Infrastructure README — AEOS Cloud Native Stack

## Directory Layout

```
infrastructure/
├── helm/aeos/                    # Helm chart (umbrella chart)
│   ├── Chart.yaml                #   Bitnami redis/kafka/qdrant sub-charts
│   ├── values.yaml               #   Default values
│   ├── values-local.yaml         #   Local dev (minikube/kind)
│   ├── values-staging.yaml       #   Staging environment
│   ├── values-production.yaml    #   Production environment
│   └── templates/                #   17 Kubernetes resource templates
│       ├── _helpers.tpl
│       ├── serviceaccount.yaml
│       ├── rbac.yaml
│       ├── configmap.yaml
│       ├── api-deployment.yaml
│       ├── worker-deployment.yaml
│       ├── scheduler-deployment.yaml
│       ├── services.yaml
│       ├── hpa.yaml
│       ├── pdb.yaml
│       ├── ingress.yaml
│       ├── networkpolicy.yaml
│       └── external-secrets.yaml
│
├── kubernetes/
│   ├── base/                     # Raw K8s manifests (Kustomize base)
│   │   ├── kustomization.yaml
│   │   ├── namespace.yaml        #   PSA labels: restricted/baseline
│   │   ├── rbac.yaml
│   │   ├── configmap.yaml
│   │   ├── api-deployment.yaml
│   │   ├── worker-deployment.yaml
│   │   ├── scheduler-deployment.yaml
│   │   ├── redis-statefulset.yaml
│   │   ├── kafka-statefulset.yaml
│   │   ├── qdrant-statefulset.yaml
│   │   ├── services.yaml
│   │   ├── ingress.yaml
│   │   ├── hpa.yaml
│   │   ├── pdb.yaml
│   │   ├── network-policy.yaml
│   │   └── network-policy-kafka.yaml
│   ├── security/
│   │   ├── cluster-secret-store.yaml  # ESO → AWS Secrets Manager
│   │   └── opa-constraints.yaml       # Gatekeeper policies
│   └── istio/
│       ├── peer-authentication.yaml   # STRICT mTLS all namespaces
│       ├── destination-rules.yaml     # Circuit breaking + connection pools
│       ├── virtual-services.yaml      # Traffic routing + Gateway
│       ├── authorization-policies.yaml # L7 RBAC (who can call whom)
│       └── canary-rollout.yaml        # 5-step canary procedure
│
├── terraform/
│   ├── versions.tf               # Pinned provider versions
│   ├── modules/
│   │   ├── vpc/                  # Multi-AZ VPC + NAT + flow logs
│   │   ├── eks/                  # EKS 1.29 + OIDC + KMS + EBS CSI
│   │   ├── iam/                  # IRSA roles (api, worker, eso)
│   │   ├── ecr/                  # Private registries + lifecycle
│   │   ├── elasticache/          # Redis 7 replication group
│   │   ├── rds/                  # PostgreSQL 16 Multi-AZ
│   │   ├── s3/                   # Artifacts + backups + tfstate
│   │   └── cloudwatch/           # Log groups + alarms + dashboard
│   └── environments/
│       ├── dev/                  # Spot nodes, single-AZ, force_destroy
│       ├── staging/              # Multi-AZ, 2-node Redis
│       └── production/           # Full HA, 3-node Redis, read replica
│
└── monitoring/
    ├── prometheus/
    │   ├── prometheus.yml            # Production scrape config (K8s SD)
    │   ├── prometheus-dev.yml        # Dev static config
    │   ├── alerts.yml                # Original alerts
    │   └── alert-rules.yml           # Production alert rules (15 rules)
    ├── alertmanager/
    │   └── alertmanager.yml          # PagerDuty + Slack routing
    └── grafana/
        ├── datasources.yml
        ├── provisioning-dashboards.yml
        └── dashboards/
            ├── aeos-overview.json    # 13-panel overview
            └── error-budget.json     # SLO burn rate + budget panels
```

---

## Quick Start: Local (Helm)

```bash
# Prerequisites: kubectl, helm, minikube
minikube start --cpus=4 --memory=8g

helm dependency update infrastructure/helm/aeos

helm upgrade --install aeos infrastructure/helm/aeos \
  -f infrastructure/helm/aeos/values-local.yaml \
  --namespace aeos-api \
  --create-namespace \
  --wait

kubectl port-forward svc/aeos-api 8000:80 -n aeos-api
curl http://localhost:8000/health
```

## Quick Start: Raw Manifests (Kustomize)

```bash
# Apply all base manifests to current cluster
kubectl apply -k infrastructure/kubernetes/base/

# Apply security policies (requires ESO + Gatekeeper installed)
kubectl apply -k infrastructure/kubernetes/security/

# Apply Istio resources (requires Istio installed)
kubectl apply -k infrastructure/kubernetes/istio/
```

## Deploy to AWS (Terraform + Helm)

```bash
# 0. Bootstrap Terraform remote state (one-time, per-account).
#    The S3 backend cannot interpolate variables, so the state bucket + lock
#    table must exist BEFORE `terraform init`. Create them once with the AWS CLI:
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "aeos-tfstate-${ACCOUNT_ID}" --region us-east-1
aws s3api put-bucket-versioning --bucket "aeos-tfstate-${ACCOUNT_ID}" \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name aeos-tflock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region us-east-1

# 1. Init with the resolved bucket (partial backend config).
cd infrastructure/terraform/environments/production
terraform init -backend-config="bucket=aeos-tfstate-${ACCOUNT_ID}"

# 2. Provision all infrastructure
terraform apply -var-file=terraform.tfvars

# 3. Update kubeconfig
aws eks update-kubeconfig --name aeos-production --region us-east-1

# 4. Deploy AEOS via Helm
helm upgrade --install aeos infrastructure/helm/aeos \
  -f infrastructure/helm/aeos/values-production.yaml \
  --set global.imageRegistry="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com" \
  --set api.image.tag=<GIT_SHA> \
  --set worker.image.tag=<GIT_SHA> \
  --namespace aeos-api \
  --create-namespace \
  --wait --timeout=15m
```

## Validation

```bash
bash scripts/validate.sh production
```

See [docs/runbooks/README.md](../docs/runbooks/README.md) for operational runbooks.
