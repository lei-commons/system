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
