terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.10"
    }
  }

  # Bucket is created by bootstrap/bootstrap.sh and passed with -backend-config.
  backend "gcs" {
    prefix = "terraform/foundation"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Billing budgets API needs a quota project when called with user credentials.
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "this" {
  project_id = var.project_id
}
