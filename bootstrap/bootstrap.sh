#!/usr/bin/env bash
# lei-commons day-zero bootstrap. Run ONCE, as the human owner, in Google Cloud Shell.
#
#   git clone https://github.com/lei-commons/system.git && cd system
#   ./bootstrap/bootstrap.sh --project lei-commons-prod --billing XXXXXX-XXXXXX-XXXXXX [--create]
#
# What it does (and nothing else):
#   1. Optionally creates the project and links billing.
#   2. Enables the APIs Terraform needs to start.
#   3. Creates a versioned bucket for Terraform state.
#   4. Runs Terraform: service accounts, buckets, Workload Identity for the
#      lei-commons GitHub org, Firestore, Artifact Registry, secrets, budget.
#   5. Prints the GitHub org variables to set (and sets them if `gh` is logged in).
#
# The system starts PAUSED. Nothing runs until you switch it to running.
# You remain owner of the project; no agent gets owner, IAM-admin or key rights.

set -euo pipefail

PROJECT=""
BILLING=""
CREATE=0
REGION="us-central1"
ORG="lei-commons"
CURRENCY="USD"
BUDGET="1500"
AUTO_APPROVE=0
BUDGET_PUBSUB=true
GATEWAY_IMAGE=""
CONSOLE=false
BUDGET_GUARD=false
SITE=false
CYCLE=false

usage() {
  sed -n '2,20p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --billing) BILLING="$2"; shift 2 ;;
    --create) CREATE=1; shift ;;
    --region) REGION="$2"; shift 2 ;;
    --currency) CURRENCY="$2"; shift 2 ;;
    --budget) BUDGET="$2"; shift 2 ;;
    --no-budget-pubsub) BUDGET_PUBSUB=false; shift ;;
    --gateway-image) GATEWAY_IMAGE="$2"; shift 2 ;;
    --console) CONSOLE=true; shift ;;
    --budget-guard) BUDGET_GUARD=true; shift ;;
    --site) SITE=true; shift ;;
    --cycle) CYCLE=true; shift ;;
    --yes) AUTO_APPROVE=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

[[ -n "$PROJECT" && -n "$BILLING" ]] || { echo "--project and --billing are required" >&2; usage 1; }
[[ -f infra/terraform/versions.tf ]] || { echo "run from the root of the system repo" >&2; exit 1; }

# Cloud Shell can ship a `terraform` stub that prints install instructions and
# exits 0, so every Terraform step would appear to pass while doing nothing.
if ! terraform version -json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["terraform_version"])' >/dev/null 2>&1; then
  echo "terraform is not installed (or is only a stub). Install it first: see docs/day-zero.md" >&2
  exit 1
fi

OWNER="$(gcloud config get-value account 2>/dev/null)"
[[ -n "$OWNER" ]] || { echo "not logged in to gcloud" >&2; exit 1; }

echo "Owner:   $OWNER"
echo "Project: $PROJECT (region $REGION)"
echo "Billing: $BILLING  budget: $BUDGET $CURRENCY/month"
echo

if [[ "$CREATE" == 1 ]]; then
  if gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
    echo "Project $PROJECT already exists; using it."
  else
    echo "Creating project $PROJECT ..."
    gcloud projects create "$PROJECT" --name="lei-commons"
  fi
fi

echo "Linking billing account ..."
gcloud billing projects link "$PROJECT" --billing-account="$BILLING" >/dev/null
gcloud config set project "$PROJECT" >/dev/null

echo "Enabling bootstrap APIs ..."
gcloud services enable \
  cloudresourcemanager.googleapis.com \
  serviceusage.googleapis.com \
  cloudbilling.googleapis.com \
  billingbudgets.googleapis.com \
  iam.googleapis.com \
  storage.googleapis.com \
  --project="$PROJECT"

STATE_BUCKET="${PROJECT}-tfstate"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1; then
  echo "Creating Terraform state bucket gs://${STATE_BUCKET} ..."
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="$PROJECT" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
  gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning
fi

# Quota project for user-credential calls (billing budgets API).
gcloud auth application-default set-quota-project "$PROJECT" >/dev/null 2>&1 || true

ORG_ID=""
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  ORG_ID="$(gh api "orgs/${ORG}" --jq .id 2>/dev/null || true)"
fi

# Services that exist must stay: leaving these options out must never remove them.
if [[ -z "$GATEWAY_IMAGE" ]]; then
  GATEWAY_IMAGE="$(gcloud run services describe conduct-gateway --project "$PROJECT" --region "$REGION" \
    --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
fi
if [[ "$CONSOLE" == false ]] && gcloud run services describe owner-console --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  CONSOLE=true
fi
if [[ "$CYCLE" == false ]] && gcloud run jobs describe load-db --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  CYCLE=true
fi
if [[ "$SITE" == false ]] && gcloud run services describe lei-commons-site --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  SITE=true
fi
if [[ "$BUDGET_GUARD" == false ]] && gcloud run services describe budget-guard --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  BUDGET_GUARD=true
fi

cd infra/terraform
terraform init -input=false -backend-config="bucket=${STATE_BUCKET}"

TF_VARS=(
  -var "project_id=${PROJECT}"
  -var "region=${REGION}"
  -var "billing_account=${BILLING}"
  -var "owner_email=${OWNER}"
  -var "github_org=${ORG}"
  -var "github_org_id=${ORG_ID}"
  -var "monthly_budget_usd=${BUDGET}"
  -var "budget_currency=${CURRENCY}"
  -var "budget_pubsub_enabled=${BUDGET_PUBSUB}"
  -var "gateway_image=${GATEWAY_IMAGE}"
  -var "console_enabled=${CONSOLE}"
  -var "budget_guard_enabled=${BUDGET_GUARD}"
  -var "site_enabled=${SITE}"
  -var "cycle_enabled=${CYCLE}"
)

terraform plan -input=false -out=day0.plan "${TF_VARS[@]}"
if [[ "$AUTO_APPROVE" != 1 ]]; then
  read -r -p "Apply this plan? [y/N] " ok
  [[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Stopped. Nothing applied."; exit 0; }
fi
terraform apply -input=false day0.plan

# The control state belongs to the owner: write it once, and never over an existing file.
# The budget field is in USD, so it is only set when the budget given here is USD too.
STATE_FILE="$(mktemp)"
if [[ "$CURRENCY" == "USD" ]]; then
  printf '{"state":"paused","paused_components":[],"monthly_budget_usd":%s,"updated_by":"bootstrap"}\n' "$BUDGET" > "$STATE_FILE"
else
  printf '{"state":"paused","paused_components":[],"updated_by":"bootstrap"}\n' > "$STATE_FILE"
fi
gcloud storage cp --no-clobber --content-type=application/json "$STATE_FILE" "gs://${PROJECT}-control/state.json"
rm -f "$STATE_FILE"

WIF="$(terraform output -raw workload_identity_provider)"
DEPLOYER="$(terraform output -json service_accounts | python3 -c 'import json,sys; print(json.load(sys.stdin)["deployer"])')"
PUBLISHER="$(terraform output -json service_accounts | python3 -c 'import json,sys; print(json.load(sys.stdin)["publisher"])')"
CONTROL_URI="$(terraform output -raw control_state_uri)"
GH_LOGIN="$(gh api user --jq .login 2>/dev/null || echo '<your-github-login>')"

declare -A VARS=(
  [GCP_PROJECT_ID]="$PROJECT"
  [GCP_REGION]="$REGION"
  [GCP_WIF_PROVIDER]="$WIF"
  [GCP_DEPLOYER_SA]="$DEPLOYER"
  [GCP_PUBLISHER_SA]="$PUBLISHER"
  [LC_CONTROL_STATE_URI]="$CONTROL_URI"
  [SYSTEM_STATE]="paused"
  [MONTHLY_BUDGET_USD]="$BUDGET"
  [OWNER_LOGIN]="$GH_LOGIN"
)

if [[ "$CURRENCY" != "USD" ]]; then
  unset 'VARS[MONTHLY_BUDGET_USD]'  # the budget given here isn't in USD
fi

echo
echo "GitHub organisation variables for ${ORG}:"
for k in "${!VARS[@]}"; do printf '  %-22s %s\n' "$k" "${VARS[$k]}"; done

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  read -r -p "Set these on github.com/${ORG} now with gh? [y/N] " ok
  if [[ "$ok" == "y" || "$ok" == "Y" ]]; then
    for k in "${!VARS[@]}"; do
      # A rerun never overwrites: SYSTEM_STATE in particular stays as the owner set it.
      if gh variable get "$k" --org "$ORG" >/dev/null 2>&1; then
        echo "  kept $k (already set)"
      else
        gh variable set "$k" --org "$ORG" --visibility all --body "${VARS[$k]}"
      fi
    done
    echo "Done."
  fi
else
  echo "Set them at https://github.com/organizations/${ORG}/settings/variables/actions"
fi

echo
echo "Bootstrap complete. The system is PAUSED."
echo "Next: follow docs/day-zero.md, then set SYSTEM_STATE=running when ready."
