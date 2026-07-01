#!/usr/bin/env bash
# macOS 一次性安装：创建项目 venv，安装转写/网页/抖音依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "!!! 当前分支按 macOS 适配；检测到系统是 $(uname -s)，继续安装但不保证非 macOS 可用。"
fi

echo ">>> 创建虚拟环境 $VENV ..."
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo ">>> 升级 pip 并安装 Python 依赖 ..."
export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-120}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
python -m pip install --retries 10 --timeout "$PIP_DEFAULT_TIMEOUT" -U pip setuptools wheel
python -m pip install --retries 10 --timeout "$PIP_DEFAULT_TIMEOUT" -r "$ROOT/requirements.txt"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo ">>> 未发现系统 ffmpeg，写入项目本地 ffmpeg 入口（imageio-ffmpeg）..."
  "$VENV/bin/python" - "$VENV/bin/ffmpeg" <<'PY'
from pathlib import Path
import sys

target = Path(sys.argv[1])
python = Path(sys.executable)
target.write_text(
    "#!" + str(python) + "\n"
    "import os, sys\n"
    "import imageio_ffmpeg\n"
    "exe = imageio_ffmpeg.get_ffmpeg_exe()\n"
    "os.execv(exe, [exe, *sys.argv[1:]])\n",
    encoding="utf-8",
)
target.chmod(target.stat().st_mode | 0o111)
PY
fi

echo ">>> 本地编译检查"
python -m compileall -q .

echo ""
echo ">>> 完成。"
echo "    Python: $("$VENV/bin/python" --version)"
echo "    yt-dlp: $("$VENV/bin/yt-dlp" --version)"
echo "    ffmpeg: $("${VENV}/bin/ffmpeg" -version | head -1)"
echo ">>> 启动：bash scripts/run.sh"
