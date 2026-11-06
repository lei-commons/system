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
    console      = "Owner console backend (behind IAP, owner only)"
    budget-guard = "Pauses the system when spend reaches the owner's cap; cannot resume"
    gateway      = "Runs the conduct gateway, the only route to the internet for jobs"
    # Used only by the owner's workflows in the control repo. Never an agent identity.
    control-operator = "Writes the control state on the owner's command, from control@main only"
  }

  # Runtime identities read the control state before every step.
  runtime_sas = [for k in keys(local.service_accounts) : k if !contains(["deployer", "control-operator"], k)]

  common_roles = ["roles/logging.logWriter", "roles/monitoring.metricWriter"]

  project_roles = {
    deployer  = ["roles/run.developer", "roles/workflows.editor", "roles/artifactregistry.writer", "roles/cloudscheduler.admin"]
    ingest    = []
    crawler   = []
    resolver  = ["roles/bigquery.jobUser"]
    maker     = ["roles/aiplatform.user"]
    checker   = ["roles/aiplatform.user"]
    publisher = ["roles/bigquery.jobUser", "roles/bigquery.dataEditor"]
    workflow  = ["roles/run.invoker", "roles/workflows.invoker"]
    console   = ["roles/datastore.user"]
    # budget-guard has no project roles beyond logging; it writes only the control bucket.
    budget-guard = []
    gateway      = []
    # No project roles: it only touches the two buckets below.
    control-operator = []
  }

  # bucket => { role => [sa keys] }
  bucket_access = {
    data = {
      "roles/storage.objectUser"    = ["ingest", "crawler", "resolver", "publisher"]
      "roles/storage.objectCreator" = ["maker", "checker"]
      "roles/storage.objectViewer"  = ["maker", "checker", "console"]
    }
    snapshots = {
      "roles/storage.objectUser"   = ["publisher"]
      "roles/storage.objectViewer" = ["console"]
    }
    decision_log = {
      "roles/storage.objectCreator" = ["ingest", "crawler", "resolver", "maker", "checker", "publisher", "workflow", "console", "budget-guard", "control-operator"]
      "roles/storage.objectViewer"  = ["publisher", "console"]
    }
    frames = {
      "roles/storage.objectCreator" = ["publisher", "workflow"]
      "roles/storage.objectViewer"  = ["console"]
    }
    control = {
      "roles/storage.objectViewer" = local.runtime_sas
      "roles/storage.objectUser"   = ["console", "budget-guard", "control-operator"]
    }
  }

  buckets = {
    data         = google_storage_bucket.data.name
    snapshots    = google_storage_bucket.snapshots.name
    decision_log = google_storage_bucket.decision_log.name
    frames       = google_storage_bucket.frames.name
    control      = google_storage_bucket.control.name
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
