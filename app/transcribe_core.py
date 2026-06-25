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
import threading
from pathlib import Path
from typing import Callable, Optional

ProgressCb = Callable[[str, float, str], None]

# 项目根；OpenVINO 转换后的 whisper 模型放在 .ovmodels/whisper-<size>-fp16-ov/
_ROOT = Path(__file__).resolve().parent.parent
_OVMODELS = _ROOT / ".ovmodels"

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


def _is_douyin_user(url: str) -> bool:
    """抖音用户主页链接（要批量抓该号全部/区间内作品）。

    主页形如 douyin.com/user/<sec_uid>。注意排除「主页弹窗看单条」的
    douyin.com/user/<sec_uid>?modal_id=<id> —— 那其实是单作品，不是要批量。
    """
    return "douyin.com/user/" in url and "modal_id=" not in url


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


def _safe_name(title: str) -> str:
    """标题转成安全文件名（去非法字符、限长）。"""
    return re.sub(r'[/\\:*?"<>|]', "_", title)[:60].strip() or "video"


def _write_transcript(outdir: Path, title: str, source: str,
                      method: str, body: str) -> Path:
    """把文字稿落盘，重名自动加序号，返回最终路径。"""
    header = f"# 标题: {title}\n# 来源: {source}\n# 方式: {method}\n\n"
    safe = _safe_name(title)
    final = outdir / f"{safe}.txt"
    n = 1
    while final.exists():
        final = outdir / f"{safe}_{n}.txt"
        n += 1
    final.write_text(header + body.strip() + "\n", encoding="utf-8")
    return final


def _f2_download(url: str, work: Path, cookie_header: str,
                 progress_cb: ProgressCb) -> tuple[str, Path]:
    """抖音：用 f2 下载单条视频，返回 (标题, mp4路径)。"""
    url = _normalize_douyin(url)
    progress_cb("downloading_audio", 5, "抖音：用 f2 下载视频…")
    dl = work / "dl"
    cmd = ["f2", "dy", "-M", "one", "-u", url, "-p", str(dl),
           "-m", "false", "-v", "false", "-d", "true", "-r", "5", "-e", "60"]
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


# faster-whisper 的模型按 size 缓存，避免批量任务里反复加载（加载一次 1~2GB 很贵）
_FW_CACHE: dict = {}


def _to_simplified(text: str) -> str:
    """统一转简体。whisper 中文输出常蹦繁体，简体用户看着别扭，强制归一。
    没装 zhconv 就原样返回（不硬依赖）。"""
    try:
        import zhconv
        return zhconv.convert(text, "zh-cn")
    except Exception:
        return text


# ---- OpenVINO + Intel GPU 引擎（核显/Arc 加速；实测 small 比 CPU 快约 2.6x）----
_OV_DEVICES = None
_OV_PIPE_CACHE: dict = {}
# 单块 iGPU：并发 generate 既不安全也无意义，GPU 推理一律串行
_OV_LOCK = threading.Lock()


def _ov_devices() -> list:
    """OpenVINO 可见设备，结果缓存。没装 openvino 就返回空列表。"""
    global _OV_DEVICES
    if _OV_DEVICES is None:
        try:
            import openvino as ov
            _OV_DEVICES = list(ov.Core().available_devices)
        except Exception:
            _OV_DEVICES = []
    return _OV_DEVICES


def _ov_model_dir(model: str) -> Optional[Path]:
    """该尺寸的 OpenVINO 模型目录（encoder/decoder 权重齐全才算数），没有返回 None。"""
    d = _OVMODELS / f"whisper-{model}-fp16-ov"
    if (d / "openvino_encoder_model.bin").exists() and \
       (d / "openvino_decoder_model.bin").exists():
        return d
    return None


def _load_audio_16k(media: Path):
    """ffmpeg 把任意音/视频解码成 16kHz 单声道 float32（OpenVINO whisper 要这个格式）。"""
    import numpy as np
    cmd = ["ffmpeg", "-nostdin", "-i", str(media),
           "-f", "f32le", "-ac", "1", "-ar", "16000", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("ffmpeg 没解出音频")
    return audio


def _whisper_media_openvino(media: Path, model: str,
                            duration: float, progress_cb: ProgressCb) -> str:
    """OpenVINO + Intel GPU 转写。没装 / 没 GPU / 没该尺寸模型 / 空结果都抛异常，
    交由上层 _whisper_media 回落到 CPU。"""
    import openvino_genai as og  # 没装就抛 ImportError 给上层
    mdir = _ov_model_dir(model)
    if mdir is None:
        raise FileNotFoundError(f"无 {model} 的 OpenVINO 模型（.ovmodels/）")
    if "GPU" not in _ov_devices():
        raise RuntimeError("OpenVINO 未发现 GPU 设备")

    # 解码不占 GPU，放锁外，可与其他任务的解码/下载并行
    audio = _load_audio_16k(media)
    mins = duration / 60 if duration > 0 else 0

    with _OV_LOCK:  # GPU 推理串行化（单 iGPU）
        pipe = _OV_PIPE_CACHE.get(model)
        if pipe is None:
            progress_cb("transcribing", 6, f"加载 OpenVINO 模型到 GPU（{model}）…")
            pipe = og.WhisperPipeline(str(mdir), "GPU")
            _OV_PIPE_CACHE[model] = pipe  # 缓存：批量逐条转写时只加载一次
        progress_cb("transcribing", 12,
                    f"⚡ GPU 转写中…（约 {mins:.1f} 分钟音频，核显比 CPU 快约 2-3 倍）")
        res = pipe.generate(audio, language="<|zh|>", task="transcribe")

    text = res.texts[0] if getattr(res, "texts", None) else str(res)
    if not text.strip():
        raise RuntimeError("OpenVINO 没产出文本")
    return text


def _whisper_media(media: Path, work: Path, model: str,
                   duration: float, progress_cb: ProgressCb) -> str:
    """对本地音/视频文件转写。三层引擎，从快到稳自动回落：
      1) OpenVINO + Intel GPU（最快，需有 GPU 且 .ovmodels 里有对应尺寸模型）
      2) faster-whisper（CPU int8）
      3) 旧 openai-whisper CLI（最稳的兜底）
    输出统一转简体。"""
    if duration <= 0:
        duration = _ffprobe_duration(media)

    # 1) OpenVINO GPU 优先
    try:
        return _to_simplified(_whisper_media_openvino(media, model, duration, progress_cb))
    except (ImportError, FileNotFoundError):
        pass  # 没装 openvino / 没该尺寸模型：静默降级到 CPU
    except Exception as e:
        progress_cb("transcribing", 8,
                    f"GPU 转写不可用（{type(e).__name__}），改用 CPU faster-whisper…")

    # 2) faster-whisper CPU
    try:
        text = _whisper_media_faster(media, model, duration, progress_cb)
    except ImportError:
        text = _whisper_media_openai(media, work, model, duration, progress_cb)
    except Exception as e:
        progress_cb("transcribing", 10,
                    f"faster-whisper 出错（{type(e).__name__}），回落到旧 whisper…")
        text = _whisper_media_openai(media, work, model, duration, progress_cb)
    return _to_simplified(text)


def _whisper_media_faster(media: Path, model: str,
                          duration: float, progress_cb: ProgressCb) -> str:
    """faster-whisper(CTranslate2 + int8 量化 + VAD 静音过滤)。"""
    from faster_whisper import WhisperModel  # 延迟导入：没装就抛 ImportError 给上层兜底

    wm = _FW_CACHE.get(model)
    if wm is None:
        progress_cb("transcribing", 6,
                    f"加载模型 {model}（首次会下载，约 1~2GB，仅一次）…")
        # device=cpu + int8：无 GPU 时最快的组合；cpu_threads=0 让 CT2 自适应
        wm = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=0)
        _FW_CACHE[model] = wm

    progress_cb("transcribing", 10, f"faster-whisper({model}) 转写中…")
    segments, info = wm.transcribe(
        str(media), language="zh", beam_size=5,
        vad_filter=True,  # 跳过静音段，口播视频提速明显
    )
    if duration <= 0:
        duration = getattr(info, "duration", 0.0) or 0.0

    parts: list[str] = []
    for seg in segments:  # segments 是惰性生成器，迭代时才真正在转写
        text = seg.text.strip()
        if text:
            parts.append(text)
        if duration > 0:
            cur = seg.end
            pct = max(10.0, min(99.0, 10 + 89 * cur / duration))
            progress_cb("transcribing", pct,
                        f"转写中… {cur/60:.1f}/{duration/60:.1f} 分钟")
    if not parts:
        raise RuntimeError("faster-whisper 没产出文本")
    return "\n".join(parts)


def _whisper_media_openai(media: Path, work: Path, model: str,
                          duration: float, progress_cb: ProgressCb) -> str:
    """旧实现：openai-whisper CLI，解析 verbose 时间戳估算进度。作兜底。"""
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
            method = f"Whisper({model})"
            body = _whisper_media(mp4, work, model, 0.0, progress_cb)
        else:
            # B站/通用：先抓字幕，没有再 whisper
            progress_cb("fetching_info", 2, "获取视频信息…")
            title, duration = _run_info(url, cookies)

            progress_cb("trying_subs", 8, "尝试抓取字幕…")
            method = "官方/AI字幕"
            body = _try_subtitles(url, work, cookies, sublangs)
            if body is None:
                method = f"Whisper({model})"
                body = _whisper_audio(url, work, cookies, model, duration, progress_cb)

        final = _write_transcript(outdir, title, url, method, body)
        progress_cb("done", 100, f"完成（{method}）")
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


def download_douyin_user(url: str, work: Path, cookie_header: str,
                         interval: str, progress_cb: ProgressCb = _noop
                         ) -> list[tuple[str, Path]]:
    """抖音用户主页：用 f2 -M post 抓该号在日期区间内的作品。

    interval 形如 'YYYY-MM-DD|YYYY-MM-DD'，传 'all' 抓全部。
    只下视频不转写，返回 [(标题, mp4路径), ...]，按创建时间(文件名)排序。
    """
    progress_cb("fetching_info", 3, f"抖音主页：f2 抓取作品列表（{interval}）…")
    dl = work / "user"
    cmd = ["f2", "dy", "-M", "post", "-u", url, "-p", str(dl),
           "-m", "false", "-v", "false", "-d", "true", "-i", interval,
           # 重试5次+超时60s：实测能把 f2 批量下载的 0 字节空文件率从一大半降到 0
           "-r", "5", "-e", "60"]
    if cookie_header:
        cmd += ["-k", cookie_header]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    results: list[tuple[str, Path]] = []
    skipped = 0
    for mp4 in sorted(dl.rglob("*.mp4")):
        if mp4.stat().st_size == 0:
            skipped += 1  # f2 偶发仍会下成 0 字节空壳，跳过，别塞给 whisper 假失败
            continue
        # f2 命名为 "{YYYY-MM-DD HH-MM-SS}_{标题}_video.mp4"，直接从文件名取标题
        # （每条各不相同；不要用 glob 找 desc，否则所有作品会取到同一个文件）
        stem = mp4.stem
        prefix = stem[:-6] if stem.endswith("_video") else stem
        m = re.match(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}_(.+)$", prefix)
        title = (m.group(1) if m else prefix).strip() or mp4.stem
        title = re.split(r"\.{3,}", title)[0].strip() or title  # 去 f2 长名截断的重复尾
        results.append((title, mp4))
    if skipped:
        progress_cb("fetching_info", 4, f"（{skipped} 条下载失败的空文件已跳过）")

    if not results:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        hint = " / ".join(t.strip() for t in tail)[:300]
        raise RuntimeError(
            "没抓到作品（f2 -M post 返回空）。可能原因："
            f"主页链接不对/该区间内无作品/cookies 失效。f2: {hint}")
    return results


def transcribe_file(media: Path, title: str, source: str, outdir: Path, *,
                    model: str = "medium",
                    progress_cb: ProgressCb = _noop) -> Path:
    """对已下载到本地的音/视频文件直接 Whisper 转写并落稿。
    批量抖音展开后，每条视频走这里（跳过下载阶段）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tr_"))
    try:
        method = f"Whisper({model})"
        body = _whisper_media(media, work, model, 0.0, progress_cb)
        final = _write_transcript(outdir, title, source, method, body)
        progress_cb("done", 100, f"完成（{method}）")
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tools_status() -> dict:
    """检查依赖是否就绪，给前端提示。"""
    import importlib.util
    return {
        "yt-dlp": shutil.which("yt-dlp") is not None,
        "whisper": shutil.which("whisper") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "f2": shutil.which("f2") is not None,
        # faster-whisper 是 Python 库不是命令行，用 find_spec 检测；装了就走加速
        "faster-whisper": importlib.util.find_spec("faster_whisper") is not None,
        # OpenVINO 能看到 Intel GPU，且至少有一个尺寸的 OV 模型，才算 GPU 加速可用
        "openvino-gpu": ("GPU" in _ov_devices()) and any(
            _ov_model_dir(s) for s in ("small", "medium", "large")),
    }
