# The daily cycle (design: one Cloud Workflows run a day). Each step starts only
# after the previous one succeeded, so the snapshot never reads half-updated
# data and the database never loads a half-built snapshot. A failed step stops
# the cycle and alerts the owner. Every job stops at once if the system isn't
# running, so a paused system makes the cycle a quick no-op.

locals {
  cycle_steps = ["ingest-gleif", "ingest-gb", "build-snapshot", "load-db"]
}

resource "google_workflows_workflow" "daily_cycle" {
  count = var.cycle_enabled ? 1 : 0

  name            = "daily-cycle"
  region          = var.region
  service_account = google_service_account.sa["workflow"].email
  description     = "Ingest, then snapshot, then load the serving database, in order."

  source_contents = yamlencode({
    main = {
      steps = [for job in local.cycle_steps : {
        (replace(job, "-", "_")) = {
          call = "googleapis.run.v2.projects.locations.jobs.run"
          args = {
            name             = "projects/${var.project_id}/locations/${var.region}/jobs/${job}"
            connector_params = { timeout = 7200 }
          }
        }
      }]
    }
  })

  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "daily_cycle" {
  count = var.cycle_enabled ? 1 : 0

  name      = "daily-cycle"
  region    = var.region
  schedule  = "0 2 * * *"
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.daily_cycle[0].id}/executions"
    body        = base64encode("{}")

    oauth_token {
      service_account_email = google_service_account.sa["workflow"].email
    }
  }
}

resource "google_monitoring_alert_policy" "cycle_failed" {
  display_name          = "lei-commons: the daily cycle failed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.owner.id]

  conditions {
    display_name = "Daily cycle execution failed"
    condition_threshold {
      filter          = "resource.type = \"workflows.googleapis.com/Workflow\" AND metric.type = \"workflows.googleapis.com/finished_execution_count\" AND metric.labels.status = \"FAILED\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  documentation {
    content   = "The daily cycle stopped at a failed step, so later steps didn't run and the site still serves yesterday's data. Open Workflows → daily-cycle to see which step."
    mime_type = "text/markdown"
  }
}

# The two jobs the cycle adds. Their images are pushed by the deploy workflows;
# the jobs are created once cycle_enabled is true (bootstrap.sh --cycle).
locals {
  image_base = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

resource "google_cloud_run_v2_job" "build_snapshot" {
  count = var.cycle_enabled ? 1 : 0

  name                = "build-snapshot"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.sa["publisher"].email
      max_retries     = 0
      timeout         = "3600s"

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.jobs.id
          subnetwork = google_compute_subnetwork.jobs.id
        }
      }

      containers {
        image = "${local.image_base}/build-snapshot:latest"
        resources {
          limits = { cpu = "2", memory = "8Gi" }
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

resource "google_cloud_run_v2_job" "load_db" {
  count = var.cycle_enabled ? 1 : 0

  name                = "load-db"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.sa["loader"].email
      max_retries     = 0
      timeout         = "7200s" # a 30M-record load takes about 40 minutes (load test, 2026-11-08)

      # All traffic through the sealed jobs network, like every job: Google APIs by
      # private DNS, the database by its own firewall rule, nothing else at all.
      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.jobs.id
          subnetwork = google_compute_subnetwork.jobs.id
        }
      }

      containers {
        image = "${local.image_base}/load-db:latest"
        resources {
          limits = { cpu = "2", memory = "4Gi" }
        }
        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "LC_SNAPSHOT_BUCKET"
          value = google_storage_bucket.snapshots.name
        }
        env {
          name  = "LC_DB_HOST"
          value = google_compute_address.database.address
        }
        env {
          name  = "LC_SITE_DB_USER"
          value = google_sql_user.site.name
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

  depends_on = [google_project_service.apis, google_secret_manager_secret_iam_member.db_admin_loader]
}
