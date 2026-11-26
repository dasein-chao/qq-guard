#!/data/data/com.termux/files/usr/bin/bash
# QQ Guard 手机端一键安装（原生 Termux，免 root）
# 用法：把本文件放到手机后执行  bash setup.sh
set -e

echo "== 1/4 更新软件源 =="
pkg update -y || true

echo "== 2/4 安装 Python 与 Termux:API 命令 =="
pkg install -y python termux-api

echo "== 3/4 检查项目文件 =="
if [ -f "$HOME/qq-guard/main.py" ]; then
  PROJECT="$HOME/qq-guard"
elif [ -f "$HOME/storage/downloads/qq-guard/main.py" ]; then
  PROJECT="$HOME/storage/downloads/qq-guard"
elif [ -f "$HOME/qq-guard-github/main.py" ]; then
  PROJECT="$HOME/qq-guard-github"
else
  echo "!! 没找到项目。请先把 qq-guard 整个文件夹放到 ~/qq-guard（或手机 Download 目录）"
  echo "   然后重新执行本脚本。"
  exit 1
fi
echo "   项目位置：$PROJECT"

echo "== 4/4 防休眠（后台保活）=="
termux-wake-lock || true

cat <<'TIP'

安装完成。接下来还需要在手机系统里做两件事（只需一次）：
  1) 设置 → 通知与状态栏 → 通知使用权 → 允许 "Termux:API"
  2) 设置 → 应用 → Termux → 电池 → 允许后台活动 / 关闭电池优化
     （OriginOS 还要在 i管家 → 后台高耗电 里放行 Termux）

验证通知能不能读到：
  termux-notification-list | head -c 300

然后先跑探针：
  cd $PROJECT && python main.py probe --seconds 90

TIP
