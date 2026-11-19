# The private site at its own address (config/site.yaml): a global external HTTPS load balancer in front of
# the site's Cloud Run service, with a Google-managed certificate and IAP on the load balancer, so the
# owner's Google login (2-step verification) is still the only way in. See config/site.yaml for the two
# steps. Cost: one forwarding-rule set (about $18 a month) plus a little per GB; the certificate is free.

locals {
  site_config          = yamldecode(file("${path.module}/../../config/site.yaml"))
  site_domain          = local.site_config.domain
  site_lb              = var.site_enabled && local.site_config.load_balancer
  site_lb_only         = local.site_lb && local.site_config.only_through_load_balancer
  site_direct_audience = "/projects/${data.google_project.this.number}/locations/${var.region}/services/lei-commons-site"
  site_lb_audience     = local.site_lb ? "/projects/${data.google_project.this.number}/global/backendServices/${google_compute_backend_service.site[0].generated_id}" : ""
  # The IAP audiences the site accepts: during step 1 either, after step 2 only the load balancer's.
  site_audiences = local.site_lb_only ? [local.site_lb_audience] : compact([local.site_direct_audience, local.site_lb_audience])
}

resource "google_compute_global_address" "site" {
  count = local.site_lb ? 1 : 0

  name        = "lc-site"
  description = "The private site's address: ${local.site_domain} (DNS A record)"

  depends_on = [google_project_service.apis]
}

resource "google_compute_managed_ssl_certificate" "site" {
  count = local.site_lb ? 1 : 0

  name = "lc-site-cert"
  managed {
    domains = [local.site_domain]
  }

  depends_on = [google_project_service.apis]
}

# The serverless NEG names the service (a string, not the resource), so the site's own settings may depend
# on the backend service below without a cycle.
resource "google_compute_region_network_endpoint_group" "site" {
  count = local.site_lb ? 1 : 0

  name                  = "lc-site-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = "lei-commons-site"
  }
}

resource "google_compute_backend_service" "site" {
  count = local.site_lb ? 1 : 0

  name                  = "lc-site"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  description           = "The private site, behind IAP (owner only)"

  backend {
    group = google_compute_region_network_endpoint_group.site[0].id
  }

  # IAP with Google's own OAuth client: the owner's Google account only (IAM below).
  iap {
    enabled = true
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_iap_web_backend_service_iam_member" "site_owner" {
  count = local.site_lb ? 1 : 0

  web_backend_service = google_compute_backend_service.site[0].name
  role                = "roles/iap.httpsResourceAccessor"
  member              = "user:${var.owner_email}"
}

resource "google_compute_url_map" "site" {
  count = local.site_lb ? 1 : 0

  name            = "lc-site"
  default_service = google_compute_backend_service.site[0].id
}

resource "google_compute_target_https_proxy" "site" {
  count = local.site_lb ? 1 : 0

  name             = "lc-site"
  url_map          = google_compute_url_map.site[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.site[0].id]
}

resource "google_compute_global_forwarding_rule" "site_https" {
  count = local.site_lb ? 1 : 0

  name                  = "lc-site-https"
  target                = google_compute_target_https_proxy.site[0].id
  ip_address            = google_compute_global_address.site[0].id
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

# http:// redirects to https://.
resource "google_compute_url_map" "site_redirect" {
  count = local.site_lb ? 1 : 0

  name = "lc-site-redirect"
  default_url_redirect {
    https_redirect         = true
    strip_query            = false
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
  }
}

resource "google_compute_target_http_proxy" "site_redirect" {
  count = local.site_lb ? 1 : 0

  name    = "lc-site-redirect"
  url_map = google_compute_url_map.site_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "site_http" {
  count = local.site_lb ? 1 : 0

  name                  = "lc-site-http"
  target                = google_compute_target_http_proxy.site_redirect[0].id
  ip_address            = google_compute_global_address.site[0].id
  port_range            = "80"
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

output "site_ip" {
  description = "Add a DNS A record: config/site.yaml domain -> this address (then Google issues the certificate)"
  value       = local.site_lb ? google_compute_global_address.site[0].address : null
}
