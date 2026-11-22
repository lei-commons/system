locals {
  bucket_prefix = var.project_id
  # The publishing settings (config/publishing.yaml, checked by lc_core.publishing and in CI).
  publishing = yamldecode(file("${path.module}/../../config/publishing.yaml"))
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

  # Source trials keep only small quality reports (docs/source-trials.md, lc_ingest.trials). They are deleted
  # when the trial ends (on promotion, by the job's first production run; dropped from the registry, by the
  # deploy workflow), within 24 hours. These two rules are the safety net: a live trial rewrites its reports
  # every daily run, so only a trial's leftovers ever reach 2 days, and replaced versions go after 1 day.
  lifecycle_rule {
    condition {
      age            = 2
      matches_prefix = ["trials/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 1
      matches_prefix             = ["trials/"]
    }
    action {
      type = "Delete"
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

  # Old versions of overwritten shards and state (storage.data_noncurrent_retain_days).
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = local.publishing.storage.data_noncurrent_retain_days
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# Signed snapshots before they are copied to GitHub Releases. Versioned.
# Day folders and unused parts are pruned by build-snapshot (snapshots.retain_days, kept
# days' dependencies honoured; docs/snapshots-v2.md). A delete leaves an old version, and
# this rule deletes old versions after snapshots.noncurrent_retain_days.
resource "google_storage_bucket" "snapshots" {
  name                        = "${local.bucket_prefix}-snapshots"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = local.publishing.snapshots.noncurrent_retain_days
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
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
