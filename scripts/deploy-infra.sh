#!/usr/bin/env bash
# 共有インフラ（VPC / ECR / DynamoDB）を順番にデプロイする。
# ECS スタックはイメージが必要なので scripts/deploy-service.sh で別管理。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

CFN_DIR="${SCRIPT_DIR}/../cloudformation"

deploy_stack() {
  local stack="$1" template="$2"
  shift 2
  log "Deploying stack: ${stack}"
  aws cloudformation deploy \
    --stack-name "${stack}" \
    --template-file "${template}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides ProjectName="${PROJECT_NAME}" "$@" \
    --no-fail-on-empty-changeset
}

deploy_stack "${NETWORK_STACK}"  "${CFN_DIR}/01-network.yaml"
deploy_stack "${ECR_STACK}"      "${CFN_DIR}/02-ecr.yaml"
deploy_stack "${DYNAMODB_STACK}" "${CFN_DIR}/03-dynamodb.yaml" \
  TableName="${DYNAMODB_USERS_TABLE:-users}"

log "Infra stacks ready."
log "  Network:  ${NETWORK_STACK}"
log "  ECR:      $(stack_output "${ECR_STACK}" RepositoryUri)"
log "  DynamoDB: $(stack_output "${DYNAMODB_STACK}" UsersTableName)"
