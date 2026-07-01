# 视频字幕 / 转写提取器

![界面截图](docs/screenshot.png)

把 **B站 / 抖音** 视频链接 → 纯文字稿。优先抓字幕（快），没字幕自动用 Whisper 本机转写。
全程在本机跑，不上传视频，不消耗云端额度。

## macOS 分支说明

当前 `macOS_branch` 只按 macOS 本地运行适配，不再维护 Ubuntu/WSL/Intel GPU 安装路径。
Apple Silicon 默认优先用 `mlx-whisper` 走 Apple GPU，失败时回落到 `faster-whisper` CPU；
没有系统 `ffmpeg` 时，`scripts/setup.sh` 会通过 `imageio-ffmpeg` 写入项目本地 `.venv/bin/ffmpeg`。

## 功能亮点

- **B站 / 抖音 → 纯文字稿**：字幕优先（秒出），没字幕走 Whisper 转写
- **macOS 本地转写**：Apple Silicon 优先用 MLX Apple GPU，失败时回落到 faster-whisper(CPU int8) / 旧 openai-whisper
- **📋 抖音用户主页批量**：输入主页链接 + 选日期区间，自动抓该号区间内全部作品、逐条转写、打包下载
- **省流量**：抖音批量只下「原声音频」(mp3) 不下高清视频，省约 70% 流量；遇到配 BGM 的作品自动回退下视频，不漏
- **Web UI**：实时进度条、右侧运行日志面板、处理中转圈动画、单条 / 打包下载

## 三步上手

```bash
# 1) 装 Python 依赖；若系统没有 ffmpeg，会自动写入项目本地 ffmpeg
bash scripts/setup.sh

# 2) 启动网页应用
bash scripts/run.sh
# 浏览器打开 http://127.0.0.1:8000
```

页面里粘贴链接（每行一个）→ 选模型 → 开始提取 → 看进度 → 单条下载或「打包下载全部」。

## 抖音用户主页批量

粘贴用户主页链接（`douyin.com/user/<sec_uid>`，**别带 `modal_id`**，那是单条作品弹窗），
选「抖音主页日期区间」，开始提取即可批量抓该号区间内作品逐条转写。
- 只下原声音频，省流量、下载快；BGM 作品自动回退下视频
- CPU 转写慢，整号几百条不现实，所以默认按日期区间限范围

## 模型选择

| 模型 | 引擎 | 适用 |
|---|---|---|
| small | MLX Apple GPU | 默认，口播够用，速度相对可控 |
| medium | MLX Apple GPU | 要更高准确度时用，比 small 慢 |
| large | MLX Apple GPU / CPU 兜底 | 最慢，一般不用 |

## 命令行版（不想开网页时）

```bash
# 单条测试
scripts/transcribe.sh "https://www.bilibili.com/video/xxxx"
# 批量：把链接逐行写进 urls.txt
scripts/transcribe.sh urls.txt
# 文字稿都在 transcripts/
```

## B站 / 抖音 登录态

Chrome 装扩展「Get cookies.txt LOCALLY」，在 B站 / 抖音 登录后导出 `cookies.txt`，
放到项目根目录 `cookies.txt`，后端会自动加载。

## 性能提醒

- Apple Silicon：先选 `small`，长视频或批量主页建议限制日期区间
- `medium` 更准但明显更慢，`large` 只建议少量短视频验收时使用
- 抓到字幕的视频几乎秒出（不走转写）
- 后端默认同时最多跑 2 个任务；长视频并发会吃满 CPU

## 目录

```
app/
  server.py          后端（FastAPI）：批量任务、进度、运行日志、打包下载
  transcribe_core.py 提取核心：字幕优先 → GPU/CPU Whisper 三层回落；抖音批量
  static/            前端页面（进度条、运行日志面板）
scripts/
  setup.sh  run.sh   macOS 安装 / 启动
  transcribe.sh      命令行批量版
  srt2txt.py         字幕清洗
transcripts/         输出的文字稿
```
