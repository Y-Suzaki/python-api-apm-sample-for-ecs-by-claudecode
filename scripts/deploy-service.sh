#!/usr/bin/env bash
# ECS スタック（ALB + Fargate Service）をデプロイ／更新する。
# 直前にビルドしたイメージ URI を .last_image_uri から拾う。明示指定も可能。
#   IMAGE_URI=...:tag scripts/deploy-service.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

CFN_DIR="${SCRIPT_DIR}/../cloudformation"

if [[ -z "${IMAGE_URI:-}" ]]; then
  if [[ -f "${SCRIPT_DIR}/../.last_image_uri" ]]; then
    IMAGE_URI="$(cat "${SCRIPT_DIR}/../.last_image_uri")"
  else
    REPO_URI="$(stack_output "${ECR_STACK}" RepositoryUri)"
    IMAGE_URI="${REPO_URI}:latest"
  fi
fi

USERS_TABLE="$(stack_output "${DYNAMODB_STACK}" UsersTableName)"
USERS_TABLE="${USERS_TABLE:-users}"

log "Deploying ECS service stack: ${ECS_STACK}"
log "  Image: ${IMAGE_URI}"
log "  Users table: ${USERS_TABLE}"

aws cloudformation deploy \
  --stack-name "${ECS_STACK}" \
  --template-file "${CFN_DIR}/04-ecs-alb.yaml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      ProjectName="${PROJECT_NAME}" \
      ImageUri="${IMAGE_URI}" \
      UsersTableName="${USERS_TABLE}" \
  --no-fail-on-empty-changeset

# 既存スタックでイメージのみ差し替えた場合、CFN は no change と判断するため
# 強制的に新しいデプロイをトリガーする。
log "Forcing new deployment to pick up the latest image"
CLUSTER_NAME="$(stack_output "${ECS_STACK}" ClusterName)"
SERVICE_NAME="$(stack_output "${ECS_STACK}" ServiceName)"
if [[ -n "${CLUSTER_NAME}" && -n "${SERVICE_NAME}" ]]; then
  aws ecs update-service \
    --cluster "${CLUSTER_NAME}" \
    --service "${SERVICE_NAME}" \
    --force-new-deployment >/dev/null
fi

ALB_DNS="$(stack_output "${ECS_STACK}" AlbDnsName)"
log "Service deployed. ALB endpoint: http://${ALB_DNS}"
log "  Health: curl http://${ALB_DNS}/health"
log "  Docs:   http://${ALB_DNS}/docs"
