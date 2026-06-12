#!/usr/bin/env bash
# 全 CloudFormation スタックをデプロイの逆順に削除する。
# 各スタックの削除完了を待ってから次へ進む。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

delete_stack() {
  local stack="$1"

  # スタックが存在しない場合はスキップ
  if ! aws cloudformation describe-stacks --stack-name "${stack}" \
       --query "Stacks[0].StackName" --output text > /dev/null 2>&1; then
    log "Stack not found, skipping: ${stack}"
    return
  fi

  log "Deleting stack: ${stack}"
  aws cloudformation delete-stack --stack-name "${stack}"

  log "Waiting for deletion to complete: ${stack}"
  aws cloudformation wait stack-delete-complete --stack-name "${stack}"
  log "Deleted: ${stack}"
}

# デプロイの逆順で削除する
# ECS（ECS サービス・タスク・ALB） → DynamoDB → ECR → Network（VPC）
delete_stack "${ECS_STACK}"
delete_stack "${DYNAMODB_STACK}"
delete_stack "${ECR_STACK}"
delete_stack "${NETWORK_STACK}"

log "All stacks deleted."
