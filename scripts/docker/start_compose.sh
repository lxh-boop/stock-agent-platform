#!/usr/bin/env bash
# Linux 启动脚本：build api + frontend、up -d、等待健康检查。
# Windows 请使用同目录的 start_compose.ps1。
#
# 用法:
#   bash scripts/docker/start_compose.sh [PROJECT_ROOT]
#   NO_BUILD=1 bash scripts/docker/start_compose.sh   # 跳过构建直接重启
#
# 前置条件（与 start_compose.ps1 相同）:
#   - docker engine 可用
#   - 项目根有 local_app_config.json（敏感配置，脚本不会创建）
#   - .env 中 STOCK_POSTGRES_SECRET_FILE 指向宿主机上的 postgres 密码文件
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
if [[ "${1:-}" != "" ]]; then
  PROJECT_ROOT="$(cd "$1" && pwd)"
fi
NO_BUILD="${NO_BUILD:-0}"
COMPOSE="docker compose -p stock_daily_app -f ${PROJECT_ROOT}/docker-compose.yml"

step() { echo; echo "=== $1 ==="; }

step "前置检查"
command -v docker >/dev/null || { echo "docker 未安装"; exit 1; }
docker info >/dev/null
[[ -f "${PROJECT_ROOT}/local_app_config.json" ]] || { echo "缺少 local_app_config.json（敏感配置，请手工准备）"; exit 1; }
[[ -f "${PROJECT_ROOT}/docker-compose.yml" ]] || { echo "缺少 docker-compose.yml"; exit 1; }

for name in data models outputs logs runtime external_repos; do
  mkdir -p "${PROJECT_ROOT}/${name}"
done

step "校验 compose 配置"
${COMPOSE} config --quiet

if [[ "${NO_BUILD}" != "1" ]]; then
  step "构建 api + frontend 生产镜像"
  ${COMPOSE} build api frontend
fi

step "启动 api + frontend"
${COMPOSE} up -d --remove-orphans --force-recreate api frontend

step "等待健康检查"
wait_http() {
  local url="$1" timeout="$2" deadline=$((SECONDS + timeout)) code
  while (( SECONDS < deadline )); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$url" || true)
    if [[ "$code" == "200" ]]; then return 0; fi
    sleep 2
  done
  echo "服务未就绪: $url"; exit 1
}
wait_http "http://127.0.0.1:8010/api/v1/health" 480
wait_http "http://127.0.0.1:3000/healthz" 300

${COMPOSE} ps
echo
echo "React:   http://127.0.0.1:3000"
echo "FastAPI: http://127.0.0.1:8010"
