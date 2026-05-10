#!/usr/bin/env bash
# Docker イメージをビルドし、ECR にプッシュする。
# 前提: scripts/deploy-infra.sh で ECR スタックは作成済み。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

cd "${SCRIPT_DIR}/.."

IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"

log "Resolving ECR repository URI from stack '${ECR_STACK}'"
REPO_URI="$(stack_output "${ECR_STACK}" RepositoryUri)"
if [[ -z "${REPO_URI}" || "${REPO_URI}" == "None" ]]; then
  echo "ECR repository not found. Run scripts/deploy-infra.sh first." >&2
  exit 1
fi
ACCOUNT_ID="$(aws_account_id)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

log "Logging in to ECR: ${REGISTRY}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

log "Building image ${REPO_URI}:${IMAGE_TAG}"
docker build \
  --platform linux/amd64 \
  -t "${REPO_URI}:${IMAGE_TAG}" \
  -t "${REPO_URI}:latest" \
  .

log "Pushing image"
docker push "${REPO_URI}:${IMAGE_TAG}"
docker push "${REPO_URI}:latest"

log "Done. Pushed: ${REPO_URI}:${IMAGE_TAG}"
echo "${REPO_URI}:${IMAGE_TAG}" > .last_image_uri
