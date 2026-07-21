# Milestone 14.1 — `aeos-deploy` Least-Privilege Role, Derived From the Real Plan

**Status:** ✅ Artifacts authored and validated. ❌ Not applied to AWS.

**Supersedes:** the *guessed* managed-policy list in doc 039 §"Fast path". Now that
Milestone 14.0 produced a real plan (doc 040), we build the deploy policy from the
30 resource types the plan actually creates — strategy (2), the correct long-term
artifact — instead of attaching broad AWS-managed policies.

## Artifacts

- `infrastructure/aws/iam/aeos-deploy-permissions.json` — permission policy (13 statements)
- `infrastructure/aws/iam/aeos-deploy-trust.json` — trust policy (dedicated user w/ MFA, or CI OIDC)
- `infrastructure/aws/iam/README.md` — create/use/tighten instructions + honesty boundary

Both JSON documents parse (`json.load` clean).

## Resource type → action namespace mapping

Every statement traces to resource types observed in `plan-show.txt`:

| Plan resource types | Policy Sid | Scope |
|---------------------|-----------|-------|
| `aws_vpc`, `aws_subnet`, `aws_route_table(_association)`, `aws_route`, `aws_internet_gateway`, `aws_nat_gateway`, `aws_eip`, `aws_security_group`, `aws_flow_log` | `VpcNetworking` | `*` (region-gated) |
| `aws_eks_cluster`, `aws_eks_node_group`, `aws_eks_addon` | `EksCluster` | `*` |
| `aws_ecr_repository`, `_policy`, `_lifecycle_policy` | `EcrRepos` | `*` |
| `aws_elasticache_replication_group`, `_subnet_group`, `_parameter_group` | `ElastiCacheRedis` | `*` |
| `aws_db_instance`, `_subnet_group`, `_parameter_group` | `RdsPostgres` | `*` |
| `aws_kms_key`, `aws_kms_alias` | `KmsEncryptionKeys` | `*` |
| `aws_cloudwatch_log_group`, `_metric_alarm`, `_dashboard` | `CloudWatchLogsAndAlarms` | `*` |
| `aws_s3_bucket` (+versioning/SSE/PAB/lifecycle) | `S3AppBuckets` | `arn:aws:s3:::aeos-*` |
| S3 remote state | `TerraformStateBackend` | `aeos-tfstate-660249531916` |
| DynamoDB lock | `TerraformStateLock` | `table/aeos-tflock` |
| `aws_iam_role`, `_policy`, `_role_policy`, `_role_policy_attachment` | `IamScopedToAeosRolesAndPolicies` | `role/aeos-*`, `policy/aeos-*` + scoped `iam:PassRole` |
| `aws_iam_openid_connect_provider`, service-linked roles | `IamServiceLinkedAndOidc` | `*` |

## Least-privilege decisions

1. **Region fence.** A leading `Deny` on everything outside `us-east-1` (except
   global services: IAM, S3, STS, ECR auth token) caps blast radius to the home region.
2. **IAM scoped to `aeos-*`.** The deploy role can only touch roles/policies named
   `aeos-*`, and `iam:PassRole` is limited to those ARNs — it cannot mint arbitrary
   privileged roles or pass unrelated ones to EKS/EC2.
3. **State backend narrowed to exact ARNs** — the tfstate bucket and the single
   `aeos-tflock` table, not `s3:*` / `dynamodb:*`.
4. **`Resource: "*"` retained only** for create-time calls on services whose ARNs
   don't exist until creation (VPC/EKS/RDS/ElastiCache/KMS). Post-apply CloudTrail
   is the input for tightening these to concrete ARNs (README §"Tightening path").
5. **Trust: never root.** Assumable only by a dedicated MFA-gated `aeos-deployer`
   user or the GitHub OIDC principal on `main`.

## Sequencing (from doc 039, now advanced)

- **14.0** ✅ real plan → resource graph (doc 040)
- **14.1** ✅ *(this doc)* plan-derived deploy policy authored + validated
- **14.2** ⏭️ create the principal, then `terraform apply` assuming `aeos-deploy`
  (this is where any missing action surfaces — the first apply validates the set)
- **14.3 → 14.5** — EKS healthy → Helm healthy → Bronze cloud certification

## Honesty boundary

- No IAM principal, policy, role, user, or OIDC provider has been created in AWS.
- The action set is derived from resource **types**, not an observed CloudTrail
  action log. Correctness is expected but unproven until the first `apply` (14.2).
- The `aeos-deployer` user and the GitHub OIDC provider named in the trust policy
  are prerequisites that do not yet exist.
