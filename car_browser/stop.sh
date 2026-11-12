#!/bin/bash
# 停止车机专用纯浏览器服务
cd "$(dirname "$0")"

if command -v docker &>/dev/null && docker compose version &>/dev/null; then
  DC="docker compose"
elif command -v docker-compose &>/dev/null; then
  DC="docker-compose"
else
  DC="docker-compose"
fi

echo "正在停止车机纯浏览器服务..."
$DC down
echo "已停止。"
