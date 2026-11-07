output "workload_identity_provider" {
  description = "Set as the GitHub org variable GCP_WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "service_accounts" {
  value = { for k, sa in google_service_account.sa : k => sa.email }
}

output "buckets" {
  value = local.buckets
}

output "control_state_uri" {
  description = "Set as LC_CONTROL_STATE_URI on every Cloud Run job and service."
  value       = "gs://${google_storage_bucket.control.name}/state.json"
}

output "gateway_url" {
  description = "Address jobs use to reach the conduct gateway. Empty until gateway_image is set."
  value       = local.gateway_enabled ? google_cloud_run_v2_service.gateway[0].uri : ""
}

output "gateway_audience" {
  description = "Audience for the ID token jobs send to the gateway. Empty until gateway_image is set."
  value       = local.gateway_enabled ? google_cloud_run_v2_service.gateway[0].uri : ""
}

output "console_url" {
  description = "The owner console. Empty until console_enabled is true."
  value       = var.console_enabled ? google_cloud_run_v2_service.console[0].uri : ""
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
