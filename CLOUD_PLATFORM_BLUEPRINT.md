## 3. DOCKER ARCHITECTURE

### 3.1 Multi-Stage Build Strategy

**Philosophy:** Minimize production image size, separate build-time from runtime dependencies, enable layer caching.

```dockerfile
# ═══════════════════════════════════════════════════════════════
# Dockerfile.api — FastAPI Service
# ═══════════════════════════════════════════════════════════════

# ─── Stage 1: Base image with Python runtime ───
FROM python:3.11-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ─── Stage 2: Builder (compile deps) ───
FROM base AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make libpq-dev git && \
    rm -rf /var/lib/apt/lists/*
COPY requirements/base.txt requirements/prod.txt ./
RUN pip install --user --no-warn-script-location \
    -r base.txt -r prod.txt

# ─── Stage 3: Production runtime ───
FROM base AS production
WORKDIR /app
RUN groupadd -r aeos && useradd -r -g aeos aeos && \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /root/.local /home/aeos/.local
COPY app/ ./app/
COPY software_intelligence/ ./software_intelligence/
COPY ml_platform/ ./ml_platform/
RUN chown -R aeos:aeos /app
USER aeos
ENV PATH=/home/aeos/.local/bin:$PATH
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", "--log-config", "logging.yaml"]
```

**Justification:**
- **Multi-stage**: Builder stage discarded → 60% smaller final image (300MB vs 800MB)
- **Non-root user**: Security best practice (prevents container breakout escalation)
- **Health check**: K8s liveness/readiness probes rely on this
- **Layer caching**: `COPY requirements` before `COPY app/` → faster rebuilds

---

```dockerfile
# ═══════════════════════════════════════════════════════════════
# Dockerfile.worker — Background Worker (Celery/OSIP/ML)
# ═══════════════════════════════════════════════════════════════

FROM python:3.11-slim-bookworm AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    C_FORCE_ROOT=1

FROM base AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make libpq-dev git && \
    rm -rf /var/lib/apt/lists/*
COPY requirements/base.txt requirements/prod.txt requirements/worker.txt ./
RUN pip install --user -r base.txt -r prod.txt -r worker.txt

FROM base AS production
WORKDIR /app
RUN groupadd -r aeos && useradd -r -g aeos aeos && \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /root/.local /home/aeos/.local
COPY app/ ./app/
COPY software_intelligence/ ./software_intelligence/
COPY ml_platform/ ./ml_platform/
RUN chown -R aeos:aeos /app
USER aeos
ENV PATH=/home/aeos/.local/bin:$PATH
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD celery -A app.workers inspect ping || exit 1
CMD ["celery", "-A", "app.workers", "worker", \
     "--loglevel=info", "--concurrency=4", "--max-tasks-per-child=100"]
```

**Justification:**
- **Separate worker image**: Different resource profile (CPU-intensive) vs API (I/O-bound)
- **`--max-tasks-per-child=100`**: Prevent memory leaks in long-running workers
- **Health check via Celery inspect**: Ensures worker is responsive to queue

---

```dockerfile
# ═══════════════════════════════════════════════════════════════
# Dockerfile.ml — ML Training/Inference (GPU support)
# ═══════════════════════════════════════════════════════════════

FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04 AS base
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

FROM base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip gcc g++ git && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements/base.txt requirements/ml.txt ./
RUN pip3 install --user -r base.txt -r ml.txt

FROM base AS production
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3-pip libgomp1 && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN groupadd -r aeos && useradd -r -g aeos aeos
COPY --from=builder /root/.local /home/aeos/.local
COPY ml_platform/ ./ml_platform/
COPY app/ml_pipeline/ ./app/ml_pipeline/
RUN chown -R aeos:aeos /app
USER aeos
ENV PATH=/home/aeos/.local/bin:$PATH
# GPU health check
HEALTHCHECK --interval=120s --timeout=10s --start-period=60s \
    CMD python3 -c "import torch; assert torch.cuda.is_available()" || exit 1
CMD ["python3", "-m", "ml_platform.training.engine", "--config", "/config/training.yaml"]
```

**Justification:**
- **CUDA base image**: Required for GPU-accelerated training
- **Separate ML image**: PyTorch + CUDA = 4GB image; keep API image lean
- **GPU health check**: Verifies CUDA availability before accepting jobs

---

### 3.2 Development Images

```dockerfile
# ═══════════════════════════════════════════════════════════════
# Dockerfile.dev — Development image with hot-reload
# ═══════════════════════════════════════════════════════════════

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    git make curl vim && \
    rm -rf /var/lib/apt/lists/*
COPY requirements/base.txt requirements/dev.txt ./
RUN pip install -r base.txt -r dev.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--reload"]
```

**Justification:**
- **Single-stage**: Build speed > image size in dev
- **`--reload`**: Auto-restart on code changes
- **Dev tools included**: `vim`, `curl`, `make` for debugging

---

### 3.3 Container Networking

```yaml
# docker-compose.yml — Local Development Stack
version: '3.9'

services:
  api:
    build:
      context: .
      dockerfile: infrastructure/docker/Dockerfile.dev
    ports:
      - "8000:8000"
    volumes:
      - .:/app
      - pip-cache:/root/.cache/pip
    environment:
      - DATABASE_URL=postgresql://aeos:dev@postgres:5432/aeos
      - REDIS_URL=redis://redis:6379/0
      - QDRANT_URL=http://qdrant:6333
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - aeos-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 3s
      retries: 3

  worker:
    build:
      context: .
      dockerfile: infrastructure/docker/Dockerfile.worker
    volumes:
      - .:/app
    environment:
      - DATABASE_URL=postgresql://aeos:dev@postgres:5432/aeos
      - REDIS_URL=redis://redis:6379/0
      - CELERY_BROKER_URL=redis://redis:6379/1
    depends_on:
      - postgres
      - redis
    networks:
      - aeos-network

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: aeos
      POSTGRES_USER: aeos
      POSTGRES_PASSWORD: dev
    volumes:
      - postgres-data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    networks:
      - aeos-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U aeos"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis-data:/data
    ports:
      - "6379:6379"
    networks:
      - aeos-network
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  qdrant:
    image: qdrant/qdrant:v1.7.4
    ports:
      - "6333:6333"
    volumes:
      - qdrant-data:/qdrant/storage
    networks:
      - aeos-network

volumes:
  postgres-data:
  redis-data:
  qdrant-data:
  pip-cache:

networks:
  aeos-network:
    driver: bridge
```

**Justification:**
- **Named network**: Containers communicate by service name (DNS-based)
- **Health checks**: `depends_on` waits for actual service readiness, not just container start
- **Volume mounts**: Dev code changes reflected without rebuild
- **Named volumes**: Data persists across `docker-compose down`

---

### 3.4 Production Docker Compose (Smoke Testing)

```yaml
# docker-compose.prod.yml
version: '3.9'

services:
  api:
    image: ${DOCKER_REGISTRY}/aeos-api:${VERSION}
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
    networks:
      - aeos-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./infrastructure/config/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro
    depends_on:
      - api
    networks:
      - aeos-network

networks:
  aeos-network:
    driver: overlay
```

**Justification:**
- **Resource limits**: Prevents single container consuming all host resources
- **Overlay network**: Multi-host networking (Swarm/K8s simulation)
- **Nginx reverse proxy**: Simulates production ALB behavior locally

---

### 3.5 Container Security & Best Practices

| Practice | Implementation | Justification |
|---|---|---|
| **Non-root user** | `USER aeos` | Limits blast radius of container breakout |
| **Read-only root FS** | `--read-only` flag in K8s | Prevents runtime tampering |
| **No secrets in image** | Env vars + Secrets Manager | Secrets in layers are immutable & auditable |
| **Minimal base image** | `slim-bookworm` (40MB) vs `latest` (900MB) | Smaller attack surface |
| **Distroless (future)** | Google distroless images | No shell, package manager → unhackable |
| **Image scanning** | Trivy in CI/CD | Detect CVEs before production |
| **Signed images** | Docker Content Trust | Verify image provenance |
| **Resource limits** | CPU/memory limits in K8s | Prevent noisy neighbor issues |

---

### 3.6 Health Check Strategy

```python
# app/health.py
from fastapi import APIRouter, status
from sqlalchemy import text
from app.database import engine
from app.redis import redis_client

router = APIRouter()

@router.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """Shallow health check (K8s liveness probe)"""
    return {"status": "ok"}

@router.get("/health/ready", status_code=status.HTTP_200_OK)
async def readiness_check():
    """Deep health check (K8s readiness probe)"""
    checks = {}
    
    # Database
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {str(e)}"
        raise
    
    # Redis
    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {str(e)}"
        raise
    
    return {"status": "ready", "checks": checks}
```

**Justification:**
- **Liveness vs Readiness**: Liveness = "is process alive?", Readiness = "can it serve traffic?"
- **Fast liveness**: No DB queries (< 100ms) to prevent cascading failures
- **Deep readiness**: Validates all dependencies before K8s routes traffic

---

## 4. KUBERNETES ARCHITECTURE

### 4.1 Namespace Design

```yaml
# kubernetes/base/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: aeos-api
  labels:
    env: production
    team: platform
---
apiVersion: v1
kind: Namespace
metadata:
  name: aeos-jobs
  labels:
    env: production
    team: platform
---
apiVersion: v1
kind: Namespace
metadata:
  name: aeos-data
  labels:
    env: production
    team: platform
---
apiVersion: v1
kind: Namespace
metadata:
  name: aeos-monitoring
  labels:
    env: production
    team: platform
```

**Justification:**
- **Namespace isolation**: RBAC boundaries, resource quotas per namespace
- **`aeos-api`**: User-facing services (FastAPI, Ingress)
- **`aeos-jobs`**: Background workers (OSIP, ML training)
- **`aeos-data`**: Stateful workloads (Redis, Qdrant)
- **`aeos-monitoring`**: Observability stack (Prometheus, Grafana)

---

### 4.2 API Deployment

```yaml
# kubernetes/base/api-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aeos-api
  namespace: aeos-api
  labels:
    app: aeos-api
    version: v1
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: aeos-api
  template:
    metadata:
      labels:
        app: aeos-api
        version: v1
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: aeos-api-sa
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchExpressions:
                - key: app
                  operator: In
                  values: [aeos-api]
              topologyKey: topology.kubernetes.io/zone
      containers:
      - name: api
        image: ${DOCKER_REGISTRY}/aeos-api:${VERSION}
        imagePullPolicy: IfNotPresent
        ports:
        - name: http
          containerPort: 8000
          protocol: TCP
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: aeos-db-secret
              key: url
        - name: REDIS_URL
          valueFrom:
            configMapKeyRef:
              name: aeos-config
              key: redis_url
        - name: OTEL_EXPORTER_OTLP_ENDPOINT
          value: "http://jaeger-collector.aeos-monitoring:4318"
        resources:
          requests:
            cpu: 500m
            memory: 1Gi
          limits:
            cpu: 2000m
            memory: 2Gi
        livenessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 3
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /health/ready
            port: http
          initialDelaySeconds: 10
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 2
        securityContext:
          runAsNonRoot: true
          runAsUser: 1000
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities:
            drop: ["ALL"]
        volumeMounts:
        - name: tmp
          mountPath: /tmp
        - name: cache
          mountPath: /app/.cache
      volumes:
      - name: tmp
        emptyDir: {}
      - name: cache
        emptyDir: {}
```

**Key Decisions:**
- **3 replicas**: Minimum for HA (survives 1 node failure)
- **RollingUpdate**: Zero-downtime deployments
- **maxUnavailable: 0**: Always maintain 3 healthy pods during rollout
- **Pod anti-affinity**: Spread pods across AZs (topology key = zone)
- **Resource requests**: K8s scheduler guarantee (500m CPU = 0.5 core)
- **Resource limits**: Hard cap to prevent runaway processes
- **Read-only root FS**: Security hardening (tmpfs for `/tmp`)
- **Security context**: Drop all Linux capabilities, non-root user

---

### 4.3 Horizontal Pod Autoscaler

```yaml
# kubernetes/base/hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: aeos-api-hpa
  namespace: aeos-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: aeos-api
  minReplicas: 3
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
  - type: Pods
    pods:
      metric:
        name: http_requests_per_second
      target:
        type: AverageValue
        averageValue: "1000"
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
      - type: Percent
        value: 50
        periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
      - type: Percent
        value: 100
        periodSeconds: 15
      - type: Pods
        value: 4
        periodSeconds: 15
      selectPolicy: Max
```

**Justification:**
- **Multi-metric HPA**: CPU + memory + custom metric (RPS)
- **70% CPU target**: Headroom for traffic spikes without triggering scale
- **Scale-up aggressive**: Double pods in 15s during spike
- **Scale-down conservative**: 5-min stabilization to prevent flapping
- **Max scale-up: 100%**: Doubles capacity per cycle (exponential growth)

---

### 4.4 Service Definition

```yaml
# kubernetes/base/api-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: aeos-api
  namespace: aeos-api
  labels:
    app: aeos-api
spec:
  type: ClusterIP
  selector:
    app: aeos-api
  ports:
  - name: http
    port: 80
    targetPort: http
    protocol: TCP
  sessionAffinity: None
```

**Justification:**
- **ClusterIP**: Internal-only; ALB/Ingress routes external traffic
- **Port 80 → 8000**: Service abstraction allows port remapping
- **No session affinity**: Stateless API (sticky sessions handled at ALB if needed)

---

### 4.5 Ingress Controller

```yaml
# kubernetes/base/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: aeos-ingress
  namespace: aeos-api
  annotations:
    kubernetes.io/ingress.class: "alb"
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/healthcheck-path: /health
    alb.ingress.kubernetes.io/healthcheck-interval-seconds: "15"
    alb.ingress.kubernetes.io/certificate-arn: ${ACM_CERT_ARN}
    alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS-1-2-2017-01
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 80}, {"HTTPS": 443}]'
    alb.ingress.kubernetes.io/ssl-redirect: "443"
spec:
  rules:
  - host: api.aeos.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: aeos-api
            port:
              number: 80
```

**Justification:**
- **AWS ALB Ingress Controller**: Native AWS integration, WAF support
- **Target type: IP**: Direct pod routing (no NodePort overhead)
- **TLS 1.2+**: Security compliance
- **Auto SSL redirect**: Enforce HTTPS

---

### 4.6 ConfigMap & Secrets

```yaml
# kubernetes/base/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aeos-config
  namespace: aeos-api
data:
  redis_url: "redis://aeos-redis.aeos-data.svc.cluster.local:6379/0"
  qdrant_url: "http://aeos-qdrant.aeos-data.svc.cluster.local:6333"
  log_level: "INFO"
  environment: "production"
  otel_enabled: "true"
  max_workers: "4"
```

```yaml
# kubernetes/base/secrets.yaml (Sealed Secrets)
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: aeos-db-secret
  namespace: aeos-api
spec:
  encryptedData:
    url: AgBY... # Encrypted DATABASE_URL
    username: AgCX...
    password: AgDZ...
```

**Justification:**
- **ConfigMap**: Non-sensitive config (URLs, log levels)
- **Sealed Secrets**: Encrypted secrets in Git (Bitnami sealed-secrets controller)
- **Alternative**: External Secrets Operator → AWS Secrets Manager sync

---

### 4.7 StatefulSet for Redis

```yaml
# kubernetes/base/redis-statefulset.yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: aeos-redis
  namespace: aeos-data
spec:
  serviceName: aeos-redis
  replicas: 3
  selector:
    matchLabels:
      app: aeos-redis
  template:
    metadata:
      labels:
        app: aeos-redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        command:
        - redis-server
        - /config/redis.conf
        ports:
        - name: redis
          containerPort: 6379
        volumeMounts:
        - name: data
          mountPath: /data
        - name: config
          mountPath: /config
        resources:
          requests:
            cpu: 250m
            memory: 512Mi
          limits:
            cpu: 1000m
            memory: 2Gi
      volumes:
      - name: config
        configMap:
          name: redis-config
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: gp3
      resources:
        requests:
          storage: 20Gi
```

**Justification:**
- **StatefulSet vs Deployment**: Stable network identity + persistent storage
- **3 replicas**: Redis Sentinel HA (1 master + 2 replicas)
- **volumeClaimTemplates**: Each pod gets dedicated PVC (data survives pod restart)
- **gp3 storage class**: AWS EBS gp3 (3000 IOPS baseline, cheaper than gp2)

---

### 4.8 Pod Disruption Budget

```yaml
# kubernetes/base/pdb.yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-api-pdb
  namespace: aeos-api
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: aeos-api
```

**Justification:**
- **minAvailable: 2**: During node drain (e.g., cluster upgrade), always keep 2 pods running
- **Prevents cascading failures**: K8s won't drain node if it violates PDB

---

### 4.9 Resource Quotas

```yaml
# kubernetes/base/resourcequota.yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: aeos-api-quota
  namespace: aeos-api
spec:
  hard:
    requests.cpu: "20"
    requests.memory: 40Gi
    limits.cpu: "40"
    limits.memory: 80Gi
    persistentvolumeclaims: "10"
    services.loadbalancers: "2"
```

**Justification:**
- **Cost control**: Namespace-level cap prevents runaway resource consumption
- **20 CPU requests**: Max 40 pods @ 500m each
- **PVC limit**: Prevents storage sprawl

---

### 4.10 Blue-Green Deployment Strategy

```yaml
# kubernetes/overlays/production/blue-green-rollout.yaml
apiVersion: v1
kind: Service
metadata:
  name: aeos-api-blue
  namespace: aeos-api
spec:
  selector:
    app: aeos-api
    version: blue
  ports:
  - port: 80
    targetPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: aeos-api-green
  namespace: aeos-api
spec:
  selector:
    app: aeos-api
    version: green
  ports:
  - port: 80
    targetPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: aeos-api-live
  namespace: aeos-api
  annotations:
    deployment-strategy: blue-green
spec:
  selector:
    app: aeos-api
    version: blue  # Initially points to blue
  ports:
  - port: 80
    targetPort: 8000
```

**Blue-Green Cutover Process:**
1. Deploy new version to `green` deployment (separate pods)
2. Run smoke tests against `aeos-api-green` service
3. If tests pass, update `aeos-api-live` selector: `version: blue → green`
4. Monitor for 15 minutes
5. If rollback needed, revert selector to `blue`
6. After validation, tear down old `blue` deployment

**Justification:**
- **Instant rollback**: Single selector change (< 1s switchover)
- **Zero downtime**: Both versions run simultaneously during cutover
- **Safety**: Smoke tests on isolated green stack before production traffic

---

### 4.11 Node Affinity & Taints

```yaml
# kubernetes/base/ml-worker-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aeos-ml-worker
  namespace: aeos-jobs
spec:
  replicas: 2
  template:
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: workload-type
                operator: In
                values:
                - gpu
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      containers:
      - name: ml-worker
        image: ${REGISTRY}/aeos-ml:${VERSION}
        resources:
          limits:
            nvidia.com/gpu: 1
```

**Node Pool Design:**
```bash
# EKS node groups
- general-purpose:  t3.xlarge (4 vCPU, 16GB) — API, workers
- memory-optimized: r6i.2xlarge (8 vCPU, 64GB) — OSIP, Redis
- gpu-accelerated:  g5.2xlarge (8 vCPU, 24GB, 1x A10G) — ML training
```

**Justification:**
- **Node affinity**: GPU pods ONLY on GPU nodes (cost optimization)
- **Taints**: Prevent non-GPU workloads from wasting expensive GPU nodes
- **Separate node pools**: Allows independent autoscaling per workload type

---

## 5. AWS REFERENCE ARCHITECTURE

### 5.1 Service Selection Matrix

| Component | AWS Service | Alternative Considered | Decision Rationale |
|---|---|---|---|
| **Container Orchestration** | EKS | ECS Fargate | EKS: Greater flexibility for stateful workloads (Redis, Qdrant), rich K8s ecosystem, multi-cloud portability |
| **Compute** | EC2 (EKS nodes) | Fargate | EC2: Cost-effective for sustained workloads, GPU support, node affinity control |
| **Database** | RDS PostgreSQL | Aurora, DynamoDB | RDS: Familiar SQL, lower cost than Aurora, automated backups, Multi-AZ failover |
| **Cache** | ElastiCache Redis | MemoryDB | ElastiCache: Redis 7 cluster mode, lower cost, sufficient for cache use case (MemoryDB for durability not needed) |
| **Vector DB** | Qdrant (self-hosted) | Pinecone, Weaviate Cloud | Self-hosted: Data sovereignty, cost control at scale, no vendor lock-in |
| **Object Storage** | S3 | EFS | S3: 11 nines durability, lifecycle policies, cross-region replication, lower cost for cold data |
| **Message Queue** | SQS | RabbitMQ, Kafka MSK | SQS: Fully managed, infinite scale, pay-per-use, no ops overhead |
| **Load Balancer** | ALB | NLB | ALB: Layer 7 routing, WAF integration, path-based routing, cognito auth support |
| **DNS** | Route 53 | CloudFlare | Route 53: Native AWS integration, health checks, geolocation routing |
| **Secrets** | Secrets Manager | SSM Parameter Store | Secrets Manager: Auto-rotation, cross-region replication, audit logging |
| **Monitoring** | CloudWatch + Prometheus | Datadog, New Relic | Hybrid: CloudWatch for AWS resources, Prometheus for app metrics (cost optimization) |
| **Logging** | CloudWatch Logs | ELK, Splunk | CloudWatch: Native integration, log retention policies, pay-per-GB |
| **Container Registry** | ECR | Docker Hub | ECR: Private, IAM-integrated, vulnerability scanning, cross-region replication |

---

### 5.2 AWS Network Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Region: us-east-1                       VPC: 10.0.0.0/16        │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Availability Zone 1 (us-east-1a)                          │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Public Subnet 1: 10.0.1.0/24                         │ │ │
│  │  │  • NAT Gateway                                        │ │ │
│  │  │  • ALB (internet-facing)                             │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Private Subnet 1 (App): 10.0.11.0/24                │ │ │
│  │  │  • EKS worker nodes (t3.xlarge)                      │ │ │
│  │  │  • API pods, worker pods                             │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Private Subnet 2 (Data): 10.0.21.0/24               │ │ │
│  │  │  • RDS primary instance                               │ │ │
│  │  │  • ElastiCache cluster node 1                        │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Availability Zone 2 (us-east-1b)                          │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Public Subnet 2: 10.0.2.0/24                         │ │ │
│  │  │  • NAT Gateway                                        │ │ │
│  │  │  • ALB (zone redundancy)                             │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Private Subnet 3 (App): 10.0.12.0/24                │ │ │
│  │  │  • EKS worker nodes (t3.xlarge)                      │ │ │
│  │  │  • API pods, worker pods                             │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Private Subnet 4 (Data): 10.0.22.0/24               │ │ │
│  │  │  • RDS standby instance (Multi-AZ)                   │ │ │
│  │  │  • ElastiCache cluster node 2                        │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Availability Zone 3 (us-east-1c)                          │ │
│  │                                                             │ │
│  │  ┌───────────────────────────────────────────────────────┐ │ │
│  │  │  Private Subnet 5 (Data): 10.0.23.0/24               │ │ │
│  │  │  • ElastiCache cluster node 3                        │ │ │
│  │  └───────────────────────────────────────────────────────┘ │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  Security Groups:                                                 │
│  • ALB-SG: 0.0.0.0/0:443 → ALB                                   │
│  • EKS-SG: ALB-SG:* → EKS nodes                                  │
│  • RDS-SG: EKS-SG:5432 → RDS                                     │
│  • Redis-SG: EKS-SG:6379 → ElastiCache                          │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

**Justification:**
- **Multi-AZ**: 3 AZs for 99.95% availability (survives 1 AZ failure)
- **Private subnets for compute**: No direct internet access (defense in depth)
- **NAT Gateway per AZ**: Prevents single point of failure
- **Separate app/data subnets**: Network-level isolation, granular security groups
- **/24 subnets**: 251 usable IPs per subnet (sufficient for 200 pods per AZ)

---

### 5.3 RDS Configuration

```hcl
# terraform/modules/rds/main.tf
resource "aws_db_instance" "aeos" {
  identifier     = "aeos-prod"
  engine         = "postgres"
  engine_version = "16.1"
  instance_class = "db.r6i.xlarge"  # 4 vCPU, 32GB RAM
  
  allocated_storage     = 500   # GB
  max_allocated_storage = 2000  # Auto-scaling up to 2TB
  storage_type          = "gp3"
  iops                  = 12000
  
  multi_az               = true  # Standby in AZ-2
  db_subnet_group_name   = aws_db_subnet_group.aeos.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  
  backup_retention_period = 30  # 30-day retention
  backup_window           = "03:00-04:00"  # UTC
  maintenance_window      = "sun:04:00-sun:05:00"
  
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
  performance_insights_retention_period = 7
  
  deletion_protection = true
  skip_final_snapshot = false
  final_snapshot_identifier = "aeos-prod-final-snapshot"
}

resource "aws_db_instance" "aeos_replica" {
  identifier     = "aeos-prod-replica"
  replicate_source_db = aws_db_instance.aeos.id
  instance_class = "db.r6i.large"  # Read replica can be smaller
  publicly_accessible = false
}
```

**Justification:**
- **r6i.xlarge**: Memory-optimized for caching query plans
- **gp3 storage**: 50% cheaper than gp2, 12K IOPS for analytical queries
- **Multi-AZ**: Auto-failover in 60-120 seconds
- **30-day backups**: Meets compliance requirements (GDPR, SOC2)
- **Read replica**: Offload analytics queries from primary

---

### 5.4 ElastiCache Redis Configuration

```hcl
# terraform/modules/elasticache/main.tf
resource "aws_elasticache_replication_group" "aeos" {
  replication_group_id       = "aeos-prod-redis"
  description                = "AEOS production Redis cluster"
  node_type                  = "cache.r7g.large"   # 2 vCPU, 13GB RAM
  num_cache_clusters         = 3                   # 1 primary + 2 replicas
  parameter_group_name       = "default.redis7.cluster.on"
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.aeos.name
  security_group_ids         = [aws_security_group.redis.id]

  automatic_failover_enabled = true     # Promotes replica in ~30s
  multi_az_enabled           = true
  at_rest_encryption_enabled = true     # AES-256
  transit_encryption_enabled = true     # TLS in-transit

  snapshot_retention_limit = 7          # 7-day RDB snapshots
  snapshot_window          = "04:00-05:00"

  log_delivery_configuration {
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
    destination      = aws_cloudwatch_log_group.redis_slow.name
  }
}
```

**Justification:**
- **cache.r7g.large**: Graviton3 = 25% cheaper than x86, 13GB enough for AEOS cache needs
- **Cluster mode disabled**: Simpler for cache-only use case (no need for 500GB+ dataset sharding)
- **TLS + encryption at rest**: Data protection in transit and storage
- **Automatic failover**: RPO = 0 (replica stays in sync), RTO = 30s for Redis

---

### 5.5 EKS Cluster Configuration

```hcl
# terraform/modules/eks/main.tf
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "aeos-production"
  cluster_version = "1.29"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Managed node groups
  eks_managed_node_groups = {
    general = {
      name           = "general-purpose"
      instance_types = ["t3.xlarge"]
      min_size       = 3
      max_size       = 20
      desired_size   = 5
      labels         = { workload-type = "general" }
      taints         = []
      disk_size      = 100
    }

    memory = {
      name           = "memory-optimized"
      instance_types = ["r6i.2xlarge"]
      min_size       = 2
      max_size       = 10
      desired_size   = 3
      labels         = { workload-type = "memory" }
      taints = [{
        key    = "memory-optimized"
        value  = "true"
        effect = "NO_SCHEDULE"
      }]
    }

    gpu = {
      name           = "gpu"
      instance_types = ["g5.2xlarge"]
      min_size       = 0
      max_size       = 5
      desired_size   = 0   # Scale from 0 (cost optimization)
      ami_type       = "AL2_x86_64_GPU"
      labels         = { workload-type = "gpu" }
      taints = [{
        key    = "nvidia.com/gpu"
        value  = "true"
        effect = "NO_SCHEDULE"
      }]
    }
  }

  # IAM Roles for Service Accounts (IRSA)
  enable_irsa = true

  # Add-ons
  cluster_addons = {
    coredns             = { most_recent = true }
    kube-proxy          = { most_recent = true }
    vpc-cni             = { most_recent = true }
    aws-ebs-csi-driver  = { most_recent = true }
    aws-load-balancer-controller = { most_recent = true }
  }
}
```

**Justification:**
- **Managed node groups**: AWS handles node provisioning, patching, replacement
- **GPU scale-to-zero**: GPU instances are expensive; scale to 0 when no ML training jobs
- **IRSA**: Pod-level IAM roles (no credential sharing between pods)
- **EBS CSI driver**: Required for PersistentVolumes (StatefulSets)
- **CoreDNS**: Service discovery within cluster

---

### 5.6 IAM Roles & IRSA

```hcl
# Pod IAM Role: API Service
resource "aws_iam_role" "aeos_api" {
  name = "aeos-api-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${aws_iam_openid_connect_provider.eks.url}:sub" =
            "system:serviceaccount:aeos-api:aeos-api-sa"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "aeos_api" {
  name = "aeos-api-policy"
  role = aws_iam_role.aeos_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:us-east-1:*:secret:aeos/prod/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${aws_s3_bucket.aeos.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage"]
        Resource = [aws_sqs_queue.aeos_jobs.arn]
      }
    ]
  })
}
```

**Justification:**
- **IRSA (IAM Roles for Service Accounts)**: Pod-level AWS credentials, no static keys
- **Least privilege**: Only the permissions each service actually needs
- **No wildcard Actions**: Explicit action list for auditing and security compliance

---

## 6. CI/CD WORKFLOW

### 6.1 Pipeline Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         CI/CD PIPELINE                                      │
│                                                                              │
│  Developer Push                                                              │
│       │                                                                      │
│  ┌────▼─────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 1: Source Control                                              │  │
│  │  • Git push to feature branch                                         │  │
│  │  • PR opened → CI triggered                                          │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 2: Code Quality (parallel)                                     │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐             │  │
│  │  │ ruff lint    │ │ ruff format  │ │ mypy type check  │             │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘             │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 3: Testing (parallel)                                          │  │
│  │  ┌──────────────┐ ┌──────────────────────────────────────┐           │  │
│  │  │ Unit tests   │ │ Integration tests (Postgres + Redis) │           │  │
│  │  │ (pytest -x)  │ │ (testcontainers)                     │           │  │
│  │  └──────────────┘ └──────────────────────────────────────┘           │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 4: Security Scanning (parallel)                                │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐             │  │
│  │  │ Bandit SAST  │ │ Safety (deps)│ │ Semgrep rules    │             │  │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘             │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 5: Container Build & Scan                                       │  │
│  │  • docker build (multi-stage)                                         │  │
│  │  • Trivy image scan                                                   │  │
│  │  • Push to ECR (SHA tag + latest)                                     │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 6: Staging Deployment (auto on main)                           │  │
│  │  • kubectl apply / Helm upgrade                                       │  │
│  │  • Run smoke tests                                                    │  │
│  │  • Run load tests (k6)                                                │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 7: Production Gate (manual approval required)                  │  │
│  │  • GitHub Environment protection rule                                 │  │
│  │  • Required reviewers: 1 platform engineer                           │  │
│  │  • 15-minute timeout window                                          │  │
│  └────┬──────────────────────────────────────────────────────────────────┘  │
│       │                                                                      │
│  ┌────▼──────────────────────────────────────────────────────────────────┐  │
│  │  STAGE 8: Production Deployment                                        │  │
│  │  • Blue-green deploy via kubectl                                      │  │
│  │  • Monitor error rate for 5 min                                      │  │
│  │  • Auto-rollback if errors > 1%                                      │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

### 6.2 GitHub Actions Workflows

```yaml
# .github/workflows/ci.yml
name: CI Pipeline

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

env:
  PYTHON_VERSION: "3.11"
  REGISTRY: ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com
  IMAGE_NAME: aeos-api

permissions:
  contents: read
  pull-requests: write
  id-token: write   # OIDC for AWS auth (no static keys)

jobs:

  # ── Quality Gate ──────────────────────────────────────────────────────
  quality:
    name: Code Quality
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: ${{ env.PYTHON_VERSION }}
        cache: pip

    - name: Install dev dependencies
      run: pip install -r requirements/dev.txt

    - name: Ruff lint
      run: ruff check . --output-format=github

    - name: Ruff format check
      run: ruff format . --check

    - name: Mypy type checking
      run: mypy app/ software_intelligence/ ml_platform/ --ignore-missing-imports

  # ── Testing ────────────────────────────────────────────────────────────
  test:
    name: Tests
    runs-on: ubuntu-latest
    needs: quality

    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_DB: aeos_test
          POSTGRES_USER: aeos
          POSTGRES_PASSWORD: test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:7
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: ${{ env.PYTHON_VERSION }}
        cache: pip

    - name: Install dependencies
      run: pip install -r requirements/base.txt -r requirements/test.txt

    - name: Run unit tests
      run: |
        pytest tests/unit/ \
          -v --tb=short \
          --cov=app --cov=software_intelligence --cov=ml_platform \
          --cov-report=xml --cov-report=term-missing \
          --cov-fail-under=80
      env:
        DATABASE_URL: postgresql://aeos:test@localhost:5432/aeos_test
        REDIS_URL: redis://localhost:6379/0

    - name: Run integration tests
      run: |
        pytest tests/integration/ -v --tb=short
      env:
        DATABASE_URL: postgresql://aeos:test@localhost:5432/aeos_test
        REDIS_URL: redis://localhost:6379/0

    - name: Upload coverage to Codecov
      uses: codecov/codecov-action@v4
      with:
        token: ${{ secrets.CODECOV_TOKEN }}
        file: coverage.xml

  # ── Security Scanning ──────────────────────────────────────────────────
  security:
    name: Security Scan
    runs-on: ubuntu-latest
    needs: quality

    steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: ${{ env.PYTHON_VERSION }}
        cache: pip

    - name: Install security tools
      run: pip install bandit safety

    - name: Bandit SAST (Python code)
      run: |
        bandit -r app/ software_intelligence/ ml_platform/ \
          -f json -o bandit-report.json \
          -ll  # Only HIGH/CRITICAL

    - name: Safety (dependency CVE check)
      run: |
        safety check -r requirements/prod.txt \
          --json --output safety-report.json

    - name: Semgrep SAST
      uses: returntocorp/semgrep-action@v1
      with:
        config: p/python p/owasp-top-ten

    - name: Upload security reports
      uses: actions/upload-artifact@v4
      if: always()
      with:
        name: security-reports
        path: |
          bandit-report.json
          safety-report.json

  # ── Container Build & Scan ─────────────────────────────────────────────
  build:
    name: Build & Scan Container
    runs-on: ubuntu-latest
    needs: [test, security]

    steps:
    - uses: actions/checkout@v4

    - name: Configure AWS credentials (OIDC)
      uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: ${{ secrets.AWS_CI_ROLE_ARN }}
        aws-region: us-east-1

    - name: Login to ECR
      id: login-ecr
      uses: aws-actions/amazon-ecr-login@v2

    - name: Set up Docker Buildx
      uses: docker/setup-buildx-action@v3

    - name: Build and push
      uses: docker/build-push-action@v5
      with:
        context: .
        file: infrastructure/docker/Dockerfile.api
        target: production
        push: true
        tags: |
          ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
          ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:latest
        cache-from: type=gha
        cache-to: type=gha,mode=max
        build-args: |
          VERSION=${{ github.sha }}

    - name: Trivy image scan
      uses: aquasecurity/trivy-action@master
      with:
        image-ref: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
        format: sarif
        severity: CRITICAL,HIGH
        exit-code: 1   # Fail on HIGH/CRITICAL CVEs

    - name: Upload Trivy results
      uses: github/codeql-action/upload-sarif@v3
      if: always()
      with:
        sarif_file: trivy-results.sarif

    outputs:
      image: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
```

---

```yaml
# .github/workflows/deploy-staging.yml
name: Deploy to Staging

on:
  push:
    branches: [main]
  workflow_run:
    workflows: ["CI Pipeline"]
    types: [completed]

jobs:
  deploy-staging:
    name: Deploy to Staging
    runs-on: ubuntu-latest
    environment: staging
    if: github.event.workflow_run.conclusion == 'success'

    steps:
    - uses: actions/checkout@v4

    - name: Configure AWS credentials
      uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
        aws-region: us-east-1

    - name: Update kubeconfig
      run: aws eks update-kubeconfig --name aeos-staging --region us-east-1

    - name: Run database migrations
      run: |
        kubectl run db-migrate \
          --image=${{ env.REGISTRY }}/aeos-api:${{ github.sha }} \
          --rm --restart=Never \
          --env="DATABASE_URL=$(kubectl get secret aeos-db-secret -o jsonpath='{.data.url}' | base64 -d)" \
          -- python -m alembic upgrade head
        kubectl wait --for=condition=complete job/db-migrate --timeout=120s

    - name: Deploy to staging
      run: |
        helm upgrade --install aeos infrastructure/kubernetes/helm/aeos \
          --namespace aeos-api \
          --values infrastructure/kubernetes/helm/aeos/values-staging.yaml \
          --set image.tag=${{ github.sha }} \
          --atomic --timeout 5m

    - name: Smoke tests
      run: bash infrastructure/ci-cd/scripts/smoke-test.sh staging

    - name: Notify Slack on success
      if: success()
      uses: slackapi/slack-github-action@v1.25.0
      with:
        payload: '{"text":"✅ Staging deployment successful: ${{ github.sha }}"}'
      env:
        SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK }}
```

---

```yaml
# .github/workflows/deploy-production.yml
name: Deploy to Production

on:
  workflow_dispatch:   # Manual trigger only
    inputs:
      version:
        description: 'Image tag to deploy (default: latest staging)'
        required: false
        type: string

jobs:
  deploy-production:
    name: Deploy to Production
    runs-on: ubuntu-latest
    environment: production   # Requires manual approval
    concurrency:
      group: production-deploy
      cancel-in-progress: false   # Never cancel ongoing deploy

    steps:
    - uses: actions/checkout@v4

    - name: Configure AWS credentials
      uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: ${{ secrets.AWS_PROD_ROLE_ARN }}
        aws-region: us-east-1

    - name: Update kubeconfig
      run: aws eks update-kubeconfig --name aeos-production --region us-east-1

    - name: Deploy green (blue-green strategy)
      run: |
        helm upgrade --install aeos-green infrastructure/kubernetes/helm/aeos \
          --namespace aeos-api \
          --values infrastructure/kubernetes/helm/aeos/values-production.yaml \
          --set image.tag=${{ inputs.version || github.sha }} \
          --set deployment.color=green \
          --atomic --timeout 10m

    - name: Smoke tests on green
      run: bash infrastructure/ci-cd/scripts/smoke-test.sh production-green

    - name: Switch traffic to green
      if: success()
      run: |
        kubectl patch service aeos-api-live \
          -n aeos-api \
          --patch '{"spec":{"selector":{"version":"green"}}}'

    - name: Monitor error rate (5 minutes)
      run: |
        bash infrastructure/ci-cd/scripts/monitor-error-rate.sh \
          --duration 300 \
          --threshold 0.01  # 1% error rate threshold
        EXIT_CODE=$?
        if [ $EXIT_CODE -ne 0 ]; then
          echo "Error rate exceeded threshold — triggering rollback"
          kubectl patch service aeos-api-live \
            -n aeos-api \
            --patch '{"spec":{"selector":{"version":"blue"}}}'
          exit 1
        fi

    - name: Teardown blue
      if: success()
      run: |
        helm uninstall aeos-blue --namespace aeos-api || true
        helm upgrade --install aeos-blue infrastructure/kubernetes/helm/aeos \
          --namespace aeos-api \
          --set image.tag=${{ inputs.version || github.sha }} \
          --set deployment.color=blue

    - name: Notify on failure and auto-rollback
      if: failure()
      run: |
        bash infrastructure/ci-cd/scripts/rollback.sh
```

---

## 7. OBSERVABILITY ARCHITECTURE

### 7.1 Three Pillars of Observability

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         OBSERVABILITY STACK                                  │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  METRICS (What is happening?)                                        │    │
│  │                                                                       │    │
│  │  App Metrics (Prometheus)     Infrastructure (CloudWatch)            │    │
│  │  • http_requests_total        • EC2 CPU/memory                       │    │
│  │  • http_latency_seconds       • RDS IOPS/connections                 │    │
│  │  • task_queue_depth           • ElastiCache hits/misses             │    │
│  │  • ml_training_duration       • ALB latency/4xx/5xx                 │    │
│  │  • osip_files_analyzed        • EKS node utilization               │    │
│  │              │                              │                         │    │
│  │              └──────────────┬───────────────┘                        │    │
│  │                             ▼                                         │    │
│  │                    Grafana Dashboards                                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  TRACES (Why is it happening?)                                       │    │
│  │                                                                       │    │
│  │  FastAPI → OpenTelemetry SDK → OTLP Collector → Jaeger               │    │
│  │                                                                       │    │
│  │  Trace spans:                                                         │    │
│  │  [HTTP Request]                                                       │    │
│  │    └── [Auth middleware]        (2ms)                                 │    │
│  │    └── [Rate limit check]       (1ms)                                 │    │
│  │    └── [DB query: get_repo]     (45ms)  ← slowest                   │    │
│  │    └── [Redis cache get]        (2ms)                                 │    │
│  │    └── [OSIP analysis trigger]  (8ms)                                 │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │  LOGS (What happened exactly?)                                       │    │
│  │                                                                       │    │
│  │  Structured JSON logs → FluentBit → CloudWatch Logs                  │    │
│  │                                   → Elasticsearch (if needed)        │    │
│  │                                                                       │    │
│  │  Log format:                                                          │    │
│  │  {                                                                    │    │
│  │    "timestamp": "2026-06-28T12:00:00Z",                              │    │
│  │    "level": "INFO",                                                   │    │
│  │    "service": "aeos-api",                                            │    │
│  │    "trace_id": "abc123",                                             │    │
│  │    "span_id": "def456",                                              │    │
│  │    "user_id": "u_789",                                               │    │
│  │    "message": "Repository analysis completed",                        │    │
│  │    "repo_id": "owner/repo",                                          │    │
│  │    "duration_ms": 1250                                               │    │
│  │  }                                                                    │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

### 7.2 OpenTelemetry Integration

```python
# app/telemetry.py
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from prometheus_client import start_http_server

def setup_telemetry(app, service_name: str = "aeos-api") -> None:
    """Bootstrap OpenTelemetry tracing + Prometheus metrics."""

    # ── Tracing ────────────────────────────────────────────────────────────
    otlp_exporter = OTLPSpanExporter(
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317"),
    )
    tracer_provider = TracerProvider(
        resource=Resource(attributes={
            SERVICE_NAME: service_name,
            "deployment.environment": os.getenv("ENVIRONMENT", "production"),
            "service.version": os.getenv("VERSION", "unknown"),
        })
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics (Prometheus) ───────────────────────────────────────────────
    reader = PrometheusMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # ── Auto-instrumentation ───────────────────────────────────────────────
    FastAPIInstrumentor.instrument_app(app)     # HTTP spans + metrics
    SQLAlchemyInstrumentor().instrument()        # DB query spans
    RedisInstrumentor().instrument()             # Redis spans

def get_tracer(name: str = "aeos"):
    return trace.get_tracer(name)

# Custom business metrics
meter = metrics.get_meter("aeos.business")

analysis_duration = meter.create_histogram(
    "aeos.analysis.duration",
    unit="seconds",
    description="Repository analysis duration",
)
task_queue_depth = meter.create_observable_gauge(
    "aeos.queue.depth",
    callbacks=[lambda: get_queue_depth()],
    description="Current task queue depth",
)
```

---

### 7.3 Prometheus Rules

```yaml
# monitoring/prometheus/alerts.yml
groups:
- name: aeos.api
  rules:
  # High error rate
  - alert: APIHighErrorRate
    expr: |
      rate(http_requests_total{status=~"5.."}[5m])
      / rate(http_requests_total[5m]) > 0.01
    for: 2m
    labels:
      severity: critical
      team: platform
    annotations:
      summary: "API error rate > 1%"
      description: "Error rate is {{ $value | humanizePercentage }} over last 5m"
      runbook: "https://wiki.example.com/runbooks/api-high-error-rate"

  # High latency
  - alert: APIHighLatency
    expr: |
      histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 2
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "API p95 latency > 2 seconds"
      description: "p95 latency: {{ $value }}s"

  # Pod crash-looping
  - alert: PodCrashLooping
    expr: |
      increase(kube_pod_container_status_restarts_total{namespace=~"aeos-.*"}[1h]) > 5
    for: 0m
    labels:
      severity: critical
    annotations:
      summary: "Pod {{ $labels.pod }} is crash-looping"

  # Queue depth alert
  - alert: TaskQueueHighDepth
    expr: aeos_queue_depth > 1000
    for: 10m
    labels:
      severity: warning
    annotations:
      summary: "Task queue depth is {{ $value }}"
      description: "Workers may be backed up. Consider scaling workers."

  # Database connection exhaustion
  - alert: DatabaseConnectionsHigh
    expr: |
      pg_stat_database_numbackends{datname="aeos"} / pg_settings_max_connections > 0.8
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Database connections at 80% capacity"

- name: aeos.infrastructure
  rules:
  # Node not ready
  - alert: NodeNotReady
    expr: kube_node_status_condition{condition="Ready",status="true"} == 0
    for: 1m
    labels:
      severity: critical

  # Disk running low
  - alert: DiskSpaceLow
    expr: |
      (node_filesystem_avail_bytes / node_filesystem_size_bytes) < 0.15
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Node {{ $labels.instance }} disk < 15%"
```

---

### 7.4 Grafana Dashboards

```json
{
  "title": "AEOS API Dashboard",
  "uid": "aeos-api-v1",
  "panels": [
    {
      "title": "Request Rate",
      "type": "graph",
      "targets": [
        {"expr": "rate(http_requests_total[5m])", "legendFormat": "{{method}} {{path}}"}
      ]
    },
    {
      "title": "p50 / p95 / p99 Latency",
      "type": "graph",
      "targets": [
        {"expr": "histogram_quantile(0.50, rate(http_request_duration_seconds_bucket[5m]))", "legendFormat": "p50"},
        {"expr": "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))", "legendFormat": "p95"},
        {"expr": "histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))", "legendFormat": "p99"}
      ]
    },
    {
      "title": "Error Rate (%)",
      "type": "stat",
      "targets": [
        {"expr": "rate(http_requests_total{status=~\"5..\"}[5m]) / rate(http_requests_total[5m]) * 100"}
      ]
    },
    {
      "title": "Task Queue Depth",
      "type": "graph",
      "targets": [{"expr": "aeos_queue_depth", "legendFormat": "Queue depth"}]
    },
    {
      "title": "Active Pods",
      "type": "stat",
      "targets": [{"expr": "kube_deployment_status_replicas_ready{deployment=\"aeos-api\"}"}]
    },
    {
      "title": "Database Connections",
      "type": "graph",
      "targets": [{"expr": "pg_stat_database_numbackends{datname=\"aeos\"}"}]
    }
  ]
}
```

---

### 7.5 Structured Logging Configuration

```python
# app/logging_config.py
import logging
import json
from datetime import datetime, timezone

class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter for CloudWatch/Elasticsearch."""

    def format(self, record: logging.LogRecord) -> str:
        log = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "service":   "aeos-api",
            "environment": os.getenv("ENVIRONMENT", "production"),
        }
        # Inject trace context (OpenTelemetry)
        span = trace.get_current_span()
        ctx  = span.get_span_context()
        if ctx.is_valid:
            log["trace_id"] = format(ctx.trace_id, "032x")
            log["span_id"]  = format(ctx.span_id, "016x")

        # Extra fields from log call
        if hasattr(record, "extra"):
            log.update(record.extra)

        # Exception info
        if record.exc_info:
            log["exception"] = self.formatException(record.exc_info)

        return json.dumps(log)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {"()": StructuredFormatter},
        "simple":     {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",  # prod
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "uvicorn":     {"level": "INFO"},
        "sqlalchemy":  {"level": "WARNING"},
        "celery":      {"level": "INFO"},
    },
}
```

---

## 8. SECURITY ARCHITECTURE

### 8.1 Defense in Depth Model

```
Layer 0 — Network:    VPC, Private subnets, Security Groups, WAF
Layer 1 — Transport:  TLS 1.3 everywhere, mTLS (future service mesh)
Layer 2 — Auth:       OAuth2 + JWT (access + refresh tokens)
Layer 3 — Authz:      RBAC (role-based), resource-level policies
Layer 4 — API:        Rate limiting, input validation, CORS
Layer 5 — Secrets:    AWS Secrets Manager, IRSA (no static keys)
Layer 6 — Data:       Encryption at rest (RDS KMS, S3 SSE, Redis TLS)
Layer 7 — Audit:      Structured audit logs, CloudTrail, S3 access logs
Layer 8 — Container:  Non-root, read-only FS, Trivy scanning
Layer 9 — Code:       Bandit, Semgrep, dependency scanning (Safety)
```

---

### 8.2 Authentication & Authorization

```python
# app/security/auth.py
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader
from jose import JWTError, jwt
from pydantic import BaseModel
import time

SECRET_KEY     = os.getenv("JWT_SECRET_KEY")   # From Secrets Manager
ALGORITHM      = "RS256"                         # RSA (asymmetric)
ACCESS_EXPIRE  = 15 * 60                         # 15 minutes
REFRESH_EXPIRE = 7 * 24 * 3600                  # 7 days

oauth2_scheme   = OAuth2PasswordBearer(tokenUrl="/auth/token")
api_key_header  = APIKeyHeader(name="X-API-Key", auto_error=False)


class TokenPayload(BaseModel):
    sub:      str           # user_id
    roles:    list[str]     # ["admin", "user", "viewer"]
    exp:      int
    iat:      int
    jti:      str           # JWT ID (for revocation)


def create_access_token(user_id: str, roles: list[str]) -> str:
    payload = {
        "sub":   user_id,
        "roles": roles,
        "exp":   int(time.time()) + ACCESS_EXPIRE,
        "iat":   int(time.time()),
        "jti":   str(uuid4()),
        "type":  "access",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
) -> TokenPayload:
    try:
        payload = jwt.decode(token, PUBLIC_KEY, algorithms=[ALGORITHM])
        return TokenPayload(**payload)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── RBAC Decorator ────────────────────────────────────────────────────────────
def require_roles(*roles: str):
    def dependency(current_user: TokenPayload = Depends(get_current_user)):
        if not any(r in current_user.roles for r in roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user
    return dependency

# Usage:
# @router.delete("/repo/{id}", dependencies=[Depends(require_roles("admin"))])
# @router.post("/analysis", dependencies=[Depends(require_roles("admin", "analyst"))])
```

---

### 8.3 Rate Limiting

```python
# app/security/rate_limit.py
import redis
from fastapi import HTTPException, Request, status
import time

class SlidingWindowRateLimiter:
    """
    Redis-backed sliding window rate limiter.
    Justification: Sliding window > fixed window (prevents burst attacks
    at window boundary). Redis ZSET for O(log N) sorted-set operations.
    """

    def __init__(self, redis_client, limit: int, window_seconds: int):
        self.redis   = redis_client
        self.limit   = limit
        self.window  = window_seconds

    async def check(self, key: str) -> bool:
        now  = time.time()
        pipe = self.redis.pipeline()
        
        # Remove old entries outside window
        pipe.zremrangebyscore(key, 0, now - self.window)
        # Count requests in current window
        pipe.zcard(key)
        # Add current request
        pipe.zadd(key, {str(now): now})
        # Set key expiry (cleanup)
        pipe.expire(key, self.window)
        
        results = pipe.execute()
        current_count = results[1]
        
        return current_count < self.limit


# Rate limit tiers:
# Unauthenticated: 60 req/min
# User:            600 req/min
# Admin:           6000 req/min
# API Key:         10000 req/min

RATE_LIMITS = {
    "anonymous": (60,   60),
    "user":      (600,  60),
    "admin":     (6000, 60),
    "api_key":   (10000, 60),
}
```

---

### 8.4 Secrets Management

```yaml
# External Secrets Operator: sync AWS Secrets Manager → K8s Secrets
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: aeos-secrets
  namespace: aeos-api
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-store
    kind: ClusterSecretStore
  target:
    name: aeos-app-secrets
    creationPolicy: Owner
  data:
  - secretKey: database_url
    remoteRef:
      key: aeos/production/database
      property: url
  - secretKey: jwt_secret
    remoteRef:
      key: aeos/production/auth
      property: jwt_secret_key
  - secretKey: openai_api_key
    remoteRef:
      key: aeos/production/external-apis
      property: openai_key
```

**Secret Rotation Policy:**
| Secret | Rotation Interval | Method |
|---|---|---|
| DB password | 30 days | AWS Secrets Manager auto-rotation (Lambda) |
| JWT signing key | 90 days | Manual + 24h overlap period |
| API keys | 180 days | Manual rotation with deprecation notice |
| TLS certificates | 90 days | ACM auto-renewal |

---

### 8.5 Audit Logging

```python
# app/security/audit.py
from dataclasses import dataclass
import json, time

@dataclass
class AuditEvent:
    timestamp:  str
    event_type: str      # "auth.login", "repo.delete", "user.create"
    user_id:    str
    ip_address: str
    resource:   str
    action:     str
    outcome:    str      # "success" | "failure" | "denied"
    metadata:   dict

def audit_log(event: AuditEvent):
    """Write to structured CloudWatch audit log group."""
    print(json.dumps({   # CloudWatch picks up stdout in K8s
        "audit": True,
        **asdict(event),
    }))

# Audit events are sent to a separate CloudWatch Log Group
# with 7-year retention for compliance (SOC2, GDPR)
```

---

## 9. SCALING STRATEGY

### 9.1 Horizontal Scaling

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      SCALING LAYERS (OUTER TO INNER)                        │
│                                                                              │
│  1. DNS Load Balancing (Route 53 weighted routing)                          │
│     └─► Multi-region traffic distribution (future: us-east-1 + eu-west-1)  │
│                                                                              │
│  2. Application Load Balancer                                               │
│     └─► Distributes across healthy EKS nodes                               │
│                                                                              │
│  3. Kubernetes HPA (Horizontal Pod Autoscaler)                             │
│     ├─► API pods:     3 → 20 (CPU 70%, RPS > 1000)                        │
│     ├─► Workers:      2 → 50 (queue depth > 100)                          │
│     └─► ML workers:   0 → 5  (GPU jobs in queue)                          │
│                                                                              │
│  4. Kubernetes Cluster Autoscaler                                           │
│     └─► Adds/removes EC2 nodes when pods are Pending or nodes underutil.   │
│                                                                              │
│  5. KEDA (Kubernetes Event-Driven Autoscaling)                             │
│     └─► Scale workers directly from SQS queue depth                        │
│         0 workers when queue empty → 50 workers at queue depth 10000       │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 9.2 KEDA ScaledObject (Queue-Driven Worker Scaling)

```yaml
# kubernetes/base/keda-scaledobject.yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: aeos-worker-scaler
  namespace: aeos-jobs
spec:
  scaleTargetRef:
    name: aeos-worker
  minReplicaCount: 0      # Scale to zero when queue empty (cost saving!)
  maxReplicaCount: 50
  cooldownPeriod: 300     # 5 min cooldown before scale-down
  pollingInterval: 30     # Check queue every 30 seconds
  triggers:
  - type: aws-sqs-queue
    authenticationRef:
      name: keda-aws-credentials
    metadata:
      queueURL: https://sqs.us-east-1.amazonaws.com/123456789/aeos-jobs
      queueLength: "5"         # 1 worker per 5 messages
      awsRegion: us-east-1
      identityOwner: operator  # Use IRSA
```

**Justification:**
- **Scale-to-zero**: Workers cost money; 0 workers = 0 cost when idle
- **Queue depth trigger**: Direct relationship between load and workers
- **5 msg/worker**: Empirically tuned for OSIP analysis job duration (~30s)

---

### 9.3 Database Scaling

```
                    ┌───────────────────────────────┐
                    │    RDS PostgreSQL Primary      │
                    │    db.r6i.xlarge (4vCPU 32GB) │
                    │    Write + Read (hot path)     │
                    └──────────────┬────────────────┘
                                   │ async replication
                 ┌─────────────────┼─────────────────┐
                 ▼                 ▼                  ▼
    ┌────────────────┐  ┌─────────────────┐  ┌───────────────┐
    │ Read Replica 1 │  │ Read Replica 2  │  │ Multi-AZ      │
    │ db.r6i.large   │  │ db.r6i.large    │  │ Standby       │
    │ Analytics      │  │ Reporting       │  │ (failover)    │
    └────────────────┘  └─────────────────┘  └───────────────┘

Connection pooling:
    App pods → PgBouncer (transaction mode, 20 connections/pod)
             → RDS Primary (max 400 connections)

Read/Write splitting:
    Writes  → engine.connect() → Primary
    Reads   → read_engine.connect() → Replica (load-balanced)
```

---

### 9.4 Cache Layers

```python
# Three-tier caching strategy
#
# L1: In-process (per-pod LRU)   — < 1ms  — hot config, schemas
# L2: Redis (cluster)            — 1-5ms  — API responses, sessions
# L3: S3 (object storage)        — 50ms   — large artifacts (models, reports)

from functools import lru_cache
from cachetools import TTLCache

# L1: In-process cache (per pod, non-shared)
_LOCAL_CACHE = TTLCache(maxsize=1000, ttl=60)

# L2: Redis cache (shared across all pods)
async def get_or_set_redis(key: str, factory, ttl=3600):
    val = await redis.get(key)
    if val:
        return json.loads(val)
    val = await factory()
    await redis.setex(key, ttl, json.dumps(val))
    return val

# Cache invalidation strategy:
# - On write: delete key (cache-aside pattern)
# - TTL expiry: safeguard for stale data
# - Version-tagged keys: "repo:v3:{repo_id}" → bump v3→v4 for mass invalidation
```

---

### 9.5 Autoscaling Policies

| Service | Min | Max | Trigger | Scale-Up Time | Scale-Down Time |
|---|---|---|---|---|---|
| API pods | 3 | 20 | CPU > 70% OR RPS > 1000 | 15s | 5 min |
| Worker pods | 0 | 50 | SQS queue depth / 5 | 30s | 5 min |
| ML workers | 0 | 5 | GPU queue depth > 0 | 60s | 10 min |
| EKS nodes | 3 | 30 | Pod pending > 60s | 3 min | 10 min |

---

## 10. DISASTER RECOVERY PLAN

### 10.1 Recovery Objectives

| Tier | Service | RTO | RPO | Strategy |
|---|---|---|---|---|
| **Tier 0 (Critical)** | API (read-only) | 1 min | 0 | Multi-AZ, Active-Active |
| **Tier 1 (Critical)** | API (writes) | 5 min | 5 min | Multi-AZ failover |
| **Tier 1 (Critical)** | PostgreSQL | 2 min | 5 min | RDS Multi-AZ auto-failover |
| **Tier 2 (High)** | Redis cache | 30 sec | 0 (in-memory) | ElastiCache auto-failover |
| **Tier 2 (High)** | Background workers | 5 min | 0 (queue) | SQS message retention 14 days |
| **Tier 3 (Medium)** | ML training | 1 hour | 30 min | Checkpoint saves every 30 min |
| **Tier 4 (Low)** | OSIP analysis | 4 hours | 1 hour | Re-analyzable from source |

---

### 10.2 Backup Strategy

```bash
# RDS Automated Backups
aws rds modify-db-instance \
  --db-instance-identifier aeos-prod \
  --backup-retention-period 30 \          # 30-day retention
  --preferred-backup-window "03:00-04:00" # Low-traffic window
  --apply-immediately

# RDS Manual Snapshot (pre-deployment)
aws rds create-db-snapshot \
  --db-instance-identifier aeos-prod \
  --db-snapshot-identifier "aeos-prod-pre-deploy-$(date +%Y%m%d%H%M)"

# S3 Cross-region replication
aws s3api put-bucket-replication \
  --bucket aeos-production-data \
  --replication-configuration file://s3-replication-config.json
  # Replication to s3://aeos-dr-us-west-2

# EBS Volume snapshots (PVC data: Qdrant, Redis AOF)
# Automated via AWS Data Lifecycle Manager (daily, 7-day retention)

# Qdrant snapshot (vector store)
curl -X POST http://qdrant:6333/collections/{collection}/snapshots
# Snapshots uploaded to S3 via CronJob (daily)
```

---

### 10.3 Multi-AZ Architecture

```
Normal Operation:
  AZ-1 (primary): API pods + DB primary + Redis primary  ← 100% traffic
  AZ-2 (standby): API pods + DB standby + Redis replica  ← 0% (hot standby)
  
AZ-1 Failure:
  AZ-2 (now primary): Automatic promotion
  • ALB health checks detect AZ-1 pods unhealthy (30s)
  • Traffic rerouted to AZ-2 pods (< 60s total)
  • RDS Multi-AZ failover (60-120s)
  • ElastiCache promotes AZ-2 replica (30s)
  
Total RTO: < 2 minutes for full AZ failure
```

---

### 10.4 Disaster Recovery Runbook

```markdown
# DISASTER RECOVERY RUNBOOK
## Severity: P0 — Full Region Failure

### Trigger Condition
- AWS us-east-1 region unavailable > 15 minutes
- All health checks failing across AZs

### Step 1: Verify (0-5 min)
□ Check AWS Service Health Dashboard
□ Confirm: not a monitoring issue (check from multiple networks)
□ Page on-call lead + VP Engineering via PagerDuty

### Step 2: Activate DR (5-15 min)
□ Switch Route 53 DNS to us-west-2 failover endpoint
  aws route53 change-resource-record-sets \
    --hosted-zone-id ${ZONE_ID} \
    --change-batch file://dr-dns-failover.json

□ Verify us-west-2 EKS cluster is alive
  aws eks describe-cluster --name aeos-dr --region us-west-2

### Step 3: Restore Data (15-30 min)
□ Promote us-west-2 RDS read replica to standalone primary
  aws rds promote-read-replica \
    --db-instance-identifier aeos-dr-replica \
    --region us-west-2

□ Update us-west-2 DATABASE_URL in Secrets Manager
□ Restart API pods to pick up new database endpoint

### Step 4: Validate (30-45 min)
□ Run smoke tests against us-west-2 endpoint
  bash infrastructure/ci-cd/scripts/smoke-test.sh production-dr
□ Verify queue messages intact (SQS)
□ Monitor error rates for 15 minutes

### Step 5: Communicate (ongoing)
□ Post status update to Status Page (Statuspage.io)
□ Notify customers via email
□ Internal Slack: #incidents

### Step 6: Recovery (when us-east-1 restored)
□ Re-sync data (pg_dump → us-east-1)
□ Switch Route 53 back to us-east-1
□ Validate, then deactivate us-west-2
□ Write incident post-mortem (within 48h)
```

---

## 11. DEPLOYMENT ROADMAP

### Phase 1 — Foundation (Weeks 1-2)
**Goal:** Development environment fully containerized

| Task | Owner | Duration |
|---|---|---|
| Write all Dockerfiles (API, worker, ML) | Platform | 2 days |
| Docker Compose local stack | Platform | 1 day |
| Makefile operational commands | Platform | 1 day |
| GitHub Actions: lint + test pipeline | Platform | 2 days |
| ECR repository setup | Cloud | 1 day |
| Development environment documentation | Platform | 1 day |

**Exit Criteria:** `make dev` starts full stack locally; CI passes on every PR

---

### Phase 2 — Cloud Foundation (Weeks 3-4)
**Goal:** AWS infrastructure provisioned with Terraform

| Task | Owner | Duration |
|---|---|---|
| Terraform VPC module | Cloud | 2 days |
| Terraform EKS module | Cloud | 2 days |
| Terraform RDS module | Cloud | 1 day |
| Terraform ElastiCache module | Cloud | 1 day |
| Terraform S3 + IAM modules | Cloud | 1 day |
| Secrets Manager setup | Security | 1 day |

**Exit Criteria:** `terraform apply` creates full AWS environment; EKS cluster healthy

---

### Phase 3 — Kubernetes Deployment (Weeks 5-6)
**Goal:** Application running on EKS with full K8s manifests

| Task | Owner | Duration |
|---|---|---|
| Write all K8s manifests (base/) | Platform | 3 days |
| Kustomize overlays (dev/staging/prod) | Platform | 2 days |
| Helm chart (aeos/) | Platform | 2 days |
| Ingress controller (ALB) | Cloud | 1 day |
| Install ArgoCD | Platform | 1 day |
| Connect ArgoCD to Git repo | Platform | 1 day |

**Exit Criteria:** `argocd app sync aeos-staging` deploys application; health checks pass

---

### Phase 4 — CI/CD Pipeline (Weeks 7-8)
**Goal:** Full automated pipeline from code push to production

| Task | Owner | Duration |
|---|---|---|
| GitHub Actions: CI workflow | Platform | 2 days |
| GitHub Actions: staging deploy | Platform | 1 day |
| GitHub Actions: production deploy | Platform | 2 days |
| Blue-green deployment scripts | Platform | 2 days |
| Smoke test suite | QA | 2 days |
| GitHub Environments (staging/prod gates) | Platform | 1 day |

**Exit Criteria:** PR merged to main → staging auto-deploy → production with approval

---

### Phase 5 — Observability (Weeks 9-10)
**Goal:** Full visibility into system health and performance

| Task | Owner | Duration |
|---|---|---|
| Prometheus stack deployment | Platform | 2 days |
| Grafana dashboards (4 dashboards) | Platform | 2 days |
| Jaeger/OpenTelemetry integration | Platform | 2 days |
| FluentBit → CloudWatch pipeline | Platform | 1 day |
| PagerDuty alert routing | Platform | 1 day |
| Alertmanager rules | Platform | 1 day |

**Exit Criteria:** Grafana shows all 6 dashboards; alert fires on test incident

---

### Phase 6 — Security Hardening (Weeks 11-12)
**Goal:** Pass security review; achieve SOC2-ready posture

| Task | Owner | Duration |
|---|---|---|
| OAuth2 + JWT authentication | Security | 3 days |
| RBAC implementation | Security | 2 days |
| Rate limiting (Redis sliding window) | Security | 1 day |
| Secrets rotation automation | Security | 1 day |
| Trivy scanning in CI/CD | Platform | 1 day |
| WAF rules (ALB) | Security | 1 day |
| Penetration test | External | 1 week |

**Exit Criteria:** External pentest passes; no CRITICAL findings

---

### Phase 7 — Scaling & Reliability (Weeks 13-14)
**Goal:** Handle 10x current load; DR tested

| Task | Owner | Duration |
|---|---|---|
| KEDA worker autoscaling | Platform | 2 days |
| Load testing (k6, 10k RPS) | QA | 2 days |
| Database connection pooling (PgBouncer) | Platform | 1 day |
| Read replica routing | Backend | 1 day |
| DR drill (full AZ failure simulation) | Platform | 1 day |
| Backup validation testing | Platform | 1 day |

**Exit Criteria:** Load test passes 10k RPS; DR drill completes in < RTO

---

### Phase 8 — Production Launch (Weeks 15-16)
**Goal:** Production traffic flowing; SLAs active

| Task | Owner | Duration |
|---|---|---|
| DNS cutover (Route 53) | Cloud | 1 day |
| CloudFront CDN setup | Cloud | 1 day |
| Status page (Statuspage.io) | Platform | 1 day |
| Runbook documentation | All | 2 days |
| On-call rotation setup | Platform | 1 day |
| SLA monitoring (99.95% uptime) | Platform | 1 day |
| Launch communication | Product | 1 day |

**Exit Criteria:** Production traffic on new platform; SLA dashboards live

---

### Summary: Technology Stack

```
Compute:         EKS (EC2 t3.xlarge + r6i.2xlarge + g5.2xlarge)
Container Reg:   Amazon ECR
API Gateway:     AWS ALB + Kong Ingress
Load Balancer:   AWS ALB (Layer 7)
Database:        RDS PostgreSQL 16 (Multi-AZ, 30-day backup)
Cache:           ElastiCache Redis 7 (cluster mode, 3 nodes)
Vector DB:       Qdrant (StatefulSet on EKS)
Queue:           Amazon SQS (standard + FIFO + DLQ)
Object Storage:  Amazon S3 (cross-region replication)
Secrets:         AWS Secrets Manager + External Secrets Operator
IaC:             Terraform (modules + environments)
Config Mgmt:     Kustomize + Helm
GitOps:          ArgoCD
CI/CD:           GitHub Actions
Tracing:         OpenTelemetry + Jaeger
Metrics:         Prometheus + Grafana
Logging:         FluentBit + CloudWatch Logs
Alerts:          Alertmanager + PagerDuty
Scanning:        Trivy (containers) + Bandit + Semgrep (code)
Auth:            OAuth2 + JWT (RS256) + RBAC
CDN:             CloudFront
DNS:             Route 53
Cert Mgmt:       AWS ACM (auto-renewal)
```

---

*End of AEOS Cloud Platform Blueprint v1.0*
*Architecture Review Ready — Senior Cloud Platform Engineering*
