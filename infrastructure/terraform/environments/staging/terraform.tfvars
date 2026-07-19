aws_region = "us-east-1"
vpc_cidr   = "10.20.0.0/16"

cicd_role_arn = "arn:aws:iam::ACCOUNT_ID:role/aeos-cicd-role"

# alerts_sns_topic_arn = "arn:aws:sns:us-east-1:ACCOUNT_ID:aeos-staging-alerts"

# Sensitive values — inject via CI/CD secrets or:
# export TF_VAR_db_password="..."
# export TF_VAR_redis_auth_token="..."
