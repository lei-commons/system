# Alerts to the owner, by email (owner decision 2026-11-07). This goes only to
# the owner, so it is consistent with the rule that agents never contact anyone.

locals {
  alert_email = var.alert_email != "" ? var.alert_email : var.owner_email
}

resource "google_monitoring_notification_channel" "owner" {
  display_name = "lei-commons owner"
  type         = "email"

  labels = {
    email_address = local.alert_email
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "job_failed" {
  display_name          = "lei-commons: a job failed"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.owner.id]

  conditions {
    display_name = "Cloud Run job execution failed"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
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
    content   = "A lei-commons job failed. Open the private site's System page to see which one, and its logs in Cloud Run."
    mime_type = "text/markdown"
  }
}

# Every request the conduct gateway refuses, except while the system is paused.
resource "google_logging_metric" "gateway_denials" {
  name   = "lc_gateway_denials"
  filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"conduct-gateway\" AND jsonPayload.allowed=false AND NOT jsonPayload.rule=\"system-not-running\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "gateway_denied" {
  display_name          = "lei-commons: the conduct gateway refused a request"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.owner.id]

  conditions {
    display_name = "Conduct gateway denial"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.gateway_denials.name}\""
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
    content   = "Something asked the conduct gateway for a request the conduct rules don't allow (constitution Article 2). Check the gateway's logs for the rule and the actor."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "site_errors" {
  display_name          = "lei-commons: the private site is returning errors"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.owner.id]

  conditions {
    display_name = "Site 5xx responses"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"lei-commons-site\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "0s"

      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  documentation {
    content   = "The private site answered more than five requests with server errors in ten minutes. Check its logs in Cloud Run."
    mime_type = "text/markdown"
  }
}
