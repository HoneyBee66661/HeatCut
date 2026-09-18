#!/usr/bin/env python3
"""HeatCut Device Worker — heatmap + clip EXPORT companion (shareable).

WHY THIS FILE EXISTS
--------------------
HeatCut's web app can live in the cloud (Vercel), but YouTube blocks
datacenter IPs, so the app itself cannot download/export clips. This
worker runs on YOUR OWN device (phone/PC — a residential connection,
where yt-dlp works fine) and gives the web app two superpowers:

    GET /health                  probe (the web page auto-detects it)
    GET /heatmap?video_id=<ID>   real viewer-retention heatmap + metadata
    GET /export?video_id=<ID>&start_time=<s>&end_time=<s>[&title=..]
                                 download a clip AS a file (your browser
                                 saves it directly, ready for CapCut)

The web page (even hosted on https://vercel.app) can reach this worker
because browsers treat http://127.0.0.1 as "potentially trustworthy" —
no tunnel, no account, no server. Worker offline = app still works,
just without heatmap/export.

RUN (on your device, after installing Python 3.10+)
--------------------------------------------------
    pip install fastapi "uvicorn[standard]" yt-dlp requests
    # ffmpeg must be installed too (yt-dlp uses it to cut clips):
    #   Windows: winget install ffmpeg      macOS: brew install ffmpeg
    #   Linux:   sudo apt install ffmpeg
    python heatcut_worker.py
    # -> worker on http://127.0.0.1:8765 — then open your HeatCut web app
    #    in a browser ON THIS SAME DEVICE.

OPTIONAL
--------
    HEATMAP_WORKER_PORT=9000            change port (default 8765)
    YT_COOKIES_FILE=/path/cookies.txt   Netscape cookies (fixes bot-checks;
                                        export from a browser logged into
                                        YouTube). Default: ./yt_cookies.txt
    PROXY_URL=http://user:pass@host:port  optional residential/ISP proxy

Export semantics match the server backend exactly: every clip is padded
with 2s before + 2s after the requested window, cut with stream-copy
(no re-encode — bitstream-identical quality), via the DASH fragment
miner when possible (only the needed seconds are downloaded — great for
long videos) with a full-download fallback.
"""

import base64
import json
import math
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

# Windows Python 3.14 compatibility hotfix (same as backend/main.py)
for flag in ('RTLD_LAZY', 'RTLD_NOW', 'RTLD_GLOBAL', 'RTLD_LOCAL',
             'RTLD_NODELETE', 'RTLD_NOLOAD', 'RTLD_DEEPBIND'):
    if not hasattr(os, flag):
        setattr(os, flag, 1)
if not hasattr(os, 'uname'):
    from collections import namedtuple
    _Uname = namedtuple('UnameResult', ['sysname', 'nodename', 'release', 'version', 'machine'])
    os.uname = lambda: _Uname('Windows', 'localhost', '10', '10.0', 'AMD64')

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(title="HeatCut Device Worker", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # loopback-only service; CORS is irrelevant off-device
    allow_methods=["*"],
    allow_headers=["*"],
)

HOST = os.environ.get("HEATMAP_WORKER_HOST", "0.0.0.0")
PORT = int(os.environ.get("HEATMAP_WORKER_PORT", "8765"))
CACHE_TTL = int(os.environ.get("HEATMAP_WORKER_CACHE_TTL", "600"))

# ---------------------------------------------------------------- Sharing guards
# This worker can serve EXPORTS on the owner's residential IP, cookies and
# bandwidth, so anything that exposes it beyond loopback (a tunnel, a LAN, a
# VPS) must carry these guards:
#   HEATMAP_WORKER_TOKEN          shared secret; when set, /heatmap and /export
#                                 need header `X-Heatcut-Token` (or ?token=).
#                                 /health stays open — it is the reachability
#                                 probe used by the web app.
#   HEATCUT_WORKER_RATE_LIMIT     max exports per IP per hour (0 = off)
#   HEATCUT_WORKER_HEATMAP_LIMIT  max heatmap fetches per IP per hour (0 = off)
#   HEATCUT_WORKER_MAX_CONCURRENCY  parallel exports allowed (keep 1-2: each one
#                                 spawns yt-dlp/ffmpeg and eats bandwidth)
# Note: a token baked into a public frontend build is only a soft barrier (anyone
# who can read the JS bundle can read it). It stops scanners/bots and accidental
# use; treat the app URL itself as part of the secret.
WORKER_TOKEN = (os.environ.get("HEATMAP_WORKER_TOKEN") or "").strip() or None
EXPORT_RATE_LIMIT = int(os.environ.get("HEATCUT_WORKER_RATE_LIMIT", "20"))
HEATMAP_RATE_LIMIT = int(os.environ.get("HEATCUT_WORKER_HEATMAP_LIMIT", "60"))
MAX_CONCURRENT_EXPORTS = max(1, int(os.environ.get("HEATCUT_WORKER_MAX_CONCURRENCY", "1")))
EXPORT_BUSY_WAIT = float(os.environ.get("HEATCUT_WORKER_BUSY_WAIT", "25"))

_rate_lock = threading.Lock()
_rate_hits = {}  # "kind:ip" -> [timestamps]
_export_slots = threading.Semaphore(MAX_CONCURRENT_EXPORTS)


def _client_ip(request: Request) -> str:
    """Caller IP for rate limits.

    Behind the Cloudflare tunnel the edge sets `cf-connecting-ip` (not
    spoofable); Vercel puts the real client first in `x-forwarded-for`. Both
    headers are client-settable in principle — treat these limits as
    anti-casual-abuse, not as authentication (the worker token is the gate).
    """
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return (request.client.host if request.client else "") or "unknown"


def _rate_limited(key: str, limit: int, window: float = 3600.0) -> bool:
    """Record a hit for key; True when it already used up `limit` hits/window."""
    if limit <= 0:
        return False
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return True
        hits.append(now)
        _rate_hits[key] = hits
        if len(_rate_hits) > 1000:  # keep the ledger bounded
            for k in [k for k, v in _rate_hits.items() if not v]:
                _rate_hits.pop(k, None)
    return False


def _require_token(request: Request) -> None:
    """401 unless the caller presents HEATMAP_WORKER_TOKEN (no-op when unset)."""
    if not WORKER_TOKEN:
        return
    sent = (request.headers.get("x-heatcut-token") or request.query_params.get("token") or "").strip()
    if not secrets.compare_digest(sent, WORKER_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized: worker token missing or wrong.")

# Optional Drive mirror ("raw clip"): every exported clip is uploaded here.
DRIVE_FOLDER_ID = os.environ.get("HEATMAP_WORKER_DRIVE_FOLDER", "").strip() or None
DRIVE_OAUTH_JSON = (os.environ.get("DRIVE_OAUTH_JSON")
                    or os.path.join(os.path.expanduser("~/.hermes"), "drive-oauth.json"))
PAD_PRE = 2.0
PAD_POST = 2.0
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_cache = {}
_cache_lock = threading.Lock()

# ----------------------------------------------------------------
# Shared export tmp workspace + liveness-aware TTL cleanup
# Mirrors backend/main.py; both processes use the same HEATCUT_EXPORT_TMP dir.
#
# NOTE: a job dir's own mtime only moves when entries are added/removed, so a
# long job writing ONE file (yt-dlp .part / ffmpeg output) used to look idle and
# got pruned mid-write. Every job now writes a heartbeat marker
# (.heatcut-job.json) and the cleaner skips dirs whose marker names a LIVE pid
# on this host — which also stops the backend and the worker from deleting each
# other's in-flight job (they share this root but each runs its own cleaner).
# ----------------------------------------------------------------
EXPORT_TMP_ROOT_REQUESTED = os.environ.get("HEATCUT_EXPORT_TMP",
                                           os.path.join(os.path.expanduser("~"), ".heatcut", "export_tmp"))
EXPORT_TTL_SECONDS = int(os.environ.get("HEATCUT_EXPORT_TTL", "300"))  # idle/crashed jobs only
EXPORT_CLEANUP_INTERVAL = 30
EXPORT_HEARTBEAT_SECONDS = 5
EXPORT_JOB_MARKER = ".heatcut-job.json"
HOSTNAME = socket.gethostname()

_export_jobs = {}  # job dir -> (stop_event, heartbeat thread)
_export_jobs_lock = threading.Lock()


def _usable_tmp_root(path: str) -> bool:
    """True when path can hold a job dir (create + write probe + clean up)."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".probe_{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve_export_tmp_root() -> str:
    """Configured root if usable, else the platform temp dir (read-only hosts)."""
    if _usable_tmp_root(EXPORT_TMP_ROOT_REQUESTED):
        return EXPORT_TMP_ROOT_REQUESTED
    fallback = os.path.join(tempfile.gettempdir(), "heatcut_export_tmp")
    if fallback != EXPORT_TMP_ROOT_REQUESTED and _usable_tmp_root(fallback):
        print(f"[worker] WARNING: {EXPORT_TMP_ROOT_REQUESTED} not writable — using {fallback}", flush=True)
        return fallback
    print(f"[worker] WARNING: no writable export tmp root ({EXPORT_TMP_ROOT_REQUESTED})", flush=True)
    return EXPORT_TMP_ROOT_REQUESTED


EXPORT_TMP_ROOT = _resolve_export_tmp_root()
EXPORT_TMP_OK = _usable_tmp_root(EXPORT_TMP_ROOT)
print(f"[worker] export tmp root: {EXPORT_TMP_ROOT}  ttl={EXPORT_TTL_SECONDS}s  writable={EXPORT_TMP_OK}", flush=True)


def _pid_alive(pid: int) -> bool:
    """Best-effort "is this pid still running" check (POSIX + Windows)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # os.kill(pid, 0) is DESTRUCTIVE on Windows (TerminateProcess) — never use it.
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True  # conservative: never prune what we cannot inspect
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _tree_latest_mtime(path: str) -> float:
    """Newest mtime anywhere under path (the dir itself + every nested entry)."""
    latest = os.path.getmtime(path)
    for root, dirs, files in os.walk(path):
        for name in list(dirs) + list(files):
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
    return latest


def _job_in_flight(path: str) -> bool:
    """True when path carries a heartbeat marker from a live pid on this host.

    Reads `.heatcut-job.json` AND its `.tmp` sibling: the heartbeat rewrites the
    marker via os.replace, so there is a microsecond window where only the tmp
    file exists. A marker file that is fresh but unparsable also counts as in
    flight — a running job must never lose protection because a read landed
    mid-write.
    """
    meta = None
    fresh_marker = False
    for name in (EXPORT_JOB_MARKER, f"{EXPORT_JOB_MARKER}.tmp"):
        p = os.path.join(path, name)
        try:
            if time.time() - os.stat(p).st_mtime <= EXPORT_HEARTBEAT_SECONDS * 3:
                fresh_marker = True
        except OSError:
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = None
        if isinstance(meta, dict):
            break
    if not isinstance(meta, dict):
        return fresh_marker  # marker present but mid-write / legacy layout
    if meta.get("host") != HOSTNAME:
        return False
    try:
        return _pid_alive(int(meta.get("pid") or 0))
    except (TypeError, ValueError):
        return fresh_marker


def _cleanup_expired_export_tmp():
    """Delete job dirs that are neither in flight nor recently touched."""
    now = time.time()
    deleted = 0
    try:
        entries = os.listdir(EXPORT_TMP_ROOT)
    except OSError:
        return 0
    for entry in entries:
        path = os.path.join(EXPORT_TMP_ROOT, entry)
        try:
            if not os.path.isdir(path):
                continue
            if _job_in_flight(path):
                continue  # an export is writing here right now
            if now - _tree_latest_mtime(path) > EXPORT_TTL_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
                deleted += 1
        except OSError:
            continue
    return deleted


def _start_export_tmp_cleanup():
    def _run():
        while True:
            try:
                n = _cleanup_expired_export_tmp()
                if n:
                    print(f"[worker] export tmp cleanup: deleted {n} expired dir(s)", flush=True)
            except Exception:
                print("[worker] export tmp cleanup error", flush=True)
            time.sleep(EXPORT_CLEANUP_INTERVAL)

    t = threading.Thread(target=_run, name="export-tmp-cleanup", daemon=True)
    t.start()
    print(f"[worker] export tmp cleanup thread started (ttl={EXPORT_TTL_SECONDS}s, interval={EXPORT_CLEANUP_INTERVAL}s)", flush=True)


def _write_job_marker(marker: str, started: float):
    """Atomically (re)write the job heartbeat marker."""
    payload = {"pid": os.getpid(), "host": HOSTNAME, "started": started, "heartbeat": time.time()}
    tmp = f"{marker}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, marker)
    except OSError:
        pass  # best-effort: the tree-mtime fallback still protects the dir


def _make_export_tmp_dir() -> str:
    """Create a per-job subdir under EXPORT_TMP_ROOT and start its heartbeat."""
    if not EXPORT_TMP_OK:
        raise RuntimeError(f"export workspace not writable: {EXPORT_TMP_ROOT}")
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    sub = f"{ts}_{uuid.uuid4().hex[:8]}"
    path = os.path.join(EXPORT_TMP_ROOT, sub)
    os.makedirs(path, exist_ok=True)
    marker = os.path.join(path, EXPORT_JOB_MARKER)
    started = time.time()
    _write_job_marker(marker, started)  # marker exists BEFORE the job can block
    stop = threading.Event()

    def _beat():
        while not stop.wait(EXPORT_HEARTBEAT_SECONDS):
            _write_job_marker(marker, started)

    t = threading.Thread(target=_beat, name="export-job-heartbeat", daemon=True)
    t.start()
    with _export_jobs_lock:
        _export_jobs[path] = (stop, t)
    return path


def _clear_export_artifacts(path: str):
    """Drop a job's partial artifacts but KEEP its liveness marker.

    The marker is the only thing telling the TTL cleaner this dir is still in
    use — wiping it here leaves the dir protected by tree-mtime alone until the
    next heartbeat, so never delete it.
    """
    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        if name.startswith(EXPORT_JOB_MARKER):
            continue
        try:
            os.remove(os.path.join(path, name))
        except OSError:
            pass


def _finish_export_job(path: str):
    """Job done (success or failure): stop its heartbeat and drop the workspace."""
    with _export_jobs_lock:
        entry = _export_jobs.pop(path, None)
    if entry is not None:
        stop, t = entry
        stop.set()
        t.join(timeout=EXPORT_HEARTBEAT_SECONDS + 5)  # wait() is interruptible
    shutil.rmtree(path, ignore_errors=True)


def _new_export_tmp_dir_or_500() -> str:
    """Job workspace, or a clean JSON 500 — never a bare crash."""
    try:
        return _make_export_tmp_dir()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=(f"Export workspace unavailable on this device ({e}). Set "
                    "HEATCUT_EXPORT_TMP to a writable directory and retry."),
        ) from e


try:
    _start_export_tmp_cleanup()
except Exception:  # noqa: BLE001 — temp hygiene must never break startup
    print("[worker] WARNING: export tmp cleanup thread not started", flush=True)


def _proxy_url() -> str:
    return (os.environ.get("PROXY_URL") or os.environ.get("WEBSHARE_PROXY") or "").strip() or None


def _cookiefile() -> str:
    p = os.environ.get("YT_COOKIES_FILE") or os.path.join(_SCRIPT_DIR, "yt_cookies.txt")
    return p if os.path.exists(p) and os.path.getsize(p) > 0 else None


def _is_botcheck(e: Exception) -> bool:
    low = str(e).lower()
    return "sign in to confirm" in low or ("bot" in low and "cookies" in low)


def _base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 15,
        "retries": 2,
    }
    proxy = _proxy_url()
    if proxy:
        opts["proxy"] = proxy
    cf = _cookiefile()
    if cf:
        opts["cookiefile"] = cf
    return opts


# ------------------------------------------------------------------
# Heatmap
# ------------------------------------------------------------------
def _extract(video_id: str) -> dict:
    import yt_dlp  # lazy import keeps cold start tiny for health probes

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = _base_opts()
    opts["skip_download"] = True
    opts["youtube_include_dash_manifest"] = False
    opts["nocheckcertificate"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
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


# ------------------------------------------------------------------
# DASH fragment-range miner (same engine as the server backend)
# ------------------------------------------------------------------
def _fmp4_sidx(buf: bytes):
    i = 0
    while i + 8 <= len(buf):
        size, typ = struct.unpack(">I4s", buf[i:i + 8])
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
        if typ == b"sidx":
            b = buf[i + 8:]
            ver = b[0] >> 4
            timescale = struct.unpack(">I", b[8:12])[0]
            if ver == 0:
                first_off = struct.unpack(">I", b[16:20])[0]
                count = struct.unpack(">H", b[22:24])[0]
                refs_start = 24
            else:
                first_off = struct.unpack(">Q", b[20:28])[0]
                count = struct.unpack(">H", b[30:32])[0]
                refs_start = 32
            abs_first = i + size + first_off
            entries = []
            pos = refs_start
            for _ in range(count):
                sz_field = struct.unpack(">I", b[pos:pos + 4])[0]
                dur = struct.unpack(">I", b[pos + 4:pos + 8])[0]
                entries.append((sz_field & 0xFFFFFF, dur))
                pos += 12
            return timescale, abs_first, entries
        if size < 8:
            break
        i += size
    raise ValueError("no sidx box in fMP4 header")


def _download_fmp4_window(fmt: dict, t0: float, t1: float) -> bytes:
    import requests as _rq
    headers = dict(fmt.get("http_headers") or {})
    head = _rq.get(fmt["url"], headers={**headers, "Range": "bytes=0-131071"}, timeout=60)
    if head.status_code not in (200, 206) or len(head.content) < 1024:
        raise ValueError(f"fMP4 header fetch failed (HTTP {head.status_code})")
    buf = head.content
    i = 0
    init_end = 0
    ts = None
    abs_first = 0
    entries = []
    while i + 8 <= len(buf):
        size, typ = struct.unpack(">I4s", buf[i:i + 8])
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
        if typ == b"moov":
            init_end = i + size
        elif typ == b"sidx":
            ts, abs_first, entries = _fmp4_sidx(buf)
            break
        if size < 8:
            break
        i += size
    if ts is None or not entries or abs_first > len(buf):
        raise ValueError("no usable sidx within header window")
    init = buf[:init_end]
    from bisect import bisect_left
    bounds = [0]
    for _sz, dur_ts in entries:
        bounds.append(bounds[-1] + dur_ts)
    t0_ts = int(t0 * ts)
    t1_ts = int(t1 * ts)
    i0 = max(0, bisect_left(bounds, t0_ts) - 1)
    i1 = max(i0, min(len(entries) - 1, bisect_left(bounds, t1_ts) - 1))
    if bounds[i1 + 1] <= t0_ts:
        raise ValueError("clip window beyond stream end")
    start_byte = abs_first + sum(e[0] for e in entries[:i0])
    end_byte = abs_first + sum(e[0] for e in entries[:i1 + 1])
    total = end_byte - start_byte
    if total <= 0 or total > 2_000_000_000:
        raise ValueError(f"invalid byte range {total}")
    rng = _rq.get(fmt["url"], headers={**headers, "Range": f"bytes={start_byte}-{end_byte - 1}"},
                  timeout=180)
    if rng.status_code != 206:
        raise ValueError(f"fMP4 range fetch failed (HTTP {rng.status_code})")
    media = rng.content
    if len(media) < total * 0.9:
        raise ValueError(f"short range read ({len(media)}/{total})")
    return init + media


def _extract_info_yt(url: str) -> dict:
    """extract_info with automatic tv/android client retry on bot-check."""
    import yt_dlp
    opts = _base_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        if not _is_botcheck(e):
            raise
        retry = dict(opts)
        retry["extractor_args"] = {"youtube": ["player_client=tv,android"]}
        with yt_dlp.YoutubeDL(retry) as ydl:
            return ydl.extract_info(url, download=False)


def _first_pts(path: str, stream: int) -> float | None:
    """First packet PTS of a stream (content time of the first fragment).

    The miner cuts video and audio DASH streams at their OWN fragment
    boundaries, so the two can start at different content times (up to one
    fragment apart) — merging as-is bakes in an A/V offset (audio ahead by
    ~1s was reported). This returns the anchor used to resync via -itsoffset.
    """
    spec = "v:0" if stream == 0 else "a:0"
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", spec,
             "-show_entries", "packet=pts_time", "-of", "csv=p=0",
             "-read_intervals", "%+#1", path],
            capture_output=True, text=True, timeout=60)
        for line in out.stdout.strip().splitlines():
            line = line.strip()
            if line and line.lower() != "n/a":
                return float(line)
    except (ValueError, subprocess.SubprocessError, OSError):
        pass
    return None


def _frag_miner_export(url: str, tmpdir: str, cut_start: float, cut_end: float) -> str:
    info = _extract_info_yt(url)
    formats = (info or {}).get("formats") or []
    vfmt = None
    v_h = -1
    for f in formats:
        if (f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
                and f.get("protocol") == "https" and f.get("ext") == "mp4"
                and f.get("url") and 0 < (f.get("height") or 0) <= 1080
                and (f.get("height") or 0) > v_h):
            vfmt = f
            v_h = f.get("height") or 0
    afmt = None
    a_b = -1.0
    for f in formats:
        if (f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
                and f.get("protocol") == "https" and f.get("ext") in ("m4a", "mp4")
                and f.get("url") and (f.get("tbr") or f.get("abr") or 0) > a_b):
            afmt = f
            a_b = f.get("tbr") or f.get("abr") or 0
    if not vfmt or not afmt:
        raise ValueError("no https DASH video+audio pair with sidx")
    v_fmp4 = os.path.join(tmpdir, "v_part.mp4")
    a_fmp4 = os.path.join(tmpdir, "a_part.m4a")
    with open(v_fmp4, "wb") as fh:
        fh.write(_download_fmp4_window(vfmt, cut_start, cut_end))
    with open(a_fmp4, "wb") as fh:
        fh.write(_download_fmp4_window(afmt, cut_start, cut_end))
    merged = os.path.join(tmpdir, "merged.mp4")
    prv, pra = _first_pts(v_fmp4, 0), _first_pts(a_fmp4, 0)
    cmd = ["ffmpeg", "-y"]
    if prv is not None and pra is not None and abs(prv - pra) > 0.05:
        # Align both streams to the LATER content start: delay the earlier
        # one so every output instant shows the same content time in both.
        if prv > pra:
            cmd += ["-itsoffset", f"{prv - pra:.3f}", "-i", a_fmp4, "-i", v_fmp4]
        else:
            cmd += ["-itsoffset", f"{pra - prv:.3f}", "-i", v_fmp4, "-i", a_fmp4]
        print(f"[export] miner av-sync: v@{prv:.3f}s a@{pra:.3f}s "
              f"-> shift {abs(prv - pra):.3f}s", flush=True)
    else:
        cmd += ["-i", v_fmp4, "-i", a_fmp4]
    cmd += ["-c", "copy", "-movflags", "+faststart", merged]
    m = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if m.returncode != 0 or not os.path.exists(merged) or os.path.getsize(merged) == 0:
        raise ValueError(f"fragment merge failed: {(m.stderr or '')[-200:]}")
    return merged  # fragment-aligned superset of [cut_start, cut_end]


def _legacy_full_export(url: str, tmpdir: str, cut_start: float, cut_end: float) -> str:
    """Full download + local stream-copy cut (with re-encode fallback)."""
    import yt_dlp
    dur = cut_end - cut_start
    out_mp4 = os.path.join(tmpdir, "clip.mp4")
    opts = _base_opts()
    opts.update({
        "format": "bv*[height<=?1080]+ba/b[height<=?1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tmpdir, "src.%(ext)s"),
    })

    def _do(opts_):
        with yt_dlp.YoutubeDL(opts_) as ydl:
            ydl.download([url])

    try:
        _do(opts)
    except Exception as e:
        if _is_botcheck(e):
            _do({**opts, "extractor_args": {"youtube": ["player_client=tv,android"]}})
        else:
            raise
    src = None
    for f in sorted(os.listdir(tmpdir)):
        if f.startswith("src."):
            src = os.path.join(tmpdir, f)
            break
    if not src:
        raise ValueError("Could not download the source video.")
    cmd = ["ffmpeg", "-y", "-ss", f"{cut_start:.3f}", "-i", src, "-t", f"{dur:.3f}",
           "-c", "copy", "-avoid_negative_ts", "make_zero", "-map", "0", out_mp4]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0 or not os.path.exists(out_mp4) or os.path.getsize(out_mp4) == 0:
        cmd2 = ["ffmpeg", "-y", "-ss", f"{cut_start:.3f}", "-i", src, "-t", f"{dur:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", "-map", "0", out_mp4]
        proc2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=1800)
        if proc2.returncode != 0 or not os.path.exists(out_mp4) or os.path.getsize(out_mp4) == 0:
            err = (proc2.stderr or proc.stderr or "unknown ffmpeg error")[-300:]
            raise ValueError(f"ffmpeg cut failed: {err}")
    return out_mp4


def _probe_duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return 0.0


# ------------------------------------------------------------------
# Optional Google Drive mirror (every exported clip -> "raw clip" folder)
# ------------------------------------------------------------------
def _drive_access_token() -> str:
    """OAuth refresh token -> user's own Drive (stdlib only, no SA fallback)."""
    if not os.path.exists(DRIVE_OAUTH_JSON):
        raise RuntimeError(f"no OAuth creds at {DRIVE_OAUTH_JSON}")
    with open(DRIVE_OAUTH_JSON) as fh:
        cfg = json.load(fh)
    body = json.dumps({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]


def _drive_find(tok: str, name: str) -> list:
    q = urllib.parse.quote(
        f"'{DRIVE_FOLDER_ID}' in parents and name = '{name}' and trashed = false")
    req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name)&pageSize=10",
        headers={"Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("files", [])


def _drive_upload(out_path: str, filename: str):
    """Best-effort mirror of an exported clip to the configured Drive folder.

    Idempotent by filename (re-export of the same window overwrites). Enabled
    only when HEATMAP_WORKER_DRIVE_FOLDER is set; never fails the request.
    """
    if not DRIVE_FOLDER_ID:
        return
    try:
        tok = _drive_access_token()
        mime = "video/mp4"
        size = os.path.getsize(out_path)
        if size > 250 * 1024 * 1024:
            print(f"[export] drive mirror SKIPPED {filename}: {size // (1024*1024)}MB > 250MB cap",
                  flush=True)
            return
        with open(out_path, "rb") as fh:
            data = fh.read()
        boundary = "----hc" + base64.b64encode(os.urandom(9)).decode()
        existing = _drive_find(tok, filename)
        if existing:
            fid = existing[0]["id"]
            meta = json.dumps({"name": filename, "mimeType": mime})  # parents NOT writable on PATCH
            url = f"https://www.googleapis.com/upload/drive/v3/files/{fid}?uploadType=multipart"
            method = "PATCH"
            verb = "UPDATED"
        else:
            meta = json.dumps({"name": filename, "mimeType": mime, "parents": [DRIVE_FOLDER_ID]})
            url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
            method = "POST"
            verb = "CREATED"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
                f"Content-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            url, method=method,
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": f"multipart/related; boundary={boundary}"},
            data=body)
        res = json.loads(urllib.request.urlopen(req, timeout=300).read())
        print(f"[export] drive mirror {verb} {filename} ({len(data)//1024}KB, id {res['id']})",
              flush=True)
    except Exception as e:  # noqa: BLE001 — mirror is best-effort
        print(f"[export] drive mirror FAILED {filename}: {str(e)[:200]}", flush=True)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.middleware("http")
async def _log_client(request: Request, call_next):
    """Log who is using the shared worker (format + abuse forensics)."""
    if request.url.path in ("/export", "/heatmap"):
        print(f"[worker] {request.method} {request.url.path} from {_client_ip(request)}", flush=True)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "heatcut-device-worker", "version": "2.0.0",
            "export": True, "auth_required": bool(WORKER_TOKEN)}


@app.get("/heatmap")
def heatmap(request: Request, video_id: str):
    """Return yt-dlp metadata + retention heatmap for a video, cached briefly."""
    _require_token(request)
    ip = _client_ip(request)
    if _rate_limited(f"heatmap:{ip}", HEATMAP_RATE_LIMIT):
        raise HTTPException(status_code=429, detail=f"Rate limit: maks {HEATMAP_RATE_LIMIT} heatmap/jam dari IP ini.")
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


@app.get("/export")
def export(request: Request, video_id: str, start_time: float, end_time: float, title: str = ""):
    """Download a clip (stream-copy, padded ±2s) straight to the browser."""
    _require_token(request)
    ip = _client_ip(request)
    if _rate_limited(f"export:{ip}", EXPORT_RATE_LIMIT):
        raise HTTPException(status_code=429, detail=f"Rate limit: maks {EXPORT_RATE_LIMIT} export/jam dari IP ini.")
    if not _export_slots.acquire(timeout=EXPORT_BUSY_WAIT):
        raise HTTPException(status_code=429, detail="Worker sedang sibuk (ada export lain jalan) — coba lagi sebentar.")
    try:
        return _export_impl(video_id, start_time, end_time, title)
    finally:
        _export_slots.release()


def _export_impl(video_id: str, start_time: float, end_time: float, title: str = ""):
    video_id = (video_id or "").strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id is required.")
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="end_time must be greater than start_time.")
    if end_time - start_time > 3600:
        raise HTTPException(status_code=400, detail="Clip too long (max 60 minutes).")

    cut_start = max(0.0, float(start_time) - PAD_PRE)
    cut_end = float(end_time) + PAD_POST
    dur = cut_end - cut_start
    tmpdir = _new_export_tmp_dir_or_500()
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        try:
            out = _frag_miner_export(url, tmpdir, cut_start, cut_end)
            out_dur = _probe_duration(out)
            if not (out_dur >= dur * 0.9 and out_dur <= dur + 25.0):
                raise ValueError(f"miner duration mismatch ({out_dur:.1f}s vs {dur:.1f}s)")
        except Exception as e:
            print(f"[export] fragment miner failed ({str(e)[:120]}) - full download fallback", flush=True)
            _clear_export_artifacts(tmpdir)
            out = _legacy_full_export(url, tmpdir, cut_start, cut_end)
    except Exception as e:
        _finish_export_job(tmpdir)
        hint = ("YouTube blocked this download as a bot check. Export a cookies.txt "
                "from a browser logged into YouTube and place it next to this script "
                "(yt_cookies.txt), then retry.") if _is_botcheck(e) else ""
        raise HTTPException(status_code=502, detail=f"Export failed: {hint or e}") from e

    base = (title or "").strip() or f"clip-{video_id}"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()[:60] or f"clip-{video_id}"
    filename = f"{slug}-{int(float(start_time))}-{int(float(end_time))}s.mp4"
    print(f"[export] {video_id} {start_time}-{end_time}s -> {filename}", flush=True)
    _drive_upload(out, filename)
    return FileResponse(
        out,
        media_type="video/mp4",
        filename=filename,
        # Heartbeat keeps running until the response is fully streamed out;
        # _finish_export_job then stops it and removes the workspace.
        background=BackgroundTask(_finish_export_job, tmpdir),
    )


if __name__ == "__main__":
    import uvicorn

    print(f"HeatCut device worker on http://{HOST}:{PORT} "
          f"(heatmap + export, cookies={'yes' if _cookiefile() else 'no'}, "
          f"drive-mirror={'yes' if DRIVE_FOLDER_ID else 'no'})")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
