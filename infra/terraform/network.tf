# The jobs network: the only way out to the internet is the conduct gateway.
# Two layers, so one mistake doesn't open it (constitution Article 2):
#   routes:   no default route; only Google's private range is reachable
#   firewall: deny all egress, except that same range
# Nothing is attached here until jobs run with Direct VPC egress (ALL_TRAFFIC).

locals {
  private_google_cidr = "199.36.153.8/30"
  private_google_ips  = ["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"]
  # Names that must resolve to the private range from inside the network.
  private_zones = {
    googleapis = "googleapis.com."
    run-app    = "run.app."
  }
}

resource "google_compute_network" "jobs" {
  name                            = "lc-jobs"
  auto_create_subnetworks         = false
  delete_default_routes_on_create = true

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "jobs" {
  name                     = "lc-jobs-${var.region}"
  region                   = var.region
  network                  = google_compute_network.jobs.id
  ip_cidr_range            = "10.20.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_route" "private_google" {
  name             = "lc-private-google"
  network          = google_compute_network.jobs.id
  dest_range       = local.private_google_cidr
  next_hop_gateway = "default-internet-gateway"
}

resource "google_compute_firewall" "deny_all_egress" {
  name               = "lc-deny-all-egress"
  network            = google_compute_network.jobs.id
  direction          = "EGRESS"
  priority           = 65000
  destination_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }

  # The security agent reads these logs for escape attempts.
  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "allow_private_google" {
  name               = "lc-allow-private-google"
  network            = google_compute_network.jobs.id
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = [local.private_google_cidr]

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_dns_managed_zone" "private" {
  for_each = local.private_zones

  name       = "lc-${each.key}"
  dns_name   = each.value
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.jobs.id
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_dns_record_set" "private_google_a" {
  for_each = local.private_zones

  managed_zone = google_dns_managed_zone.private[each.key].name
  name         = each.key == "googleapis" ? "private.googleapis.com." : each.value
  type         = "A"
  ttl          = 300
  rrdatas      = local.private_google_ips
}

resource "google_dns_record_set" "private_google_cname" {
  for_each = local.private_zones

  managed_zone = google_dns_managed_zone.private[each.key].name
  name         = "*.${each.value}"
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["private.googleapis.com."]
}
