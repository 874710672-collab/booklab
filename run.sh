#!/bin/sh
# 书析 BookLab 启动脚本
# 用法：./run.sh [端口]
cd "$(dirname "$0")" || exit 1

PORT="${1:-8787}"

# 清理旧进程
pkill -f 'app/server.py' 2>/dev/null
sleep 0.4

mkdir -p data/uploads

# 检查依赖
if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，请先执行：apk add python3"
  exit 1
fi

if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo "⚠️  未设置 DEEPSEEK_API_KEY，拆书功能将不可用"
fi

python3 app/db.py >/dev/null 2>&1

export PORT
nohup python3 app/server.py > data/server.log 2>&1 &
PID=$!
sleep 1.5

if kill -0 $PID 2>/dev/null; then
  IP=$(ip route get 1 2>/dev/null | awk '{print $NF; exit}')
  [ -z "$IP" ] && IP=$(hostname -i 2>/dev/null | awk '{print $1}')
  echo "✓ 书析已启动 (pid $PID)"
  echo "  本机：  http://127.0.0.1:$PORT"
  [ -n "$IP" ] && echo "  局域网：http://$IP:$PORT"
  echo "  日志：  $(pwd)/data/server.log"
else
  echo "✗ 启动失败，查看日志："
  tail -20 data/server.log
  exit 1
fi
