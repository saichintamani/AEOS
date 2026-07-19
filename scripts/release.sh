#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# AEOS Release Script — Phase 10
#
# Usage:
#   ./scripts/release.sh 0.1.0            # tag and build v0.1.0
#   ./scripts/release.sh 0.1.0 --publish  # also push to PyPI
#
# What it does:
#   1. Validates the working tree is clean
#   2. Updates version in aeos/__init__.py and pyproject.toml
#   3. Runs the full test suite
#   4. Builds the distribution package
#   5. Creates a git tag
#   6. Optionally publishes to PyPI (requires TWINE_USERNAME/TWINE_PASSWORD)
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

VERSION="${1:-}"
PUBLISH="${2:-}"

if [[ -z "$VERSION" ]]; then
    echo "Usage: $0 <version> [--publish]"
    echo "  Example: $0 0.1.0"
    exit 1
fi

echo "=== AEOS Release v${VERSION} ==="

# 1. Clean working tree check
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "ERROR: Working tree is not clean. Commit or stash your changes first."
    git status --short
    exit 1
fi

# 2. Update version strings
echo "Updating version to ${VERSION}..."
sed -i "s/__version__ = \".*\"/__version__ = \"${VERSION}\"/" aeos/__init__.py
sed -i "s/^version = \".*\"/version = \"${VERSION}\"/" pyproject.toml

# 3. Run tests
echo "Running test suite..."
python -m pytest tests/ -x -q --tb=short
echo "All tests passed."

# 4. Build
echo "Building distribution..."
rm -rf dist/ build/
python -m build
echo "Build complete:"
ls dist/

# 5. Git tag
git add aeos/__init__.py pyproject.toml
git commit -m "chore: bump version to v${VERSION}"
git tag -a "v${VERSION}" -m "Release v${VERSION}"
echo "Tagged: v${VERSION}"

# 6. Publish (optional)
if [[ "$PUBLISH" == "--publish" ]]; then
    echo "Publishing to PyPI..."
    python -m twine upload dist/*
    echo "Published v${VERSION} to PyPI."
fi

echo ""
echo "=== Release v${VERSION} complete ==="
echo "Push with: git push origin main --tags"
