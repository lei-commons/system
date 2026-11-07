# The serving database for the private site and API (owner decision 2026-11-07:
# Cloud SQL Postgres, so search and the graph hold at 30 million records and beyond).
#
# Private only, on the jobs network: nothing on the internet can reach it.
# Connections must be encrypted. The site signs in with its own Google identity
# (IAM database authentication), so it holds no password. The one admin password,
# used by the loader to rebuild tables, lives in Secret Manager and only the
# loader identity can read it.

# Reached through Private Service Connect: the database gets one private
# address inside the jobs network. No network peering, which nfh.global's
# compute.restrictVpcPeering policy forbids (and which would link our network
# to another). Cloud SQL only offers this when an instance is created.
# Its own small subnet: Cloud Run's direct network access takes addresses from
# the jobs subnet for its instances, so a fixed address there can collide.
resource "google_compute_subnetwork" "endpoints" {
  name                     = "lc-endpoints-${var.region}"
  region                   = var.region
  network                  = google_compute_network.jobs.id
  ip_cidr_range            = "10.20.3.0/28"
  private_ip_google_access = true
}

resource "google_compute_address" "database" {
  name         = "lc-database"
  region       = var.region
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.endpoints.id
}

resource "google_compute_forwarding_rule" "database" {
  name                  = "lc-database"
  region                = var.region
  network               = google_compute_network.jobs.id
  ip_address            = google_compute_address.database.id
  load_balancing_scheme = ""
  target                = google_sql_database_instance.directory.psc_service_attachment_link
}

# The jobs network denies all egress by default; this opens Postgres on that one address.
resource "google_compute_firewall" "allow_database" {
  name               = "lc-allow-database"
  network            = google_compute_network.jobs.id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["${google_compute_address.database.address}/32"]

  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

resource "google_sql_database_instance" "directory" {
  name                = "lc-directory"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = true

  settings {
    tier                        = "db-custom-2-8192"
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 100
    disk_autoresize             = true
    deletion_protection_enabled = true

    ip_configuration {
      ipv4_enabled = false
      ssl_mode     = "ENCRYPTED_ONLY"

      psc_config {
        psc_enabled               = true
        allowed_consumer_projects = [var.project_id]
      }
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "09:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 14
      }
    }

    maintenance_window {
      day  = 7
      hour = 10
    }

    insights_config {
      query_insights_enabled = true
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "directory" {
  name     = "lei_commons"
  instance = google_sql_database_instance.directory.name
}

resource "random_password" "db_admin" {
  length  = 40
  special = false
}

resource "google_sql_user" "admin" {
  name     = "lc_admin"
  instance = google_sql_database_instance.directory.name
  password = random_password.db_admin.result
}

resource "google_secret_manager_secret" "db_admin" {
  secret_id = "db-admin-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_admin" {
  secret      = google_secret_manager_secret.db_admin.id
  secret_data = random_password.db_admin.result
}

resource "google_secret_manager_secret_iam_member" "db_admin_loader" {
  secret_id = google_secret_manager_secret.db_admin.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["loader"].member
}

# The site reads with its own identity; the loader grants it SELECT on each load.
resource "google_sql_user" "site" {
  name     = trimsuffix(google_service_account.sa["site"].email, ".gserviceaccount.com")
  instance = google_sql_database_instance.directory.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}
