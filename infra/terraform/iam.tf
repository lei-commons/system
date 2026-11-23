# One service account per role. No agent identity holds any IAM-admin,
# billing, project-owner or key-creation permission (constitution Article 1).

locals {
  service_accounts = {
    deployer     = "Deploys images, jobs and workflows from system@main"
    ingest       = "Downloads registry and GLEIF files"
    crawler      = "Fetches public web pages through the conduct-policy proxy"
    resolver     = "Entity resolution and deduplication"
    maker        = "Maker agents (model calls that propose changes)"
    checker      = "Checker agents (model calls that review changes)"
    publisher    = "Builds, signs and publishes snapshots"
    workflow     = "Runs the daily cycle"
    budget-guard = "Pauses the system when spend reaches the owner's cap; cannot resume"
    gateway      = "Runs the conduct gateway, the only route to the internet for jobs"
    site         = "Serves the private site and API from the latest snapshot; reads snapshots only"
    loader       = "Loads each snapshot into the serving database"
    # Lets Claude read the owner's suggestions and source edits, privately, from intake@main. Read-only.
    reader = "Reads the owner's source edits and approvals (control bucket, two objects), from intake@main only"
  }

  # Runtime identities read the control state before every step.
  runtime_sas = [for k in keys(local.service_accounts) : k if !contains(["deployer", "reader"], k)]

  common_roles = ["roles/logging.logWriter", "roles/monitoring.metricWriter"]

  project_roles = {
    # logging.viewer lets deploy workflows read job results; it grants no write access.
    deployer  = ["roles/run.developer", "roles/workflows.editor", "roles/artifactregistry.writer", "roles/cloudscheduler.admin", "roles/logging.viewer"]
    ingest    = []
    # The web-claims pilot (owner decision 2026-11-23): reads archived pages, has a model read them into claims.
    crawler   = ["roles/aiplatform.user"]
    resolver  = ["roles/bigquery.jobUser"]
    maker     = ["roles/aiplatform.user"]
    checker   = ["roles/aiplatform.user"]
    publisher = ["roles/bigquery.jobUser", "roles/bigquery.dataEditor"]
    workflow  = ["roles/run.invoker", "roles/run.viewer", "roles/workflows.invoker"]
    # budget-guard has no project roles beyond logging; it writes only the control bucket.
    budget-guard = []
    gateway      = []
    # Read-only, for the system view: job runs, services, schedules and daily cycles.
    site = ["roles/run.viewer", "roles/cloudscheduler.viewer", "roles/workflows.viewer", "roles/cloudsql.client", "roles/cloudsql.instanceUser"]
    # Connects to the serving database; the admin password comes from Secret Manager.
    loader = ["roles/cloudsql.client"]
    # No project roles: it reads two objects in the control bucket (below).
    reader = []
  }

  # bucket => { role => [sa keys] }
  bucket_access = {
    data = {
      "roles/storage.objectUser"    = ["ingest", "crawler", "resolver", "publisher"]
      "roles/storage.objectCreator" = ["maker", "checker"]
      "roles/storage.objectViewer"  = ["maker", "checker", "site"]
    }
    snapshots = {
      "roles/storage.objectUser"   = ["publisher"]
      # The crawler picks the web-claims pilot's candidates from the published snapshot.
      "roles/storage.objectViewer" = ["site", "loader", "crawler"]
    }
    decision_log = {
      # The deployer records each merge and deploy (.github/workflows/changes.yml): create-only, like every writer.
      "roles/storage.objectCreator" = ["ingest", "crawler", "resolver", "maker", "checker", "publisher", "workflow", "budget-guard", "loader", "site", "deployer"]
      "roles/storage.objectViewer"  = ["publisher", "site"]
    }
    ledger_baselines = {
      "roles/storage.objectCreator" = ["maker"]
      "roles/storage.objectViewer"  = ["maker", "checker"]
    }
    frames = {
      "roles/storage.objectCreator" = ["publisher", "workflow"]
    }
    control = {
      "roles/storage.objectViewer" = local.runtime_sas
      # The site takes the owner's actions on the admin page (IAP re-authentication plus a fresh code each time).
      "roles/storage.objectUser" = ["budget-guard", "site"]
    }
  }

  buckets = {
    data             = google_storage_bucket.data.name
    snapshots        = google_storage_bucket.snapshots.name
    decision_log     = google_storage_bucket.decision_log.name
    ledger_baselines = google_storage_bucket.ledger_baselines.name
    frames           = google_storage_bucket.frames.name
    control          = google_storage_bucket.control.name
  }

  project_bindings = flatten([
    for sa, roles in local.project_roles : [
      for role in concat(roles, local.common_roles) : { sa = sa, role = role }
    ]
  ])

  bucket_bindings = flatten([
    for bucket, roles in local.bucket_access : [
      for role, sas in roles : [
        for sa in sas : { bucket = bucket, role = role, sa = sa }
      ]
    ]
  ])
}

resource "google_service_account" "sa" {
  for_each = local.service_accounts

  account_id   = "lc-${each.key}"
  display_name = "lei-commons ${each.key}"
  description  = each.value

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "sa" {
  for_each = { for b in local.project_bindings : "${b.sa}|${b.role}" => b }

  project = var.project_id
  role    = each.value.role
  member  = google_service_account.sa[each.value.sa].member
}

resource "google_storage_bucket_iam_member" "sa" {
  for_each = { for b in local.bucket_bindings : "${b.bucket}|${b.role}|${b.sa}" => b }

  bucket = local.buckets[each.value.bucket]
  role   = each.value.role
  member = google_service_account.sa[each.value.sa].member
}

# The reader sees only the owner's source edits and approvals, nothing else in the control bucket.
resource "google_storage_bucket_iam_member" "reader_source_files" {
  bucket = google_storage_bucket.control.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.sa["reader"].member

  condition {
    title       = "source-files-only"
    description = "source-edits.json and source-approvals.json"
    expression  = "resource.name == \"projects/_/buckets/${google_storage_bucket.control.name}/objects/source-edits.json\" || resource.name == \"projects/_/buckets/${google_storage_bucket.control.name}/objects/source-approvals.json\""
  }
}

# The deployer reads the owner's control state, and nothing else in the control bucket, so that every
# deploy stops unless the system is running (scripts/check_system_state.py; owner decision 2026-11-21).
resource "google_storage_bucket_iam_member" "deployer_control_state" {
  bucket = google_storage_bucket.control.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.sa["deployer"].member

  condition {
    title       = "control-state-only"
    description = "state.json"
    expression  = "resource.name == \"projects/_/buckets/${google_storage_bucket.control.name}/objects/state.json\""
  }
}

# The deployer may attach runtime identities to the jobs it deploys, nothing more.
resource "google_service_account_iam_member" "deployer_act_as" {
  for_each = toset(local.runtime_sas)

  service_account_id = google_service_account.sa[each.value].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.sa["deployer"].member
}

# The human owner stays owner of the project (constitution Article 1.1).
resource "google_project_iam_member" "owner" {
  project = var.project_id
  role    = "roles/owner"
  member  = "user:${var.owner_email}"
}
