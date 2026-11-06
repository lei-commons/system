# The owner console. IAP is the first factor; the console checks a second one
# itself. Created when console_enabled is true, after the first image is pushed.

locals {
  console_image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/owner-console:latest"
}

resource "google_secret_manager_secret" "console_totp" {
  secret_id = "console-totp"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "console_totp" {
  for_each = toset(["roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionAdder"])

  secret_id = google_secret_manager_secret.console_totp.id
  role      = each.value
  member    = google_service_account.sa["console"].member
}

resource "google_cloud_run_v2_service" "console" {
  count = var.console_enabled ? 1 : 0

  name                = "owner-console"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.sa["console"].email

    # Sessions, replay protection and the lockout live in memory.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = local.console_image

      env {
        name  = "LC_CONSOLE_OWNERS"
        value = var.owner_email
      }
      env {
        name  = "LC_IAP_AUDIENCE"
        value = "/projects/${data.google_project.this.number}/locations/${var.region}/services/owner-console"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "LC_CONTROL_STATE_URI"
        value = "gs://${google_storage_bucket.control.name}/state.json"
      }
    }
  }

  lifecycle {
    # Later images are rolled out by the deploy workflow.
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [google_project_service.apis, google_secret_manager_secret_iam_member.console_totp]
}

resource "google_project_service_identity" "iap" {
  provider = google-beta
  count    = var.console_enabled ? 1 : 0

  service = "iap.googleapis.com"
}

resource "google_cloud_run_v2_service_iam_member" "console_iap" {
  count = var.console_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.console[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_project_service_identity.iap[0].member
}

resource "google_iap_web_cloud_run_service_iam_member" "owner" {
  count = var.console_enabled ? 1 : 0

  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.console[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = "user:${var.owner_email}"
}
