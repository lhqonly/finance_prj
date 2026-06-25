#!/usr/bin/env bash
# 启动网页应用：浏览器打开 http://127.0.0.1:8000
# 自动适配两种安装方式：有 .venv 用 venv，否则用用户目录(pip --user)。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
# 确保用户级安装的 yt-dlp/whisper/f2 在 PATH 上（后端 subprocess 要调它们）
export PATH="$HOME/.local/bin:$PATH"

if [ -d "$VENV" ]; then
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
fi

cd "$ROOT"
echo ">>> 启动中… 打开浏览器访问 http://127.0.0.1:8000"
exec python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8000
