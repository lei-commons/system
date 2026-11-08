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

Never install any of them on `control`. Its private keys are in Secret Manager as `github-app-lei-commons-<name>`, created by `bootstrap/github-apps.sh`; nothing else holds them.

The fifth App in the design, `lei-commons-control`, waits until GitHub's permission for organisation variables can be confirmed. Until then the `control` repository uses its own token.
