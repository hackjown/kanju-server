#!/bin/bash
# 一键启动车机专用纯浏览器服务 (KasmVNC 独立容器)
cd "$(dirname "$0")"

# 检测 docker compose 指令兼容性
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
  DC="docker compose"
elif command -v docker-compose &>/dev/null; then
  DC="docker-compose"
else
  echo "[错误] 未检测到 Docker Compose 环境，请先安装 Docker！"
  exit 1
fi

echo "========================================================"
echo " 正在启动车机专用云端纯浏览器 (方案四) ..."
echo " 说明: 本服务与 juchang8 完全物理隔离，互不干扰"
echo "========================================================"

$DC pull car_browser
$DC up -d

echo ""
echo "=== 启动成功 ==="
echo "车机端访问地址: http://<服务器IP或DDNS>:3000"
echo "提示: 车机浏览器打开该地址即可直接进入全屏无拦截看剧模式！"
