# GitHub Apps

## If GitHub just sent you here

You've created an App. Copy the value after `code=` in this page's address, and paste it into `bootstrap/github-apps.sh` in Cloud Shell. Then go back to `bootstrap/github-apps.html` for the next App. The code expires after an hour.

## The Apps

The agents act on GitHub only through these Apps (constitution Article 5). Makers and checkers are different Apps, so no agent approves its own work. None has the `workflows` permission, so no agent can change CI or deployment, and none has any organisation permission.

| App | Permissions | Install on |
| --- | --- | --- |
| `lei-commons-maker` | contents, pull requests, issues: write | `system`, `ledger-*`, `status`, `intake` |
| `lei-commons-checker` | pull requests, checks, statuses: write; contents, issues: read | `system`, `ledger-*`, `status`, `intake` |
| `lei-commons-guardian` | checks, statuses: write; contents, pull requests: read | every repository except `control` |
| `lei-commons-release` | contents: write | `releases` only |

The site's admin area uses `lei-commons-guardian` to set a pull request's `owner-approval` status after the owner
approves it (`config/approvals.yaml`, `lc_site.approvals`); the site may read that App's key and no other.

Never install any of them on `control`. Its private keys are in Secret Manager as `github-app-lei-commons-<name>`, created by `bootstrap/github-apps.sh`; nothing else holds them.

The fifth App in the design, `lei-commons-control`, is no longer needed: since 2026-11-21 the control state lives only in the cloud control bucket and changes only on the admin page, so the `control` repository has no workflows and no token.
