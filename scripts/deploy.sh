#!/usr/bin/env bash
# Build and push both container images, then apply K8s manifests.
# Reads GCP_PROJECT_ID, GCP_REGION, GCS_ARTIFACTS_BUCKET from .env by default.
# Usage: ./scripts/deploy.sh [project_id] [region] [artifacts_bucket]

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"

# Load .env if present
if [[ -f "${ROOT}/.env" ]]; then
  set -o allexport
  source "${ROOT}/.env"
  set +o allexport
fi

# CLI args override .env values
PROJECT_ID="${1:-${GCP_PROJECT_ID:?GCP_PROJECT_ID not set in .env or args}}"
REGION="${2:-${GCP_REGION:-us-central1}}"
ARTIFACTS_BUCKET="${3:-${GCS_ARTIFACTS_BUCKET:?GCS_ARTIFACTS_BUCKET not set in .env or args}}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/storybook-images"

echo "==> Project:  ${PROJECT_ID}"
echo "==> Region:   ${REGION}"
echo "==> Bucket:   ${ARTIFACTS_BUCKET}"
echo "==> Registry: ${REGISTRY}"
echo ""

echo "==> Authenticating Docker with Artifact Registry..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "==> Building backend image..."
docker build -f "${ROOT}/Dockerfile.backend" -t "${REGISTRY}/backend:latest" "${ROOT}"
docker push "${REGISTRY}/backend:latest"

echo "==> Building frontend image..."
docker build -f "${ROOT}/Dockerfile.frontend" -t "${REGISTRY}/frontend:latest" "${ROOT}"
docker push "${REGISTRY}/frontend:latest"

echo "==> Patching K8s manifests..."
TMP=$(mktemp -d)
for f in "${ROOT}/k8s"/*.yaml; do
  sed \
    -e "s|PROJECT_ID|${PROJECT_ID}|g" \
    -e "s|REGION|${REGION}|g" \
    -e "s|ARTIFACTS_BUCKET_NAME|${ARTIFACTS_BUCKET}|g" \
    "$f" > "${TMP}/$(basename "$f")"
done

echo "==> Applying manifests..."
kubectl apply -f "${TMP}/"

echo "==> Waiting for rollout..."
kubectl rollout status deployment/storybook-backend
kubectl rollout status deployment/storybook-ui

INGRESS_IP=$(kubectl get ingress storybook-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "pending")
echo ""
echo "==> Done! Ingress IP: ${INGRESS_IP}"
