#!/bin/sh
# 书析 BookLab 启动脚本
#
# 用法：
#   ./run.sh            # 默认端口 8787
#   ./run.sh 9000       # 指定端口
#   ./run.sh status     # 查看运行状态
#   ./run.sh stop       # 停止服务
#
cd "$(dirname "$0")" || exit 1

PIDFILE="data/server.pid"
LOGFILE="data/server.log"

running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

case "$1" in
  status)
    if running; then
      PID=$(cat "$PIDFILE")
      echo "✓ 运行中 (pid $PID)"
      echo "  本地：   http://127.0.0.1:$(cat data/.port 2>/dev/null || echo 8787)"
      IP=$(cat data/.ip 2>/dev/null)
      [ -n "$IP" ] && echo "  局域网： http://$IP:$(cat data/.port 2>/dev/null || echo 8787)"
    else
      echo "✗ 未运行"
    fi
    exit 0
    ;;
  stop)
    pkill -f 'app/server.py' 2>/dev/null
    rm -f "$PIDFILE"
    echo "已停止"
    exit 0
    ;;
esac

PORT="${1:-8787}"

# 先停旧的，避免多进程抢端口
pkill -f 'app/server.py' 2>/dev/null
sleep 0.5

mkdir -p data/uploads

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，请先执行：apk add python3"
  exit 1
fi

if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo "⚠️  未设置 DEEPSEEK_API_KEY，拆书功能将不可用"
fi

# 字体解码依赖（缺了只影响番茄采集）
python3 -c "import numpy, PIL" 2>/dev/null || \
  echo "⚠️  缺少 numpy/Pillow，番茄采集的字体解码会不可用（apk add py3-numpy py3-pillow）"

python3 app/db.py >/dev/null 2>&1

export PORT
nohup python3 app/server.py > "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
echo "$PORT" > data/.port
sleep 2

if ! kill -0 "$PID" 2>/dev/null; then
  echo "✗ 启动失败，日志："
  tail -20 "$LOGFILE"
  exit 1
fi

# 拿局域网 IP（取第一个非 127 的）
IP=$(ip -4 addr show 2>/dev/null | grep -o 'inet [0-9.]*' | awk '{print $2}' | grep -v '^127\.' | head -1)
[ -n "$IP" ] && echo "$IP" > data/.ip

echo "✓ 书析已启动 (pid $PID)"
echo "  本地：   http://127.0.0.1:$PORT"
if [ -n "$IP" ]; then
  echo "  局域网： http://$IP:$PORT"
  echo ""
  echo "  ↑ 在 iPad 的 Safari 里打开上面这个局域网地址"
else
  echo "  ⚠️ 未检测到局域网 IP，请检查是否连着 Wi-Fi"
fi
echo ""
echo "  日志：   $(pwd)/$LOGFILE"
echo "  停止：   ./run.sh stop"
