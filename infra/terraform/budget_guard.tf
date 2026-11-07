# Turns budget notifications into a pause. Created when budget_guard_enabled is
# true, after the first image is pushed. It reaches the guard through a push
# subscription, so nothing outside the project can call it.

resource "google_cloud_run_v2_service" "budget_guard" {
  count = var.budget_guard_enabled ? 1 : 0

  name                = "budget-guard"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account = google_service_account.sa["budget-guard"].email

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/budget-guard:latest"

      env {
        name  = "LC_CONTROL_BUCKET"
        value = google_storage_bucket.control.name
      }
      env {
        name  = "LC_LOG_BUCKET"
        value = google_storage_bucket.decision_log.name
      }
    }
  }

  lifecycle {
    # Later images are rolled out by the deploy workflow.
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "budget_guard_push" {
  count = var.budget_guard_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.budget_guard[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.sa["budget-guard"].member
}

resource "google_pubsub_subscription" "budget_guard" {
  count = var.budget_guard_enabled ? 1 : 0

  name                 = "budget-guard"
  topic                = google_pubsub_topic.budget.id
  ack_deadline_seconds = 60

  push_config {
    push_endpoint = google_cloud_run_v2_service.budget_guard[0].uri

    oidc_token {
      service_account_email = google_service_account.sa["budget-guard"].email
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}
