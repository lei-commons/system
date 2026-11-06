locals {
  bucket_prefix = var.project_id
}

# Working data: raw source files, Parquet, proposals, verdicts. Private.
resource "google_storage_bucket" "data" {
  name                        = "${local.bucket_prefix}-data"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      age            = 30
      matches_prefix = ["raw/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age            = 7
      matches_prefix = ["tmp/"]
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# Signed snapshots before they are copied to GitHub Releases. Versioned.
resource "google_storage_bucket" "snapshots" {
  name                        = "${local.bucket_prefix}-snapshots"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.apis]
}

# Daily decision-log JSONL files. Versioned with a retention period so no
# agent can quietly rewrite history.
resource "google_storage_bucket" "decision_log" {
  name                        = "${local.bucket_prefix}-decision-log"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = 2592000 # 30 days; lock after the first month of operation
    is_locked        = false
  }

  depends_on = [google_project_service.apis]
}

# Recorded frames for the system view (Replay and Evolution modes).
resource "google_storage_bucket" "frames" {
  name                        = "${local.bucket_prefix}-frames"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  depends_on = [google_project_service.apis]
}

# Owner control state (constitution Article 1). Only the owner and the console
# service write here; every agent can read it.
resource "google_storage_bucket" "control" {
  name                        = "${local.bucket_prefix}-control"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.apis]
}

# The system starts paused. The owner switches it to running from the console
# (or by editing this object) after the day-zero checks pass.
resource "google_storage_bucket_object" "control_state" {
  bucket       = google_storage_bucket.control.name
  name         = "state.json"
  content_type = "application/json"
  content = jsonencode({
    state              = "paused"
    paused_components  = []
    monthly_budget_usd = var.monthly_budget_usd
    updated_by         = "bootstrap"
  })

  lifecycle {
    # After day zero the owner owns this object. Terraform never overwrites it.
    ignore_changes = [content]
  }
}
