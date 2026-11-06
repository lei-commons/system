#!/usr/bin/env bash
# Builds the conduct gateway image and pushes it to Artifact Registry.
# Run as the owner from the root of the system repo, in Cloud Shell:
#   ./bootstrap/build-gateway.sh lei-commons-prod
# Prints the image digest to pass to bootstrap.sh with --gateway-image.
set -euo pipefail

PROJECT="${1:?usage: build-gateway.sh <project-id> [region]}"
REGION="${2:-us-central1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/lei-commons/conduct-gateway"
TAG="$(git rev-parse --short=12 HEAD)"

gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -f services/conduct_proxy/Dockerfile -t "${IMAGE}:${TAG}" .
docker push "${IMAGE}:${TAG}"

DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "${IMAGE}:${TAG}")"
echo
echo "Image: ${DIGEST}"
