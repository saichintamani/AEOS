# Milestone 14.2 — First Real `terraform apply` (Runbook + Capture Harness)

**Status:** 🟡 BOOTSTRAP DONE — apply not yet run. P1 (state backend) and P2 (deploy
principal: 2 policies + role + user + OIDC) were created for real against account
660249531916 on 2026-07-21 via `bootstrap-deploy.ps1 -EnableOidc` from the root session.
Remaining before the apply: enable MFA on `aeos-deployer` + give it programmatic access.
The apply itself is an interactive, credentialed, **billable** action you run — not automated here.

**Goal:** create the AEOS dev infrastructure for real, assuming the scoped
`aeos-deploy` role (never root), and **capture every failure** (IAM denial, quota,
limit, backend) so the deploy policy can be hardened against reality. On a first
apply the *findings are the deliverable* — a green run is a bonus.

> ⚠️ **Cost + reversibility.** This plan creates an EKS cluster, an RDS instance,
> 3 NAT gateways, and an ElastiCache replication group — real hourly charges that
> continue until `terraform destroy`. Do this deliberately, in a window where you
> can tear down afterward if it's just a validation pass.

---

## Prerequisites (one-time, interactive — you do these)

### P1. Create the state backend (if not already present)
The plan ran with `-backend=false`; a real apply needs remote state.
```bash
aws s3api create-bucket --bucket aeos-tfstate-660249531916 --region us-east-1
aws s3api put-bucket-versioning --bucket aeos-tfstate-660249531916 \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name aeos-tflock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region us-east-1
```

### P2. Create the deploy principal (moves off root — Milestone 14.1 artifacts)
From an admin/root session (this is the *last* thing you do as admin/root), just run
the bootstrap script — it does P1 and P2 idempotently and in the correct order:
```powershell
cd "D:\My projects\AEOS\infrastructure\aws"
powershell -ExecutionPolicy Bypass -File .\bootstrap-deploy.ps1 -EnableOidc
```
What it creates (order matters — the trust policy names both the user and the OIDC
provider as principals, so they must exist before the role):
1. state bucket + lock table (P1)
2. **two** managed policies — `aeos-deploy` (control plane) + `aeos-deploy-services`
   (EKS/ECR/RDS/ElastiCache/KMS/CloudWatch/S3-app). A single policy exceeds IAM's
   6144-char `PolicySize` limit, and the apply loop below only grows it.
3. `aeos-deployer` user → OIDC provider → `aeos-deploy` role with **both** policies attached.

Then finish MFA by hand — the trust policy requires `aws:MultiFactorAuthPresent=true`:
```bash
aws iam create-virtual-mfa-device --virtual-mfa-device-name aeos-deployer \
  --outfile qr.png --bootstrap-method QRCodePNG
aws iam enable-mfa-device --user-name aeos-deployer \
  --serial-number arn:aws:iam::660249531916:mfa/aeos-deployer \
  --authentication-code-1 <code1> --authentication-code-2 <code2>
```

### P3. (Optional) GitHub OIDC for CI deploys
The trust policy already allows `repo:saichintamani/AEOS:ref:refs/heads/main` via
`token.actions.githubusercontent.com`. To use it, create the OIDC provider once:
```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list <current GitHub Actions thumbprint>
```
Not required for a manual local apply — skip for the first run.

---

## The apply loop (iterate until zero findings)

```powershell
cd "D:\My projects\AEOS\infrastructure\terraform\environments\dev"

# Stable secrets for the real apply (do NOT let these rotate between retries):
$env:TF_VAR_db_password      = "<24+ char, RDS-compliant>"
$env:TF_VAR_redis_auth_token = "<32+ char, Redis-compliant>"

powershell -ExecutionPolicy Bypass -File .\run-apply.ps1
```

`run-apply.ps1` does: assume `aeos-deploy` → `sts get-caller-identity` →
`init` the S3 backend → `terraform apply` tee'd to `apply-<runid>.log` →
runs the capture harness automatically.

### Reading the results
The harness (`scripts/capture_apply_failures.py`) prints, and writes
`apply-<runid>.report.json`, four finding classes:

| Class | What to do |
|-------|-----------|
| **Missing IAM actions** | It names the exact action (`is not authorized to perform: X`). Paste the suggested JSON snippet into the matching `Sid` in **the file that owns that service** — control-plane actions (ec2/iam/s3-state/dynamodb) → `aeos-deploy-permissions.json`; service actions (eks/ecr/rds/elasticache/kms/logs/cloudwatch/s3-app) → `aeos-deploy-services-permissions.json`. |
| **EC2 UnauthorizedOperation** | EC2 often hides the action. Use the error-header context to see which resource failed, add the matching `ec2:*`. |
| **Quota / limits** | Not a policy fix — request a Service Quota increase (e.g. VPCs/region, EIPs, RDS instances) or reduce the plan. |
| **Backend / state** | Bucket/lock table missing or lock stuck → revisit P1. |

### After each fix
Re-version whichever policy you edited (each is capped at 5 versions):
```bash
# control-plane edits:
aws iam create-policy-version --policy-arn arn:aws:iam::660249531916:policy/aeos-deploy \
  --policy-document file://../../../aws/iam/aeos-deploy-permissions.json --set-as-default
# service edits:
aws iam create-policy-version --policy-arn arn:aws:iam::660249531916:policy/aeos-deploy-services \
  --policy-document file://../../../aws/iam/aeos-deploy-services-permissions.json --set-as-default
```
Then re-run `run-apply.ps1`. Terraform is idempotent — it resumes from partial
state and only creates what's missing. Repeat until the report shows
**0 actionable findings** and `terraform apply` exits 0.

---

## Definition of done for 14.2
- [ ] `terraform apply` exits 0; `terraform show` lists the 112 resources as created.
- [ ] `aeos-deploy-permissions.json` reflects the **real** action set (every addition
      traceable to a captured denial), committed with the apply report as evidence.
- [ ] All quota increases that were needed are documented.
- [ ] Evidence doc 043 written: what the first apply actually surfaced.
- [ ] (If validation-only) `terraform destroy` run and confirmed clean.

## Honesty boundary
- **Bootstrap (P1+P2) HAS been executed** against AWS account 660249531916 (2026-07-21):
  the state bucket, lock table, `aeos-deploy` + `aeos-deploy-services` policies, the
  `aeos-deploy` role, the `aeos-deployer` user, and the GitHub OIDC provider now exist.
  The first bootstrap run surfaced two real bugs (PolicySize > 6144 → policy split;
  role created before its principals → reordered); both fixed. See doc 043 when written.
- **No terraform apply has run.** No VPC/EKS/RDS/ElastiCache/NAT — none of the 112
  billable resources — has been created. MFA on `aeos-deployer` is still pending.
- The deploy policy is still derived from resource *types*, not observed denials —
  **this milestone is exactly where that guess gets corrected.** Expect 1–3 iterations.
