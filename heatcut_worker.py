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


def _yt_env_opts() -> dict:
    """yt-dlp options for the YouTube JS-challenge (n-sig/EJS) era.

    yt-dlp needs an EXTERNAL JavaScript runtime plus the yt-dlp-ejs challenge
    solver scripts to solve YouTube's signature/`n` challenges. Without them
    every browser-ish client fails even with a valid logged-in cookies.txt:
    "The page needs to be reloaded." (web/tv) or "No video formats found!"
    (web_safari/mweb/android/ios). Measured 2026-09-19: cookies only = 0/4
    test ids, cookies + node + EJS = 4/4.

    Env overrides: HEATCUT_YT_JS_RUNTIME (default: first of deno/node/bun/
    quickjs in PATH; "none" disables) and HEATCUT_YT_REMOTE_COMPONENTS
    (default "ejs:github"; comma-separated, empty disables). Unsupported keys
    are dropped so an older yt-dlp behaves exactly as before.
    """
    out: dict = {}
    try:
        from yt_dlp.globals import supported_js_runtimes, supported_remote_components

        want = (os.environ.get("HEATCUT_YT_JS_RUNTIME") or "").strip()
        if want.lower() == "none":
            names = []
        else:
            names = [want] if want else ["deno", "node", "bun", "quickjs"]
        runtimes = supported_js_runtimes.value
        chosen = {n: {} for n in names if n in runtimes and shutil.which(n)}
        if chosen:
            out["js_runtimes"] = chosen

        spec = os.environ.get("HEATCUT_YT_REMOTE_COMPONENTS")
        spec = "ejs:github" if spec is None else spec.strip()
        comps = {c.strip() for c in spec.split(",") if c.strip()}
        comps &= set(supported_remote_components.value)
        if comps:
            out["remote_components"] = comps
    except Exception:  # noqa: BLE001 — extraction must never break on this
        pass
    return out


def _is_botcheck(e: Exception) -> bool:
    low = str(e).lower()
    return "sign in to confirm" in low or ("bot" in low and "cookies" in low)


# ------------------------------------------------------------------
# Which player client actually SERVES MEDIA (not just extracts)
# ------------------------------------------------------------------
# Extraction succeeding is not proof the media is fetchable. Measured from this
# host 2026-09-19 with a live logged-in jar + JS runtime + EJS: `default` and
# `mweb` handed back signed URLs that 403 on the FIRST byte, while `web_safari`
# served the same window fine (range GET 206) — the miner AND the full-download
# fallback both died on those 403s even though `yt-dlp --simulate` looked
# perfect. So the export PICKS a client by probing the URL, never by trusting
# extraction. Order is overridable: HEATCUT_YT_EXPORT_CLIENTS="web_safari,default".
DEFAULT_EXPORT_CLIENTS = "web_safari,default"


def _yt_export_clients() -> list:
    raw = (os.environ.get("HEATCUT_YT_EXPORT_CLIENTS") or DEFAULT_EXPORT_CLIENTS).strip()
    out = [c.strip() for c in raw.split(",") if c.strip()]
    if "default" not in out:
        out.append("default")  # keep the historical behaviour as the last resort
    return out


def _media_url_ok(fmt: dict, timeout: int = 20) -> bool:
    """1 KB range GET on a format's media URL: is it actually served?

    Same client + `http_headers` as the miner (`_download_fmp4_window`), so the
    probe cannot pass where the real fetch would fail.
    """
    import requests as _rq
    url = (fmt or {}).get("url")
    if not url:
        return False
    try:
        # Retried: a fresh googlevideo URL 403s for ~2 s after extraction, and a
        # single-shot GET read that as "DASH is blocked" for a whole session.
        r = _media_get(url, fmt.get("http_headers") or {}, "bytes=0-1023",
                       timeout=timeout, attempts=3)
        return r.status_code in (200, 206)
    except Exception:  # noqa: BLE001 — any refusal/error means "not usable"
        return False


def _miner_pick_pair(formats: list):
    """The (video, audio) DASH pair the fragment miner will cut (height<=1080)."""
    vfmt, v_h = None, -1
    for f in formats or []:
        if (f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
                and f.get("protocol") == "https" and f.get("ext") == "mp4"
                and f.get("url") and 0 < (f.get("height") or 0) <= 1080
                and (f.get("height") or 0) > v_h):
            vfmt, v_h = f, f.get("height") or 0
    afmt, a_b = None, -1.0
    for f in formats or []:
        if (f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
                and f.get("protocol") == "https" and f.get("ext") in ("m4a", "mp4")
                and f.get("url") and (f.get("tbr") or f.get("abr") or 0) > a_b):
            afmt, a_b = f, f.get("tbr") or f.get("abr") or 0
    return vfmt, afmt


def _extract_info_for_export(url: str):
    """Extract, preferring a client whose media URLs are actually served.

    Returns (info, client): client is None for the default (no override), else
    the name to pin via extractor_args on the full-download fallback. Walks
    _yt_export_clients(), probing the miner's video AND audio URLs with a 1 KB
    range GET and stopping at the first client that serves bytes. When no client
    serves bytes the last successful extraction is returned anyway, so the user
    sees the real downstream error instead of a probe message.
    """
    import yt_dlp
    last = None
    for name in _yt_export_clients():
        opts = _base_opts()
        opts["skip_download"] = True
        if name and name != "default":
            opts["extractor_args"] = {"youtube": {"player_client": [name]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 — try the next client
            print(f"[export] player_client={name}: extraction failed ({str(e)[:110]})", flush=True)
            continue
        pinned = None if (not name or name == "default") else name
        if last is None:
            last = (info, pinned)
        vfmt, afmt = _miner_pick_pair((info or {}).get("formats") or [])
        if vfmt and afmt and _media_url_ok(vfmt) and _media_url_ok(afmt):
            print(f"[export] using player_client={name} (media URLs serve bytes)", flush=True)
            return info, pinned
        print(f"[export] player_client={name}: media URLs refused — next client", flush=True)
    if last is None:
        raise ValueError("no player client could extract this video")
    return last


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
    opts.update(_yt_env_opts())
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
# Partial-export ladder — DASH ranges first, then HLS segments
# ------------------------------------------------------------------
# Measured on this host 2026-09-19 (numbers kept in the cheat-clip-webapp skill):
#   * A freshly issued googlevideo URL refuses EVERY request for ~2 s after
#     extraction (403 at +0/+0.3/+1 s, 206 at +2 s; sleeping 3.5 s before the
#     first request answers 206 immediately) — a single-shot GET looks exactly
#     like a hard block while the URL is merely not warm yet.
#   * Past the edge-cached prefix, byte RANGE requests are refused for good
#     (1.1 GB video: 0-13 MB served, >=14 MB 403; 116 MB video: 20 MB 403), so
#     a window deep inside a long source can never be mined by range.
#   * HLS segment URLs are whole resources (no Range header) and ARE served —
#     why the segment path is the dependable cheap route.
MEDIA_GET_ATTEMPTS = 4
MEDIA_GET_BACKOFF = 1.2

# Full-video route throughput measured here: 1128 MB in ~80 s.
FULL_EXPORT_MB_PER_SEC = 14.0


def _media_get(url: str, headers: dict, range_header=None,
               timeout: int = 60, attempts: int = MEDIA_GET_ATTEMPTS):
    """GET a media URL, retrying the 403s a fresh googlevideo URL answers with.

    Only 403/429/5xx are retried; everything else is handed back as-is.
    """
    import requests as _rq
    h = dict(headers or {})
    if range_header:
        h["Range"] = range_header
    last = None
    tries = max(1, attempts)
    for attempt in range(tries):
        try:
            r = _rq.get(url, headers=h, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — transient network/refusal
            last = e
        else:
            if r.status_code not in (403, 429) and r.status_code < 500:
                return r
            last = r
        if attempt + 1 < tries:
            time.sleep(MEDIA_GET_BACKOFF)
    if isinstance(last, Exception):
        raise last
    return last


class PartialBlocked(Exception):
    """Both cheap partial paths were refused — the API answers 409 instead."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.est_seconds = 90


def _hls_clients() -> list:
    raw = (os.environ.get("HEATCUT_YT_HLS_CLIENTS") or "web_safari").strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


def _hls_pick_format(formats: list):
    """Best MUXED HLS (m3u8) format <=1080p — HLS here carries video AND audio."""
    best, best_h = None, -1
    for f in formats or []:
        if not f.get("url") or "m3u8" not in str(f.get("protocol") or ""):
            continue
        if f.get("vcodec") in (None, "none") or f.get("acodec") in (None, "none"):
            continue
        h = f.get("height") or 0
        if 0 < h <= 1080 and h > best_h:
            best, best_h = f, h
    return best


def _hls_servable_format(url: str):
    """Extract with an HLS-capable player client; return its best HLS format."""
    import yt_dlp
    err = None
    for name in _hls_clients():
        opts = _base_opts()
        opts["skip_download"] = True
        opts["extractor_args"] = {"youtube": {"player_client": [name]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 — try the next client
            err = e
            print(f"[export] HLS client={name}: extraction failed ({str(e)[:110]})", flush=True)
            continue
        fmt = _hls_pick_format((info or {}).get("formats") or [])
        if fmt:
            return fmt
    raise ValueError(f"no HLS format available ({str(err)[:110]})" if err else "no HLS format available")


def _hls_segment_export(fmt: dict, tmpdir: str, cut_start: float, cut_end: float) -> str:
    """Fetch ONLY the HLS segments covering [cut_start, cut_end], then cut.

    Segment URLs are standalone resources (no Range header), which is what gets
    around both 403 modes that stop the DASH range miner.
    """
    from bisect import bisect_left, bisect_right
    from urllib.parse import urljoin

    headers = dict(fmt.get("http_headers") or {})
    playlist = _media_get(fmt["url"], headers, None, timeout=90, attempts=3)
    if playlist.status_code not in (200, 206):
        raise ValueError(f"HLS playlist fetch failed (HTTP {playlist.status_code})")
    text = playlist.text
    if "#EXTINF" not in text:
        # A master playlist points at variants — follow the first one.
        variant = next((l.strip() for l in text.splitlines()
                        if l.strip() and not l.strip().startswith("#")), None)
        if not variant:
            raise ValueError("HLS master playlist without variants")
        playlist = _media_get(urljoin(fmt["url"], variant), headers, None, timeout=90, attempts=3)
        if playlist.status_code not in (200, 206):
            raise ValueError(f"HLS variant fetch failed (HTTP {playlist.status_code})")
        text = playlist.text
    if "#EXT-X-KEY" in text and "METHOD=NONE" not in text:
        raise ValueError("encrypted HLS playlist")

    segs = []
    durs = []
    pending = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line.split(":", 1)[1].split(",")[0])
            except (ValueError, IndexError):
                pending = None
            continue
        if line.startswith("#") or pending is None:
            continue
        segs.append(urljoin(fmt["url"], line))
        durs.append(pending)
        pending = None
    if not segs or sum(durs) <= 0:
        raise ValueError("no usable HLS segments in playlist")

    bounds = [0.0]
    for d in durs:
        bounds.append(bounds[-1] + d)
    i0 = max(0, bisect_right(bounds, cut_start) - 1)
    i1 = min(len(segs) - 1, bisect_left(bounds, cut_end))
    if bounds[i1 + 1] <= cut_start:
        raise ValueError("clip window beyond the playlist end")
    # No headroom segments: the cut below is exact (-ss inside the first segment,
    # -t for the window), and every HLS segment starts on a keyframe — so the
    # segment list is just the window's coverage, which keeps the transfer small.

    parts = []
    for idx in range(i0, i1 + 1):
        seg = _media_get(segs[idx], headers, None, timeout=180)
        if seg.status_code not in (200, 206) or not seg.content:
            raise ValueError(f"HLS segment {idx} refused (HTTP {seg.status_code})")
        part = os.path.join(tmpdir, f"hls_{idx:05d}.ts")
        with open(part, "wb") as fh:
            fh.write(seg.content)
        parts.append(part)

    joined = os.path.join(tmpdir, "hls_all.ts")
    with open(joined, "wb") as out:
        for part in parts:
            with open(part, "rb") as fh:
                shutil.copyfileobj(fh, out, 1 << 20)

    seg_start = bounds[i0]
    dur = cut_end - cut_start
    ss = max(0.0, cut_start - seg_start)
    out_mp4 = os.path.join(tmpdir, "clip.mp4")
    cmd = ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", joined, "-t", f"{dur:.3f}",
           "-c", "copy", "-avoid_negative_ts", "make_zero", "-map", "0", out_mp4]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not os.path.exists(out_mp4) or os.path.getsize(out_mp4) == 0:
        cmd2 = ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", joined, "-t", f"{dur:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", "-map", "0", out_mp4]
        proc2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=1800)
        if proc2.returncode != 0 or not os.path.exists(out_mp4) or os.path.getsize(out_mp4) == 0:
            raise ValueError(f"ffmpeg cut failed: {(proc2.stderr or proc.stderr or 'unknown')[-200:]}")
    print(f"[export] HLS segments {i0}-{i1}/{len(segs)} "
          f"({sum(durs[i0:i1 + 1]):.1f}s of media, {len(parts)} files) -> clip.mp4", flush=True)
    return out_mp4


def _auto_partial_export(url: str, tmpdir: str, cut_start: float, cut_end: float,
                         info=None) -> str:
    """Cheapest partial path that works: DASH range mining, else HLS segments.

    Raises `PartialBlocked` when both are refused, so the API can answer with the
    user-facing choice (whole video vs direct download) instead of a hard error.
    """
    dur = cut_end - cut_start
    errors = []

    if info is not None:
        try:
            mined = _frag_miner_export(url, tmpdir, cut_start, cut_end, info=info)
            out_dur = _probe_duration(mined)
            if out_dur >= dur * 0.9 and out_dur <= dur + 25.0:
                return mined
            raise ValueError(f"miner output duration mismatch ({out_dur:.1f}s vs {dur:.1f}s)")
        except Exception as e:  # noqa: BLE001 — fall through to the HLS path
            errors.append(f"dash-range: {str(e)[:110]}")
            _clear_export_artifacts(tmpdir)
            print(f"[export] DASH range miner refused ({str(e)[:110]}) - trying HLS segments", flush=True)
    else:
        errors.append("dash-range: no extraction")

    try:
        hls_fmt = _hls_servable_format(url)
    except Exception as e:  # noqa: BLE001
        errors.append(f"hls-playlist: {str(e)[:110]}")
    else:
        try:
            out = _hls_segment_export(hls_fmt, tmpdir, cut_start, cut_end)
            out_dur = _probe_duration(out)
            if out_dur >= dur * 0.9:
                return out
            raise ValueError(f"HLS output too short ({out_dur:.1f}s vs {dur:.1f}s)")
        except Exception as e:  # noqa: BLE001
            errors.append(f"hls-segments: {str(e)[:110]}")
            _clear_export_artifacts(tmpdir)

    joined = " | ".join(errors)
    low = joined.lower()
    code = "yt_bot_check" if ("sign in to confirm" in low or "bot" in low or "cookies" in low) else "yt_partial_blocked"
    raise PartialBlocked(code, joined)


def _estimate_full_seconds(info) -> int:
    """Rough ETA for the whole-video route — the UI counts this down."""
    try:
        secs = float((info or {}).get("duration") or 0)
    except (TypeError, ValueError):
        secs = 0.0
    size_mb = secs * 0.6            # 1080p ≈ 4.8 Mbps
    if size_mb <= 0:
        return 90
    return int(max(25, min(900, size_mb / FULL_EXPORT_MB_PER_SEC + 8)))


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


def _download_fmp4_window(fmt: dict, t0: float, t1: float) -> tuple:
    """Partial download of one DASH stream; returns (bytes, content_begin, content_end).

    The byte range covers the fragments a fragment-boundary-aligned SUPERSET of
    [t0, t1], and the sidx already tells us the exact content times of those
    boundaries, so we return them: the caller needs them to align video against
    audio (the two streams are cut at their OWN boundaries, which differ by up
    to one fragment — merging as-is bakes in an A/V offset, and `-itsoffset`
    canNOT fix that, see `_frag_miner_export`).
    """
    import requests as _rq
    headers = dict(fmt.get("http_headers") or {})
    # Retried: a fresh googlevideo URL 403s on EVERY range for ~2 s after
    # extraction (measured), and the old single-shot GET gave up on that.
    head = _media_get(fmt["url"], headers, "bytes=0-131071", timeout=60)
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
    rng = _media_get(fmt["url"], headers, f"bytes={start_byte}-{end_byte - 1}", timeout=180)
    if rng.status_code != 206:
        raise ValueError(f"fMP4 range fetch failed (HTTP {rng.status_code})")
    media = rng.content
    if len(media) < total * 0.9:
        raise ValueError(f"short range read ({len(media)}/{total})")
    return init + media, bounds[i0] / ts, bounds[i1 + 1] / ts


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
        retry["extractor_args"] = {"youtube": {"player_client": ["tv", "android"]}}
        with yt_dlp.YoutubeDL(retry) as ydl:
            return ydl.extract_info(url, download=False)


def _first_pts(path: str, stream: int) -> float | None:
    """First packet PTS of a stream (content time of the first sample).

    Diagnostic only now (the miner gets its anchors from the sidx, not from
    this). Two traps learned the hard way:
      * `-of csv=p=0` can emit a TRAILING COMMA ("19.969161,") — a bare
        `float(line)` throws ValueError, the whole probe returned None and the
        resync silently never ran (that is exactly how a 3.9 s A/V offset
        shipped): always take the first CSV field.
      * `-read_intervals "%+#1"` returns nothing for some audio-only fMP4
        parts, so fall back to reading the first packet of the whole file.
    """
    spec = "v:0" if stream == 0 else "a:0"
    attempts = (
        ["-read_intervals", "%+#1"],   # cheap, but fails on some audio parts
        [],                            # full scan fallback (bounded by the walk)
    )
    for extra in attempts:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", spec,
                 "-show_entries", "packet=pts_time", "-of", "csv=p=0", *extra, path],
                capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            continue
        for line in out.stdout.splitlines():
            field = line.split(",")[0].strip()
            if field and field.lower() != "n/a":
                try:
                    return float(field)
                except ValueError:
                    break  # malformed line: try the next strategy
        if extra:
            continue  # first packet of the file is fine even without intervals
        break
    return None


def _frag_miner_export(url: str, tmpdir: str, cut_start: float, cut_end: float,
                       info=None) -> str:
    # `info` is normally extracted by _extract_info_for_export (which already
    # picked a client whose media URLs serve bytes) so the URLs and the client
    # stay consistent between the miner and the full-download fallback.
    if info is None:
        info, _client = _extract_info_for_export(url)
    formats = (info or {}).get("formats") or []
    vfmt, afmt = _miner_pick_pair(formats)
    if not vfmt or not afmt:
        raise ValueError("no https DASH video+audio pair with sidx")
    v_fmp4 = os.path.join(tmpdir, "v_part.mp4")
    a_fmp4 = os.path.join(tmpdir, "a_part.m4a")
    a_trim = os.path.join(tmpdir, "a_trim.m4a")
    v_bytes, v_begin, v_end = _download_fmp4_window(vfmt, cut_start, cut_end)
    with open(v_fmp4, "wb") as fh:
        fh.write(v_bytes)
    # Anchor the clip on the VIDEO: its start must be a keyframe (fragment
    # boundary), so it is the only stream we can cut without re-encoding.
    # Fetch audio from a boundary at or BEFORE that anchor (the sidx pulls the
    # boundary <= the requested time, so asking for min(cut_start, v_begin)
    # guarantees audio_begin <= v_begin), then trim the audio lead-in and tail
    # to match the video span exactly.
    a_req = min(cut_start, v_begin)
    a_bytes, a_begin, a_end = _download_fmp4_window(afmt, a_req, cut_end)
    with open(a_fmp4, "wb") as fh:
        fh.write(a_bytes)

    lead = v_begin - a_begin          # >= 0 by construction
    span = v_end - v_begin
    a_src = a_fmp4
    if lead > 0.005 and span > 0.05 and lead < 0.9 * (a_end - a_begin):
        # Packet-accurate AUDIO-only trim (audio frames are independent, video
        # is not) — this, not -itsoffset, is what fixes the sync: the muxer
        # rebases each input's start to 0, so shifting timestamps with
        # -itsoffset is a NO-OP once the streams are muxed (measured: 0.85 s
        # desync survived -itsoffset, trimmed = 0.03 s).
        trim = ["ffmpeg", "-y", "-ss", f"{lead:.3f}", "-i", a_fmp4,
                "-t", f"{span + 0.05:.3f}", "-c:a", "copy", a_trim]
        tr = subprocess.run(trim, capture_output=True, text=True, timeout=300)
        if tr.returncode != 0 or not os.path.exists(a_trim) or os.path.getsize(a_trim) == 0:
            # some codecs/containers refuse a copy trim — re-encode the audio
            # slice instead (cheap: audio only) so the offset is still gone.
            tr = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{lead:.3f}", "-i", a_fmp4,
                 "-t", f"{span + 0.05:.3f}", "-c:a", "aac", "-b:a", "192k", a_trim],
                capture_output=True, text=True, timeout=600)
        if tr.returncode == 0 and os.path.exists(a_trim) and os.path.getsize(a_trim) > 0:
            a_src = a_trim
            print(f"[export] miner av-anchor: v@{v_begin:.3f}s a@{a_begin:.3f}s "
                  f"-> trimmed audio lead {lead:.3f}s, span {span:.3f}s", flush=True)

    merged = os.path.join(tmpdir, "merged.mp4")
    cmd = ["ffmpeg", "-y", "-i", v_fmp4, "-i", a_src, "-c", "copy",
           "-movflags", "+faststart", merged]
    m = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if m.returncode != 0 or not os.path.exists(merged) or os.path.getsize(merged) == 0:
        raise ValueError(f"fragment merge failed: {(m.stderr or '')[-200:]}")

    # Post-merge sanity: both streams must start together and cover the same
    # span. A mismatch here means an A/V offset shipped, so warn loudly (the
    # caller's duration check would not catch it).
    pv, pa = _first_pts(merged, 0), _first_pts(merged, 1)
    if pv is not None and pa is not None and abs(pv - pa) > 0.25:
        print(f"[export] WARNING: merged A/V head mismatch v@{pv:.3f}s a@{pa:.3f}s", flush=True)
    return merged  # fragment-aligned superset of [cut_start, cut_end]


def _legacy_full_export(url: str, tmpdir: str, cut_start: float, cut_end: float,
                        client=None) -> str:
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
    if client:
        # Pin the client whose media URLs the probe showed to be served — the
        # DEFAULT client's URLs 403 on some videos even with a live jar.
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}

    def _do(opts_):
        with yt_dlp.YoutubeDL(opts_) as ydl:
            ydl.download([url])

    # Ladder: the pinned client first (its media URLs probed OK), then the rest.
    # A media 403 ("unable to download video data: HTTP Error 403") is NOT a
    # bot-check string, so the old tv/android retry never fired for it — this
    # loop is what actually recovers from a refused media URL.
    attempts = [client] if client else ["default"]
    for c in _yt_export_clients():
        if c not in attempts:
            attempts.append(c)

    err = None
    for name in attempts:
        o = dict(opts)
        if name and name != "default":
            o["extractor_args"] = {"youtube": {"player_client": [name]}}
        try:
            _do(o)
            err = None
            break
        except Exception as e:  # noqa: BLE001 — try the next client
            err = e
            if _is_botcheck(e):
                try:
                    _do({**o, "extractor_args": {"youtube": {"player_client": ["tv", "android"]}}})
                    err = None
                    break
                except Exception as e2:  # noqa: BLE001
                    err = e2
            print(f"[export] full download via player_client={name or 'default'} "
                  f"failed ({str(err)[:110]})", flush=True)
            _clear_export_artifacts(tmpdir)
    if err is not None:
        raise err
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
def export(request: Request, video_id: str, start_time: float, end_time: float, title: str = "",
           mode: str = ""):
    """Download a clip (stream-copy, padded ±2s) straight to the browser.

    `mode=auto` (new frontend) uses the cheapest partial route the worker can
    find (DASH byte ranges → HLS segments) and answers HTTP 409 with
    {code, est_seconds, source_url} when YouTube refuses both, so the UI can offer
    a choice instead of silently downloading the whole video. `mode=full` is that
    whole-video route, only on the user's explicit request. No `mode` at all =
    an older frontend: partial first, then the whole-video fallback as before.
    """
    _require_token(request)
    ip = _client_ip(request)
    if _rate_limited(f"export:{ip}", EXPORT_RATE_LIMIT):
        raise HTTPException(status_code=429, detail=f"Rate limit: maks {EXPORT_RATE_LIMIT} export/jam dari IP ini.")
    if not _export_slots.acquire(timeout=EXPORT_BUSY_WAIT):
        raise HTTPException(status_code=429, detail="Worker sedang sibuk (ada export lain jalan) — coba lagi sebentar.")
    try:
        mode = (mode or "legacy").strip().lower()
        if mode not in ("auto", "full", "legacy"):
            mode = "legacy"
        return _export_impl(video_id, start_time, end_time, title, mode=mode)
    finally:
        _export_slots.release()


def _export_impl(video_id: str, start_time: float, end_time: float, title: str = "",
                 mode: str = "auto"):
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

    info = None
    client = None
    try:
        try:
            info, client = _extract_info_for_export(url)
        except Exception as e:  # noqa: BLE001 — the cheap paths will report it
            print(f"[export] client probe failed ({str(e)[:110]}) - using defaults", flush=True)

        out = None
        if mode in ("auto", "legacy"):
            try:
                out = _auto_partial_export(url, tmpdir, cut_start, cut_end, info=info)
            except PartialBlocked as pb:
                pb.est_seconds = _estimate_full_seconds(info)
                if mode == "auto":
                    # New frontend: it renders the user's choice (whole video with
                    # an ETA, or download the source) instead of burning the whole
                    # download unnoticed.
                    raise
                print("[export] partial routes refused - legacy client: full download", flush=True)
                _clear_export_artifacts(tmpdir)
        if out is None:
            # mode == "full" (or legacy after a refusal) → whole video, then cut.
            # NOTE: never return from here — the tail of this function builds the
            # FileResponse; returning the path would send a bare path string.
            out = _legacy_full_export(url, tmpdir, cut_start, cut_end, client=client)
    except PartialBlocked as pb:
        pb.est_seconds = _estimate_full_seconds(info)
        _finish_export_job(tmpdir)
        raise HTTPException(status_code=409, detail={
            "code": pb.code,
            "est_seconds": pb.est_seconds,
            "source_url": f"https://www.youtube.com/watch?v={video_id}"
                          f"&t={int(max(0.0, float(start_time)))}s",
            "message": ("YouTube refused our request to fetch only part of this video — "
                        "the clip can still be produced from the whole video."),
            "reason": pb.detail[:300],
        }) from pb
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
