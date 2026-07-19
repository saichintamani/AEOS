aws_region = "us-east-1"
vpc_cidr   = "10.10.0.0/16"

# CIDRs allowed to reach the public K8s API endpoint in dev
developer_cidrs = ["0.0.0.0/0"]

# IAM role used by GitHub Actions / CI pipeline
cicd_role_arn = "arn:aws:iam::ACCOUNT_ID:role/aeos-cicd-role"

# Sensitive — set via environment variable TF_VAR_db_password
# db_password = ""
# redis_auth_token = ""
