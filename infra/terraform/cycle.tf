# The daily cycle (design: one Cloud Workflows run a day). Each source is independent (owner decision
# 2026-11-18): a source whose job fails is logged and retried later in the run, and never stops the
# others; the snapshot and the database load then run (these two still stop the cycle if they fail),
# and the cycle ends as failed if any source never succeeded; the day's final check-in reports it to the owner. Every job stops at
# once if the system isn't running, so a paused system makes the cycle a quick no-op.
#
# The definition is rendered by scripts/render_cycle.py from config/sources.yaml and config/cycle.yaml
# into infra/workflows/daily-cycle.yaml; this file and deploy-cycle.yml both deploy that same file.

locals {
  sources_registry = yamldecode(file("${path.module}/../../config/sources.yaml"))
  cycle_config     = yamldecode(file("${path.module}/../../config/cycle.yaml"))
  ingest_steps     = [for s in local.sources_registry.sources : s.pipeline.job if try(s.pipeline.job, null) != null]
  cycle_steps      = concat(local.ingest_steps, ["build-snapshot", "load-db"])
  cycle_time       = split(":", local.cycle_config.schedule_utc)
  cycle_cron       = "${tonumber(local.cycle_time[1])} ${tonumber(local.cycle_time[0])} * * *"
}

resource "google_workflows_workflow" "daily_cycle" {
  count = var.cycle_enabled ? 1 : 0

  name            = "daily-cycle"
  region          = var.region
  service_account = google_service_account.sa["workflow"].email
  description     = "Ingest every source (each independent, failures retried), then snapshot, then load the serving database."

  source_contents = file("${path.module}/../workflows/daily-cycle.yaml")

  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "daily_cycle" {
  count = var.cycle_enabled ? 1 : 0

  name      = "daily-cycle"
  region    = var.region
  schedule  = local.cycle_cron # config/cycle.yaml schedule_utc
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

# The cycle's end is no longer emailed on its own (it was the "daily cycle failed" alert, removed 2026-11-28): the
# final check-in's one summary a day reports it, after any recovery run (owner: "absolutely the FINAL summary rather
# than any intermediate status"; checkin.tf).

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
      timeout         = "${lookup(local.cycle_config.timeouts_minutes, "build-snapshot", local.cycle_config.timeouts_minutes["default"]) * 60}s" # config/cycle.yaml

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
          limits = { cpu = "4", memory = "16Gi" } # local disk is memory on Cloud Run: the build and the load keep the snapshot files there (42M records, 2026-11-18)
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
      timeout         = "${lookup(local.cycle_config.timeouts_minutes, "load-db", local.cycle_config.timeouts_minutes["default"]) * 60}s" # config/cycle.yaml

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
          limits = { cpu = "4", memory = "16Gi" } # local disk is memory on Cloud Run: the build and the load keep the snapshot files there (42M records, 2026-11-18)
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

# The site starts these jobs when the owner asks on the admin page ("Run now", confirmed with a fresh
# second-factor code). Granted on each job, not project-wide.
resource "google_cloud_run_v2_job_iam_member" "site_runs" {
  for_each = var.cycle_enabled && var.site_enabled ? toset(local.cycle_steps) : toset([])

  location = var.region
  name     = each.value
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.sa["site"].email}"
}

# The admin page's tools run two jobs with their command overridden for one run (lc_core.owner, owner decision
# 2026-11-21): a probe through ingest-gleif and the load test through load-db. Only on those two jobs.
resource "google_cloud_run_v2_job_iam_member" "site_runs_with_overrides" {
  for_each = var.cycle_enabled && var.site_enabled ? toset(["ingest-gleif", "load-db"]) : toset([])

  location = var.region
  name     = each.value
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.sa["site"].email}"
}
