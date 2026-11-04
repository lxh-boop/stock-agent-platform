#!/usr/bin/env bash
# 服务器端一键更新：解包最新代码并重建指定服务
# 用法：
#   bash ~/stock-agent-platform/deploy/update.sh          # 默认重建 api（后端）
#   bash ~/stock-agent-platform/deploy/update.sh frontend # 只重建前端
#   bash ~/stock-agent-platform/deploy/update.sh all      # 全部重建
set -e
cd ~/stock-agent-platform

tar xzf ~/migration/code.tar.gz -C ~/stock-agent-platform

TARGET="${1:-api}"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file deploy/server.env"

if [ "$TARGET" = "all" ]; then
    $COMPOSE up -d --build
else
    $COMPOSE up -d --build "$TARGET"
fi

echo ""
echo "✅ 更新完成。常用检查："
echo "   dc ps                    # 状态应 healthy"
echo "   dc logs -f api           # 看日志"
echo "   curl http://127.0.0.1:3000/api/v1/readiness"
