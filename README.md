# 视频字幕 / 转写提取器

把 B站 / 抖音 视频链接 → 纯文字稿。优先抓字幕（快），没字幕自动 Whisper 转写。
全程在本机跑，不上传视频，不消耗云端额度。

## 三步上手

```bash
# 1) 装系统级 ffmpeg（一次性，需要 sudo + 联网）
sudo apt update && sudo apt install -y ffmpeg

# 2) 装 Python 依赖（创建 .venv，装 yt-dlp/whisper/fastapi）
bash scripts/setup.sh

# 3) 启动网页应用
bash scripts/run.sh
# 浏览器打开 http://127.0.0.1:8000
```

页面里粘贴链接（每行一个）→ 选模型 → 开始提取 → 看进度条 → 完成后单条下载或「打包下载全部」。

## 命令行版（不想开网页时）

```bash
source .venv/bin/activate
# 单条测试
scripts/transcribe.sh "https://www.bilibili.com/video/xxxx"
# 批量：把链接逐行写进 urls.txt
scripts/transcribe.sh urls.txt
# 文字稿都在 transcripts/
```

## B站 AI 字幕需要登录态（WSL 注意）

WSL 里 `--cookies-from-browser chrome` 通常失败（Windows 侧 Chrome 的 cookie 是 DPAPI 加密，Linux 解不开）。
可靠做法：Chrome 装扩展「Get cookies.txt LOCALLY」，在 B站登录后导出 `cookies.txt`，
放到项目根目录 `/home/lhq24/finance/cookies.txt`，后端会自动加载。

## 性能提醒

- 没有 GPU，Whisper 走 CPU：40 分钟视频用 `medium` 大约要十几到几十分钟。嫌慢选 `small`。
- 抓到字幕的视频几乎是秒出（不走转写）。
- 后端默认同时最多跑 2 个任务（whisper 很吃 CPU）。

## 目录

```
app/
  server.py          后端（FastAPI）
  transcribe_core.py 提取核心：字幕优先 → Whisper 兜底
  static/            前端页面
scripts/
  setup.sh  run.sh   安装 / 启动
  transcribe.sh      命令行批量版
  srt2txt.py         字幕清洗
transcripts/         输出的文字稿
```
