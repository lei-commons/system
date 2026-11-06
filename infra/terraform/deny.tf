# Defence in depth for constitution Article 1. Agent identities already lack
# these permissions; the deny policy makes sure no later grant can give them.

resource "google_iam_deny_policy" "agents" {
  count = var.enable_deny_policy ? 1 : 0

  parent       = urlencode("cloudresourcemanager.googleapis.com/projects/${var.project_id}")
  name         = "lei-commons-agent-limits"
  display_name = "lei-commons: agents never administer IAM, keys or the project"

  rules {
    description = "Agents cannot change IAM, create keys, or delete the project"
    deny_rule {
      denied_principals = [
        for k, sa in google_service_account.sa :
        "principal://iam.googleapis.com/projects/-/serviceAccounts/${sa.email}"
      ]
      denied_permissions = [
        "cloudresourcemanager.googleapis.com/projects.setIamPolicy",
        "cloudresourcemanager.googleapis.com/projects.delete",
        "iam.googleapis.com/serviceAccountKeys.create",
        "iam.googleapis.com/serviceAccounts.setIamPolicy",
        "iam.googleapis.com/roles.create",
        "iam.googleapis.com/roles.update",
      ]
    }
  }
}
