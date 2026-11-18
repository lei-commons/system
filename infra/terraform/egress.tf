# The conduct gateway's own way out (owner decision 2026-11-18): a small network whose only exit to the
# internet is Cloud NAT on one reserved address, so every outside site sees lei-commons at one fixed IP.
# A site that blocks or limits us can then name the address, and the owner can ask for it to be allowed.
# (2026-11-18: GLEIF's file server answered 403 to the gateway's shared Cloud Run address, while the same
# files downloaded fine from elsewhere.)
#
# Only the gateway is attached here. The jobs stay on the sealed lc-jobs network (network.tf), whose only
# way out is this gateway. The gateway still refuses non-public addresses itself (conduct_proxy.upstream).
# Cost: the address (about $3.65 a month while in use) and Cloud NAT (about $1 a month plus $0.045 a GB
# processed; a few tens of GB a month at the current sources).

resource "google_compute_network" "egress" {
  name                    = "lc-egress"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  description             = "The conduct gateway's network: out to the internet only through Cloud NAT on one fixed address"

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "egress" {
  name                     = "lc-egress-${var.region}"
  region                   = var.region
  network                  = google_compute_network.egress.id
  ip_cidr_range            = "10.30.0.0/26" # Cloud Run Direct VPC egress needs at least a /26
  private_ip_google_access = true
}

resource "google_compute_address" "gateway_egress" {
  name         = "lc-gateway-egress"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
  description  = "The one address every outside site sees lei-commons at (Cloud NAT for the conduct gateway)"

  depends_on = [google_project_service.apis]
}

resource "google_compute_router" "egress" {
  name    = "lc-egress-router"
  region  = var.region
  network = google_compute_network.egress.id
}

resource "google_compute_router_nat" "egress" {
  name                               = "lc-gateway-nat"
  router                             = google_compute_router.egress.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.gateway_egress.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  # Enough ports for every job at once (a deploy starts them all; the default 64 per instance ran out on
  # 2026-11-18 and connections timed out). Dynamic allocation grows an instance's ports as it needs them.
  enable_dynamic_port_allocation      = true
  enable_endpoint_independent_mapping = false
  min_ports_per_vm                    = 1024
  max_ports_per_vm                    = 8192

  subnetwork {
    name                    = google_compute_subnetwork.egress.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

output "gateway_egress_ip" {
  description = "The fixed address outside sites see lei-commons at (give it to a source that asks)"
  value       = google_compute_address.gateway_egress.address
}
