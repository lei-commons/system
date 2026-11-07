# Firestore (work queue, console state), Artifact Registry, BigQuery, Pub/Sub,
# Secret Manager and the budget.

resource "google_firestore_database" "default" {
  name            = "(default)"
  location_id     = var.region
  type            = "FIRESTORE_NATIVE"
  deletion_policy = "ABANDON"

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "lei-commons"
  format        = "DOCKER"
  description   = "Container images for lei-commons jobs and services"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_dataset" "dashboards" {
  dataset_id  = "lei_commons"
  location    = var.region
  description = "Aggregates for the public dashboards and the system view"

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "budget" {
  name       = "budget-alerts"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "control" {
  name       = "control-events"
  depends_on = [google_project_service.apis]
}

# Empty secrets for sources that need an owner-provided credential
# (decision: owner enters them in the console's Sources page; until then the
# source runs in web-discovery mode).
resource "google_secret_manager_secret" "source" {
  for_each  = toset(var.owner_key_sources)
  secret_id = "source-${each.value}"

  replication {
    auto {}
  }

  labels = {
    kind = "source-credential"
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "ingest_reads" {
  for_each  = google_secret_manager_secret.source
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["ingest"].member
}

resource "google_secret_manager_secret_iam_member" "console_adds" {
  for_each  = google_secret_manager_secret.source
  secret_id = each.value.id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = google_service_account.sa["console"].member
}

# The budget sends spend updates to Pub/Sub several times a day. The budget
# guard compares them with the owner's live cap in control/state.json and
# pauses the system at 100%. The amount here is only the initial value.
resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "lei-commons monthly cap"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.8
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  # Without this block the budget still emails billing admins at each threshold,
  # but the budget guard gets no spend updates and cannot pause the system.
  dynamic "all_updates_rule" {
    for_each = var.budget_pubsub_enabled ? [1] : []
    content {
      pubsub_topic                     = google_pubsub_topic.budget.id
      disable_default_iam_recipients   = false
      monitoring_notification_channels = [google_monitoring_notification_channel.owner.id]
    }
  }

  lifecycle {
    ignore_changes = [amount]
  }

  depends_on = [google_project_service.apis]
}
