# DeDi publication (docs/dedi.md): the signing key's secret, the watchdog job that checks (and repairs)
# the published set, and its daily schedule. The secret is created empty: the first publication adds the
# key, so no person ever sees it, and only the publishing identity may read it or add a version.

locals {
  dedi_config     = yamldecode(file("${path.module}/../../config/dedi.yaml"))
  dedi_secret_id  = element(split("/", local.dedi_config.key.ref), length(split("/", local.dedi_config.key.ref)) - 1)
  dedi_check_at   = split(":", local.cycle_config.watchdogs.dedi_check_utc)
  dedi_check_cron = "${tonumber(local.dedi_check_at[1])} ${tonumber(local.dedi_check_at[0])} * * *"
}

resource "google_secret_manager_secret" "dedi_signing_key" {
  secret_id = local.dedi_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "dedi_signing_key" {
  for_each = toset(["roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionAdder"])

  secret_id = google_secret_manager_secret.dedi_signing_key.id
  role      = each.value
  member    = google_service_account.sa["publisher"].member
}

# Verifies the published set as a DeDi server would and, if it fails or would go stale, publishes it
# again (--repair). The cycle runs it after the load (a failure there doesn't fail the cycle), and the
# schedule below runs it again in the afternoon, so the files stay fresh on a day the cycle failed. A
# check that still fails exits non-zero, and the job alert emails the owner.
resource "google_cloud_run_v2_job" "dedi_check" {
  count = var.cycle_enabled ? 1 : 0

  name                = "dedi-check"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.sa["publisher"].email
      max_retries     = 0
      timeout         = "900s"

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.jobs.id
          subnetwork = google_compute_subnetwork.jobs.id
        }
      }

      containers {
        image   = "${local.image_base}/build-snapshot:latest"
        command = ["python"]
        args    = ["-m", "lc_publish.dedi_job", "check", "--repair"]
        resources {
          limits = { cpu = "1", memory = "1Gi" }
        }
        env {
          name  = "LC_DATA_BUCKET"
          value = google_storage_bucket.data.name
        }
        env {
          name  = "LC_SNAPSHOT_BUCKET"
          value = google_storage_bucket.snapshots.name
        }
        env {
          name  = "LC_CONTROL_STATE_URI"
          value = "gs://${google_storage_bucket.control.name}/state.json"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "dedi_check" {
  count = var.cycle_enabled ? 1 : 0

  name      = "dedi-check"
  region    = var.region
  schedule  = local.dedi_check_cron # config/cycle.yaml watchdogs.dedi_check_utc
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.dedi_check[0].name}:run"

    oauth_token {
      service_account_email = google_service_account.sa["workflow"].email
    }
  }
}
