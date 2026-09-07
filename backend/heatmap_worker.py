#!/usr/bin/env python3
"""CHEAT CLIP — Heatmap Worker (loopback companion service).

Why this exists
---------------
Cloud deployments (Vercel/serverless) cannot scrape YouTube retention heatmaps:
YouTube blocks datacenter IPs, and Supadata exposes no heatmap endpoint. But the
user's own device (phone/PC) sits on a residential connection where yt-dlp works.

This tiny service runs on 127.0.0.1:<port> on the user's device. When the web app
is opened from that same device, the page auto-detects the worker (a ~400ms probe)
and attaches real yt-dlp metadata + heatmap — fetched over the residential
connection — to every analysis it sends to the cloud backend. No tunnel, no
24/7 process on a server, no cost. Worker offline = the app falls back silently.

Run
---
    .venv/bin/python backend/heatmap_worker.py          # default 127.0.0.1:8765
    HEATMAP_WORKER_PORT=9000 .venv/bin/python backend/heatmap_worker.py

Endpoints
---------
    GET /health                       -> {"status": "ok", ...}
    GET /heatmap?video_id=<ID>        -> {video_id, title, duration, heatmap,
                                          is_live, live_status, source, cached}
Residential/ISP proxy (optional): PROXY_URL or WEBSHARE_PROXY env var.
"""

import os
import sys
import time
import threading

# Windows Python 3.14 compatibility hotfix (same as backend/main.py)
for flag in ('RTLD_LAZY', 'RTLD_NOW', 'RTLD_GLOBAL', 'RTLD_LOCAL',
             'RTLD_NODELETE', 'RTLD_NOLOAD', 'RTLD_DEEPBIND'):
    if not hasattr(os, flag):
        setattr(os, flag, 1)
if not hasattr(os, 'uname'):
    from collections import namedtuple
    _Uname = namedtuple('UnameResult', ['sysname', 'nodename', 'release', 'version', 'machine'])
    os.uname = lambda: _Uname('Windows', 'localhost', '10', '10.0', 'AMD64')

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="CHEAT CLIP Heatmap Worker", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # loopback-only service; CORS is irrelevant off-device
    allow_methods=["*"],
    allow_headers=["*"],
)

HOST = os.environ.get("HEATMAP_WORKER_HOST", "127.0.0.1")
PORT = int(os.environ.get("HEATMAP_WORKER_PORT", "8765"))
CACHE_TTL = int(os.environ.get("HEATMAP_WORKER_CACHE_TTL", "600"))

_cache = {}          # video_id -> (expires_at, payload)
_cache_lock = threading.Lock()


def _proxy_url() -> str:
    return (os.environ.get("PROXY_URL") or os.environ.get("WEBSHARE_PROXY") or "").strip() or None


def _extract(video_id: str) -> dict:
    """Fetch metadata + retention heatmap via yt-dlp (must run on a residential IP)."""
    import yt_dlp  # lazy import keeps cold start tiny for health probes

    url = f"https://www.youtube.com/watch?v={video_id}"
    proxy = _proxy_url()
    ydl_opts = {
        "skip_download": True,
        "youtube_include_dash_manifest": False,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "proxy": proxy,
        "socket_timeout": 12,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"YouTube extraction failed: {e}") from e

    if not info:
        raise HTTPException(status_code=502, detail="yt-dlp returned no info for this video.")

    heatmap = []
    for pt in (info.get("heatmap") or []):
        try:
            heatmap.append({
                "start_time": float(pt.get("start_time", 0.0)),
                "end_time": float(pt.get("end_time", 0.0)),
                "value": float(pt.get("value", 0.0)),
            })
        except (TypeError, ValueError, AttributeError):
            continue

    return {
        "video_id": video_id,
        "title": info.get("title") or "Unknown YouTube Video",
        "duration": float(info.get("duration") or 0.0),
        "heatmap": heatmap,
        "is_live": bool(info.get("is_live") or False),
        "live_status": info.get("live_status") or "not_live",
        "source": "yt-dlp",
        "cached": False,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "cheat-clip-heatmap-worker", "version": "1.0.0"}


@app.get("/heatmap")
def heatmap(video_id: str):
    """Return yt-dlp metadata + retention heatmap for a video, cached briefly."""
    video_id = (video_id or "").strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id is required.")

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(video_id)
        if hit and hit[0] > now:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload

    payload = _extract(video_id)
    with _cache_lock:
        _cache[video_id] = (now + CACHE_TTL, payload)
    return payload


if __name__ == "__main__":
    import uvicorn

    print(f"CHEAT CLIP heatmap worker on http://{HOST}:{PORT} (cache {CACHE_TTL}s)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
