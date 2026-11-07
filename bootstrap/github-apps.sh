#!/usr/bin/env bash
# Stores the lei-commons GitHub Apps' private keys in Secret Manager.
# Run as the owner, in Cloud Shell, while creating the Apps from
# bootstrap/github-apps.html:
#   ./bootstrap/github-apps.sh lei-commons-prod
# Keys go from GitHub straight into Secret Manager and are never printed.
set -euo pipefail

PROJECT="${1:?usage: github-apps.sh <project-id>}"
APPS=(lei-commons-maker lei-commons-checker lei-commons-guardian lei-commons-release)

store() {
  local name="$1" code="$2"
  curl -fsS -X POST -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app-manifests/${code}/conversions" |
  PROJECT="$PROJECT" APP="$name" python3 -c '
import json, os, subprocess, sys
app = json.load(sys.stdin)
project, name = os.environ["PROJECT"], os.environ["APP"]
secret = f"github-app-{name}"

def put(secret_id, value):
    exists = subprocess.run(["gcloud", "secrets", "describe", secret_id, "--project", project],
                            capture_output=True).returncode == 0
    if not exists:
        subprocess.run(["gcloud", "secrets", "create", secret_id, "--project", project,
                        "--replication-policy", "automatic", "--quiet"], check=True, capture_output=True)
    subprocess.run(["gcloud", "secrets", "versions", "add", secret_id, "--project", project, "--data-file", "-"],
                   input=value.encode(), check=True, capture_output=True)

got, app_id, slug = app.get("name"), app.get("id"), app.get("slug") or name
if got != name:
    sys.exit("GitHub created an App called " + repr(got) + ", not " + repr(name) + "; nothing stored")
put(secret, app["pem"])
print("  stored the private key for " + name + " in Secret Manager as " + secret)
print("  App ID: " + str(app_id))
print("  Now install it: https://github.com/apps/" + slug + "/installations/new")
'
}

echo "Open bootstrap/github-apps.html and create the Apps in this order: ${APPS[*]}"
for app in "${APPS[@]}"; do
  echo
  read -r -p "Paste the code GitHub gave you for ${app}: " code
  code="${code#*code=}"; code="${code%%&*}"
  [[ "$code" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "that doesn't look like a code; run the script again" >&2; exit 1; }
  store "$app" "$code"
done
echo
echo "Done. Install each App as printed above, choosing only the repositories listed in docs/github-apps.md."
