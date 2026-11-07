# The conduct gateway (services/conduct_proxy). It has no VPC attachment, so it
# keeps normal internet egress; everything else reaches it from the jobs network.
# Created once gateway_image is set: see "Deploy the gateway" in docs/day-zero.md.

locals {
  gateway_enabled = var.gateway_image != ""
  gateway_callers = ["ingest", "crawler", "resolver", "maker", "checker", "publisher", "workflow"]
}

resource "google_cloud_run_v2_service" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name                = "conduct-gateway"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account                  = google_service_account.sa["gateway"].email
    timeout                          = "3600s"
    max_instance_request_concurrency = 40

    # Pacing lives in memory, so a second instance would break Article 2.4.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.gateway_image

      env {
        name  = "LC_CONTROL_STATE_URI"
        value = "gs://${google_storage_bucket.control.name}/state.json"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "gateway_callers" {
  for_each = local.gateway_enabled ? toset(local.gateway_callers) : toset([])

  name     = google_cloud_run_v2_service.gateway[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.sa[each.value].member
}

# Tries to escape from inside the jobs network. Run it after any network change.
resource "google_cloud_run_v2_job" "egress_check" {
  count = local.gateway_enabled ? 1 : 0

  name                = "egress-check"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.sa["crawler"].email
      max_retries     = 0
      timeout         = "120s"

      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.jobs.id
          subnetwork = google_compute_subnetwork.jobs.id
        }
      }

      containers {
        image   = var.gateway_image
        command = ["python", "-m", "conduct_proxy.egress_check"]

        env {
          name  = "LC_GATEWAY_URL"
          value = google_cloud_run_v2_service.gateway[0].uri
        }

        env {
          name  = "LC_GATEWAY_AUDIENCE"
          value = google_cloud_run_v2_service.gateway[0].uri
        }
      }
    }
  }

  depends_on = [
    google_compute_route.private_google,
    google_dns_record_set.private_google_cname,
  ]
}
