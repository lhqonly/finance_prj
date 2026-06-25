"""字幕/转写核心逻辑：一个 URL -> 一份纯文字稿。

策略：
  1) 先尝试抓官方/AI字幕（B站常有，最快、最准、零转写）
  2) 抓不到字幕，降级用 Whisper 转写音频（抖音口播必走这条）

进度通过 progress_cb(stage:str, percent:float, message:str) 回调上报。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

ProgressCb = Callable[[str, float, str], None]

# vtt/srt 清洗用的正则
_TAG = re.compile(r"<[^>]+>")
# whisper verbose 输出形如：[00:01.000 --> 00:05.000]  文本
_TS = re.compile(r"-->\s*(\d+):(\d+)(?::(\d+))?[.,](\d+)")


def _noop(stage: str, percent: float, message: str) -> None:
    pass


def _run_info(url: str, cookies: Optional[str]) -> tuple[str, float]:
    """取标题和时长（秒）。失败给安全默认值。"""
    cmd = ["yt-dlp", "--no-warnings", "--print", "%(title)s|||%(duration)s"]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
        title, _, dur = out.partition("|||")
        title = title.strip() or "video"
        try:
            duration = float(dur.strip())
        except ValueError:
            duration = 0.0
        return title, duration
    except Exception:
        return "video", 0.0


def _clean_subtitle(path: Path) -> str:
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        if s.upper().startswith("WEBVTT") or s.startswith(("Kind:", "Language:")):
            continue
        s = _TAG.sub("", s).strip()
        if s:
            lines.append(s)
    # 相邻去重（AI 字幕常重复刷同一句）
    out: list[str] = []
    for s in lines:
        if not out or out[-1] != s:
            out.append(s)
    return "\n".join(out)


def _try_subtitles(url: str, work: Path, cookies: Optional[str], sublangs: str) -> Optional[str]:
    cmd = [
        "yt-dlp", "--write-subs", "--write-auto-subs",
        "--sub-langs", sublangs, "--convert-subs", "srt",
        "--skip-download", "-o", str(work / "sub"), url,
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    subprocess.run(cmd, capture_output=True, text=True)
    for ext in ("*.srt", "*.vtt"):
        hit = next(iter(work.glob(ext)), None)
        if hit:
            return _clean_subtitle(hit)
    return None


def _is_douyin(url: str) -> bool:
    return "douyin.com" in url


def _normalize_douyin(url: str) -> str:
    """把各种抖音链接归一成 f2 认的单作品链接。

    支持：主页带 modal_id=、/video/<id>、/note/<id>，以及一堆 query 参数。
    短链 v.douyin.com 原样返回（让 f2 自己跳转解析）。
    """
    if "v.douyin.com" in url:        # 分享短链，交给 f2 解析
        return url
    m = re.search(r"modal_id=(\d+)", url)        # 主页弹窗形式
    if not m:
        m = re.search(r"/(?:video|note)/(\d+)", url)  # 标准作品链接
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return url


def _cookie_header(cookies_path: Optional[str]) -> str:
    """把 netscape cookies.txt 转成 f2 要的 'k=v; k=v' 头字符串。"""
    if not cookies_path or not Path(cookies_path).exists():
        return ""
    out = []
    for line in Path(cookies_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.rstrip("\n")
        if not raw.strip():
            continue
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_"):]
        elif raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5].strip(), parts[6].strip()
        if name:
            out.append(f"{name}={value}")
    return "; ".join(out)


def _ffprobe_duration(media: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(media)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _f2_download(url: str, work: Path, cookie_header: str,
                 progress_cb: ProgressCb) -> tuple[str, Path]:
    """抖音：用 f2 下载单条视频，返回 (标题, mp4路径)。"""
    url = _normalize_douyin(url)
    progress_cb("downloading_audio", 5, "抖音：用 f2 下载视频…")
    dl = work / "dl"
    cmd = ["f2", "dy", "-M", "one", "-u", url, "-p", str(dl),
           "-m", "false", "-v", "false", "-d", "true"]
    if cookie_header:
        cmd += ["-k", cookie_header]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    mp4 = next(iter(dl.rglob("*.mp4")), None)
    if not mp4:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        hint = " / ".join(t.strip() for t in tail)[:300]
        raise RuntimeError(f"抖音下载失败（f2 没拿到视频）。可能原因：链接不对/视频已删/cookies 失效。f2: {hint}")
    # 标题取 desc.txt 首行，没有就用文件名
    desc = next(iter(dl.rglob("*_desc.txt")), None)
    if desc:
        first = desc.read_text(encoding="utf-8", errors="ignore").splitlines()
        title = (first[0].strip() if first else "") or mp4.stem
    else:
        title = mp4.stem
    return title, mp4


def _whisper_audio(url: str, work: Path, cookies: Optional[str], model: str,
                   duration: float, progress_cb: ProgressCb) -> str:
    """B站/通用：yt-dlp 抽音频再转写。"""
    progress_cb("downloading_audio", 5, "没字幕，正在下载音频…")
    dl = ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(work / "audio.%(ext)s"), url]
    if cookies:
        dl += ["--cookies", cookies]
    subprocess.run(dl, capture_output=True, text=True)
    audio = next(iter(work.glob("audio.*")), None)
    if not audio:
        raise RuntimeError("音频下载失败")
    return _whisper_media(audio, work, model, duration, progress_cb)


def _whisper_media(media: Path, work: Path, model: str,
                   duration: float, progress_cb: ProgressCb) -> str:
    """对本地音/视频文件跑 whisper，解析 verbose 时间戳估算进度。"""
    if duration <= 0:
        duration = _ffprobe_duration(media)
    progress_cb("transcribing", 10, f"Whisper({model}) 转写中…（CPU 较慢，请耐心）")
    cmd = [
        "whisper", str(media), "--language", "zh", "--model", model,
        "--output_format", "txt", "--output_dir", str(work), "--verbose", "True",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        m = _TS.search(line)
        if m and duration > 0:
            h, mi, se, ms = m.group(1), m.group(2), m.group(3), m.group(4)
            if se is None:  # 格式是 mm:ss.ms
                cur = int(h) * 60 + int(mi) + float(f"0.{ms}")
            else:           # 格式是 hh:mm:ss.ms
                cur = int(h) * 3600 + int(mi) * 60 + int(se) + float(f"0.{ms}")
            pct = max(10.0, min(99.0, 10 + 89 * cur / duration))
            progress_cb("transcribing", pct, f"转写中… {cur/60:.1f}/{duration/60:.1f} 分钟")
    proc.wait()
    txt = next(iter(work.glob("*.txt")), None)
    if not txt:
        raise RuntimeError("转写失败（whisper 没有产出文本，检查 ffmpeg 是否安装）")
    return txt.read_text(encoding="utf-8", errors="ignore")


def extract(url: str, outdir: Path, *, cookies: Optional[str] = None,
            model: str = "medium",
            sublangs: str = "zh.*,ai.*,Chinese,zh-Hans,zh-CN",
            progress_cb: ProgressCb = _noop) -> Path:
    """提取单个 URL 的文字稿，写入 outdir，返回 txt 路径。"""
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tr_"))
    try:
        if _is_douyin(url):
            # 抖音：yt-dlp 抓不动，走 f2 下载 + whisper 转写
            progress_cb("fetching_info", 2, "抖音视频，准备用 f2 下载…")
            title, mp4 = _f2_download(url, work, _cookie_header(cookies), progress_cb)
            safe = re.sub(r'[/\\:*?"<>|]', "_", title)[:60].strip() or "video"
            method = f"Whisper({model})"
            body = _whisper_media(mp4, work, model, 0.0, progress_cb)
        else:
            # B站/通用：先抓字幕，没有再 whisper
            progress_cb("fetching_info", 2, "获取视频信息…")
            title, duration = _run_info(url, cookies)
            safe = re.sub(r'[/\\:*?"<>|]', "_", title)[:60].strip() or "video"

            progress_cb("trying_subs", 8, "尝试抓取字幕…")
            method = "官方/AI字幕"
            body = _try_subtitles(url, work, cookies, sublangs)
            if body is None:
                method = f"Whisper({model})"
                body = _whisper_audio(url, work, cookies, model, duration, progress_cb)

        header = f"# 标题: {title}\n# 来源: {url}\n# 方式: {method}\n\n"
        final = outdir / f"{safe}.txt"
        n = 1
        while final.exists():  # 重名加序号
            final = outdir / f"{safe}_{n}.txt"
            n += 1
        final.write_text(header + body.strip() + "\n", encoding="utf-8")
        progress_cb("done", 100, f"完成（{method}）")
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tools_status() -> dict:
    """检查依赖是否就绪，给前端提示。"""
    return {
        "yt-dlp": shutil.which("yt-dlp") is not None,
        "whisper": shutil.which("whisper") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "f2": shutil.which("f2") is not None,
    }
