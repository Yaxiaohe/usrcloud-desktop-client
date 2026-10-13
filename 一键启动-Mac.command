#!/bin/bash
# 有人云巡站客户端 · Electron 启动脚本（macOS / Linux）
# 双击即可运行；首次需先在「终端」里执行:  chmod +x 一键启动-Mac.command

# Mac 端统一从本地副本启动（OneDrive 不支持符号链接，Electron 在此目录无法运行）
LOCAL_APP="$HOME/Applications/有人云巡站客户端/一键启动-Mac.command"
if [ -x "$LOCAL_APP" ]; then
  exec "$LOCAL_APP"
fi

cd "$(dirname "$0")"

# 双击运行时 PATH 是精简的，补上常见安装位置（Homebrew / WorkBuddy 托管运行时）
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.workbuddy/binaries/python/envs/default/bin:$HOME/.workbuddy/binaries/node/versions/22.22.2-5/bin:$HOME/.workbuddy/binaries/python/versions/3.13.12/bin:$PATH"

# 该变量会让 Electron 退化成纯 Node 模式，必须清掉
unset ELECTRON_RUN_AS_NODE

echo "============================================"
echo "  有人云巡站客户端（Electron 启动）"
echo "============================================"
echo

if ! command -v node >/dev/null 2>&1; then
  echo "[!] 未找到 Node.js。请先安装：https://nodejs.org/ （选 LTS）"
  echo
  read -n 1 -s -r -p "按任意键退出…"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] 未找到 python3。macOS 上请先装 Python3（含 Pillow、requests）。"
  echo
  read -n 1 -s -r -p "按任意键退出…"
  exit 1
fi

if [ ! -x "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron" ] && [ ! -f "node_modules/electron/path.txt" ]; then
  echo "[i] 首次运行，正在安装 electron（国内镜像，可能需要几分钟）…"
  ELECTRON_MIRROR="https://registry.npmmirror.com/-/binary/electron/" npm install
fi

echo "[i] 启动中…"
npm start
read -n 1 -s -r -p "按任意键退出…"
