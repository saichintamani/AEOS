# Milestone 14.0 — First Real `terraform plan` Succeeded (Evidence)

**Status:** ✅ DONE — the network-blocked window is over. A real, credentialed
`terraform plan` against AWS account `660249531916` (`us-east-1`) now produces a
complete, valid resource graph. This is the milestone doc 039 was explicitly
waiting on ("*it becomes actionable immediately after Milestone 14.0 succeeds*").

## Provenance (what actually ran)

| Field | Value |
|-------|-------|
| Environment | `infrastructure/terraform/environments/dev` |
| Runner | `run-plan.ps1` (PowerShell bridges IAM Identity Center → concrete env keys) |
| Terraform | v1.9.8 (windows_amd64) |
| AWS provider | hashicorp/aws v5.100.0 (+ tls v4.0.6) |
| AWS account | `660249531916` |
| Region | `us-east-1` |
| Identity check | `aws sts get-caller-identity` passed before plan |
| Backend | `-backend=false` (plan is read-only; no state bucket touched) |
| Plan artifact | `aeos-dev.plan` (40 KB, sha256 `9cff8ee2e9f8…`) |
| Rendered | `plan-show.txt` (3,285 lines, `terraform show -no-color`) |

Sequence executed: `init -backend=false` → `validate` → `plan -out=aeos-dev.plan`.
`terraform validate` passed and the plan renders with **zero warnings and zero
deprecations**.

## Result

```
Plan: 112 to add, 0 to change, 0 to destroy.
```

A first plan against an empty account is expected to be all-adds; **0 to change /
0 to destroy** confirms the config is internally consistent (no drift-inducing or
self-conflicting resources).

### Resource graph — 30 AWS resource types, 112 resources

| Count | Type | Module |
|------:|------|--------|
| 10 | `aws_iam_role_policy_attachment` | eks, iam |
| 9 | `aws_subnet` | vpc |
| 9 | `aws_route_table_association` | vpc |
| 8 | `aws_iam_role` | eks, iam, rds, vpc |
| 5 | `aws_route_table` | vpc |
| 5 | `aws_kms_key` | (encryption at rest across modules) |
| 5 | `aws_cloudwatch_metric_alarm` | cloudwatch |
| 5 | `aws_cloudwatch_log_group` | cloudwatch, eks |
| 4 | `aws_route` | vpc |
| 4 | `aws_eks_addon` | eks |
| 3 | `aws_security_group` | vpc |
| 3 | `aws_nat_gateway` | vpc |
| 3 | `aws_kms_alias` | |
| 3 | `aws_iam_policy` | iam (api / worker / eso) |
| 3 | `aws_eip` | vpc |
| 3 | `aws_ecr_repository` / `_policy` / `_lifecycle_policy` | ecr |
| 2 | `aws_s3_bucket` (+ versioning/SSE/PAB/lifecycle) | s3 |
| 1 each | `aws_vpc`, `aws_internet_gateway`, `aws_flow_log`, `aws_iam_openid_connect_provider`, `aws_iam_role_policy` | vpc, iam |
| 1 each | `aws_eks_cluster`, `aws_eks_node_group` | eks |
| 1 each | `aws_elasticache_replication_group` / `_subnet_group` / `_parameter_group` | elasticache |
| 1 each | `aws_db_instance` / `aws_db_subnet_group` / `aws_db_parameter_group` | rds |
| 1 | `aws_cloudwatch_dashboard` | cloudwatch |

### IAM footprint (feeds Milestone 14.1)

**8 roles created by the stack** (workload/service roles, NOT the deploy principal):
`eks.cluster`, `eks.ebs_csi`, `eks.node_group`, `iam.irsa["api"]`,
`iam.irsa["eso"]`, `iam.irsa["worker"]`, `rds.enhanced_monitoring`,
`vpc.flow_logs`.

**3 workload policies:** `iam.api`, `iam.worker`, `iam.eso`.

These are the roles the *application* assumes at runtime. The **deploy** principal
(the thing that runs `terraform apply`) is a separate concern — its least-privilege
policy is now derivable from this exact resource-type set (see doc 041).

## What this unblocks

Per the roadmap in doc 039:

- **14.0** ✅ real plan → concrete resource graph. *(this doc)*
- **14.1** ➡️ derive the real `aeos-deploy` least-privilege policy from the 30
  resource types above (no longer guessing from managed-policy names). *(doc 041)*
- **14.2** — `terraform apply` assuming `aeos-deploy` (never root).
- **14.3 → 14.5** — EKS healthy → Helm healthy → Bronze cloud certification.

## Honesty boundary

- This is a **plan**, not an **apply**. Nothing has been provisioned; account
  `660249531916` is still empty of these 112 resources.
- The plan ran with `-backend=false`, so the S3 remote state bucket + `aeos-tflock`
  DynamoDB lock table were **not** exercised. A real `apply` (14.2) must first
  `init` the S3 backend (`bucket=aeos-tfstate-660249531916`), which is a separate
  reachability + permissions check not covered here.
- `db_password` and `redis_auth_token` were ephemeral plan-only secrets generated
  by `run-plan.ps1` and discarded; they are not the values a real apply will use.
- Two `sensitive` values in the plan (DB/Redis auth) are correctly redacted in
  `plan-show.txt`.
