#!/usr/bin/env bash
# Installs the owner-approval workflow (private-repos/owner-approval.yml) in the private repositories that take
# pull requests, as .github/workflows/owner-approval.yml. Run by the owner's GitHub login after the pull request
# that changes this file is approved and merged in system (it writes a workflow file, which no App may):
#   ./private-repos/install.sh
# Safe to rerun: it writes the file only where it differs. Never installed in control (no workflows, no Apps) or
# in the empty repositories (status, ledger-in): run it again once they have a main branch.
set -euo pipefail

ORG=lei-commons
REPOS="releases intake ledger-gleif ledger-gb ledger-br ledger-us ledger-sg"
HERE="$(cd "$(dirname "$0")" && pwd)"
FILE="$HERE/owner-approval.yml"
DEST=.github/workflows/owner-approval.yml
WANT="$(base64 < "$FILE" | tr -d '\n')"

for repo in $REPOS; do
  current="$(gh api "repos/$ORG/$repo/contents/$DEST" 2>/dev/null || true)"
  sha="$(printf '%s' "$current" | jq -r '.sha // empty' 2>/dev/null || true)"
  have="$(printf '%s' "$current" | jq -r '.content // empty' 2>/dev/null | tr -d '\n' || true)"
  if [ "$have" = "$WANT" ]; then
    echo "$repo: already installed"
    continue
  fi
  args=(-X PUT "repos/$ORG/$repo/contents/$DEST" -f message="Install the owner-approval workflow (system private-repos/owner-approval.yml)" -f content="$WANT")
  if [ -n "$sha" ]; then
    args+=(-f sha="$sha")
  fi
  gh api "${args[@]}" --jq '.commit.sha[0:7]' | sed "s/^/$repo: installed at /"
done
