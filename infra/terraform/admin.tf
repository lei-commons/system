# The admin area's second factor and its GitHub App key. The owner console that once lived here was retired on
# 2026-11-22 (its actions moved to the site's /admin area, behind IAP re-authentication and a fresh code). The
# secret keeps its name, console-totp, so the owner's authenticator keeps working.

resource "google_secret_manager_secret" "console_totp" {
  secret_id = "console-totp"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# The site checks the second factor for every owner action, and its admin area sets the key up and rotates it
# (lc_core.second_factor), so it adds versions too.
resource "google_secret_manager_secret_iam_member" "site_totp" {
  for_each = toset(["roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionAdder"])

  secret_id = google_secret_manager_secret.console_totp.id
  role      = each.value
  member    = google_service_account.sa["site"].member
}

# The admin page's Approvals (lc_site.approvals) talk to GitHub as the guardian App (statuses: write; pull
# requests and contents: read): the site may read that App's key, and no other App's.
resource "google_secret_manager_secret_iam_member" "site_guardian_key" {
  count = length(var.app_key_readers) > 0 ? 1 : 0

  secret_id = "projects/${var.project_id}/secrets/${yamldecode(file("${path.module}/../../config/approvals.yaml")).github_app.key_secret}"
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["site"].member
}
