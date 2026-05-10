#!/usr/bin/env bash
# 共通環境変数 / ヘルパ。各スクリプトから source される。
set -euo pipefail

# ----- 必須環境変数チェック -----
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required}"

# CLAUDE.md 指示通り、東京リージョン固定（環境変数で上書き可能）
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION}}"

# プロジェクト命名は CloudFormation の Default と一致させる
export PROJECT_NAME="${PROJECT_NAME:-py-apm-sample}"

# Stack 名
export NETWORK_STACK="${PROJECT_NAME}-network"
export ECR_STACK="${PROJECT_NAME}-ecr"
export DYNAMODB_STACK="${PROJECT_NAME}-dynamodb"
export ECS_STACK="${PROJECT_NAME}-ecs"

# ----- ヘルパ -----
log() {
  printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"
}

aws_account_id() {
  aws sts get-caller-identity --query Account --output text
}

stack_output() {
  local stack="$1" key="$2"
  aws cloudformation describe-stacks \
    --stack-name "${stack}" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text
}
