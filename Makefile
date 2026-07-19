## ═══════════════════════════════════════════════════════════════════════════
## AEOS — Developer Makefile
## Usage: make <target>
## ═══════════════════════════════════════════════════════════════════════════

.PHONY: help install dev-install start test test-unit test-integration lint \
        format typecheck build cluster-start cluster-stop cluster-health \
        benchmark validate docs clean \
        proto-lint proto-breaking proto-format proto-build proto-gen proto-check

PYTHON  ?= python3
PIP     ?= pip3
UVICORN  = $(PYTHON) -m uvicorn
PORT    ?= 8000

# ── Help ─────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "AEOS — AI Engineering Orchestration System"
	@echo ""
	@echo "  Development:"
	@echo "    make install          Install runtime dependencies"
	@echo "    make dev-install      Install all dev + optional dependencies"
	@echo "    make start            Start AEOS API server (port $(PORT))"
	@echo ""
	@echo "  Testing:"
	@echo "    make test             Run full test suite"
	@echo "    make test-unit        Run unit tests only"
	@echo "    make test-integration Run integration tests only"
	@echo ""
	@echo "  Quality:"
	@echo "    make lint             Run ruff linter"
	@echo "    make format           Auto-format with ruff"
	@echo "    make typecheck        Run mypy type checker"
	@echo ""
	@echo "  Cluster:"
	@echo "    make cluster-start    Start 3-node docker cluster"
	@echo "    make cluster-stop     Stop the docker cluster"
	@echo "    make cluster-health   Check all node health"
	@echo ""
	@echo "  Ops:"
	@echo "    make benchmark        Run performance benchmark (local mode)"
	@echo "    make validate         Run invariant engine evaluation"
	@echo "    make build            Build distributable package"
	@echo "    make clean            Remove build artefacts and caches"
	@echo ""

# ── Install ───────────────────────────────────────────────────────────────────
install:
	$(PIP) install -e .

dev-install:
	$(PIP) install -e ".[all]"

# ── Start ─────────────────────────────────────────────────────────────────────
start:
	$(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT) --reload

start-prod:
	$(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT) --workers 4

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	$(PYTHON) -m pytest tests/ -x -q

test-unit:
	$(PYTHON) -m pytest tests/unit/ -x -q

test-integration:
	$(PYTHON) -m pytest tests/integration/ -x -q

test-validation:
	$(PYTHON) -m pytest tests/unit/validation/ -v

# ── Quality ───────────────────────────────────────────────────────────────────
lint:
	$(PYTHON) -m ruff check app/ aeos/ tests/ --fix

format:
	$(PYTHON) -m ruff format app/ aeos/ tests/

typecheck:
	$(PYTHON) -m mypy app/ aeos/ --ignore-missing-imports

# ── Cluster ───────────────────────────────────────────────────────────────────
cluster-start:
	docker-compose -f docker-compose.cluster.yml up -d

cluster-start-monitor:
	docker-compose -f docker-compose.cluster.yml --profile monitoring up -d

cluster-stop:
	docker-compose -f docker-compose.cluster.yml --profile monitoring down

cluster-health:
	aeos cluster health

# ── Ops ───────────────────────────────────────────────────────────────────────
benchmark:
	$(PYTHON) scripts/benchmark.py --mode local --scale 100,1000

benchmark-http:
	$(PYTHON) scripts/benchmark.py --mode http --scale 100,1000 --host http://localhost:$(PORT)

validate:
	aeos validate

# ── Build & Release ───────────────────────────────────────────────────────────
build:
	$(PYTHON) -m build

release-check:
	$(PYTHON) -m twine check dist/*

# ── Docker (dev) ─────────────────────────────────────────────────────────────
docker-dev:
	docker-compose up -d

docker-dev-monitor:
	docker-compose --profile monitoring up -d

docker-down:
	docker-compose --profile monitoring --profile dev-tools down

# ── Proto Governance (buf) ────────────────────────────────────────────────────
#
# Prerequisites: brew install bufbuild/buf/buf  (or go install github.com/bufbuild/buf/cmd/buf@latest)
# Docs: https://buf.build/docs/

BUF ?= buf

# Lint all proto files against AEOS style rules (buf.yaml [lint])
proto-lint:
	$(BUF) lint

# Check for wire-incompatible changes against the main branch
proto-breaking:
	$(BUF) breaking --against '.git#branch=main'

# Format all proto files in-place
proto-format:
	$(BUF) format -w

# Check format without modifying (CI mode)
proto-format-check:
	$(BUF) format --diff --exit-code

# Build (compile) all proto files — validates syntax + imports
proto-build:
	$(BUF) build --error-format text

# Generate Python gRPC stubs from proto files
proto-gen:
	$(BUF) generate
	@echo ""
	@echo "Stubs generated in app/distributed/grpc/generated/"
	@echo "Do NOT commit generated files — they are rebuilt in CI."

# Run all proto checks (lint + format + build) — mirrors CI proto-governance
proto-check: proto-lint proto-format-check proto-build
	@echo ""
	@echo "All proto governance checks passed ✓"

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/ .ruff_cache/ .mypy_cache/
