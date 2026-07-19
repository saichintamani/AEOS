aws_region = "us-east-1"
vpc_cidr   = "10.30.0.0/16"

cicd_role_arn = "arn:aws:iam::ACCOUNT_ID:role/aeos-cicd-role"

alert_email_addresses = [
  # "platform-oncall@yourorg.com",
  # "sre@yourorg.com",
]

# NEVER commit sensitive values.
# Inject at plan/apply time:
#   export TF_VAR_db_password="$(aws secretsmanager get-secret-value --secret-id aeos/prod/db-password --query SecretString --output text)"
#   export TF_VAR_redis_auth_token="$(aws secretsmanager get-secret-value --secret-id aeos/prod/redis-auth-token --query SecretString --output text)"
