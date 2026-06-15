#!/usr/bin/env bash
# Run once to provision GCP infrastructure and migrate Terraform state to GCS.
# Usage: ./scripts/bootstrap-terraform.sh <PROJECT_ID>

set -euo pipefail

PROJECT_ID="${1:?Usage: $0 <project_id>}"

cd "$(git rev-parse --show-toplevel)/infra/terraform"

echo "==> Initializing Terraform with local state..."
terraform init

echo "==> Applying (state bucket + artifacts bucket first)..."
terraform apply \
  -target=random_id.bucket_suffix \
  -target=google_project_service.apis \
  -target=google_storage_bucket.tfstate \
  -target=google_storage_bucket.artifacts \
  -var="project_id=${PROJECT_ID}" \
  -auto-approve

TFSTATE_BUCKET=$(terraform output -raw tfstate_bucket)
echo ""
echo "==> State bucket created: ${TFSTATE_BUCKET}"

echo "==> Writing backend.tf..."
cat > backend.tf <<EOF
terraform {
  backend "gcs" {
    bucket = "${TFSTATE_BUCKET}"
    prefix = "storybook/state"
  }
}
EOF

echo "==> Migrating local state to GCS..."
terraform init -migrate-state -force-copy

echo "==> Applying remaining resources..."
terraform apply -var="project_id=${PROJECT_ID}" -auto-approve

echo ""
echo "==> Done. Run the following to configure kubectl:"
terraform output -raw gke_connect_cmd
echo ""
