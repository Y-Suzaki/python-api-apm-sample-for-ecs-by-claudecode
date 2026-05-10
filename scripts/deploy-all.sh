#!/usr/bin/env bash
# ワンショット: インフラ → イメージビルド → サービスデプロイをまとめて実行。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/deploy-infra.sh"
"${SCRIPT_DIR}/build.sh"
"${SCRIPT_DIR}/deploy-service.sh"
