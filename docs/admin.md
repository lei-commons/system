# The admin area and its two factors

Owner decision 2026-11-21: every administrative action and system change sits behind two-factor
authentication before the directory opens to the public.

## Where

`https://<domain>/admin/` (config/site.yaml `domain` and `admin`). It holds:

- **System**: the cycle, jobs, control state, and every owner action (pause, stop, resume, manual mode,
  run a job, pause or resume one job, the monthly budget), plus two tools: **probe a URL** (the source's first
  kilobyte through the gateway, as the ingest-gleif job; allow-listed hosts only) and the **load test** (a
  synthetic copy of the database, timed and dropped, as the load-db job). They replaced the gateway-probe and
  load-test workflows; their results show in the status report (`status.yml`);
- **Data sources**: approve, deny or keep pending; add, edit or remove a source; leave Claude a suggestion;
- **Approvals**: pull requests that change sensitive paths (`config/approvals.yaml`: how the system is built,
  deployed and given access, and how the owner signs in) wait here. The owner approves a pull request's exact
  head commit with a reason and a fresh code; the site then sets its `owner-approval` status to success as the
  `lei-commons-guardian` GitHub App. The owner-approval workflow (`pull_request_target`, so always main's own
  copy and rules) sets the status for every pull request: success at once when nothing sensitive changes,
  otherwise pending, assigned to the owner, with a comment linking here. Branch protection on `system` requires
  it; the private repositories can't be protected on GitHub's free plan, so there the session never merges a
  sensitive change without it;
- **Second factor**: set up the authenticator, or rotate its key.

The rest of the site (Search, Graph explorer, Dashboards, Data, Log) stays at `/`. It is still private behind
IAP; going public later removes IAP there and never touches the admin area.

## The two layers

1. **IAP with re-authentication.** The load balancer sends `/admin`, `/admin/*` and `/api/v1/admin/*` to their
   own backend service (`lc-site-admin`, infra/terraform/site_lb.tf). IAP there admits only the owner's
   Google account and asks them to sign in again with 2-step verification (`admin.reauth_method`) whenever
   the last sign-in is older than `admin.reauth_max_age_minutes`. The site accepts admin requests only with
   that backend's IAP token (`LC_IAP_ADMIN_AUDIENCE`), so the admin paths can't be reached through the
   ordinary backend. Until the owner's bootstrap creates the admin backend, the variable is unset and the
   site logs `admin-without-reauth` at startup.
2. **A fresh authenticator code for every change.** Each action and each second-factor step needs a code from
   the owner's authenticator app (RFC 6238, `lc_core.totp`). A code works once across every service; five
   wrong codes lock all services out for five minutes (the count is kept in the control bucket, so a restart
   doesn't reset it). Every action, and every refused code, is recorded in the decision log (never the code).

## The key

Kept in Secret Manager (`console-totp`), in one of three states (`lc_core.second_factor`): being set up
(shown on the Second factor page until a code from it activates it), active, or rotating (the current key
still works until a code from the new one replaces it). Starting a rotation needs a code from the current
key, and once a key is set up no page load ever shows a key, so reaching the admin area is never enough to
change it.

Break glass: the owner, with their own Google account, can add a new version of the secret in the Cloud
console (for example `pending:<new base32 key>` to start over), then set it up on the Second factor page.

## Changing the settings

Edit `admin` in config/site.yaml (method, maximum age, paths) through a pull request; the owner's bootstrap
rerun applies it. Add paths to the app (`lc_site.server.is_admin`) in the same change; a test checks they
agree.
