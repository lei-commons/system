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

# state.json belongs to the owner, so Terraform must never manage it. While it
# did, a rerun replaced the file as soon as it differed, undoing owner commands.
# bootstrap.sh creates it once, if it is missing. This forgets it without deleting it.
removed {
  from = google_storage_bucket_object.control_state

  lifecycle {
    destroy = false
  }
}

# Full-copy ledger baselines too big for a git repository (Brazil, New York,
# Singapore). Written once each; the git ledger pins every file's hash.
# Versioned, so nothing is lost if an object is ever overwritten.
resource "google_storage_bucket" "ledger_baselines" {
  name                        = "${local.bucket_prefix}-ledger-baselines"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.apis]
}
