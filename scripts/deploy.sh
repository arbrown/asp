#!/usr/bin/env bash
# Build and push both container images, then apply K8s manifests.
# Usage: ./scripts/deploy.sh <PROJECT_ID> <REGION> <ARTIFACTS_BUCKET>

set -euo pipefail

PROJECT_ID="${1:?Usage: $0 <project_id> <region> <artifacts_bucket>}"
REGION="${2:-us-central1}"
ARTIFACTS_BUCKET="${3:?provide artifacts bucket name}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/storybook-images"
ROOT="$(git rev-parse --show-toplevel)"

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
