#!/usr/bin/env bash
## validate.sh — Pre-deployment verification suite for AEOS infrastructure.
## Runs: Helm lint, Terraform validate, kubectl dry-run, security checks.
## Exit code 0 = all checks passed. Non-zero = at least one check failed.

set -euo pipefail

## ─── Colours ──────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0
ERRORS=()

pass()  { echo -e "${GREEN}✓${NC} $1"; ((PASS++)); }
fail()  { echo -e "${RED}✗${NC} $1"; ((FAIL++)); ERRORS+=("$1"); }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; ((WARN++)); }
header(){ echo -e "\n${CYAN}══════════════════════════════════════════${NC}"; echo -e "${CYAN}  $1${NC}"; echo -e "${CYAN}══════════════════════════════════════════${NC}"; }

## ─── Config ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELM_DIR="${REPO_ROOT}/infrastructure/helm/aeos"
TF_DIR="${REPO_ROOT}/infrastructure/terraform"
K8S_DIR="${REPO_ROOT}/infrastructure/kubernetes"
ENVIRONMENT="${1:-staging}"

echo -e "\n${CYAN}AEOS Infrastructure Validation Suite${NC}"
echo "Environment: ${ENVIRONMENT}"
echo "Repo root:   ${REPO_ROOT}"
echo "Started:     $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

## ─── Tool Availability ────────────────────────────────────────────────────
header "0. Tool availability"

for tool in helm terraform kubectl kubeval kube-score trivy; do
  if command -v "$tool" &>/dev/null; then
    pass "$tool: $(${tool} version --short 2>/dev/null || ${tool} --version 2>/dev/null | head -1)"
  else
    warn "$tool not found — skipping related checks"
  fi
done

## ─── Helm Checks ──────────────────────────────────────────────────────────
header "1. Helm chart validation"

if command -v helm &>/dev/null; then
  # 1a. Helm lint default values
  echo "  helm lint (default values)..."
  if helm lint "${HELM_DIR}" --quiet 2>&1; then
    pass "helm lint: default values"
  else
    fail "helm lint: default values"
  fi

  # 1b. Helm lint with environment values
  VALUES_FILE="${HELM_DIR}/values-${ENVIRONMENT}.yaml"
  if [[ -f "$VALUES_FILE" ]]; then
    echo "  helm lint (${ENVIRONMENT} values)..."
    if helm lint "${HELM_DIR}" -f "$VALUES_FILE" --quiet 2>&1; then
      pass "helm lint: ${ENVIRONMENT} values"
    else
      fail "helm lint: ${ENVIRONMENT} values"
    fi
  else
    warn "No values-${ENVIRONMENT}.yaml found — skipping environment-specific lint"
  fi

  # 1c. Helm template dry-run
  echo "  helm template (dry-run render)..."
  RENDERED=$(helm template aeos "${HELM_DIR}" \
    --namespace aeos-api \
    --set global.environment="${ENVIRONMENT}" \
    --set api.image.tag=validate-test \
    --set worker.image.tag=validate-test \
    2>&1) && {
    pass "helm template: renders without error"
    TEMPLATE_OUTPUT="$RENDERED"
  } || {
    fail "helm template: render failed"
    TEMPLATE_OUTPUT=""
  }

  # 1d. Check required templates are present
  for resource in "kind: Deployment" "kind: Service" "kind: HorizontalPodAutoscaler" "kind: NetworkPolicy"; do
    if echo "$TEMPLATE_OUTPUT" | grep -q "$resource"; then
      pass "Template includes: ${resource}"
    else
      fail "Template missing: ${resource}"
    fi
  done

  # 1e. Dependency check
  echo "  helm dependency list..."
  if helm dependency list "${HELM_DIR}" 2>&1 | grep -q "ok\|missing"; then
    pass "helm dependencies declared"
  else
    warn "Could not verify helm dependencies (run: helm dependency update)"
  fi
else
  warn "helm not installed — skipping all Helm checks"
fi

## ─── Terraform Checks ─────────────────────────────────────────────────────
header "2. Terraform validation"

if command -v terraform &>/dev/null; then
  for env_dir in "${TF_DIR}/environments"/*/; do
    env_name=$(basename "$env_dir")
    echo "  terraform validate: ${env_name}..."
    if (cd "$env_dir" && terraform init -backend=false -input=false -no-color -upgrade=false &>/dev/null && terraform validate -no-color 2>&1); then
      pass "terraform validate: ${env_name}"
    else
      fail "terraform validate: ${env_name}"
    fi
  done

  # Validate all modules independently
  for mod_dir in "${TF_DIR}/modules"/*/; do
    mod_name=$(basename "$mod_dir")
    echo "  terraform validate module: ${mod_name}..."
    if (cd "$mod_dir" && terraform init -backend=false -input=false -no-color &>/dev/null && terraform validate -no-color 2>&1); then
      pass "terraform validate module: ${mod_name}"
    else
      fail "terraform validate module: ${mod_name}"
    fi
  done

  # Terraform fmt check
  echo "  terraform fmt check..."
  if terraform fmt -check -recursive "${TF_DIR}" &>/dev/null; then
    pass "terraform fmt: all files formatted"
  else
    warn "terraform fmt: some files not formatted (run: terraform fmt -recursive ./infrastructure/terraform)"
  fi
else
  warn "terraform not installed — skipping all Terraform checks"
fi

## ─── Kubernetes Manifest Checks ───────────────────────────────────────────
header "3. Kubernetes manifest validation"

if command -v kubectl &>/dev/null; then
  # Check if a cluster is reachable for dry-run
  CLUSTER_AVAILABLE=false
  if kubectl cluster-info &>/dev/null 2>&1; then
    CLUSTER_AVAILABLE=true
    echo "  Cluster reachable — running server-side dry-run"
  else
    echo "  No cluster reachable — running client-side validation only"
  fi

  for manifest in "${K8S_DIR}/base/"*.yaml; do
    manifest_name=$(basename "$manifest")
    echo "  kubectl validate: ${manifest_name}..."
    if $CLUSTER_AVAILABLE; then
      if kubectl apply --dry-run=server -f "$manifest" &>/dev/null 2>&1; then
        pass "kubectl dry-run (server): ${manifest_name}"
      else
        fail "kubectl dry-run (server): ${manifest_name}"
      fi
    else
      if kubectl apply --dry-run=client -f "$manifest" &>/dev/null 2>&1; then
        pass "kubectl dry-run (client): ${manifest_name}"
      else
        fail "kubectl dry-run (client): ${manifest_name}"
      fi
    fi
  done

  # Security manifests
  for manifest in "${K8S_DIR}/security/"*.yaml "${K8S_DIR}/istio/"*.yaml; do
    [[ -f "$manifest" ]] || continue
    manifest_name=$(basename "$manifest")
    if kubectl apply --dry-run=client -f "$manifest" &>/dev/null 2>&1; then
      pass "kubectl dry-run (client): ${manifest_name}"
    else
      # CRDs for ESO/Istio/Gatekeeper may not be installed in CI — warn not fail
      warn "kubectl dry-run: ${manifest_name} (may require CRDs)"
    fi
  done
else
  warn "kubectl not installed — skipping Kubernetes checks"
fi

## ─── Security Scanning ────────────────────────────────────────────────────
header "4. Security validation"

# 4a. Check for hardcoded secrets in YAML
echo "  Scanning for hardcoded secrets..."
SECRET_PATTERNS='password\s*[:=]\s*["\x27][^"\x27${}][^"\x27]*["\x27]|secret\s*[:=]\s*["\x27][^"\x27${}][^"\x27]*["\x27]|api[_-]?key\s*[:=]\s*["\x27][^"\x27${}][^"\x27]*["\x27]'
if grep -rE "$SECRET_PATTERNS" "${K8S_DIR}" "${HELM_DIR}" \
    --include="*.yaml" --include="*.yml" \
    --exclude-dir=".git" 2>/dev/null | grep -v "example\|placeholder\|REPLACE\|YOUR_" | grep -q .; then
  fail "Potential hardcoded secrets found in manifests"
else
  pass "No hardcoded secrets detected"
fi

# 4b. Check all containers have securityContext.runAsNonRoot
echo "  Checking securityContext in Helm template output..."
if [[ -n "${TEMPLATE_OUTPUT:-}" ]]; then
  if echo "$TEMPLATE_OUTPUT" | grep -A5 "containers:" | grep -q "runAsNonRoot: true"; then
    pass "securityContext: runAsNonRoot set in templates"
  else
    warn "securityContext: runAsNonRoot not detected in all containers"
  fi

  if echo "$TEMPLATE_OUTPUT" | grep -q "readOnlyRootFilesystem: true"; then
    pass "securityContext: readOnlyRootFilesystem set"
  else
    warn "securityContext: readOnlyRootFilesystem not in all containers"
  fi

  if echo "$TEMPLATE_OUTPUT" | grep -q "allowPrivilegeEscalation: false"; then
    pass "securityContext: allowPrivilegeEscalation: false set"
  else
    fail "securityContext: allowPrivilegeEscalation not disabled"
  fi
fi

# 4c. Trivy config scan
if command -v trivy &>/dev/null; then
  echo "  trivy config scan: Helm chart..."
  if trivy config "${HELM_DIR}" --exit-code 1 --severity HIGH,CRITICAL --quiet 2>&1; then
    pass "trivy: no HIGH/CRITICAL misconfigurations in Helm chart"
  else
    fail "trivy: HIGH/CRITICAL misconfigurations found"
  fi

  echo "  trivy config scan: K8s manifests..."
  if trivy config "${K8S_DIR}" --exit-code 1 --severity HIGH,CRITICAL --quiet 2>&1; then
    pass "trivy: no HIGH/CRITICAL misconfigurations in K8s manifests"
  else
    fail "trivy: HIGH/CRITICAL misconfigurations in K8s manifests"
  fi
else
  warn "trivy not installed — skipping config security scan"
fi

# 4d. Check NetworkPolicy default-deny exists
echo "  Checking for default-deny NetworkPolicies..."
if grep -r "podSelector: {}" "${K8S_DIR}" --include="*.yaml" | grep -q "Ingress\|Egress"; then
  pass "Default-deny NetworkPolicy present"
else
  fail "Default-deny NetworkPolicy not found — Zero Trust not enforced"
fi

## ─── Application Checks ───────────────────────────────────────────────────
header "5. Application validation"

# 5a. Python syntax check
echo "  Python syntax check..."
if command -v python3 &>/dev/null; then
  PY_ERRORS=0
  while IFS= read -r -d '' pyfile; do
    if ! python3 -m py_compile "$pyfile" 2>/dev/null; then
      fail "Python syntax error: $pyfile"
      ((PY_ERRORS++))
    fi
  done < <(find "${REPO_ROOT}/app" "${REPO_ROOT}/aeos" -name "*.py" -print0 2>/dev/null)
  if [[ $PY_ERRORS -eq 0 ]]; then
    pass "Python syntax: all files valid"
  fi
else
  warn "python3 not available — skipping syntax check"
fi

# 5b. Check required files exist
echo "  Checking required project files..."
REQUIRED_FILES=(
  "app/main.py"
  "requirements.txt"
  "pyproject.toml"
  "Makefile"
  "CHANGELOG.md"
  "infrastructure/helm/aeos/Chart.yaml"
  "infrastructure/helm/aeos/values.yaml"
)
for f in "${REQUIRED_FILES[@]}"; do
  if [[ -f "${REPO_ROOT}/${f}" ]]; then
    pass "Required file: ${f}"
  else
    fail "Missing required file: ${f}"
  fi
done

## ─── Summary ──────────────────────────────────────────────────────────────
header "Summary"
echo ""
echo -e "  ${GREEN}Passed:${NC}   ${PASS}"
echo -e "  ${YELLOW}Warnings:${NC} ${WARN}"
echo -e "  ${RED}Failed:${NC}   ${FAIL}"
echo ""

if [[ ${FAIL} -gt 0 ]]; then
  echo -e "${RED}VALIDATION FAILED${NC} — ${FAIL} check(s) must be resolved before deployment.\n"
  for err in "${ERRORS[@]}"; do
    echo -e "  ${RED}✗${NC} $err"
  done
  echo ""
  exit 1
else
  echo -e "${GREEN}ALL CHECKS PASSED${NC} — Safe to deploy to ${ENVIRONMENT}."
  echo ""
  exit 0
fi
