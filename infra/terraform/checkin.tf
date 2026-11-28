# The daily check-in (services/checkin; owner, 2026-11-28: "Check-in, any self-recovery, etc all should run on the
# cloud itself", and one email, "absolutely the FINAL summary rather than any intermediate status", sent "when entire
# daily cycle is fully completed", and never "in the middle of the regular cloud jobs"). One job, three stages:
# - cycle: the cycle's step after all its work; decides on recovery and may start one recovery run. No email.
# - final: the cycle's last step, 2 hours later (config/cycle.yaml checkin.final_after_minutes): one `checkin-report`
#   log entry for the day, which the alert below emails to the owner (the existing owner channel: no new address and
#   no outside mail service). A run that started a recovery run leaves the report to that run.
# - watchdog: the schedule below; a day with no cycle gets one, and a day whose summary never went out gets it late.

locals {
  checkin_at   = split(":", local.cycle_config.checkin.watchdog_utc)
  checkin_cron = "${tonumber(local.checkin_at[1])} ${tonumber(local.checkin_at[0])} * * *"
  # The emailed lines, in order: each is a field of the report entry (services/checkin/checkin/report.py summary).
  checkin_lines = ["cycle", "issues", "recovered", "actions", "needs", "sources", "trials", "github", "other"]
}

resource "google_cloud_run_v2_job" "checkin" {
  count = var.cycle_enabled ? 1 : 0

  name                = "checkin"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.sa["checkin"].email
      max_retries     = 0
      timeout         = "${lookup(local.cycle_config.timeouts_minutes, "checkin", local.cycle_config.timeouts_minutes["default"]) * 60}s" # config/cycle.yaml

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.jobs.id
          subnetwork = google_compute_subnetwork.jobs.id
        }
      }

      containers {
        image = "${local.image_base}/checkin:latest"
        resources {
          limits = { cpu = "1", memory = "512Mi" }
        }
        env {
          name  = "LC_PROJECT"
          value = var.project_id
        }
        env {
          name  = "LC_REGION"
          value = var.region
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

resource "google_cloud_scheduler_job" "checkin_watchdog" {
  count = var.cycle_enabled ? 1 : 0

  name      = "checkin-watchdog"
  region    = var.region
  schedule  = local.checkin_cron # config/cycle.yaml checkin.watchdog_utc
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.checkin[0].name}:run"
    body        = base64encode(jsonencode({ overrides = { containerOverrides = [{ args = ["--stage", "watchdog"] }] } }))
    headers     = { "Content-Type" = "application/json" }

    oauth_token {
      service_account_email = google_service_account.sa["workflow"].email
    }
  }
}


# The owner's email: one a day, the final check-in's report. A log-based alert carries the report's lines as labels into the
# email's subject and body; the full report stays in the logs (the session reads it with checkin-report.yml).
resource "google_monitoring_alert_policy" "checkin_report" {
  display_name          = "lei-commons daily check-in"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.owner.id]

  conditions {
    display_name = "The daily check-in's report"
    condition_matched_log {
      filter           = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"checkin\" AND jsonPayload.event=\"checkin-report\""
      label_extractors = merge({ subject = "EXTRACT(jsonPayload.subject)" }, { for k in local.checkin_lines : k => "EXTRACT(jsonPayload.${k})" })
    }
  }

  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    subject   = "$${log.extracted_label.subject}"
    mime_type = "text/markdown"
    content   = <<-EOT
      **Cycle:** $${log.extracted_label.cycle}

      **Issues:** $${log.extracted_label.issues}

      **Recovered on its own:** $${log.extracted_label.recovered}

      **What the check-in did:** $${log.extracted_label.actions}

      **Needs you:** $${log.extracted_label.needs}

      **Sources:** $${log.extracted_label.sources}

      **Trials:** $${log.extracted_label.trials}

      **GitHub workflows:** $${log.extracted_label.github}

      **Other:** $${log.extracted_label.other}

      Times are IST. The full report is in the logs (checkin-report); the private site's System page has the details.
    EOT
  }
}
