# DR Runbook: Secret Rotation

**Schedule**: Rotate all secrets every 90 days. Rotate immediately on breach.

## Secret Inventory

| Secret | Location | Rotation Impact | Zero-Downtime? |
|--------|----------|-----------------|----------------|
| OpenAI API Key | AWS Secrets Manager → ESO | Workers temporarily fail AI calls | Yes (dual-key period) |
| GitHub Token | AWS Secrets Manager → ESO | Code fetch fails during gap | Yes (dual-key period) |
| Redis AUTH Token | AWS Secrets Manager → ESO | Connection drop on K8s secret update | Brief (pod restart) |
| RDS Password | AWS Secrets Manager → ESO | Connection pool drain | Yes (multi-user rotation) |
| JWT Secret Key | AWS Secrets Manager → ESO | All existing tokens invalidated | No — schedule in maint window |
| IRSA Roles | AWS IAM (auto-rotated by AWS) | None — short-lived tokens | Yes (automatic) |

---

## Procedure 1: Rotate OpenAI / GitHub Keys (Zero Downtime)

External API keys support overlapping rotation:

```bash
# Step 1: Generate new key in OpenAI/GitHub console
NEW_KEY="sk-..."

# Step 2: Add new key to Secrets Manager (keep old key active)
aws secretsmanager update-secret \
  --secret-id aeos/production/api-secrets \
  --secret-string "{\"openai_api_key\":\"$NEW_KEY\",\"openai_api_key_prev\":\"$OLD_KEY\"}"

# Step 3: Wait for ESO to sync (refreshInterval: 1h, or force refresh)
kubectl annotate externalsecret aeos-api-secrets -n aeos-api \
  force-sync=$(date +%s) --overwrite

# Step 4: Verify new key is working
kubectl exec -n aeos-api deployment/aeos-api -- \
  python -c "
import openai, os
client = openai.OpenAI(api_key=os.environ['OPENAI_API_KEY'])
print(client.models.list().data[0].id)
"

# Step 5: Revoke old key in OpenAI console
# Step 6: Remove _prev key from Secrets Manager
aws secretsmanager update-secret \
  --secret-id aeos/production/api-secrets \
  --secret-string "{\"openai_api_key\":\"$NEW_KEY\"}"
```

---

## Procedure 2: Rotate Redis AUTH Token

Redis requires connections to use the new password — brief disruption during pod restart.

```bash
# Step 1: Update ElastiCache to accept BOTH old and new token (dual-mode)
# Note: ElastiCache does not natively support dual auth tokens; use maintenance window
# Schedule rotation for lowest-traffic period

# Step 2: Update Secrets Manager
NEW_TOKEN=$(openssl rand -base64 32)
aws secretsmanager update-secret \
  --secret-id aeos/production/redis \
  --secret-string "{\"auth_token\":\"$NEW_TOKEN\"}"

# Step 3: Update ElastiCache cluster (requires modification + apply-immediately)
aws elasticache modify-replication-group \
  --replication-group-id aeos-production \
  --auth-token "$NEW_TOKEN" \
  --auth-token-update-strategy ROTATE \
  --apply-immediately

# Step 4: Force ESO to sync K8s secret
kubectl annotate externalsecret aeos-redis-secrets -n aeos-data \
  force-sync=$(date +%s) --overwrite

# Step 5: Rolling restart API and worker (picks up new Redis password)
kubectl rollout restart deployment/aeos-api -n aeos-api
kubectl rollout restart deployment/aeos-worker -n aeos-jobs
kubectl rollout status deployment/aeos-api -n aeos-api --timeout=5m

# Step 6: Complete token rotation (removes old token from ElastiCache)
aws elasticache modify-replication-group \
  --replication-group-id aeos-production \
  --auth-token-update-strategy SET \
  --apply-immediately
```

---

## Procedure 3: Rotate RDS Password (Multi-User, Zero Downtime)

```bash
# Step 1: Create a second DB user with same privileges
kubectl exec -n aeos-api deployment/aeos-api -- psql $DATABASE_URL \
  -c "CREATE USER aeos_admin_2 WITH PASSWORD '$NEW_PASS';"
kubectl exec -n aeos-api deployment/aeos-api -- psql $DATABASE_URL \
  -c "GRANT ALL PRIVILEGES ON DATABASE aeos TO aeos_admin_2;"

# Step 2: Update DATABASE_URL in Secrets Manager to use new user
NEW_URL="postgresql://aeos_admin_2:$NEW_PASS@<host>/aeos"
aws secretsmanager update-secret \
  --secret-id aeos/production/database \
  --secret-string "{\"url\":\"$NEW_URL\",\"host\":\"<host>\"}"

# Step 3: Force ESO sync + rolling restart
kubectl annotate externalsecret aeos-db-secrets -n aeos-api \
  force-sync=$(date +%s) --overwrite
kubectl rollout restart deployment/aeos-api -n aeos-api

# Step 4: After verification, drop old user
kubectl exec -n aeos-api deployment/aeos-api -- psql $DATABASE_URL \
  -c "DROP USER aeos_admin;"

# Step 5: Rename new user (optional)
kubectl exec -n aeos-api deployment/aeos-api -- psql $DATABASE_URL \
  -c "ALTER USER aeos_admin_2 RENAME TO aeos_admin;"
```

---

## Procedure 4: Rotate JWT Secret (Maintenance Window Required)

**Impact**: All existing JWT tokens are immediately invalidated — users are logged out.

Schedule during low-traffic window. Announce in advance.

```bash
# Step 1: Generate new JWT secret
NEW_JWT=$(openssl rand -hex 64)

# Step 2: Update Secrets Manager
aws secretsmanager update-secret \
  --secret-id aeos/production/api-secrets \
  --secret-string "$(aws secretsmanager get-secret-value --secret-id aeos/production/api-secrets --query SecretString --output text | jq --arg k "$NEW_JWT" '.jwt_secret_key=$k')"

# Step 3: Force ESO sync
kubectl annotate externalsecret aeos-api-secrets -n aeos-api \
  force-sync=$(date +%s) --overwrite

# Step 4: Rolling restart (all pods get new JWT key simultaneously — brief auth disruption)
kubectl rollout restart deployment/aeos-api -n aeos-api
kubectl rollout status deployment/aeos-api -n aeos-api --timeout=5m

# Step 5: Verify login still works
curl -X POST https://api.aeos.example.com/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"test","password":"test"}'
```

---

## Automated Rotation (AWS Secrets Manager Lambda)

For fully automated rotation without manual steps, configure Secrets Manager rotation:

```bash
aws secretsmanager rotate-secret \
  --secret-id aeos/production/api-secrets \
  --rotation-lambda-arn arn:aws:lambda:us-east-1:ACCOUNT:function:aeos-secret-rotator \
  --rotation-rules AutomaticallyAfterDays=90
```

The rotation Lambda: (1) generates new credential, (2) tests it, (3) updates Secrets Manager, (4) triggers ESO sync via annotation, (5) monitors health.

---

## Emergency Rotation (Suspected Breach)

```bash
# 1. Revoke ALL secrets immediately
for secret in api-secrets redis database; do
  aws secretsmanager delete-secret \
    --secret-id aeos/production/$secret \
    --force-delete-without-recovery
done

# 2. Follow each rotation procedure above with new values
# 3. Restart all pods to flush in-memory credentials
kubectl rollout restart deployment/aeos-api -n aeos-api
kubectl rollout restart deployment/aeos-worker -n aeos-jobs

# 4. Audit access logs
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue=aeos/production \
  --start-time $(date -d '7 days ago' -u +%Y-%m-%dT%H:%M:%SZ)

# 5. File security incident report
```
