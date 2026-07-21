# `aeos-deploy` — Scoped Infrastructure Deploy Role

Least-privilege IAM principal that runs `terraform apply` for the AEOS stack,
so ongoing infrastructure operations never run from
`arn:aws:iam::660249531916:root`.

Unlike the pre-staged guess in `docs/architecture/039`, the permission policy here
is **derived from the real plan** (Milestone 14.0, doc 040): every statement maps
to one of the 30 AWS resource types the plan actually creates. See doc 041 for the
resource-type → action-namespace mapping.

## Files

| File | What it is |
|------|-----------|
| `aeos-deploy-permissions.json` | Permission policy — what the role can do (scoped to `us-east-1`, `aeos-*` IAM/S3, the `aeos-tflock` lock table). |
| `aeos-deploy-trust.json` | Trust policy — who may assume it: a dedicated `aeos-deployer` IAM user (MFA-gated) or the GitHub Actions OIDC principal on `main`. Root is **not** trusted. |

## Create (run once, from an admin session — NOT from root day-to-day)

```bash
ACCOUNT=660249531916

# 1. Permission policy
aws iam create-policy \
  --policy-name aeos-deploy \
  --policy-document file://aeos-deploy-permissions.json

# 2. Role with the trust policy
aws iam create-role \
  --role-name aeos-deploy \
  --assume-role-policy-document file://aeos-deploy-trust.json

# 3. Attach
aws iam attach-role-policy \
  --role-name aeos-deploy \
  --policy-arn arn:aws:iam::${ACCOUNT}:policy/aeos-deploy
```

## Use for a real apply (Milestone 14.2)

```bash
# Assume the role instead of using root/admin keys
eval "$(aws sts assume-role \
  --role-arn arn:aws:iam::660249531916:role/aeos-deploy \
  --role-session-name aeos-apply \
  --query 'Credentials.[
      `AWS_ACCESS_KEY_ID=`+AccessKeyId,
      `AWS_SECRET_ACCESS_KEY=`+SecretAccessKey,
      `AWS_SESSION_TOKEN=`+SessionToken]' \
  --output text | sed 's/^/export /')"

cd ../../terraform/environments/dev
terraform init -backend-config="bucket=aeos-tfstate-660249531916"
terraform apply aeos-dev.plan
```

## Tightening path

This policy is least-privilege **at the action level** but still uses `Resource: "*"`
for services that cannot be pre-scoped before their resources exist (VPC, EKS, RDS,
ElastiCache, KMS create calls). After the first successful `apply`, enable CloudTrail
data events and replace `"*"` with the concrete ARNs from the run — that is the
strategy-(2) endpoint described in doc 039.

## Honesty boundary

- **Nothing here is deployed.** No IAM policy, role, user, or OIDC provider has been
  created in AWS. These are reviewed JSON artifacts only.
- The `aeos-deployer` user and the `token.actions.githubusercontent.com` OIDC
  provider referenced in the trust policy **must exist first**; creating them is a
  prerequisite not performed by these files.
- The action set was derived from the plan's resource *types*, not from an observed
  CloudTrail action list. It is expected to be correct for `apply`, but the first
  real apply is where any missing action surfaces — treat 14.2 as the validation.
