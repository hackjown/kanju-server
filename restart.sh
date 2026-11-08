#!/bin/bash
# 重启复刻站：先杀旧进程（用端口找 PID，不用 pkill -f 避免杀到自己），再后台启动
cd "$(dirname "$0")"

# 1) 用 lsof/ss 找占 8077 的进程并杀掉
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

# 2) 后台启动
rm -f /tmp/kanju.log
if [ -x ".venv/bin/python3" ]; then
  PY=".venv/bin/python3"
else
  PY="python3"
fi
nohup $PY app.py > /tmp/kanju.log 2>&1 &
echo "started pid=$!"

# 3) 轮询端口最多 20 秒
for i in $(seq 1 20); do
  sleep 1
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:8077/ 2>/dev/null)
  if [ "$CODE" = "200" ]; then
    echo "ready after ${i}s: index=$CODE"
    break
  fi
done
echo "--- 日志尾部 ---"
tail -15 /tmp/kanju.log
