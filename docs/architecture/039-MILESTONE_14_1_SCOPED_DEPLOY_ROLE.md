# Milestone 14.1 — Scoped AEOS Deploy Role (pre-staged, no AWS calls yet)

**Purpose:** Replace root-account usage for `terraform apply` with a least-privilege
IAM principal, per the standing instruction: *never run ongoing infrastructure
operations from `arn:aws:iam::660249531916:root`.*

This document is **pre-staged during the network-blocked window**. Nothing here has
been applied to AWS. It becomes actionable immediately after Milestone 14.0 (first
real `terraform plan`) succeeds and we can see the actual resource graph.

---

## What the dev stack actually creates (from `environments/dev/main.tf`)

| Module | AWS resource families the deploy role must manage |
|--------|---------------------------------------------------|
| vpc | EC2 (VPC, subnets, route tables, IGW/NAT, SG, EIP) |
| eks | EKS cluster + node groups, associated IAM, OIDC provider |
| iam / IRSA | IAM roles, policies, OIDC provider, `iam:PassRole` (scoped) |
| ecr | ECR repositories (api / worker / scheduler) |
| elasticache | ElastiCache (Redis) replication group + subnet group |
| rds | RDS instance + subnet group + parameter group |
| s3 | S3 buckets (app data; tfstate bucket is `create_tfstate_bucket=false`) |
| cloudwatch | CloudWatch log groups, alarms |
| backend (S3) | s3 state bucket RW + DynamoDB `aeos-tflock` lock table RW |

## Recommended shape

**Preferred:** an IAM **role** `aeos-deploy` assumed by a dedicated IAM user (or the
CI OIDC principal), NOT the root account. Trust policy limits who can assume it;
permission policy limits what it can do.

Two viable permission-policy strategies:

1. **Fast path (managed policies)** — attach AWS-managed policies matching the
   resource families above. Quicker to stand up; broader than strictly necessary.
   Candidates: `AmazonVPCFullAccess`, `AmazonEKSClusterPolicy` +
   `AmazonEKSWorkerNodePolicy` (for node roles), `AmazonEC2ContainerRegistryFullAccess`,
   `AmazonElastiCacheFullAccess`, `AmazonRDSFullAccess`, `AmazonS3FullAccess`,
   `CloudWatchFullAccess`, plus a **custom** IAM-management policy scoped to
   `aeos-*` roles and DynamoDB access to the `aeos-tflock` table.

2. **Least-privilege path (custom policy)** — author a single custom policy whose
   actions are derived from the **actual plan output**. This is the correct
   long-term artifact, and it's *why 14.0 must come first*: the plan (and a first
   `apply` run with CloudTrail) tells us the exact action set, so we don't guess.

## Sequencing (unchanged from the agreed roadmap)

1. **14.0** — real `terraform plan` succeeds → concrete resource graph + first
   real IAM/quota/VPC gaps. *(blocked only on network reachability)*
2. **14.1** — create `aeos-deploy` role using strategy (1) to move OFF root fast,
   then tighten toward strategy (2) using the plan/apply action set.
3. **14.2** — `terraform apply` assuming `aeos-deploy` (never root).
4. **14.3 → 14.5** — EKS healthy → Helm healthy → Bronze certification on cloud.

## Honesty boundary

- No IAM principal has been created. This is a design artifact only.
- The exact least-privilege action list **cannot** be finalized until the real
  plan output exists — deriving it from the plan is the entire point of doing 14.0
  before 14.1.
