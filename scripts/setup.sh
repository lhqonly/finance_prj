#!/usr/bin/env bash
# 一次性安装：在 ~/finance/.venv 里装 yt-dlp + openai-whisper
# ffmpeg 需要系统级安装（需要 sudo），脚本会提示你手动跑。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

echo ">>> 检查 ffmpeg ..."
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!!! 没有 ffmpeg。请先手动运行（需要联网+sudo）："
  echo "    sudo apt update && sudo apt install -y ffmpeg"
  echo "    装完再重新跑本脚本。"
  exit 1
fi
echo "    ffmpeg OK: $(ffmpeg -version | head -1)"

echo ">>> 创建虚拟环境 $VENV ..."
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo ">>> 升级 pip 并安装 yt-dlp + openai-whisper + 网页后端依赖 ..."
pip install -U pip
pip install -U yt-dlp openai-whisper fastapi "uvicorn[standard]"

# 没有 GPU，可选装 faster-whisper（CPU 上比 openai-whisper 快好几倍）
# 想用就取消下一行注释：
# pip install -U faster-whisper

echo ""
echo ">>> 完成。yt-dlp $(yt-dlp --version)"
echo ">>> 以后每次新开终端，先激活环境： source $VENV/bin/activate"
