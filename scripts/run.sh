#!/usr/bin/env bash
# 启动网页应用：浏览器打开 http://127.0.0.1:8000
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

if [ ! -d "$VENV" ]; then
  echo "!!! 未找到 .venv，请先运行：bash scripts/setup.sh"
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
PYTHON_BIN="$VENV/bin/python"

# faster-whisper 首次下模型走 HuggingFace。新的 xet 后端在本机网络上会挂起，
# 强制走普通 HTTP 下载。
export HF_HUB_DISABLE_XET=1

cd "$ROOT"
PORT="${PORT:-8000}"
while ! "$PYTHON_BIN" - "$PORT" <<'PY' >/dev/null 2>&1
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", port))
PY
do
  PORT=$((PORT + 1))
done

echo ">>> 启动中… 打开浏览器访问 http://127.0.0.1:${PORT}"
exec "$PYTHON_BIN" -m uvicorn app.server:app --host 127.0.0.1 --port "$PORT"
