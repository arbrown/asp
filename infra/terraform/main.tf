provider "google" {
  project = var.project_id
  region  = var.region
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# ── APIs ──────────────────────────────────────────────────────────────────────

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])

  service            = each.key
  disable_on_destroy = false
}

# ── Storage ───────────────────────────────────────────────────────────────────

resource "google_storage_bucket" "artifacts" {
  name     = "storybook-artifacts-${random_id.bucket_suffix.hex}"
  location = var.region

  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition { age = 180 }
    action    { type = "Delete" }
  }

  depends_on = [google_project_service.apis["storage.googleapis.com"]]
}

# Terraform state bucket — bootstrapped on first apply, then used as backend
resource "google_storage_bucket" "tfstate" {
  name     = "storybook-tfstate-${random_id.bucket_suffix.hex}"
  location = var.region

  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.apis["storage.googleapis.com"]]
}

# ── Artifact Registry ─────────────────────────────────────────────────────────

resource "google_artifact_registry_repository" "images" {
  repository_id = "storybook-images"
  location      = var.region
  format        = "DOCKER"
  description   = "Container images for Storybook Agent"

  depends_on = [google_project_service.apis["artifactregistry.googleapis.com"]]
}

# ── GKE Autopilot ─────────────────────────────────────────────────────────────

resource "google_container_cluster" "storybook" {
  name     = var.cluster_name
  location = var.region

  enable_autopilot    = true
  deletion_protection = false

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {}

  depends_on = [google_project_service.apis["container.googleapis.com"]]
}

# ── Service Accounts ──────────────────────────────────────────────────────────

resource "google_service_account" "backend" {
  account_id   = "storybook-backend"
  display_name = "Storybook Backend (API + ADK pipeline)"
}

resource "google_service_account" "cloudbuild" {
  account_id   = "storybook-cloudbuild"
  display_name = "Storybook Cloud Build"
}

# ── IAM: backend service account ──────────────────────────────────────────────

resource "google_project_iam_member" "backend_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

resource "google_project_iam_member" "backend_trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.backend.email}"
}

resource "google_storage_bucket_iam_member" "backend_artifacts_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.backend.email}"
}

# Workload Identity: allow the K8s SA in the default namespace to impersonate the GCP SA
resource "google_service_account_iam_member" "backend_workload_identity" {
  service_account_id = google_service_account.backend.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[default/storybook-backend]"

  depends_on = [google_container_cluster.storybook]
}

# ── IAM: Cloud Build ──────────────────────────────────────────────────────────

resource "google_project_iam_member" "cloudbuild_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.cloudbuild.email}"
}

resource "google_project_iam_member" "cloudbuild_gke" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.cloudbuild.email}"
}
