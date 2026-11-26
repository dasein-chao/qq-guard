#!/data/data/com.termux/files/usr/bin/bash
# 启动监控（后台常驻）。日志写在 qqguard.log，按天滚动由 store 默认清理策略管。
cd "$(dirname "$0")/../.." 2>/dev/null || cd "$HOME/qq-guard"
termux-wake-lock
mkdir -p logs
echo "[$(date '+%F %T')] 启动" >> logs/qqguard.log
# nohup 保证退出 Termux 界面后仍在跑（Termux 本身不被杀的前提下）
nohup python main.py daemon >> logs/qqguard.log 2>&1 &
echo "已启动，PID=$!"
echo "看日志： tail -f logs/qqguard.log"
echo "停止：   pkill -f 'main.py daemon'"
