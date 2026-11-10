#!/usr/bin/env bash
# Installs the sync workflow and its settings in the ledger repositories.
# Run by the owner's GitHub login (it writes a workflow file, which no App may).
#   ./ledger/install.sh
# Safe to rerun: it updates the workflow and the variables in place.
set -euo pipefail

ORG=lei-commons
PROJECT=lei-commons-prod
HERE="$(cd "$(dirname "$0")" && pwd)"

put_file() { # repo path message file
  local sha
  sha="$(gh api "repos/$ORG/$1/contents/$2" --jq .sha 2>/dev/null || true)"
  if [ -n "$sha" ]; then
    gh api -X PUT "repos/$ORG/$1/contents/$2" -f message="$3" -f content="$(base64 < "$4" | tr -d '\n')" -f sha="$sha" >/dev/null
  else
    gh api -X PUT "repos/$ORG/$1/contents/$2" -f message="$3" -f content="$(base64 < "$4" | tr -d '\n')" >/dev/null
  fi
}

install() { # repo ledgers description [baseline bucket]
  local repo="$1" ledgers="$2" baselines="${4:-}" readme
  readme="$(mktemp)"
  cat > "$readme" <<EOF
# $repo

The lei-commons ledger for $3: every record as of the baseline, and every
field-level change since, committed daily by the lei-commons-maker App.

- \`baseline/\`: the records in 256 shards by directory ID, written once.
- \`changes/\`: one file per ingested publication, one JSON line per changed field.
- \`LEDGER.json\`: the change files in order, with their hashes, and the store state they reach.

Each sync rebuilds every shard from this repository and compares it with the
store before it commits (\`python -m lc_ingest.export verify\`). Private until the owner approves going public.
EOF
  put_file "$repo" README.md "Describe the ledger" "$readme"
  put_file "$repo" .github/workflows/sync.yml "Install the ledger sync workflow" "$HERE/sync.yml"
  rm -f "$readme"
  gh variable set LC_DATA_BUCKET -R "$ORG/$repo" --body "$PROJECT-data"
  gh variable set LC_DECISIONS_BUCKET -R "$ORG/$repo" --body "$PROJECT-decision-log"
  gh variable set LC_LEDGERS -R "$ORG/$repo" --body "$ledgers"
  if [ -n "$baselines" ]; then
    gh variable set LC_BASELINE_BUCKET -R "$ORG/$repo" --body "$baselines"
  fi
  gh variable set GCP_MAKER_SA -R "$ORG/$repo" --body "lc-maker@$PROJECT.iam.gserviceaccount.com"
  gh variable set LC_MAKER_APP_ID -R "$ORG/$repo" \
    --body "$(gh api "orgs/$ORG/installations" --jq '.installations[] | select(.app_slug == "lei-commons-maker") | .app_id')"
  gh variable set GCP_WIF_PROVIDER -R "$ORG/$repo" \
    --body "$(gh variable get GCP_WIF_PROVIDER --org "$ORG")"
  echo "installed in $repo"
}

install ledger-gleif "gleif:. gleif-rr:relationships" "GLEIF legal entities and their relationships"
install ledger-gb "gb:." "the UK Companies House register"
# Too big for a git repository: the baseline goes in the ledger-baselines bucket, pinned by hash.
install ledger-br "br:." "Brazil's CNPJ register (Receita Federal), companies only" "$PROJECT-ledger-baselines"
install ledger-us "us-ny:ny" "US state registers, starting with the New York Department of State" "$PROJECT-ledger-baselines"
install ledger-sg "sg:." "Singapore's ACRA corporate entities" "$PROJECT-ledger-baselines"
