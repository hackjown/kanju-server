#!/bin/bash
cd "$(dirname "$0")"
echo "启动 剧场8(juchang8.com) 服务 → http://127.0.0.1:8077"
if [ -x ".venv/bin/python3" ]; then
  exec .venv/bin/python3 app.py
else
  exec python3 app.py
fi
