# AEOS — AWS Deployment Architecture

## Topology

```
Internet → Route 53 → ALB → ECS Fargate (aeos-api)
                              ↓
                         EFS /app/data  (model registry + datasets)
                         Secrets Manager (GITHUB_TOKEN, API keys)
                         CloudWatch Logs + X-Ray Tracing
                              ↓
                     Amazon OpenSearch Service
                     (or Pinecone VPC / Weaviate Cloud)
                     replace ChromaDB EphemeralClient in production
```

## ECS Fargate Task Definition

```json
{
  "family": "aeos-api",
  "cpu": "2048",
  "memory": "4096",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "containerDefinitions": [
    {
      "name": "aeos-api",
      "image": "<ECR_URI>/aeos:latest",
      "portMappings": [{"containerPort": 8000}],
      "environment": [
        {"name": "ENVIRONMENT",  "value": "production"},
        {"name": "LOG_JSON",     "value": "true"},
        {"name": "CHROMA_HOST",  "value": "<opensearch-or-chroma-host>"}
      ],
      "secrets": [
        {"name": "GITHUB_TOKEN", "valueFrom": "arn:aws:secretsmanager:..."}
      ],
      "mountPoints": [
        {"sourceVolume": "aeos-data", "containerPath": "/app/data"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group":  "/ecs/aeos",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "api"
        }
      }
    }
  ],
  "volumes": [
    {
      "name": "aeos-data",
      "efsVolumeConfiguration": {"fileSystemId": "<EFS_ID>"}
    }
  ]
}
```

## Key Decisions

| Concern | Solution |
|---|---|
| sentence-transformers memory | 2 vCPU / 4GB RAM minimum; scale horizontally not vertically |
| Model persistence | EFS mount at `/app/data` — survives task restarts |
| Secrets | AWS Secrets Manager injected as env vars at runtime |
| Vector DB | Swap ChromaDB for Pinecone or Weaviate Cloud in production |
| CI/CD | Push to ECR on main branch via GitHub Actions → ECS rolling deploy |
| Monitoring | CloudWatch Logs Insights for log queries; X-Ray for distributed tracing |

## CI/CD Pipeline (GitHub Actions)

```yaml
- name: Build and push to ECR
  run: |
    aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_URI
    docker build -t aeos .
    docker tag aeos:latest $ECR_URI/aeos:latest
    docker push $ECR_URI/aeos:latest

- name: Deploy to ECS
  run: |
    aws ecs update-service \
      --cluster aeos-cluster \
      --service aeos-api \
      --force-new-deployment
```

## Scaling Strategy

- **Horizontal scaling**: Run multiple ECS tasks behind ALB (sticky sessions not required — stateless API)
- **No GPU needed**: sentence-transformers CPU inference is ~100ms per embed call, acceptable for < 50 req/s
- **GPU path** (future): Replace sentence-transformers with a dedicated embedding service (e.g., AWS Bedrock Titan Embeddings) — zero code change, just swap `EMBEDDING_MODEL` config
