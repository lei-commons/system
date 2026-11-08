# GitHub Actions authenticate with short-lived OIDC tokens. No service-account
# keys exist anywhere (and the conduct check rejects any that appear in code).

locals {
  owner_condition = var.github_org_id != "" ? "assertion.repository_owner_id == '${var.github_org_id}'" : "assertion.repository_owner == '${var.github_org}'"

  # "<repo>@<ref>" => service account. Only these combinations can act on GCP.
  # The ledger repositories' sync workflows read the store and commit as the maker App.
  wif_bindings = {
    "system@refs/heads/main"       = "deployer"
    "releases@refs/heads/main"     = "publisher"
    "control@refs/heads/main"      = "control-operator"
    "ledger-gleif@refs/heads/main" = "maker"
    "ledger-gb@refs/heads/main"    = "maker"
  }
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub (${var.github_org})"
  description               = "Workflows in the ${var.github_org} GitHub organisation"

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.github_org
  display_name                       = "GitHub Actions OIDC"

  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
    "attribute.ref"              = "assertion.ref"
    "attribute.repo_ref"         = "assertion.repository + '@' + assertion.ref"
  }

  attribute_condition = local.owner_condition

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "wif" {
  for_each = local.wif_bindings

  service_account_id = google_service_account.sa[each.value].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repo_ref/${var.github_org}/${each.key}"
}

# The GitHub App keys are created by bootstrap/github-apps.sh, outside Terraform.
# Each agent identity may read only its own App's key.
resource "google_secret_manager_secret_iam_member" "app_key" {
  for_each = toset(var.app_key_readers)

  secret_id = "projects/${var.project_id}/secrets/github-app-${each.value}"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.sa[each.value].email}"
}
