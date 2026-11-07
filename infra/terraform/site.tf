# The lei-commons site and API, private to the owner. IAP admits only the
# owner's Google account, and the service checks IAP's token itself on every
# request. Nothing here is public until the owner approves going public.
# Created when site_enabled is true, after the first image is pushed.

resource "google_cloud_run_v2_service" "site" {
  provider = google-beta
  count    = var.site_enabled ? 1 : 0

  name                = "lei-commons-site"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  launch_stage        = "BETA"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.sa["site"].email
    timeout         = "300s"

    # Private ranges only, to reach the serving database. Unlike the jobs, the site
    # must also reach www.gstatic.com for IAP's signing keys, which the private DNS
    # zones don't cover, and it never fetches third-party content.
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.jobs.id
        subnetwork = google_compute_subnetwork.jobs.id
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/site:latest"

      resources {
        limits = {
          cpu    = "2"
          memory = "8Gi"
        }
      }

      env {
        name  = "LC_SITE_OWNERS"
        value = var.owner_email
      }
      env {
        name  = "LC_IAP_AUDIENCE"
        value = "/projects/${data.google_project.this.number}/locations/${var.region}/services/lei-commons-site"
      }
      env {
        name  = "LC_SNAPSHOT_BUCKET"
        value = google_storage_bucket.snapshots.name
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "LC_DB_HOST"
        value = google_sql_database_instance.directory.private_ip_address
      }
      env {
        name  = "LC_DB_USER"
        value = google_sql_user.site.name
      }

      # Loading the snapshot takes a while; give it time before the first request.
      startup_probe {
        initial_delay_seconds = 10
        period_seconds        = 10
        failure_threshold     = 24
        tcp_socket {
          port = 8080
        }
      }
    }
  }

  lifecycle {
    # Later images are rolled out by the deploy workflow.
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "site_iap" {
  count = var.site_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.site[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

resource "google_iap_web_cloud_run_service_iam_member" "site_owner" {
  count = var.site_enabled ? 1 : 0

  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.site[0].name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = "user:${var.owner_email}"
}
