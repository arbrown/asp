#!/usr/bin/env bash
# One-shot Mac dev environment setup for the Storybook Agent project.
# Safe to re-run — all steps are idempotent.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "${ROOT}"

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "==> Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# ── Core tools ────────────────────────────────────────────────────────────────
echo "==> Installing core tools..."
brew install --quiet \
  google-cloud-sdk \
  node \
  python@3.12 \
  uv \
  gettext

# ── Docker buildx (eliminates the 'legacy builder' warning) ──────────────────
if ! docker buildx version &>/dev/null 2>&1; then
  echo "==> Installing docker buildx..."
  brew install --quiet docker-buildx
  mkdir -p ~/.docker/cli-plugins
  ln -sf "$(brew --prefix docker-buildx)/bin/docker-buildx" ~/.docker/cli-plugins/docker-buildx
fi

# Enable BuildKit as the default builder
if ! docker buildx inspect default-builder &>/dev/null 2>&1; then
  docker buildx create --name default-builder --use --bootstrap 2>/dev/null || true
fi

# ── gcloud components ─────────────────────────────────────────────────────────
echo "==> Installing gcloud components..."
gcloud components install kubectl gke-gcloud-auth-plugin --quiet 2>/dev/null || true

# ── Python virtual environment ────────────────────────────────────────────────
echo "==> Setting up Python venv..."
uv venv --python python3.12 .venv
uv pip install -e ".[dev]"

# ── Frontend dependencies + lockfile ─────────────────────────────────────────
echo "==> Installing frontend dependencies..."
cd frontend && npm install
cd "${ROOT}"

# Commit the lockfile if it was just generated
if git diff --name-only | grep -q "package-lock.json"; then
  git add frontend/package-lock.json
  git commit -m "chore: add frontend package-lock.json"
  echo "==> Committed frontend/package-lock.json"
fi

# ── DOCKER_DEFAULT_PLATFORM in .env ──────────────────────────────────────────
if ! grep -q "DOCKER_DEFAULT_PLATFORM" "${ROOT}/.env" 2>/dev/null; then
  echo "" >> "${ROOT}/.env"
  echo "# Tell Docker to always build for GKE (amd64), not your Mac (arm64)" >> "${ROOT}/.env"
  echo "DOCKER_DEFAULT_PLATFORM=linux/amd64" >> "${ROOT}/.env"
  echo "==> Added DOCKER_DEFAULT_PLATFORM=linux/amd64 to .env"
fi

# ── Shell hint ────────────────────────────────────────────────────────────────
echo ""
echo "==> Done! A few manual steps if you haven't done them yet:"
echo ""
echo "  1. Authenticate gcloud:"
echo "       gcloud auth login"
echo "       gcloud auth application-default login"
echo "       gcloud config set project \$(grep GCP_PROJECT_ID .env | cut -d= -f2)"
echo ""
echo "  2. Connect kubectl to your cluster:"
echo "       \$(grep -A1 gke_connect_cmd <<< \"\$(cd infra/terraform && terraform output)\" | tail -1)"
echo "       # or: gcloud container clusters get-credentials storybook-cluster --region us-central1"
echo ""
echo "  3. Activate the venv in your shell:"
echo "       source .venv/bin/activate"
echo ""
echo "  Run 'make' to see available dev commands."
