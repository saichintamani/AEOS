#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# AEOS Cluster Management — Phase 9B.6
# Usage:
#   ./scripts/cluster.sh start              # 3-node cluster
#   ./scripts/cluster.sh start --monitor    # 3-node + observability stack
#   ./scripts/cluster.sh stop
#   ./scripts/cluster.sh status
#   ./scripts/cluster.sh health             # hit /health on all 3 nodes
#   ./scripts/cluster.sh bench              # run benchmark against live cluster
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

COMPOSE_FILE="docker-compose.cluster.yml"
NODES=("http://localhost:8001" "http://localhost:8002" "http://localhost:8003")

cmd="${1:-status}"

start() {
    local extra_args=""
    if [[ "${2:-}" == "--monitor" ]]; then
        extra_args="--profile monitoring"
    fi
    echo "Starting AEOS 3-node cluster..."
    docker-compose -f "$COMPOSE_FILE" $extra_args up -d
    echo ""
    echo "Waiting for nodes to be healthy..."
    sleep 5
    health
}

stop() {
    echo "Stopping AEOS cluster..."
    docker-compose -f "$COMPOSE_FILE" --profile monitoring down
    echo "Done."
}

status() {
    echo "=== AEOS Cluster Status ==="
    docker-compose -f "$COMPOSE_FILE" ps
}

health() {
    echo "=== Node Health Checks ==="
    for node in "${NODES[@]}"; do
        result=$(curl -sf "${node}/health" 2>/dev/null || echo '{"status":"unreachable"}')
        echo "  ${node}: $(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status","?"))' 2>/dev/null || echo "error")"
    done
    echo ""
    echo "=== Invariant Status ==="
    for node in "${NODES[@]}"; do
        result=$(curl -sf "${node}/api/v1/validation/status" 2>/dev/null || echo '{"status":"unreachable"}')
        echo "  ${node}/validation: $(echo "$result" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status","?"))' 2>/dev/null || echo "error")"
    done
}

bench() {
    echo "Running benchmark against live cluster (node-1)..."
    python3 scripts/benchmark.py --mode http --host "http://localhost:8001" --scale "100,1000" --concurrency 20
}

case "$cmd" in
    start)   start "$@" ;;
    stop)    stop ;;
    status)  status ;;
    health)  health ;;
    bench)   bench ;;
    *)
        echo "Usage: $0 {start [--monitor]|stop|status|health|bench}"
        exit 1
        ;;
esac
