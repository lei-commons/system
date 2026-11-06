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

# Jobs reach the gateway through an internal load balancer. Calling the run.app
# URL through Private Google Access got a 404 from Cloud Run's ingress check even
# with the documented setup; an internal load balancer is an allowed source.
# Plain HTTP stays inside the VPC, because a certificate needs a domain or a
# private key in Terraform state. Revisit before owner keys pass through here.
resource "google_compute_subnetwork" "proxy_only" {
  count = local.gateway_enabled ? 1 : 0

  name          = "lc-lb-proxy-${var.region}"
  region        = var.region
  network       = google_compute_network.jobs.id
  ip_cidr_range = "10.20.2.0/24"
  purpose       = "REGIONAL_MANAGED_PROXY"
  role          = "ACTIVE"
}

resource "google_compute_address" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name         = "lc-gateway"
  region       = var.region
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.jobs.id
  address      = "10.20.0.10"
}

resource "google_compute_region_network_endpoint_group" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name                  = "lc-gateway"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.gateway[0].name
  }
}

resource "google_compute_region_backend_service" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name                  = "lc-gateway"
  region                = var.region
  protocol              = "HTTP"
  load_balancing_scheme = "INTERNAL_MANAGED"

  backend {
    group           = google_compute_region_network_endpoint_group.gateway[0].id
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1
  }
}

resource "google_compute_region_url_map" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name            = "lc-gateway"
  region          = var.region
  default_service = google_compute_region_backend_service.gateway[0].id
}

resource "google_compute_region_target_http_proxy" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name    = "lc-gateway"
  region  = var.region
  url_map = google_compute_region_url_map.gateway[0].id
}

resource "google_compute_forwarding_rule" "gateway" {
  count = local.gateway_enabled ? 1 : 0

  name                  = "lc-gateway"
  region                = var.region
  load_balancing_scheme = "INTERNAL_MANAGED"
  ip_protocol           = "TCP"
  port_range            = "80"
  ip_address            = google_compute_address.gateway[0].address
  target                = google_compute_region_target_http_proxy.gateway[0].id
  network               = google_compute_network.jobs.id
  subnetwork            = google_compute_subnetwork.jobs.id

  depends_on = [google_compute_subnetwork.proxy_only]
}

resource "google_compute_firewall" "allow_gateway" {
  count = local.gateway_enabled ? 1 : 0

  name               = "lc-allow-gateway"
  network            = google_compute_network.jobs.id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["${google_compute_address.gateway[0].address}/32"]

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
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
          value = "http://${google_compute_address.gateway[0].address}"
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
    google_compute_forwarding_rule.gateway,
    google_compute_firewall.allow_gateway,
  ]
}
