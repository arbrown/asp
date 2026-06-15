output "artifacts_bucket" {
  description = "GCS bucket for session artifacts"
  value       = google_storage_bucket.artifacts.name
}

output "tfstate_bucket" {
  description = "GCS bucket for Terraform state (use in backend.tf after first apply)"
  value       = google_storage_bucket.tfstate.name
}

output "artifact_registry_url" {
  description = "Docker registry URL for pushing images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/storybook-images"
}

output "cluster_name" {
  description = "GKE cluster name"
  value       = google_container_cluster.storybook.name
}

output "backend_service_account" {
  description = "Service account email for the backend pod"
  value       = google_service_account.backend.email
}

output "gke_connect_cmd" {
  description = "Command to configure kubectl for this cluster"
  value       = "gcloud container clusters get-credentials ${google_container_cluster.storybook.name} --region ${var.region} --project ${var.project_id}"
}
