"""FastAPI 后端：接收视频链接 -> 后台提取字幕/转写 -> 提供进度查询与下载。

运行： uvicorn app.server:app --host 127.0.0.1 --port 8000
（推荐用 scripts/run.sh 一键启动）
"""
from __future__ import annotations

import io
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .transcribe_core import (
    extract, tools_status, download_douyin_user, transcribe_file,
    _cookie_header, _is_douyin_user,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "transcripts"
OUT.mkdir(exist_ok=True)
COOKIES = (ROOT / "cookies.txt") if (ROOT / "cookies.txt").exists() else None

app = FastAPI(title="字幕/转写提取器")

# 同时最多跑 2 个（CPU 跑 whisper 很吃资源，别开太多）
_pool = ThreadPoolExecutor(max_workers=2)
_lock = threading.Lock()
# batch_id -> {"items": [item, ...]}；item = {url,state,stage,percent,message,file,error}
_BATCHES: dict[str, dict] = {}


class BatchReq(BaseModel):
    urls: list[str]
    model: str = "medium"
    # 抖音用户主页批量抓取的日期区间：'YYYY-MM-DD|YYYY-MM-DD' 或 'all'
    interval: Optional[str] = None


def _expand_douyin_user(batch_id: str, item: dict, url: str,
                        interval: str, cb) -> None:
    """抖音用户主页 item：先 f2 抓该号区间内全部作品，再把每条作品
    作为新的 local item 追加进同一批次、提交转写。自身标记为「已展开」。"""
    # 不能用自动清理的临时目录：子任务转写时还要读这些 mp4，下载完不能删
    work = Path(tempfile.mkdtemp(prefix="dyuser_"))
    cookie = _cookie_header(str(COOKIES) if COOKIES else None)
    results = download_douyin_user(url, work, cookie, interval, cb)

    with _lock:
        b = _BATCHES[batch_id]
        base = len(b["items"])
        for title, mp4 in results:
            b["items"].append({
                "url": url, "kind": "local", "mp4": str(mp4), "label": title,
                "state": "queued", "stage": "queued", "percent": 0.0,
                "message": "排队中…", "file": None, "error": None,
            })
        item.update(state="done", percent=100.0, stage="done", file=None,
                    message=f"已展开 {len(results)} 条作品，逐条转写中…")
    for j in range(len(results)):
        _pool.submit(_process, batch_id, base + j)


def _process(batch_id: str, idx: int):
    with _lock:
        item = _BATCHES[batch_id]["items"][idx]
        item["state"] = "running"
        kind = item.get("kind", "single")
        url = item["url"]
        model = _BATCHES[batch_id]["model"]

    def cb(stage: str, percent: float, message: str):
        with _lock:
            item.update(stage=stage, percent=round(percent, 1), message=message)

    try:
        if kind == "douyin_user":
            _expand_douyin_user(batch_id, item, url, item.get("interval") or "all", cb)
        elif kind == "local":
            mp4 = Path(item["mp4"])
            path = transcribe_file(mp4, item.get("label") or mp4.stem, url, OUT,
                                   model=model, progress_cb=cb)
            with _lock:
                item.update(state="done", percent=100.0, stage="done",
                            message="完成", file=path.name)
            mp4.unlink(missing_ok=True)  # 转写完删源视频，省磁盘
        else:
            path = extract(url, OUT, cookies=str(COOKIES) if COOKIES else None,
                           model=model, progress_cb=cb)
            with _lock:
                item.update(state="done", percent=100.0, stage="done",
                            message="完成", file=path.name)
    except Exception as e:  # noqa: BLE001
        with _lock:
            item.update(state="error", message=str(e), error=str(e))


@app.get("/api/status")
def status():
    return tools_status() | {"cookies": COOKIES is not None}


@app.post("/api/batches")
def create_batch(req: BatchReq):
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "没有有效链接")
    batch_id = uuid.uuid4().hex[:12]
    interval = (req.interval or "all").strip() or "all"
    items = []
    for u in urls:
        if _is_douyin_user(u):
            # 用户主页：后台展开成该号区间内的多条作品
            items.append({"url": u, "kind": "douyin_user", "interval": interval,
                          "label": None, "state": "queued", "stage": "queued",
                          "percent": 0.0, "message": "排队中…（待展开作品列表）",
                          "file": None, "error": None})
        else:
            items.append({"url": u, "kind": "single", "label": None,
                          "state": "queued", "stage": "queued", "percent": 0.0,
                          "message": "排队中…", "file": None, "error": None})
    _BATCHES[batch_id] = {"items": items, "model": req.model}
    for i in range(len(items)):
        _pool.submit(_process, batch_id, i)
    return {"batch_id": batch_id, "count": len(items)}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    b = _BATCHES.get(batch_id)
    if not b:
        raise HTTPException(404, "找不到该批次")
    with _lock:
        items = [dict(it) for it in b["items"]]
    done = sum(1 for it in items if it["state"] == "done")
    err = sum(1 for it in items if it["state"] == "error")
    overall = round(sum(it["percent"] for it in items) / len(items), 1)
    return {"items": items, "done": done, "error": err,
            "total": len(items), "overall": overall}


@app.get("/api/download/{name}")
def download(name: str):
    # 防目录穿越
    target = (OUT / name).resolve()
    if not str(target).startswith(str(OUT.resolve())) or not target.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, media_type="text/plain; charset=utf-8", filename=name)


@app.get("/api/batches/{batch_id}/zip")
def download_zip(batch_id: str):
    b = _BATCHES.get(batch_id)
    if not b:
        raise HTTPException(404, "找不到该批次")
    files = [it["file"] for it in b["items"] if it.get("file")]
    if not files:
        raise HTTPException(404, "还没有可下载的文件")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in files:
            p = OUT / name
            if p.exists():
                z.write(p, arcname=name)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="transcripts_{batch_id}.zip"'},
    )


# 前端静态页面挂在根路径
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")
