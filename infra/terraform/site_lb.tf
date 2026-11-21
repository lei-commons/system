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

  # The admin area (config/site.yaml admin; owner decision 2026-11-21): its paths go to a backend of their own,
  # where IAP asks for a recent sign-in with 2-step verification. The site accepts admin requests only with
  # this backend's token.
  site_admin          = local.site_config.admin
  site_admin_audience = local.site_lb ? "/projects/${data.google_project.this.number}/global/backendServices/${google_compute_backend_service.site_admin[0].generated_id}" : ""
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

# The admin area's backend: the same Cloud Run service through the same NEG, with its own IAP settings.
resource "google_compute_backend_service" "site_admin" {
  count = local.site_lb ? 1 : 0

  name                  = "lc-site-admin"
  protocol              = "HTTPS"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  description           = "The admin area (${join(", ", local.site_admin.paths)}): IAP with re-authentication"

  backend {
    group = google_compute_region_network_endpoint_group.site[0].id
  }

  iap {
    enabled = true
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_iap_web_backend_service_iam_member" "site_admin_owner" {
  count = local.site_lb ? 1 : 0

  web_backend_service = google_compute_backend_service.site_admin[0].name
  role                = "roles/iap.httpsResourceAccessor"
  member              = "user:${var.owner_email}"
}

# Re-authentication on the admin backend: IAP asks the owner to sign in again (with the method below) whenever
# the last sign-in is older than the maximum age, before any admin page or action answers.
resource "google_iap_settings" "site_admin" {
  count = local.site_lb ? 1 : 0

  name = "projects/${data.google_project.this.number}/iap_web/compute/services/${google_compute_backend_service.site_admin[0].name}"

  access_settings {
    reauth_settings {
      method      = local.site_admin.reauth_method
      max_age     = "${local.site_admin.reauth_max_age_minutes * 60}s"
      policy_type = "MINIMUM"
    }
  }

  lifecycle {
    precondition {
      condition     = contains(["ENROLLED_SECOND_FACTORS", "SECURE_KEY", "LOGIN"], local.site_admin.reauth_method)
      error_message = "config/site.yaml admin.reauth_method must be ENROLLED_SECOND_FACTORS, SECURE_KEY or LOGIN."
    }
    precondition {
      condition     = local.site_admin.reauth_max_age_minutes >= 5 && local.site_admin.reauth_max_age_minutes <= 1440
      error_message = "config/site.yaml admin.reauth_max_age_minutes must be 5 to 1440."
    }
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

  host_rule {
    hosts        = [local.site_domain]
    path_matcher = "site"
  }

  # The admin paths go to the admin backend; everything else to the ordinary one.
  path_matcher {
    name            = "site"
    default_service = google_compute_backend_service.site[0].id

    path_rule {
      paths   = local.site_admin.paths
      service = google_compute_backend_service.site_admin[0].id
    }
  }
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
