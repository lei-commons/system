variable "project_id" {
  description = "Google Cloud project ID for lei-commons."
  type        = string
}

variable "region" {
  description = "Primary region (decision: us-central1, cheapest flexible option)."
  type        = string
  default     = "us-central1"
}

variable "billing_account" {
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) the project is linked to."
  type        = string
}

variable "owner_email" {
  description = "The human owner's Google account. Owner of the project; receives budget emails."
  type        = string
}

variable "github_org" {
  description = "GitHub organisation whose workflows may act on this project."
  type        = string
  default     = "lei-commons"
}

variable "github_org_id" {
  description = "Numeric GitHub org ID. When set, Workload Identity trusts only this ID, which survives org renames and blocks name squatting."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Initial monthly budget. Changed live by the owner in the console afterwards; Terraform does not revert it."
  type        = number
  default     = 1500
}

variable "budget_currency" {
  description = "Must match the billing account's currency (for example USD or INR). The amount is converted by the owner if not USD."
  type        = string
  default     = "USD"
}

variable "budget_pubsub_enabled" {
  description = "Send budget updates to Pub/Sub for the budget guard. Needs Google's billing service account on the topic, which a domain-restricted-sharing org policy can block."
  type        = bool
  default     = true
}

variable "cycle_enabled" {
  description = "Create the build-snapshot and load-db jobs of the daily cycle. Push their images first: their deploy workflows do this."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Where alerts go. Defaults to the owner's account."
  type        = string
  default     = ""
}

variable "site_enabled" {
  description = "Create the private site and API. Push the image first: the deploy-site workflow does this."
  type        = bool
  default     = false
}

variable "budget_guard_enabled" {
  description = "Create the budget guard. Push the image first: the deploy-budget-guard workflow does this."
  type        = bool
  default     = false
}

variable "console_enabled" {
  description = "Create the owner console. Push the image first: the deploy-console workflow does this."
  type        = bool
  default     = false
}

variable "gateway_image" {
  description = "Conduct gateway image, by digest, from bootstrap/build-gateway.sh. Empty until the first build; the gateway and egress check are created once it is set."
  type        = string
  default     = ""
}

variable "enable_deny_policy" {
  description = "Create the IAM deny policy. Needs the Deny Admin role, which some accounts can only grant at organisation level. Least-privilege roles apply either way."
  type        = bool
  default     = false
}

variable "owner_key_sources" {
  description = "Sources whose access needs an owner-provided credential. One empty secret is created per source; the owner adds the value in the console."
  type        = list(string)
  default     = ["gb-coh-api-key", "in-mca-credentials"]
}

variable "app_key_readers" {
  description = "Agent identities that read their own GitHub App key (github-app-lei-commons-<name> in Secret Manager). Empty in a test project, where the keys don't exist."
  type        = list(string)
  default     = ["maker"]
}
