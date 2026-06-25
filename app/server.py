"""FastAPI 后端：接收视频链接 -> 后台提取字幕/转写 -> 提供进度查询与下载。

运行： uvicorn app.server:app --host 127.0.0.1 --port 8000
（推荐用 scripts/run.sh 一键启动）
"""
from __future__ import annotations

import io
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .transcribe_core import extract, tools_status

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


def _process(batch_id: str, idx: int):
    with _lock:
        item = _BATCHES[batch_id]["items"][idx]
        item["state"] = "running"
        url = item["url"]
        model = _BATCHES[batch_id]["model"]

    def cb(stage: str, percent: float, message: str):
        with _lock:
            item.update(stage=stage, percent=round(percent, 1), message=message)

    try:
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
    items = [{"url": u, "state": "queued", "stage": "queued", "percent": 0.0,
              "message": "排队中…", "file": None, "error": None} for u in urls]
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
