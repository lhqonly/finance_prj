#!/usr/bin/env bash
# 批量：一串 URL -> 一堆纯文字稿
#
# 用法：
#   单条测试:   ./transcribe.sh "https://www.bilibili.com/video/xxxx"
#   批量:       ./transcribe.sh urls.txt
#
# 策略：
#   1) 先尝试抓官方/AI字幕（B站常有，最快、最准、零转写）
#   2) 抓不到字幕，自动降级用 Whisper 把音频转写（抖音口播必走这条）
#
# 可选环境变量：
#   COOKIES=/path/to/cookies.txt   B站AI字幕需要登录态时传入（WSL 推荐导出 cookies.txt）
#   MODEL=medium                   Whisper 模型: tiny/base/small/medium/large；CPU慢可降到 small
#   SUBLANGS="zh.*,ai.*,Chinese"   想抓的字幕语言
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
OUT="$ROOT/transcripts"
WORK="$ROOT/.work"
SRT2TXT="$ROOT/scripts/srt2txt.py"

MODEL="${MODEL:-medium}"
SUBLANGS="${SUBLANGS:-zh.*,ai.*,Chinese,zh-Hans,zh-CN}"
COOKIES="${COOKIES:-}"

[ -d "$VENV" ] && source "$VENV/bin/activate"
mkdir -p "$OUT" "$WORK"

# 收集 URL 列表
ARG="${1:-}"
if [ -z "$ARG" ]; then
  echo "用法: ./transcribe.sh <URL | urls.txt>"; exit 1
fi
URLS=()
if [ -f "$ARG" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"; line="$(echo "$line" | xargs)"   # 去注释去空白
    [ -n "$line" ] && URLS+=("$line")
  done < "$ARG"
else
  URLS+=("$ARG")
fi

COOKIE_ARGS=()
[ -n "$COOKIES" ] && COOKIE_ARGS=(--cookies "$COOKIES")

i=0
for url in "${URLS[@]}"; do
  i=$((i+1))
  echo ""
  echo "============================================================"
  echo "[$i/${#URLS[@]}] $url"
  echo "============================================================"

  # 取一个安全的标题做文件名
  title="$(yt-dlp "${COOKIE_ARGS[@]}" --get-title --no-warnings "$url" 2>/dev/null | head -1)"
  [ -z "$title" ] && title="video_$i"
  safe="$(echo "$title" | tr '/\\:*?"<>|' '_' | cut -c1-60)"
  base="$(printf '%02d_%s' "$i" "$safe")"
  final="$OUT/$base.txt"

  if [ -f "$final" ]; then
    echo ">>> 已存在，跳过: $final"
    continue
  fi

  tmp="$WORK/$base"
  mkdir -p "$tmp"

  # ---------- 第一步：试抓字幕 ----------
  echo ">>> 尝试抓字幕 ..."
  yt-dlp "${COOKIE_ARGS[@]}" \
    --write-subs --write-auto-subs \
    --sub-langs "$SUBLANGS" --convert-subs srt \
    --skip-download -o "$tmp/sub" "$url" >/dev/null 2>&1 || true

  sub="$(find "$tmp" -name '*.srt' -o -name '*.vtt' 2>/dev/null | head -1)"
  if [ -n "$sub" ]; then
    echo ">>> 抓到字幕，清洗为文本: $sub"
    { echo "# 标题: $title"; echo "# 来源: $url"; echo "# 方式: 官方/AI字幕"; echo ""; \
      python3 "$SRT2TXT" "$sub"; } > "$final"
    echo ">>> 完成: $final"
    rm -rf "$tmp"
    continue
  fi

  # ---------- 第二步：降级 Whisper 转写 ----------
  echo ">>> 没字幕，下载音频走 Whisper（model=$MODEL，CPU 可能较慢）..."
  yt-dlp "${COOKIE_ARGS[@]}" -x --audio-format mp3 \
    -o "$tmp/audio.%(ext)s" "$url" >/dev/null 2>&1
  audio="$(find "$tmp" -name 'audio.*' | head -1)"
  if [ -z "$audio" ]; then
    echo "!!! 音频下载失败，跳过: $url"; continue
  fi

  whisper "$audio" --language zh --model "$MODEL" \
    --output_format txt --output_dir "$tmp" >/dev/null 2>&1
  wtxt="$(find "$tmp" -name '*.txt' | head -1)"
  if [ -z "$wtxt" ]; then
    echo "!!! 转写失败，跳过: $url"; continue
  fi
  { echo "# 标题: $title"; echo "# 来源: $url"; echo "# 方式: Whisper($MODEL)"; echo ""; \
    cat "$wtxt"; } > "$final"
  echo ">>> 完成: $final"
  rm -rf "$tmp"
done

echo ""
echo ">>> 全部结束。文字稿都在: $OUT"
ls -la "$OUT"
