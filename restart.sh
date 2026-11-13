#!/bin/bash
# 剧场8一键重启脚本：智能识别 Docker Compose 与 本地 Python 两种运行模式
cd "$(dirname "$0")"

# 1) 检测是否处于 Docker Compose 环境 (检查 docker 指令与 docker-compose.yml)
USE_DOCKER=0
if command -v docker &>/dev/null && [ -f "docker-compose.yml" ]; then
  if docker compose version &>/dev/null; then
    DC="docker compose"
  elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
  fi
  if [ -n "$DC" ]; then
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qE '^juchang8|^juchang8_app'; then
      USE_DOCKER=1
    fi
  fi
fi

# 如果是 Docker 部署环境，直接以 Docker 标准方式重启容器！
if [ "$USE_DOCKER" -eq 1 ]; then
  echo "========================================================"
  echo " 检测到 juchang8 Docker 容器环境，正在一键重启服务..."
  echo "========================================================"
  $DC restart juchang8 2>/dev/null || $DC up -d
  echo "=== Docker 容器已成功重启并载入最新代码 ==="
  exit 0
fi

# 2) 非 Docker 环境（宿主机直接运行模式）
echo "========================================================"
echo " 正在重启剧场8本地服务 (宿主机 Python 模式) ..."
echo "========================================================"

# 杀掉占用 8077 端口的旧进程
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -t -i:8077 2>/dev/null)
else
  PIDS=$(ss -lptn 'sport = :8077' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)
fi
if [ -n "$PIDS" ]; then
  kill $PIDS 2>/dev/null
  sleep 1
  kill -9 $PIDS 2>/dev/null
fi

# 自动定位或创建虚拟环境
if [ -x ".venv/bin/python3" ]; then
  PY=".venv/bin/python3"
elif [ -x "venv/bin/python3" ]; then
  PY="venv/bin/python3"
else
  PY="python3"
fi

# 检查 Flask 依赖，如缺失自动创建虚拟环境并补齐依赖
if ! $PY -c "import flask" 2>/dev/null; then
  echo "检测到 Python 环境未安装 Flask，正在自动配置环境并安装依赖..."
  if [ ! -d ".venv" ]; then
    python3 -m venv .venv 2>/dev/null
    if [ -x ".venv/bin/python3" ]; then
      PY=".venv/bin/python3"
    fi
  fi
  $PY -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt || \
  $PY -m pip install -q -r requirements.txt || true
fi

# 后台启动
rm -f /tmp/juchang8.log /tmp/kanju.log
nohup $PY app.py > /tmp/juchang8.log 2>&1 &
echo "started pid=$!"

# 轮询端口最多 15 秒
for i in $(seq 1 15); do
  sleep 1
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:8077/ 2>/dev/null)
  if [ "$CODE" = "200" ]; then
    echo "ready after ${i}s: index=$CODE"
    break
  fi
done
echo "--- 日志尾部 ---"
tail -15 /tmp/juchang8.log
