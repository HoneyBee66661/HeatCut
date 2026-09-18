import os
import sys

# Windows Python 3.14 compatibility hotfix for unix RTLD flags and uname used in yt-dlp plugins
for flag in ('RTLD_LAZY', 'RTLD_NOW', 'RTLD_GLOBAL', 'RTLD_LOCAL', 'RTLD_NODELETE', 'RTLD_NOLOAD', 'RTLD_DEEPBIND'):
    if not hasattr(os, flag):
        setattr(os, flag, 1)

if not hasattr(os, 'uname'):
    from collections import namedtuple
    UnameResult = namedtuple('UnameResult', ['sysname', 'nodename', 'release', 'version', 'machine'])
    os.uname = lambda: UnameResult('Windows', 'localhost', '10', '10.0', 'AMD64')

from dotenv import load_dotenv

# Automatically load environment variables from backend/.env or root .env
_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, ".env"))
load_dotenv(os.path.join(_base_dir, "..", ".env"))
load_dotenv()

import re
import math
import socket
import struct
import subprocess
import logging
import asyncio
import json
import shutil
import tempfile
import time
import threading
import uuid
from typing import List, Optional, Any
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
# NOTE: yt_dlp & youtube_transcript_api are intentionally NOT imported here.
# They are heavy packages needed only for local/direct scraping; on serverless
# (Vercel/AWS) they are either blocked or unused. They are lazy-imported inside
# the functions that need them to keep cold starts lean.
from google import genai
from google.genai import types
# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cheat-clip")

# ----------------------------------------------------------------
# Dedicated temp workspace + liveness-aware TTL cleanup
# ----------------------------------------------------------------
# All export/worker processing artifacts land in one shared directory
# instead of scattered tempfile.mkdtemp() calls. A background thread scans it
# every 30s and prunes job dirs that are BOTH unused and older than the TTL.
#
# Why mtime alone is NOT enough (regression fixed here): a directory's own
# mtime only moves when entries are added/removed, so a long job that keeps
# writing ONE file (yt-dlp `.part`, ffmpeg output) looks idle — the cleaner
# deleted a live job dir mid-write, the writer kept filling an unlinked inode
# and the next open() raised FileNotFoundError. So every job now writes a
# heartbeat marker (.heatcut-job.json: pid/host/heartbeat) every
# EXPORT_HEARTBEAT_SECONDS; the cleaner SKIPS any dir whose marker names a pid
# that is still alive on this host, and for everything else falls back to the
# newest mtime over the WHOLE tree. This also makes the shared root safe: the
# backend and the worker each run a cleaner, but neither can delete the
# other's in-flight job. TTL therefore only governs crashed/abandoned jobs.
EXPORT_TMP_ROOT_REQUESTED = os.environ.get("HEATCUT_EXPORT_TMP", os.path.join(os.path.expanduser("~"), ".heatcut", "export_tmp"))
EXPORT_TTL_SECONDS = int(os.environ.get("HEATCUT_EXPORT_TTL", "300"))  # idle/crashed jobs only
EXPORT_CLEANUP_INTERVAL = 30  # scan every 30 seconds
EXPORT_HEARTBEAT_SECONDS = 5  # job liveness ping
EXPORT_JOB_MARKER = ".heatcut-job.json"
HOSTNAME = socket.gethostname()

_export_jobs: dict = {}  # job dir -> (stop_event, heartbeat thread)
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
    """Configured root if usable, else the platform temp dir.

    Serverless platforms (Vercel) mount everything read-only except the temp
    dir, so the configured path under $HOME cannot even be created there. The
    import must survive that (every /api/* route shares this module) AND the
    export route must not crash with a bare 500 on the first makedirs — hence a
    real fallback plus EXPORT_TMP_OK so /api/health can show the truth.
    """
    if _usable_tmp_root(EXPORT_TMP_ROOT_REQUESTED):
        return EXPORT_TMP_ROOT_REQUESTED
    fallback = os.path.join(tempfile.gettempdir(), "heatcut_export_tmp")
    if fallback != EXPORT_TMP_ROOT_REQUESTED and _usable_tmp_root(fallback):
        logger.warning(
            "export tmp root %s is not writable — falling back to %s",
            EXPORT_TMP_ROOT_REQUESTED, fallback,
        )
        return fallback
    logger.warning("no writable export tmp root (%s and %s both failed) — exports will fail",
                   EXPORT_TMP_ROOT_REQUESTED, fallback)
    return EXPORT_TMP_ROOT_REQUESTED


EXPORT_TMP_ROOT = _resolve_export_tmp_root()
EXPORT_TMP_OK = _usable_tmp_root(EXPORT_TMP_ROOT)
logger.info("export tmp root: %s  ttl=%ds  writable=%s", EXPORT_TMP_ROOT, EXPORT_TTL_SECONDS, EXPORT_TMP_OK)

# ---------------------------------------------------------------- Fair use
# /api/analyze can spend the SERVER's own GEMINI_API_KEY / Supadata keys, so a
# shared link must not be an open bar. Best-effort per-IP caps: memory only, so
# they reset with each serverless instance — good enough to stop casual abuse of
# a small circle. 0 disables a cap.
ANALYZE_RATE_LIMIT = int(os.environ.get("HEATCUT_ANALYZE_RATE_LIMIT", "20"))   # per IP per hour
ANALYZE_DAILY_CAP = int(os.environ.get("HEATCUT_ANALYZE_DAILY_CAP", "60"))     # per IP per day

_rate_lock = threading.Lock()
_rate_hits: dict = {}  # "kind:ip" -> [timestamps]


def _client_ip(request: Request) -> str:
    """Caller IP for rate limits.

    Vercel puts the real client first in `x-forwarded-for`; behind a Cloudflare
    tunnel the edge sets `cf-connecting-ip`. Both are client-settable in
    principle — treat these caps as anti-casual-abuse, not authentication.
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


def _rate_limited(key: str, limit: int, window: float) -> bool:
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
        if len(_rate_hits) > 2000:  # keep the ledger bounded
            for k in [k for k, v in _rate_hits.items() if not v]:
                _rate_hits.pop(k, None)
    return False


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
    file exists (caught live: 1 sample in 107 showed exactly that). A marker
    file that is fresh but unparsable also counts as in flight — a running job
    never loses protection just because a read landed mid-write.
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


def _cleanup_expired_export_tmp() -> int:
    """Delete job dirs that are neither in flight nor recently touched."""
    now = time.time()
    deleted = 0
    try:
        entries = os.listdir(EXPORT_TMP_ROOT)
    except OSError:
        return 0  # permissions / missing dir — best-effort only
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


def _start_export_tmp_cleanup() -> None:
    """Spawn a daemon thread that prunes expired export tmp dirs."""

    def _run() -> None:
        while True:
            try:
                _cleanup_expired_export_tmp()
            except Exception:  # noqa: BLE001 — never let cleanup kill the server
                logger.exception("export tmp cleanup failed")
            time.sleep(EXPORT_CLEANUP_INTERVAL)

    t = threading.Thread(target=_run, name="export-tmp-cleanup", daemon=True)
    t.start()
    logger.info("export tmp cleanup thread started (ttl=%ds, interval=%ds)", EXPORT_TTL_SECONDS, EXPORT_CLEANUP_INTERVAL)


def _write_job_marker(marker: str, started: float) -> None:
    """Atomically (re)write the job heartbeat marker."""
    payload = {"pid": os.getpid(), "host": HOSTNAME, "started": started, "heartbeat": time.time()}
    tmp = f"{marker}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, marker)  # readers never see a partial marker
    except OSError:
        pass  # best-effort: the tree-mtime fallback still protects the dir


def _make_export_tmp_dir() -> str:
    """Create a per-job subdirectory under EXPORT_TMP_ROOT and start its heartbeat."""
    if not EXPORT_TMP_OK:
        # No writable workspace: fail loudly but *handled* — an unguarded raise
        # here reaches the client as a bare "Internal Server Error" instead of a
        # useful message (that is exactly what Vercel showed).
        raise RuntimeError(f"export workspace not writable: {EXPORT_TMP_ROOT}")
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    sub = f"{ts}_{uuid.uuid4().hex[:8]}"
    path = os.path.join(EXPORT_TMP_ROOT, sub)
    os.makedirs(path, exist_ok=True)
    marker = os.path.join(path, EXPORT_JOB_MARKER)
    started = time.time()
    _write_job_marker(marker, started)  # marker exists BEFORE the job can block
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(EXPORT_HEARTBEAT_SECONDS):
            _write_job_marker(marker, started)

    t = threading.Thread(target=_beat, name="export-job-heartbeat", daemon=True)
    t.start()
    with _export_jobs_lock:
        _export_jobs[path] = (stop, t)
    return path


def _clear_export_artifacts(path: str) -> None:
    """Drop a job's partial artifacts but KEEP its liveness marker.

    The marker is the only thing telling the TTL cleaner this dir is still in
    use — wiping it here (as a naive `for f in os.listdir(tmpdir): remove(f)`
    does) leaves the dir protected by tree-mtime alone until the next
    heartbeat, so never delete it.
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


def _finish_export_job(path: str) -> None:
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
            detail=(f"Export workspace unavailable on this host ({e}). Exports need "
                    "a writable temp dir — on serverless hosts run heatcut_worker.py "
                    "on your own device and download from there."),
        ) from e


# Start the TTL cleanup thread on module load (runs once per process).
try:
    _start_export_tmp_cleanup()
except Exception:  # noqa: BLE001 — temp hygiene must never break module import
    logger.warning("export tmp cleanup thread not started", exc_info=True)


app = FastAPI(title="HEATCUT API", description="AI-powered YouTube Viral Hotspot Finder")

# Configure CORS — origins come from ALLOWED_ORIGINS (comma-separated env var).
# Default (unset): local Vite dev servers only. In production the frontend and
# API are same-origin (Vercel routes /api to this app), so no entry is needed
# unless the API is called cross-origin from another site. "*" is an explicit
# opt-in for open dev setups. allow_credentials is False: the app authenticates
# with client-supplied keys (localStorage), never cookies.
def _cors_origins() -> List[str]:
    raw = (os.environ.get("ALLOWED_ORIGINS") or "").strip()
    if raw == "*":
        return ["*"]
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------
# Pydantic Schemas for Gemini Structured Output
# ----------------------------------------------------------------

class ViralClip(BaseModel):
    title: str = Field(description="Catchy clip title, max 8 words")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    hook_time: float = Field(description="Absolute timestamp in seconds from video start where the potential hook occurs inside this clip range (must be >= start_time and <= end_time)")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    transcript: str = Field(description="Spoken text of the clip")
    title_suggestion: str = Field(default="", description="Catchy alternative title suggestion")
    caption_suggestion: str = Field(default="", description="Engaging social media caption suggestion")
    hashtag_suggestion: str = Field(default="", description="Relevant hashtags suggestion (e.g. #hashtag1 #hashtag2)")
    signal: Optional[str] = Field(default=None, description="Evidence source: 'retention', 'text', or 'both'")
    heat_score: Optional[float] = Field(default=None, description="0-1 retention evidence over the clip range")
    text_score: Optional[float] = Field(default=None, description="0-1 transcript-structure evidence over the clip range")

class ViralClipGemini(BaseModel):
    title: str = Field(description="Catchy clip title, max 8 words")
    start_time: float = Field(description="Clip start in seconds, aligned to a sentence boundary")
    end_time: float = Field(description="Clip end in seconds, aligned to a sentence boundary")
    hook_time: float = Field(description="Absolute timestamp in seconds from video start where the potential hook occurs inside this clip range (must be >= start_time and <= end_time)")
    virality_score: int = Field(description="Virality score 1-100")
    key_quotes: List[str] = Field(description="1-2 key quotes from the clip")
    title_suggestion: str = Field(default="", description="Catchy alternative title suggestion")
    caption_suggestion: str = Field(default="", description="Engaging social media caption suggestion")
    hashtag_suggestion: str = Field(default="", description="Relevant hashtags suggestion (e.g. #hashtag1 #hashtag2)")

class VideoAnalysis(BaseModel):
    summary: str = Field(description="1-2 sentence video summary, followed by 2-4 general hashtags (e.g. #podcast #marriage #success)")
    clips: List[ViralClipGemini] = Field(description="List of viral clip candidates, sorted by virality_score desc")

# ----------------------------------------------------------------
# API Request/Response Schemas
# ----------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    duration: str = Field("30s", description="Target clip duration: '15s', '30s', or '60s'")
    api_key: Optional[str] = Field(None, description="Optional custom Gemini API key provided by the user")
    model: Optional[str] = Field("gemini-2.5-flash", description="Preferred Gemini model name")
    custom_prompt: Optional[str] = Field(None, description="Optional custom focus prompt for clips search")
    range_start: Optional[float] = Field(None, description="Search range start in seconds")
    range_end: Optional[float] = Field(None, description="Search range end in seconds")
    subtitles: Optional[str] = Field(None, description="Optional manual subtitles text (SRT or TXT)")
    subtitles_filename: Optional[str] = Field(None, description="Optional manual subtitles filename")
    target_clip_count: Optional[int] = Field(None, description="Optional target number of clips (1-50)")
    provider: Optional[str] = Field(default="gemini", description="AI provider: 'gemini', 'openai', 'anthropic', or 'openai-compatible'")
    base_url: Optional[str] = Field(default=None, description="Custom OpenAI-compatible base URL (e.g. https://api.deepseek.com/v1) for provider='openai-compatible'")
    mode: Optional[str] = Field(default="auto", description="Analysis mode: 'auto' (default — detect transcript, fall back to heatmap-only when unavailable), 'podcast' (transcript required, text-first), 'concert' (heatmap-only — skip transcript entirely)")
    client_heatmap: Optional[List[dict]] = Field(default=None, description="Client-asserted retention heatmap from the device loopback worker (list of {start_time, end_time, value})")
    client_title: Optional[str] = Field(default=None, description="Client-asserted video title from the device loopback worker")
    client_duration: Optional[float] = Field(default=None, description="Client-asserted video duration in seconds from the device loopback worker")

class HeatmapPoint(BaseModel):
    start_time: float
    end_time: float
    value: float

class TranscriptLine(BaseModel):
    start: float
    end: float
    text: str
    engagement: Optional[float] = None

class AnalyzeResponse(BaseModel):
    video_id: str
    title: str
    duration: float
    heatmap: List[HeatmapPoint]
    summary: str
    clips: List[ViralClip]
    transcript: Optional[List[TranscriptLine]] = None
    model: Optional[str] = None

# ----------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------

def parse_time_str(time_str: str) -> float:
    """Parses time string in formats like HH:MM:SS,mmm or MM:SS,mmm or HH:MM:SS or MM:SS to seconds."""
    time_str = time_str.strip().replace(',', '.')
    # Extract millisecond if present
    ms = 0.0
    if '.' in time_str:
        parts = time_str.split('.')
        time_str = parts[0]
        try:
            ms = float('0.' + parts[1])
        except ValueError:
            pass
            
    time_parts = time_str.split(':')
    try:
        if len(time_parts) == 3:
            return int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2]) + ms
        elif len(time_parts) == 2:
            return int(time_parts[0]) * 60 + int(time_parts[1]) + ms
        elif len(time_parts) == 1:
            return float(time_parts[0]) + ms
    except ValueError:
        return 0.0

def parse_manual_subtitles(content: str, default_duration: float = 0.0) -> List[dict]:
    # Normalize line endings
    content = content.replace('\r\n', '\n').strip()
    
    # 1. Try standard SRT parsing first
    # SRT block regex: index (optional), time range, text
    # e.g.,
    # 1
    # 00:00:01,000 --> 00:00:04,500
    # Hello
    srt_regex = r'(?:\d+\n)?(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\n(.*?)(?=\n\n|\n\d+\n|\Z)'
    srt_matches = re.findall(srt_regex, content, re.DOTALL)
    
    if srt_matches:
        results = []
        for start_str, end_str, text in srt_matches:
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            cleaned_text = text.replace('\n', ' ').strip()
            results.append({
                "text": cleaned_text,
                "start": start,
                "duration": max(0.1, end - start)
            })
        if results:
            return results

    # 2. Try parsing line-by-line for timestamped lines
    # Patterns:
    # [00:12] Hello or 00:12 Hello
    # [01:02:15] Hello or 01:02:15 Hello
    # [00:12 - 00:15] Hello or 00:12 - 00:15 Hello
    # Let's match timestamp patterns at the start of the line or enclosed in brackets/parens
    line_time_range_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)\s*(?:-|-->|\s)\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    line_single_time_regex = r'^[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)[\]\)]?\s*(.*)'
    
    lines = content.split('\n')
    results = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Match range first (e.g. 00:12 - 00:15 Text)
        m_range = re.match(line_time_range_regex, line)
        if m_range:
            start_str, end_str, text = m_range.groups()
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            results.append({
                "text": text.strip(),
                "start": start,
                "duration": max(0.1, end - start)
            })
            continue
            
        # Match single timestamp (e.g. 00:12 Text)
        m_single = re.match(line_single_time_regex, line)
        if m_single:
            start_str, text = m_single.groups()
            start = parse_time_str(start_str)
            results.append({
                "text": text.strip(),
                "start": start,
                "duration": -1.0  # Will fill in later
            })
            continue

    if results:
        # Resolve duration for single timestamps
        # Set duration to the difference between next start and current start, or a default 3.0s
        for i in range(len(results)):
            if results[i]["duration"] == -1.0:
                if i < len(results) - 1:
                    next_start = results[i+1]["start"]
                    diff = next_start - results[i]["start"]
                    results[i]["duration"] = max(0.5, diff)
                else:
                    results[i]["duration"] = 3.0  # default for the last line
        return results

    # 3. Fallback: split text into paragraphs or sentences and distribute evenly across video duration
    duration_to_use = default_duration if default_duration > 0 else 60.0
    # Clean multiple newlines and split by sentences
    sentences = re.split(r'(?<=[.!?])\s+|\n+', content)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    if sentences:
        num_sentences = len(sentences)
        sec_per_sentence = duration_to_use / num_sentences
        results = []
        for i, text in enumerate(sentences):
            start = i * sec_per_sentence
            results.append({
                "text": text,
                "start": round(start, 2),
                "duration": round(sec_per_sentence, 2)
            })
        return results
        
    return []


def extract_video_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL formats."""
    # Handle shorts, embed, watch?v=, youtu.be, etc.
    patterns = [
        r"(?:v=|\/v\/|embed\/|shorts\/|youtu\.be\/|\/embed\/|\/watch\?v=|\/watch\?.+&v=)([^#\&\?]{11})",
        r"^(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?([^#\&\?]{11})"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    # Simple length check fallback if the user just pasted the ID
    if len(url.strip()) == 11:
        return url.strip()
    return None

def get_proxy_url() -> Optional[str]:
    """Retrieves proxy URL from environment variables (PROXY_URL or WEBSHARE_PROXY)."""
    proxy = os.environ.get("PROXY_URL") or os.environ.get("WEBSHARE_PROXY") or ""
    return proxy.strip() or None


def get_yt_cookiefile() -> Optional[str]:
    """Path to a Netscape-format yt-dlp cookies file, or None if absent.

    Override with env YT_COOKIES_FILE; default `<backend>/yt_cookies.txt`.
    YouTube bot-checks non-residential IPs on popular/live content
    ("Sign in to confirm you're not a bot") — a cookies export from a
    logged-in browser fixes it. The file is gitignored; treat as a secret.
    """
    p = os.environ.get("YT_COOKIES_FILE") or os.path.join(_base_dir, "yt_cookies.txt")
    return p if os.path.exists(p) and os.path.getsize(p) > 0 else None


def _botcheck_message(e: Exception) -> Optional[str]:
    """Maps a yt-dlp bot-check error to a friendly remediation string."""
    s = str(e)
    low = s.lower()
    if "sign in to confirm" in low or ("bot" in low and "cookies" in low):
        return (
            "YouTube blocked this download as a bot check on the server's IP "
            "(common for popular/live videos). Fix: export a cookies.txt from a "
            "browser logged into YouTube and place it at backend/yt_cookies.txt "
            "(or set YT_COOKIES_FILE). See the app's README/help for the exact "
            "export steps, then retry this clip."
        )
    return None


def fetch_video_metadata(url: str):
    """Fetches video title, duration, and viewer retention heatmap using yt-dlp."""
    import yt_dlp  # lazy: heavy, only needed for direct scraping
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    proxy = get_proxy_url()
    
    # On Vercel, YouTube blocks direct datacenter IPs, so try proxy first if configured; locally try direct first
    attempts = [proxy, None] if (is_vercel and proxy) else [None, proxy] if proxy else [None]
    
    for attempt_proxy in attempts:
        ydl_opts: Any = {
            'skip_download': True,
            'youtube_include_dash_manifest': False,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'proxy': attempt_proxy,
            'socket_timeout': 10
        }
        cf = get_yt_cookiefile()
        if cf:
            ydl_opts['cookiefile'] = cf
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise Exception("yt-dlp returned empty info dict")
                return {
                    "title": info.get('title') or 'Unknown YouTube Video',
                    "duration": float(info.get('duration') or 0.0),
                    "heatmap": info.get('heatmap') or [],
                    "is_live": bool(info.get('is_live') or False),
                    "live_status": info.get('live_status') or 'not_live'
                }
        except Exception as e:
            logger.warning(f"yt-dlp metadata extraction failed (proxy={'yes' if attempt_proxy else 'no'}): {e}")
            continue

    # Bot-check fallback: the web client sometimes trips YouTube's
    # "Sign in to confirm you're not a bot" on popular/live content —
    # retry once with the tv/android player client (no login needed)
    # before giving up to Supadata.
    try:
        bot_opts: Any = {
            'skip_download': True,
            'youtube_include_dash_manifest': False,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'socket_timeout': 10,
            'extractor_args': {'youtube': ['player_client=tv,android']},
        }
        cf = get_yt_cookiefile()
        if cf:
            bot_opts['cookiefile'] = cf
        with yt_dlp.YoutubeDL(bot_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                return {
                    "title": info.get('title') or 'Unknown YouTube Video',
                    "duration": float(info.get('duration') or 0.0),
                    "heatmap": info.get('heatmap') or [],
                    "is_live": bool(info.get('is_live') or False),
                    "live_status": info.get('live_status') or 'not_live'
                }
    except Exception as e:
        logger.warning(f"yt-dlp bot-check fallback (tv/android client) failed: {e}")

    # Serverless-friendly fallback: Supadata unified metadata (title + duration only —
    # Supadata has NO retention-heatmap endpoint, so heatmap stays empty on this path).
    # Keeps the API functional on Vercel/datacenter IPs where yt-dlp is blocked.
    try:
        supadata_meta = fetch_metadata_supadata(url)
    except Exception as e:
        logger.warning(f"Supadata metadata fallback failed: {e}")
        supadata_meta = None
    if supadata_meta:
        logger.info("Video metadata loaded via Supadata fallback (title/duration, no heatmap)")
        return {
            **supadata_meta,
            "heatmap": [],
            "is_live": False,
            "live_status": "not_live",
        }

    # Final fallback to URL video ID parsing if everything fails
    video_id = extract_video_id(url)
    if video_id:
        return {
            "title": f"YouTube Video ({video_id})",
            "duration": 0.0,
            "heatmap": [],
            "is_live": False,
            "live_status": "not_live"
        }
    raise HTTPException(status_code=400, detail="Failed to retrieve YouTube video details from URL.")


_supadata_key_index = 0

def get_supadata_keys() -> List[str]:
    """Retrieves list of Supadata API keys from environment variables."""
    raw = os.environ.get("SUPADATA_API_KEYS") or os.environ.get("SUPADATA_API_KEY") or ""
    # Extract keys starting with sd_ or split by comma/whitespace/quotes
    keys = re.findall(r'sd_[a-zA-Z0-9]+', raw)
    if not keys:
        keys = [k.strip('\"\' ') for k in re.split(r'[,\s\n]+', raw) if k.strip('\"\' ')]
    return keys

def fetch_transcript_supadata(video_id: str) -> List[dict]:
    """Fetches transcript from Supadata API, rotating through available keys if rate limits/quotas occur."""
    global _supadata_key_index
    import requests
    
    keys = get_supadata_keys()
    if not keys:
        return []

    # Round-robin key rotation to evenly distribute load across keys
    start_idx = _supadata_key_index % len(keys)
    rotated_keys = keys[start_idx:] + keys[:start_idx]
    _supadata_key_index = (_supadata_key_index + 1) % len(keys)

    for key in rotated_keys:
        masked_key = f"{key[:7]}...{key[-4:]}" if len(key) >= 11 else "***"
        try:
            logger.info(f"Attempting Supadata transcript fetch with key {masked_key}")
            response = requests.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                headers={"x-api-key": key},
                params={"videoId": video_id},
                timeout=25
            )
            if response.status_code == 200:
                data = response.json()
                content = data.get("content") or []
                if content:
                    result = []
                    for seg in content:
                        text = seg.get("text", "").strip()
                        if text:
                            start = float(seg.get("offset", 0)) / 1000.0
                            dur = float(seg.get("duration", 0)) / 1000.0
                            result.append({"text": text, "start": start, "duration": dur})
                    if result:
                        logger.info(f"Successfully retrieved {len(result)} transcript lines via Supadata ({masked_key})")
                        return result
            elif response.status_code in (429, 402, 403, 401):
                logger.warning(f"Supadata key {masked_key} returned status {response.status_code} (quota/limit). Rotating to next key...")
                continue
            else:
                logger.warning(f"Supadata key {masked_key} returned status {response.status_code}: {response.text[:100]}")
        except Exception as e:
            logger.warning(f"Supadata request with key {masked_key} failed: {e}")
            continue

    return []


def fetch_metadata_supadata(url: str) -> Optional[dict]:
    """Fetches video title/duration from Supadata's unified /v1/metadata endpoint.

    Serverless-friendly: works from datacenter IPs where yt-dlp is blocked.
    Returns None when no keys are configured or every attempt fails.
    NOTE: Supadata exposes no retention-heatmap endpoint — callers receive
    title/duration only and must treat heatmap as empty on this path.
    """
    global _supadata_key_index
    import requests

    keys = get_supadata_keys()
    if not keys:
        return None

    start_idx = _supadata_key_index % len(keys)
    rotated_keys = keys[start_idx:] + keys[:start_idx]
    _supadata_key_index = (_supadata_key_index + 1) % len(keys)

    for key in rotated_keys:
        masked_key = f"{key[:7]}...{key[-4:]}" if len(key) >= 11 else "***"
        try:
            response = requests.get(
                "https://api.supadata.ai/v1/metadata",
                headers={"x-api-key": key},
                params={"url": url},
                timeout=15,
            )
            if response.status_code == 200:
                data = response.json() or {}
                title = (data.get("title") or "").strip()
                media = data.get("media") or {}
                duration = float(media.get("duration") or 0.0)
                if title:
                    logger.info(f"Supadata metadata OK ({masked_key}): \"{title[:45]}\" ({int(duration)}s)")
                    return {"title": title, "duration": duration}
                logger.warning(f"Supadata metadata response missing title ({masked_key})")
            elif response.status_code in (429, 402, 403, 401):
                logger.warning(f"Supadata key {masked_key} returned {response.status_code} (quota/limit). Rotating...")
                continue
            else:
                logger.warning(f"Supadata key {masked_key} returned {response.status_code}: {response.text[:100]}")
        except Exception as e:
            logger.warning(f"Supadata metadata request with key {masked_key} failed: {e}")
            continue

    return None


def fetch_transcript_ytdlp(video_id: str) -> List[dict]:
    """Attempts to extract captions using yt-dlp's player response directly (free, no quota used)."""
    import yt_dlp  # lazy: heavy, only needed for direct scraping
    import requests
    proxy = get_proxy_url()
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'proxy': proxy,
        'socket_timeout': 8
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if not info:
                return []
            
            subtitles = info.get('subtitles') or {}
            auto_subtitles = info.get('automatic_captions') or {}
            
            priority_langs = ['id', 'en', 'es', 'pt', 'fr', 'de', 'ja', 'ko', 'zh-Hans', 'zh-Hant', 'ar', 'hi', 'ru']
            # Search manual first, then automatic captions
            for lang_dict, is_auto in [(subtitles, False), (auto_subtitles, True)]:
                for lang in priority_langs:
                    formats = lang_dict.get(lang) or []
                    json3_entry = next((f['url'] for f in formats if f.get('ext') == 'json3'), None)
                    if json3_entry:
                        # Try direct first, then proxy if needed
                        proxies_dict = {'http': proxy, 'https': proxy} if proxy else None
                        for p in [None, proxies_dict]:
                            try:
                                r = requests.get(json3_entry, proxies=p, timeout=5)
                                if r.status_code == 200:
                                    events = r.json().get('events', [])
                                    result = []
                                    for ev in events:
                                        segs = ev.get('segs', [])
                                        text = ''.join(s.get('utf8', '') for s in segs).strip()
                                        if text:
                                            start = ev.get('tStartMs', 0) / 1000.0
                                            dur = ev.get('dDurationMs', 0) / 1000.0
                                            result.append({'text': text, 'start': start, 'duration': dur})
                                    if result:
                                        logger.info(f"Transcript fetched via yt-dlp (lang={lang}, auto={is_auto})")
                                        return result
                            except Exception:
                                continue
    except Exception as e:
        logger.warning(f"yt-dlp subtitle extraction failed: {e}")
    return []


def fetch_transcript(video_id: str) -> List[dict]:
    """Retrieves subtitles. On Vercel / serverless cloud environments, prioritizes rotating Supadata
    to avoid datacenter IP bans and 10s execution timeouts. Locally, prioritizes free direct fetch."""
    from youtube_transcript_api import YouTubeTranscriptApi  # lazy: local/direct path only

    def to_dict_list(fetched) -> List[dict]:
        return [
            {
                "text": getattr(line, "text", ""),
                "start": getattr(line, "start", 0.0),
                "duration": getattr(line, "duration", 0.0)
            }
            for line in fetched
        ]

    keys = get_supadata_keys()
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

    # ── On Vercel / Serverless: Use Supadata First if Available ──────────────
    # YouTube strictly blocks all Vercel/AWS datacenter IPs. Trying multiple scraping
    # attempts on Vercel burns 20+ seconds and triggers Vercel Gateway Timeouts.
    if is_vercel and keys:
        logger.info("Vercel deployment detected — utilizing Supadata API for cloud transcript retrieval")
        supadata_data = fetch_transcript_supadata(video_id)
        if supadata_data:
            return supadata_data

    # ── Strategy 1: Fast direct fetch (works on localhost/residential IPs) ─────
    priority_langs = ['id', 'en', 'es', 'pt', 'fr', 'de', 'ja', 'ko', 'zh-Hans', 'zh-Hant', 'ar', 'hi', 'ru']
    api = YouTubeTranscriptApi()
    try:
        # Pass all priority languages in ONE single network request (fast!)
        data = to_dict_list(api.fetch(video_id, languages=priority_langs))
        if data:
            logger.info("Transcript fetched via direct YouTube fetch")
            return data
    except Exception as e:
        logger.info(f"Direct fetch missed: {e}")

    # ── Strategy 2: List all transcripts (manual then auto) ────────────────────
    try:
        all_transcripts = list(api.list(video_id))
        manual    = [t for t in all_transcripts if not getattr(t, 'is_generated', False)]
        generated = [t for t in all_transcripts if     getattr(t, 'is_generated', False)]

        for transcript in (manual + generated):
            try:
                data = to_dict_list(transcript.fetch())
                if data:
                    logger.info(
                        f"Transcript fetched via list: {transcript.language} "
                        f"({'auto' if getattr(transcript, 'is_generated', False) else 'manual'})"
                    )
                    return data
            except Exception as e:
                logger.warning(f"Failed ({transcript.language_code}): {e}")
                continue
    except Exception as e:
        logger.warning(f"Could not list transcripts: {e}")

    # ── Strategy 3: yt-dlp native extraction fallback ──────────────────────────
    ytdlp_data = fetch_transcript_ytdlp(video_id)
    if ytdlp_data:
        return ytdlp_data

    # ── Strategy 4: Supadata API fallback (for localhost when direct fails) ────
    if keys:
        supadata_data = fetch_transcript_supadata(video_id)
        if supadata_data:
            return supadata_data

    # ── All strategies exhausted ──────────────────────────────────────────────
    raise HTTPException(
        status_code=400,
        detail=(
            "No subtitles could be retrieved for this video. "
            "Subtitles might be disabled, or the video may be age-restricted, private, or require a login."
        )
    )





def lowercase_hashtags_in_string(text: str) -> str:
    """Finds all hashtags (#word) in a string and converts them to lowercase."""
    if not text:
        return text
    return re.sub(r'#\w+', lambda m: m.group(0).lower(), text)

def get_average_heatmap_value(start: float, end: float, heatmap: List[dict]) -> float:
    """Calculates the average retention score from the heatmap for a transcript time segment."""
    if not heatmap:
        return 0.0
    
    overlaps = []
    for point in heatmap:
        p_start = point.get('start_time', 0.0)
        p_end = point.get('end_time', 0.0)
        p_val = point.get('value', 0.0)
        
        # Check if heatmap point overlaps with transcript segment
        if max(start, p_start) < min(end, p_end):
            overlaps.append(p_val)
            
    if overlaps:
        return sum(overlaps) / len(overlaps)
        
    # Fallback to closest point if no direct overlap matches
    closest_val = 0.0
    min_dist = float('inf')
    mid_time = (start + end) / 2.0
    for point in heatmap:
        p_mid = (point.get('start_time', 0.0) + point.get('end_time', 0.0)) / 2.0
        dist = abs(p_mid - mid_time)
        if dist < min_dist:
            min_dist = dist
            closest_val = point.get('value', 0.0)
    return closest_val


# ----------------------------------------------------------------
# Hybrid evidence: viewer-retention + transcript-structure signals
# ----------------------------------------------------------------
# Two independent lenses, both grounded in published / industry practice:
#   heat = real viewer-rewatch evidence (YouTube player telemetry)
#   text = transcript-structure evidence — curiosity gaps (Loewenstein),
#          open loops (Zeigarnik), punchlines/emotional peaks, specificity,
#          contrast/pattern interrupts, actionable advice. This second lens
#          catches strong moments on flat-retention videos (podcasts,
#          lectures, interviews) that pure heatmap mining would miss.

def _heat_evidence(start: float, end: float, heatmap: List[dict]) -> float:
    """0..1 — retention evidence over [start, end): mean blended toward the peak,
    so a single strong rewatch spike still registers."""
    if not heatmap:
        return 0.0
    vals = []
    for p in heatmap:
        ps = p.get('start_time', 0.0)
        pe = p.get('end_time', 0.0)
        pv = p.get('value', 0.0)
        if max(start, ps) < min(end, pe):
            vals.append(pv)
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    peak = max(vals)
    return round(min(1.0, 0.45 * mean + 0.55 * peak), 3)


TEXT_LEXICON = {
    # Curiosity gap (Loewenstein): info that begs a resolution
    "curiosity": [
        "you won't believe", "wait until", "wait till", "here's the thing",
        "here is the thing", "the problem is", "the reason", "turns out",
        "secret", "nobody tells you", "nobody talks about", "what happens",
        "the catch", "i'll show you", "i will show you", "let me show you",
        "listen to this", "you need to hear", "this is why", "and then",
    ],
    # Contrast / pattern interrupt: breaks expected flow
    "contrast": [
        "but", "however", "instead", "surprisingly", "actually",
        "the biggest mistake", "the worst", "the best", "stop doing",
        "never do", "always do", "i used to", "went from", "big mistake",
        "huge mistake", "wrong", "changed everything",
    ],
    # Emotional peak / punchline: high-arousal words (most-rewatched moments)
    "emotion": [
        "insane", "crazy", "amazing", "shocking", "incredible", "terrible",
        "horrible", "hate", "love", "best", "worst", "never", "always",
        "literally", "mind-blowing", "game changer", "game-changer",
        "nightmare", "disaster", "genius", "stupid", "dangerous", "scared",
        "fear", "hilarious", "ridiculous", "unbelievable", "awesome",
    ],
    # Specificity: numbers, units, concrete stakes (beats generalities)
    "specificity": [
        "percent", "million", "billion", "thousand", "years", "times",
        "steps", "ways", "reasons", "mistakes", "secrets", "dollar",
        "dollars", "hour", "minutes", "days", "months", "episode",
    ],
    # Open loop (Zeigarnik): promise of payoff later in the piece
    "open_loop": [
        "coming up", "later in this", "stay tuned", "at the end",
        "in a minute", "in a moment", "the answer", "i'll explain",
        "i will explain", "stick around", "part 2", "part two",
        "next video", "hold on", "but first",
    ],
    # Actionable value / advice framing
    "advice": [
        "you should", "you need to", "you have to", "make sure",
        "remember", "if you want", "don't forget", "pro tip", "the key",
        "the trick", "the best way", "how to", "why you", "here's how",
        "here is how", "number one", "the most important",
    ],
    # Laughter cues (punchlines in transcript form)
    "laughter": [
        "(laughter)", "(laughs)", "(laughing)", "haha", "hahaha",
        "lol", "that's funny", "so funny",
    ],
}
_SPECIFICITY_NUM_RE = re.compile(r"(?<!\d)\d{2,}(?:[.,]\d+)?")
_QUESTION_WORDS = ("who", "what", "why", "how", "when", "where", "is it",
                   "do you", "did you", "have you", "can you", "will you",
                   "are you", "would you")


def _phrase_hits(low_text: str, phrase: str) -> int:
    """Count word-boundary occurrences of a phrase in lowercased text."""
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", low_text))


def _text_evidence(text: str) -> float:
    """0..1 — structural hook evidence inside one transcript line."""
    if not text:
        return 0.0
    low = " " + text.lower() + " "
    raw = 0.0
    cats_hit = 0
    for cat, words in TEXT_LEXICON.items():
        cat_count = 0
        for w in words:
            cat_count += min(_phrase_hits(low, w), 2)
        if cat_count:
            cats_hit += 1
            raw += min(cat_count, 4)
    # Numbers carry specificity weight even without a lexicon word
    num_hits = min(len(_SPECIFICITY_NUM_RE.findall(low)), 3)
    if num_hits:
        raw += num_hits * 0.8
        cats_hit += 0  # counted within raw only
    # Direct question = built-in curiosity device
    if "?" in low:
        raw += 1.2
        cats_hit += 1
    elif any(_phrase_hits(low, q) for q in _QUESTION_WORDS):
        raw += 0.8
        cats_hit += 1
    if raw <= 0:
        return 0.0
    # Saturating transform: more distinct categories >> repeated same word
    score = 1.0 - 1.0 / (1.0 + (0.55 * raw + 0.65 * cats_hit))
    return round(min(1.0, score), 3)


def _line_end(line: dict) -> float:
    return float(line.get("end", line.get("start", 0.0) + line.get("duration", 0.0)))


def _lines_in(start: float, end: float, lines: List[dict]) -> List[dict]:
    return [
        l for l in lines
        if max(start, float(l.get("start", 0.0))) < min(end, _line_end(l))
    ]


def _window_text(lines: List[dict]) -> str:
    return " ".join(l.get("text", "") for l in lines if l.get("text")).strip()


def _window_text_evidence(lines: List[dict]) -> float:
    vals = [_text_evidence(l.get("text", "")) for l in lines]
    return max(vals, default=0.0)


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float, threshold: float = 0.45) -> bool:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    if union <= 0:
        return False
    return (inter / union) > threshold


def _title_from_text(text: str, max_words: int = 8) -> str:
    """Deterministic title for text-detected clips (no LLM call)."""
    words = re.split(r"\s+", (text or "").strip())
    words = [w for w in words if w][:max_words]
    if not words:
        return "Interesting moment"
    title = " ".join(words).strip(" ,.;:-")
    if len(title) > 60:
        title = title[:60].rsplit(" ", 1)[0] + "…"
    return title[:1].upper() + title[1:]


# ----------------------------------------------------------------
# Heatmap-only clip mining (concert mode / no-transcript fallback)
# ----------------------------------------------------------------
# Pure-retention selection: resample heatmap marks onto a dense time
# grid, smooth, and compare against a LONG local baseline — concerts
# are loud everywhere, so what matters is a moment standing out from
# its OWN neighborhood (3-min baseline), not an absolute threshold.
# Then pick local maxima, build clip windows, and non-max suppress.

def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"

def _heatmap_peaks(heatmap: List[dict], duration: float, target_secs: float,
                   count: int, start_bound: float = 0.0,
                   end_bound: Optional[float] = None) -> List[dict]:
    """Return clip-window dicts for the strongest retention peaks.

    Each dict: {start, end, hook (peak second), heat (0-1 evidence),
    score (0-1 composite), z (relative prominence)}. Sorted by score.
    """
    if not heatmap or duration <= 0 or count <= 0:
        return []
    end_bound = min(end_bound if end_bound is not None else duration, duration)
    span = end_bound - start_bound
    if span < 8:
        return []

    # 1) Dense grid (dt=2s; coarser only if the video is very short)
    dt = 2.0
    n = int(math.ceil(span / dt))
    if n < 12:
        dt = span / 12.0
        n = 12
    grid = [0.0] * n
    for p in heatmap:
        ps = float(p.get("start_time", 0.0))
        pe = float(p.get("end_time", 0.0))
        pv = float(p.get("value", 0.0))
        lo = max(0, int(math.floor((ps - start_bound) / dt)))
        hi = min(n - 1, int(math.floor((pe - start_bound) / dt)))
        for i in range(lo, hi + 1):
            cell_s = start_bound + i * dt
            cell_e = min(cell_s + dt, end_bound)
            if max(cell_s, ps) < min(cell_e, pe):
                grid[i] = pv  # marks tile the timeline; last-writer per cell

    def _smooth(vals, half):
        # Rolling mean over window = 2*half+1 cells (prefix sums, O(n))
        m = len(vals)
        pref = [0.0] * (m + 1)
        for i in range(m):
            pref[i + 1] = pref[i] + vals[i]
        out = [0.0] * m
        for i in range(m):
            a = max(0, i - half)
            b = min(m - 1, i + half)
            out[i] = (pref[b + 1] - pref[a]) / (b - a + 1)
        return out

    short_half = max(2, int(target_secs * 0.30 / dt))   # local shape (~±10s)
    long_half  = max(short_half * 3, int(180.0 / dt))   # ~3-min baseline
    S = _smooth(grid, short_half)
    B = _smooth(grid, long_half)
    rel = [S[i] - B[i] for i in range(n)]
    mean_r = sum(rel) / n
    var_r = sum((r - mean_r) ** 2 for r in rel) / n
    std_r = math.sqrt(var_r) if var_r > 0 else 0.0

    # 2) Local maxima on the smoothed curve, ranked by relative prominence
    peaks = []  # (index, prominence z, smoothed value)
    for i in range(1, n - 1):
        if S[i] >= S[i - 1] and S[i] >= S[i + 1] and (S[i] > S[i - 1] or S[i] > S[i + 1]):
            z = (rel[i] - mean_r) / std_r if std_r > 0 else 0.0
            peaks.append((i, z, S[i]))
    if not peaks:
        return []

    # Prominence floor: keep moments that genuinely stand out from their own
    # neighborhood (≥1.25σ above the ~3-min baseline, or ≥35% of the
    # strongest peak). This is what separates a crowd-favorite song from
    # ordinary curve noise on a loud concert heatmap.
    z_max = max(p[1] for p in peaks)
    z_min = max(1.25, 0.35 * z_max)
    strong = [p for p in peaks if p[1] >= z_min]
    if not strong:
        strong = [max(peaks, key=lambda p: p[1])]

    # Proximity suppression: one clip per standout moment — a wide spike
    # produces several adjacent local maxima; keep only the strongest peak
    # inside any target-window span.
    sep_cells = max(1, int(target_secs / dt))
    strong.sort(key=lambda p: p[1], reverse=True)
    suppressed = []
    for p in strong:
        if any(abs(p[0] - q[0]) < sep_cells for q in suppressed):
            continue
        suppressed.append(p)
    suppressed.sort(key=lambda p: p[0])  # back to chronological order

    min_dur = max(6.0, target_secs * 0.45)

    def _build_window(i, z):
        c = start_bound + i * dt
        s = max(start_bound, c - target_secs / 2.0)
        e = min(end_bound, c + target_secs / 2.0)
        if e - s < min_dur:
            return None
        heat = _heat_evidence(s, e, heatmap)
        z_norm = min(1.0, max(0.0, z) / 3.0)
        score = min(1.0, 0.5 * heat + 0.5 * z_norm)
        return {"start": round(s, 2), "end": round(e, 2), "hook": round(c, 2),
                "heat": heat, "z": z, "score": round(score, 4)}

    scored = []
    for i, z, _sv in suppressed:
        w = _build_window(i, z)
        if w:
            scored.append(w)
    if not scored:
        return []

    # 3) Greedy NMS: keep highest score, drop anything overlapping ≥45%
    scored.sort(key=lambda w: w["score"], reverse=True)
    picks = []
    for w in scored:
        if any(_overlaps(w["start"], w["end"], p["start"], p["end"]) for p in picks):
            continue
        picks.append(w)
        if len(picks) >= count:
            break
    # Flat-ish heatmaps: relax overlap tolerance so we still surface some clips
    if len(picks) < min(3, count):
        picks = []
        for w in scored:
            if any(_overlaps(w["start"], w["end"], p["start"], p["end"], 0.85) for p in picks):
                continue
            picks.append(w)
            if len(picks) >= count:
                break

    picks.sort(key=lambda w: w["score"], reverse=True)
    return picks


# ----------------------------------------------------------------
# DASH fragment-range miner — TRUE partial downloads for long sources
# ----------------------------------------------------------------
# YouTube's https DASH streams are single range-able fMP4 files:
#   [ftyp+moov] [sidx index] [moof/mdat fragments ...]
# The sidx box lists every fragment's byte size + duration, so a clip
# window maps to a contiguous byte range. We fetch ONLY that range per
# stream (video + audio), prepend the tiny ftyp+moov init, remux with
# ffmpeg -c copy, and trim — transferring ~seconds of media instead of
# the whole video. Any failure falls back to the full download path.

def _fmp4_sidx(buf: bytes):
    """Parse the first sidx box; return (timescale, abs_first_byte, entries).

    entries = [(size, duration_ts), ...] in media order. abs_first_byte is
    the file offset where the first fragment's data begins.
    """
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
    """Fetch ftyp+moov plus exactly the fragments overlapping [t0, t1]."""
    import requests as _rq
    headers = dict(fmt.get("http_headers") or {})
    # 1) Header: ftyp+moov+sidx. Fragmented-DASH moovs are tiny (no sample
    #    tables); sidx grows ~12 B/fragment (a 3.5h video ≈ 25 KB), so a
    #    128 KB head fetch covers both comfortably.
    head = _rq.get(fmt["url"], headers={**headers, "Range": "bytes=0-131071"}, timeout=60)
    if head.status_code not in (200, 206) or len(head.content) < 1024:
        raise ValueError(f"fMP4 header fetch failed (HTTP {head.status_code})")
    buf = head.content
    # locate moov end (init prefix) while walking to sidx
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

    # 2) Map [t0, t1] → fragment indices (durations in timescale units)
    from bisect import bisect_left
    bounds = [0]
    for _sz, dur_ts in entries:
        bounds.append(bounds[-1] + dur_ts)
    t0_ts = int(t0 * ts)
    t1_ts = int(t1 * ts)
    i0 = max(0, bisect_left(bounds, t0_ts) - 1)          # fragment containing t0
    i1 = max(i0, min(len(entries) - 1, bisect_left(bounds, t1_ts) - 1))
    if bounds[i1 + 1] <= t0_ts:                          # window past last frag
        raise ValueError("clip window beyond stream end")
    start_byte = abs_first + sum(e[0] for e in entries[:i0])
    end_byte = abs_first + sum(e[0] for e in entries[:i1 + 1])
    total = end_byte - start_byte
    if total <= 0 or total > 2_000_000_000:
        raise ValueError(f"invalid byte range {total}")

    # 3) Single ranged GET for the whole window slice
    rng = _rq.get(fmt["url"], headers={**headers, "Range": f"bytes={start_byte}-{end_byte - 1}"},
                  timeout=180)
    if rng.status_code != 206:
        raise ValueError(f"fMP4 range fetch failed (HTTP {rng.status_code})")
    media = rng.content
    if len(media) < total * 0.9:
        raise ValueError(f"short range read ({len(media)}/{total})")
    return init + media


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
    """Try a partial (fragment-range) export; raise on any failure."""
    import yt_dlp  # lazy: heavy scraping package

    with yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True, "noprogress": True,
        "socket_timeout": 15, "retries": 2,
    }) as ydl:
        info = ydl.extract_info(url, download=False)
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
    if not (vfmt.get("height") or 0) >= 360:
        raise ValueError("DASH video too small")

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

    # No further trim: the merged file starts at the first fragment boundary
    # AT OR BEFORE cut_start and ends at the last fragment boundary AT OR
    # AFTER cut_end — a strict SUPERSET of the padded window, so the hook is
    # never lost. (A post-merge `-ss` stream-copy trim proved unreliable on
    # this input — it dropped ~11s of the window — so we keep the fragment-
    # aligned cut and let the editor trim, same philosophy as the ±2s pad.)
    # Extra headroom is bounded by one fragment per side (video fragments are
    # ~2-7s on YouTube; the miner's caller validates the result's duration).
    return merged


def annotate_and_extend_clips(
    clips: List[ViralClip],
    lines: List[dict],
    heatmap: List[dict],
    target_secs: float,
    max_text_only: int = 6,
) -> List[ViralClip]:
    """Attach hybrid evidence to every clip (signal source + heat/text scores) and
    append text-only candidates: transcript windows with strong structural hook
    evidence but weak/no retention spike (the flat-retention safety net)."""
    covered = []
    out = []
    for c in clips:
        s, e = float(c.start_time), float(c.end_time)
        heat = _heat_evidence(s, e, heatmap)
        ls = _lines_in(s, e, lines)
        txt = _window_text_evidence(ls)
        c.heat_score = heat
        c.text_score = txt
        if heat >= 0.35 and txt >= 0.25:
            c.signal = "both"
        elif heat >= 0.35:
            c.signal = "retention"
        elif txt >= 0.30:
            c.signal = "text"
        else:
            c.signal = None
        out.append(c)
        covered.append((s, e))

    if not lines:
        return out

    min_dur = max(6.0, target_secs * 0.45)
    max_dur = target_secs * 1.9
    n = len(lines)

    # Seed windows: contiguous runs of lines with strong text evidence
    scored_idx = [i for i, l in enumerate(lines) if _text_evidence(l.get("text", "")) >= 0.30]
    if not scored_idx:
        return out
    groups = []
    cur = [scored_idx[0]]
    for i in scored_idx[1:]:
        if i - cur[-1] <= 2:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)

    candidates = []
    for g in groups:
        lo, hi = g[0], g[-1]

        def win_dur() -> float:
            return _line_end(lines[hi]) - float(lines[lo].get("start", 0.0))

        # Expand the window to a usable clip length (but never over max_dur)
        while lo > 0 and win_dur() < min_dur:
            lo -= 1
        while hi < n - 1 and win_dur() < min_dur:
            hi += 1
        s = float(lines[lo].get("start", 0.0))
        e = _line_end(lines[hi])
        if e - s < min_dur * 0.6 or e - s > max_dur:
            continue
        if any(_overlaps(s, e, a, b) for a, b in covered):
            continue

        group_lines = lines[g[0]:g[-1] + 1]
        txt = _window_text_evidence(group_lines)
        heat = _heat_evidence(s, e, heatmap)
        if txt < 0.40:
            continue
        composite = round(min(1.0, 0.62 * txt + 0.38 * heat) * 100)
        hook_line = max(group_lines, key=lambda l: _text_evidence(l.get("text", "")))
        seg_lines = _lines_in(s, e, lines)
        quotes = sorted(
            seg_lines, key=lambda l: _text_evidence(l.get("text", "")), reverse=True
        )[:2]
        seg_text = _window_text(seg_lines)
        candidates.append(ViralClip(
            title=_title_from_text(quotes[0].get("text", "") if quotes else seg_text),
            start_time=round(s, 2),
            end_time=round(e, 2),
            hook_time=round(float(hook_line.get("start", s)), 2),
            virality_score=max(1, composite),
            key_quotes=[q.get("text", "") for q in quotes if q.get("text")],
            transcript=seg_text,
            signal="text" if heat < 0.35 else "both",
            heat_score=heat,
            text_score=txt,
        ))

    candidates.sort(key=lambda c: c.virality_score, reverse=True)
    for c in candidates[:max_text_only]:
        out.append(c)
        covered.append((c.start_time, c.end_time))

    out.sort(key=lambda c: c.virality_score, reverse=True)
    return out

# ----------------------------------------------------------------
# Routes
# ----------------------------------------------------------------

def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Event string."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

@app.get("/api/health")
def health_check():
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    keys = get_supadata_keys()
    proxy = get_proxy_url()
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    return {
        "status": "ok",
        "message": "HEATCUT API is active",
        "is_vercel": is_vercel,
        "supadata_keys_count": len(keys),
        "proxy_configured": bool(proxy),
        "gemini_env_configured": has_gemini,
        # export workspace diagnostics: the configured path may be unwritable
        # (serverless), in which case exports use EXPORT_TMP_ROOT's fallback.
        "export_tmp_root": EXPORT_TMP_ROOT,
        "export_tmp_ok": EXPORT_TMP_OK,
    }



def parse_gemini_model_sort_key(name: str):
    """Sort key for Gemini models: parses major and minor versions (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5),
    tier (standard > lite/8b > preview/exp), so newest and most capable models come first."""
    name_clean = (name or "").split('/')[-1].lower()
    m = re.search(r'(\d+)(?:\.(\d+))?', name_clean)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) is not None else 0
    else:
        major, minor = 0, 0

    if 'lite' in name_clean or '8b' in name_clean:
        tier = 2
    elif 'exp' in name_clean or 'preview' in name_clean:
        tier = 1
    else:
        tier = 3

    return (major, minor, tier, name_clean)


KNOWN_FLASH_MODELS = [
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite',
    'gemini-2.0-flash',
    'gemini-2.0-flash-lite',
    'gemini-1.5-flash',
    'gemini-1.5-flash-8b',
]

def get_flash_models_for_key(client: genai.Client) -> List[str]:
    """Dynamically query all available flash models for the given API key.
    Discovers newer versions (e.g., 3.7, 3.6, 3.5) and earlier versions (2.5, 2.0, 1.5),
    merging with known fallback models and sorting in descending order of version/capability."""
    discovered = []
    try:
        models_page = client.models.list()
        for m in models_page:
            name = m.name or ""
            short_name = name.split('/')[-1]
            if "gemini" in short_name.lower() and "flash" in short_name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                # Exclude non-text, specialized, or non-generative tasks
                exclude_keywords = [
                    'tuning', 'thinking', 'vision', 'image', 'tts',
                    'omni', 'customtools', 'embed', 'realtime', 'robotics'
                ]
                if not any(x in short_name.lower() for x in exclude_keywords):
                    if short_name not in discovered:
                        discovered.append(short_name)
    except Exception as e:
        logger.warning(f"Could not dynamically list models: {e}")

    # Combine discovered with known flash models, preserving uniqueness
    combined_pool = list(dict.fromkeys(discovered + KNOWN_FLASH_MODELS))
    # Sort descending so newest versions (3.7, 3.6, 3.5, 2.5, 2.0, 1.5) are prioritized
    ordered = sorted(combined_pool, key=parse_gemini_model_sort_key, reverse=True)
    return ordered


@app.get("/api/models")
def list_available_models(api_key: str = ""):
    """Fetches list of available Gemini models using the user's API key, prioritizing Flash models (newest first)."""
    default_models = [
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
        'gemini-2.0-flash',
        'gemini-2.0-flash-lite',
        'gemini-1.5-flash',
        'gemini-2.5-pro'
    ]
    key_to_use = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key_to_use or key_to_use.lower() == "mock":
        return {"models": default_models}
    try:
        client = genai.Client(api_key=key_to_use)
        models_page = client.models.list()
        
        flash_models = []
        pro_models = []
        other_models = []
        
        for m in models_page:
            name = m.name or ""
            if "gemini" in name.lower():
                if m.supported_actions and "generateContent" not in m.supported_actions:
                    continue
                
                short_name = name.split('/')[-1]
                exclude_keywords = [
                    'tuning', 'thinking', 'vision', 'image', 'tts',
                    'omni', 'customtools', 'embed', 'realtime', 'robotics'
                ]
                if any(x in short_name.lower() for x in exclude_keywords):
                    continue
                
                if "flash" in short_name.lower():
                    if short_name not in flash_models:
                        flash_models.append(short_name)
                elif "pro" in short_name.lower():
                    if short_name not in pro_models:
                        pro_models.append(short_name)
                elif any(x in short_name.lower() for x in ['lite', 'exp']):
                    if short_name not in other_models:
                        other_models.append(short_name)
        
        # Sort flash models by version descending (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5)
        ordered_flash = sorted(
            list(dict.fromkeys(flash_models + KNOWN_FLASH_MODELS)),
            key=parse_gemini_model_sort_key,
            reverse=True
        )
        ordered_pro = sorted(pro_models, key=parse_gemini_model_sort_key, reverse=True)
        ordered_other = sorted(other_models, key=parse_gemini_model_sort_key, reverse=True)
        
        final_list = ordered_flash + ordered_pro + ordered_other
        if not final_list:
            final_list = default_models
            
        return {"models": final_list}
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        return {"models": default_models}

def _provider_llm_call(provider: str, model: str, prompt: str, api_key: str, base_url: Optional[str] = None) -> str:
    """Calls a non-Gemini LLM provider and returns raw text for JSON parsing.

    provider: 'openai' | 'anthropic' | 'openai-compatible'
    OpenAI-compatible base URL must include the API root, e.g.
    https://api.openai.com/v1 or https://api.deepseek.com/v1. This one path
    covers OpenAI, DeepSeek, OpenRouter, Groq, Ollama proxies, etc.
    """
    import requests as _rq

    if not api_key:
        raise ValueError(f"API key required for provider '{provider}' (set it in the app's AI Settings or via {provider.upper()}_API_KEY env).")

    json_instruction = (
        "\n\nRespond with ONLY a single JSON object, no markdown, no commentary:\n"
        '{"summary": "1-2 sentence summary with 2-4 hashtags", '
        '"clips": [{"title": "catchy max 8 words", "start_time": float, '
        '"end_time": float, "hook_time": float, "virality_score": 1-100, '
        '"key_quotes": ["quote"], "title_suggestion": "", '
        '"caption_suggestion": "", "hashtag_suggestion": ""}]}'
    )
    user_content = prompt + json_instruction

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": 8192,
            "temperature": 0.2,
            "system": "You are a precise viral video clip finder. You always return valid JSON matching the requested schema exactly.",
            "messages": [{"role": "user", "content": user_content}],
        }
    else:
        base = (base_url or "https://api.openai.com/v1").rstrip("/")
        if base.endswith("/chat/completions"):
            url = base
        else:
            url = base + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        body = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "You are a precise viral video clip finder. You always return valid JSON matching the requested schema exactly."},
                {"role": "user", "content": user_content},
            ],
        }

    resp = _rq.post(url, json=body, headers=headers, timeout=150)
    if resp.status_code != 200:
        raise ValueError(f"{provider} API error {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        if provider == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"{provider} unexpected response shape: {e}") from e


def _parse_json_response(raw_text: str) -> Optional[dict]:
    """Robustly extracts the first JSON object from an LLM text response."""
    if not raw_text:
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fallback: find the outermost {...} block (handles stray prose)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


@app.post("/api/analyze")
async def analyze_video(request: AnalyzeRequest, http_request: Request):
    """Stream real-time progress via Server-Sent Events, then deliver the final result."""
    # ── Fair-use guards (per caller IP, best-effort) ────────────────────────
    # The server may hold its own GEMINI_API_KEY / Supadata keys, which makes
    # /api/analyze spendable by whoever has the link. These caps keep a small
    # circle usable without letting a stranger drain the quota.
    ip = _client_ip(http_request)
    if _rate_limited(f"analyze:{ip}", ANALYZE_RATE_LIMIT, 3600.0):
        raise HTTPException(status_code=429, detail=(
            f"Batas pemakaian: maks {ANALYZE_RATE_LIMIT} analisis per jam dari IP ini. "
            "Coba lagi nanti, atau isi API key sendiri."
        ))
    if _rate_limited(f"analyze-day:{ip}", ANALYZE_DAILY_CAP, 86400.0):
        raise HTTPException(status_code=429, detail=(
            f"Batas harian tercapai (maks {ANALYZE_DAILY_CAP} analisis/hari dari IP ini). "
            "Coba lagi besok, atau isi API key sendiri."
        ))

    async def stream():
        provider = (request.provider or "gemini").strip().lower()
        # Normalize analysis mode: auto (default), podcast (text-first), concert (heatmap-only)
        mode = (request.mode or "auto").strip().lower()
        if mode not in ("auto", "podcast", "concert"):
            mode = "auto"
        _prov_env = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
                     "anthropic": "ANTHROPIC_API_KEY", "openai-compatible": "OPENAI_API_KEY"}.get(provider, "GEMINI_API_KEY")
        gemini_key = (request.api_key or os.environ.get(_prov_env) or '').strip()
        is_mock = gemini_key.lower() == "mock"

        if not gemini_key and mode != "concert":
            yield _sse({"error": "AI API key is required. Add it under AI Settings (⚙️), or configure it on the server via env.", "status": 400})
            return

        # ── Step 1: Extract video ID & metadata ─────────────────────────────
        video_id = extract_video_id(request.url)
        if not video_id:
            if not is_mock:
                yield _sse({"error": "Invalid YouTube URL. Please check the link and try again.", "status": 400})
                return
            video_id = "dQw4w9WgXcQ"

        yield _sse({
            "step": 1,
            "step_progress": 30,
            "overall_progress": 8,
            "stage": "Connecting to YouTube",
            "detail": "Connecting to YouTube & fetching video metadata...",
            "message": "Connecting to YouTube — fetching video title and duration..."
        })

        try:
            if request.client_heatmap is not None:
                # Client-asserted metadata from the device loopback heatmap worker:
                # real yt-dlp data fetched over the user's residential connection
                # (see README "Heatmap via your device (loopback worker)").
                metadata = {
                    "title": (request.client_title or "").strip() or "YouTube Video",
                    "duration": float(request.client_duration or 0.0),
                    "heatmap": request.client_heatmap,
                    "is_live": False,
                    "live_status": "not_live",
                }
                logger.info(f"Client-supplied metadata/heatmap accepted ({len(request.client_heatmap)} heatmap points).")
            else:
                metadata = await asyncio.to_thread(fetch_video_metadata, request.url)
            title    = metadata["title"]
            duration = metadata["duration"]
            heatmap  = metadata.get("heatmap") or []
            is_live  = metadata.get("is_live", False)
            live_status = metadata.get("live_status", "not_live")
            yield _sse({
                "step": 1,
                "step_progress": 100,
                "overall_progress": 25,
                "stage": "Video Verified",
                "detail": f"Loaded metadata for \"{title[:45]}\" ({int(duration)}s)",
                "message": f"Connected — \"{title[:45]}\" ({int(duration)}s)"
            })
        except Exception as e:
            if is_mock:
                title = "Mock YouTube Video"
                duration = 212.0
                heatmap = []
                is_live = False
                live_status = "not_live"
                yield _sse({
                    "step": 1,
                    "step_progress": 100,
                    "overall_progress": 25,
                    "stage": "Video Verified",
                    "detail": "Loaded mock video metadata (212s)",
                    "message": "Mock video metadata loaded"
                })
            else:
                msg = e.detail if isinstance(e, HTTPException) else str(e)
                yield _sse({"error": f"Failed to fetch video details: {msg}", "status": 500})
                return

        logger.info(f"Metadata fetched: title='{title}', duration={duration}s, heatmap_pts={len(heatmap)}")

        # ── Step 2: Heatmap ──────────────────────────────────────────────────
        yield _sse({
            "step": 2,
            "step_progress": 40,
            "overall_progress": 35,
            "stage": "Scraping Retention",
            "detail": "Extracting viewer replay telemetry and retention curve...",
            "message": "Scraping player viewer retention curve..."
        })
        if heatmap:
            yield _sse({
                "step": 2,
                "step_progress": 100,
                "overall_progress": 50,
                "stage": "Retention Decoded",
                "detail": f"Viewer retention heatmap loaded — {len(heatmap)} audience interest data points parsed.",
                "message": f"Viewer retention heatmap loaded — {len(heatmap)} data points scraped."
            })
        else:
            yield _sse({
                "step": 2,
                "step_progress": 100,
                "overall_progress": 50,
                "stage": "Dialogue Fallback",
                "detail": "No heatmap curve available — relying on full transcript dialogue analysis.",
                "message": "No heatmap available for this video — will rely on transcript content analysis."
            })

        # ── Step 3: Transcript ───────────────────────────────────────────────
        if request.subtitles:
            yield _sse({
                "step": 3,
                "step_progress": 30,
                "overall_progress": 55,
                "stage": "Parsing Subtitles",
                "detail": "Parsing custom SRT/TXT subtitle timestamps...",
                "message": "Parsing manual subtitles..."
            })
            try:
                transcript_lines = parse_manual_subtitles(request.subtitles, duration)
                if not transcript_lines:
                    raise Exception("Custom subtitles parsed into empty array.")
                yield _sse({
                    "step": 3,
                    "step_progress": 100,
                    "overall_progress": 70,
                    "stage": "Subtitles Ready",
                    "detail": f"Custom subtitles parsed — {len(transcript_lines)} timestamped lines loaded.",
                    "message": f"Custom subtitles parsed — {len(transcript_lines)} lines loaded successfully."
                })
            except Exception as e:
                yield _sse({"error": f"Failed to parse manual subtitles: {str(e)}", "status": 400})
                return
        else:
            # ── Mode-aware transcript acquisition ─────────────────────────
            #   concert: user asserts no useful speech — skip the fetch
            #            entirely and mine the retention heatmap only.
            #   auto:    try transcript; if none exists AND a heatmap is
            #            available, fall back to the heatmap-only path
            #            instead of erroring (concerts, instrumentals, ...).
            #   podcast: transcript is the primary signal — required.
            if mode == "concert" and not is_mock:
                transcript_lines = []
                logger.info("Concert mode — skipping transcript fetch (heatmap-only analysis).")
                yield _sse({
                    "step": 3,
                    "step_progress": 100,
                    "overall_progress": 60,
                    "stage": "Subtitles Skipped",
                    "detail": "Concert mode — no speech expected; analyzing viewer retention heatmap only.",
                    "message": "Concert mode — skipping subtitles, using retention heatmap."
                })
            else:
                yield _sse({
                    "step": 3,
                    "step_progress": 30,
                    "overall_progress": 55,
                    "stage": "Fetching Subtitles",
                    "detail": "Querying YouTube caption tracks & auto-generated transcripts...",
                    "message": "Fetching subtitles — trying video's original language..."
                })
                try:
                    transcript_lines = await asyncio.to_thread(fetch_transcript, video_id)
                    if mode == "concert" and transcript_lines:
                        # Auto-captions on music are usually "[Music]" noise —
                        # ignore them in concert mode, they add no signal.
                        logger.info(f"Concert mode — ignoring {len(transcript_lines)} auto-caption lines (noise).")
                        transcript_lines = []
                    yield _sse({
                        "step": 3,
                        "step_progress": 100,
                        "overall_progress": 70,
                        "stage": "Subtitles Ready",
                        "detail": f"Subtitles loaded — {len(transcript_lines)} dialogue sentences with timestamps ready.",
                        "message": f"Subtitles loaded — {len(transcript_lines)} lines parsed successfully."
                    })
                except Exception as e:
                    if is_mock:
                        transcript_lines = [
                            {"text": "Hello and welcome to this video.",            "start":  0.0, "duration": 3.0},
                            {"text": "Today we are looking at how this app works.",  "start":  3.0, "duration": 4.0},
                            {"text": "It finds viral hotspots and highlights them.",  "start":  7.0, "duration": 4.0},
                            {"text": "Most people think it's magic.",               "start": 11.0, "duration": 3.0},
                            {"text": "But it uses YouTube player heatmaps.",         "start": 14.0, "duration": 4.0},
                            {"text": "And processes them with Gemini AI models.",    "start": 18.0, "duration": 4.0},
                            {"text": "This is changing how editors crop videos.",    "start": 22.0, "duration": 5.0},
                            {"text": "If you want to grow on TikTok, try it.",      "start": 27.0, "duration": 5.0},
                            {"text": "We will explore the code next.",               "start": 32.0, "duration": 3.0},
                        ]
                        yield _sse({
                            "step": 3,
                            "step_progress": 100,
                            "overall_progress": 70,
                            "stage": "Subtitles Ready",
                            "detail": "Mock mode — 9 sample dialogue lines loaded.",
                            "message": "Mock mode — using sample transcript."
                        })
                    elif mode == "auto" and heatmap:
                        # No captions, but real viewer-retention telemetry exists
                        # → continue on the heatmap-only (concert) path.
                        transcript_lines = []
                        logger.info("Auto mode — no transcript, heatmap present. Falling back to heatmap-only analysis.")
                        yield _sse({
                            "step": 3,
                            "step_progress": 100,
                            "overall_progress": 60,
                            "stage": "Subtitles Unavailable",
                            "detail": "No transcript found — falling back to viewer-retention heatmap analysis.",
                            "message": "No subtitles — switching to retention-heatmap analysis."
                        })
                    else:
                        # Provide a helpful error message if the video is live or recently completed
                        if is_live or live_status in ('is_live', 'is_upcoming', 'post_live'):
                            yield _sse({
                                "error": (
                                    "No subtitles could be retrieved because this video is currently live, "
                                    "upcoming, or recently completed (post-live processing). Subtitles are only "
                                    "available once the live stream ends and YouTube finishes processing the video. "
                                    "You can upload custom subtitles manually to analyze this video."
                                ),
                                "status": 400
                            })
                        else:
                            msg = e.detail if isinstance(e, HTTPException) else str(e)
                            if mode == "podcast":
                                yield _sse({
                                    "error": (
                                        f"No transcript available for this video ({msg}). Podcast mode needs "
                                        "spoken content — switch to Concert mode (retention heatmap only) or "
                                        "upload custom subtitles/setlist manually."
                                    ),
                                    "status": 400
                                })
                            else:
                                yield _sse({"error": msg, "status": 400})
                        return

        # Estimate duration from transcript if missing
        if duration == 0.0 and transcript_lines:
            last = transcript_lines[-1]
            duration = last.get("start", 0.0) + last.get("duration", 0.0)


        # Slice transcript based on custom search range if provided.
        # (Heatmap-only runs — no transcript — still honor the bounds; the
        # heatmap peak miner uses start_bound/end_bound below.)
        start_bound = 0.0
        end_bound = duration
        if request.range_start is not None or request.range_end is not None:
            start_bound = request.range_start if request.range_start is not None else 0.0
            end_bound = request.range_end if request.range_end is not None else duration

            if start_bound < 0.0:
                start_bound = 0.0
            if end_bound > duration:
                end_bound = duration

            if start_bound >= end_bound:
                yield _sse({"error": "Invalid search range: start time must be less than end time.", "status": 400})
                return

            if transcript_lines:
                filtered_lines = []
                for line in transcript_lines:
                    ls = line.get("start", 0.0)
                    le = ls + line.get("duration", 0.0)
                    if max(ls, start_bound) < min(le, end_bound):
                        filtered_lines.append(line)
                
                transcript_lines = filtered_lines
                if not transcript_lines:
                    yield _sse({"error": f"No subtitles found in the specified range {start_bound}s to {end_bound}s.", "status": 400})
                    return
            
            duration = end_bound - start_bound
            logger.info(f"Filtered analysis to custom range: {start_bound}s to {end_bound}s (duration: {duration}s)")

        # Enrich transcript with heatmap engagement scores
        enriched_transcript = []
        for line in transcript_lines:
            ls   = line.get("start", 0.0)
            ld   = line.get("duration", 0.0)
            le   = ls + ld
            score = get_average_heatmap_value(ls, le, heatmap)
            enriched_transcript.append({
                "start":      round(ls, 2),
                "end":        round(le, 2),
                "text":       line.get("text", ""),
                "engagement": round(score, 3)
            })

        # ── Mock short-circuit ───────────────────────────────────────────────
        if is_mock:
            mock_stages = [
                ("Context Assembly", "Aligning 9 transcript dialogue lines with retention telemetry...", 30, 78),
                ("Viral Hook & Curiosity Detection", "Scanning transcript dialogue for viral hooks & curiosity gaps...", 65, 88),
                ("Virality Scoring & Selection", "Calculating virality coefficients and formatting clip candidates...", 92, 95),
            ]
            for s_name, s_detail, s_prog, o_prog in mock_stages:
                yield _sse({
                    "step": 4,
                    "step_progress": s_prog,
                    "overall_progress": o_prog,
                    "stage": s_name,
                    "detail": s_detail,
                    "model": "gemini-2.5-flash (Mock)",
                    "message": f"Mock AI ({s_name}): {s_detail}"
                })
                await asyncio.sleep(0.7)

            mock_clips = [
                ViralClip(title="Finding hotspots using heatmaps",  start_time=11.0, end_time=22.0, hook_time=14.0, virality_score=95,
                          key_quotes=["Uses YouTube player heatmaps.", "Processes using Gemini AI."],
                          transcript="Most people think it's magic. But it uses YouTube player heatmaps.",
                          title_suggestion="Unlock Video Virality Secrets",
                          caption_suggestion="Stop guessing what works! Here's how to use heatmaps to find viral hotspots in seconds. 🔥",
                          hashtag_suggestion="#viralclips #videoediting #heatmaps #aitools"),
                ViralClip(title="Grow on TikTok or Reels",          start_time=22.0, end_time=32.0, hook_time=27.0, virality_score=88,
                          key_quotes=["Changing how editors crop videos.", "If you want to grow on TikTok, try it."],
                          transcript="This is changing how editors crop videos. If you want to grow on TikTok, try it.",
                          title_suggestion="The Ultimate TikTok Growth Hack",
                          caption_suggestion="Want to scale your TikTok views? This tool will revolutionize your workflow. 🚀",
                          hashtag_suggestion="#tiktokgrowth #reels #shorts #editingtips"),
                ViralClip(title="Introductory overview of the tool", start_time=0.0,  end_time=11.0, hook_time=3.0, virality_score=72,
                          key_quotes=["Hello and welcome.", "Finds viral hotspots."],
                          transcript="Hello and welcome. It finds viral hotspots and highlights them.",
                          title_suggestion="Meet HeatCut AI",
                          caption_suggestion="Say hello to your new AI co-editor. Find the absolute best parts of any video instantly.",
                          hashtag_suggestion="#heatcut #aiediting #growthmindset"),
            ]
            mock_heatmap = [
                HeatmapPoint(start_time=i*10.0, end_time=(i+1)*10.0,
                             value=0.2 + (0.6 if i in [2,5,8,12,16] else 0.1))
                for i in range(20)
            ] if not heatmap else [
                HeatmapPoint(start_time=float(pt.get('start_time',0.0)),
                             end_time=float(pt.get('end_time',0.0)),
                             value=float(pt.get('value',0.0)))
                for pt in heatmap
            ]
            # Hybrid evidence pass (labels + text-only candidates) so mock mode
            # demos the same signal model as the real pipeline.
            _mock_target = {"15s": 18, "30s": 32, "60s": 60}.get(request.duration, 32)
            mock_clips = annotate_and_extend_clips(
                mock_clips,
                transcript_lines,
                [pt.model_dump() for pt in mock_heatmap],
                _mock_target,
            )
            result = AnalyzeResponse(
                video_id=video_id, title=title, duration=duration or 200.0,
                heatmap=mock_heatmap,
                summary="Mock analysis: this video explains how HEATCUT works. #aitools #videoediting #productivity",
                clips=mock_clips,
                model="Mock Gemini"
            )
            yield _sse({
                "step": 4,
                "step_progress": 100,
                "overall_progress": 100,
                "stage": "Analysis Complete",
                "detail": f"Generated {len(mock_clips)} clip candidates successfully.",
                "done": True,
                "result": result.model_dump()
            })
            return

        # ── Heatmap-only path (concert mode / auto fallback) ─────────────
        # No transcript lines survived, but viewer-retention telemetry is
        # available: select clip windows algorithmically from heatmap peaks.
        # No LLM call happens here — no API key required. (Cheap LLM titling
        # of the top-K peaks is a separate enhancement on top of this path.)
        if not transcript_lines and heatmap:
            if duration <= 0:
                yield _sse({"error": "Cannot analyze: no transcript and no video duration available.", "status": 400})
                return
            target_secs = {"15s": 18, "30s": 32, "60s": 60}.get(request.duration, 32)
            if request.target_clip_count:
                want = min(50, max(1, request.target_clip_count))
            else:
                want = min(20, max(8, int(duration / 240)))  # ~1 per 4 min, 8..20
            yield _sse({
                "step": 4,
                "step_progress": 20,
                "overall_progress": 78,
                "stage": "Retention Peak Detection",
                "detail": "Scanning viewer-retention curve for rewatch peaks against a local baseline...",
                "message": "Heatmap-only mode — detecting retention peaks..."
            })
            windows = _heatmap_peaks(heatmap, duration, target_secs, want, start_bound, end_bound)
            if not windows:
                yield _sse({
                    "error": "No clear retention peaks found — the audience curve is too flat to mine. "
                             "Try Podcast mode (needs captions) or upload custom subtitles/setlist.",
                    "status": 422
                })
                return
            yield _sse({
                "step": 4,
                "step_progress": 75,
                "overall_progress": 92,
                "stage": "Selecting Top Rewinds",
                "detail": f"{len(windows)} retention peaks found — building clip windows around the strongest...",
                "message": f"Found {len(windows)} rewatch peaks — assembling clips."
            })

            # ── Optional LLM pass: catchy titles/captions for the peaks ──
            # The heatmap path works fully keyless; when a key IS present, ask
            # the model only to write copy for the windows we already picked
            # (tiny prompt, no transcript). Any failure keeps algorithmic titles.
            titles_by_start = {}
            titled_model = None
            if gemini_key and not is_mock:
                yield _sse({
                    "step": 4,
                    "step_progress": 85,
                    "overall_progress": 95,
                    "stage": "Titling Peaks",
                    "detail": f"Drafting viral titles/captions for {len(windows)} retention peaks...",
                    "message": "Polishing titles & captions for the top rewatch moments..."
                })
                try:
                    req_model = (request.model or 'gemini-2.5-flash').strip()
                    win_desc = "\n".join(
                        f"{w['start']:.0f}|{w['end']:.0f}|{w['hook']:.0f}|{w['score']:.2f}"
                        for w in windows
                    )
                    title_prompt = (
                        "You are a viral-clip titling assistant. Below are the most-rewatched moments "
                        f"from a video with NO transcript (concert/live music/music video):\n"
                        f"Video title: {title}\n"
                        "Columns: start|end|hook|rewatch_score(0-1)\n"
                        f"---\n{win_desc}\n---\n"
                        "For EVERY moment return: a catchy short title (max 8 words, emoji ok), a punchy "
                        "alternative title suggestion, an engaging short caption for TikTok/Reels/Shorts "
                        "that mentions the hook timestamp, and 3-5 lowercase hashtags. If the video title "
                        "language is obvious, match it; otherwise use English.\n"
                        'Return ONLY JSON: {"clips": [{"start_time": float, "title": "...", '
                        '"title_suggestion": "...", "caption_suggestion": "...", "hashtag_suggestion": "..."}]}'
                    )
                    if provider == "gemini":
                        _gclient = genai.Client(api_key=gemini_key)

                        def _gemini_title_call():
                            _resp = _gclient.models.generate_content(
                                model=req_model,
                                contents=title_prompt,
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    temperature=0.2,
                                )
                            )
                            return _resp.text or ""

                        raw_title = await asyncio.wait_for(
                            asyncio.to_thread(_gemini_title_call), timeout=60)
                    else:
                        raw_title = await asyncio.wait_for(
                            asyncio.to_thread(
                                _provider_llm_call, provider, req_model, title_prompt,
                                request.api_key or gemini_key, request.base_url or None,
                            ),
                            timeout=60,
                        )
                    parsed_title = _parse_json_response(raw_title) or {}
                    for item in parsed_title.get("clips", []):
                        try:
                            st = round(float(item.get("start_time", -1)), 1)
                        except (TypeError, ValueError):
                            continue
                        if st >= 0 and isinstance(item, dict):
                            titles_by_start[st] = item
                    if titles_by_start:
                        titled_model = req_model
                        logger.info(f"Heatmap titling applied to {len(titles_by_start)}/{len(windows)} peaks via {req_model}.")
                except Exception as e:
                    logger.info(f"Heatmap titling skipped ({str(e)[:120]}) — keeping algorithmic titles.")

            heat_clips = []
            for n_i, w in enumerate(windows, start=1):
                meta = titles_by_start.get(round(w["start"], 1)) or {}
                clip_title = (meta.get("title") or "").strip() or f"Peak {n_i} — {_fmt_ts(w['hook'])}"
                clip_title_sug = (meta.get("title_suggestion") or "").strip()
                clip_caption = (meta.get("caption_suggestion") or "").strip() or (
                    f"🔥 Most-rewatched moment at {_fmt_ts(w['hook'])} — "
                    f"{title[:60]} #viral #highlights"
                )
                clip_hashtags = (meta.get("hashtag_suggestion") or "").strip() or "#viral #shorts #highlights"
                heat_clips.append(ViralClip(
                    title=clip_title,
                    start_time=w["start"],
                    end_time=w["end"],
                    hook_time=w["hook"],
                    virality_score=max(1, min(98, int(round(w["score"] * 100)))),
                    key_quotes=[],
                    transcript="",
                    title_suggestion=clip_title_sug,
                    caption_suggestion=lowercase_hashtags_in_string(clip_caption),
                    hashtag_suggestion=lowercase_hashtags_in_string(clip_hashtags),
                    signal="retention",
                    heat_score=round(w["heat"], 3),
                    text_score=0.0,
                ))
            heat_pts = [
                HeatmapPoint(
                    start_time=float(pt.get('start_time', 0.0)),
                    end_time=float(pt.get('end_time', 0.0)),
                    value=float(pt.get('value', 0.0))
                )
                for pt in heatmap
            ]
            summary = (f"Retention analysis of \"{title}\": {len(heat_clips)} most-rewatched moments "
                       f"found from the viewer heatmap (no transcript needed). #viral #highlights")
            heat_result = AnalyzeResponse(
                video_id=video_id,
                title=title,
                duration=duration,
                heatmap=heat_pts,
                summary=summary,
                clips=heat_clips,
                transcript=None,
                model=titled_model or "heatmap-peaks"
            )
            yield _sse({
                "step": 4,
                "step_progress": 100,
                "overall_progress": 100,
                "stage": "Analysis Complete",
                "detail": f"Generated {len(heat_clips)} retention-peak clips from the heatmap.",
                "done": True,
                "result": heat_result.model_dump()
            })
            logger.info(f"Heatmap-only analysis complete — {len(heat_clips)} retention clips (mode={mode}).")
            return

        is_long_video = duration > 3600
        if request.target_clip_count:
            N = request.target_clip_count
            if N <= 5:
                min_clips = max(1, N - 1)
                max_clips = N + 2
            elif N <= 10:
                min_clips = max(1, N - 2)
                max_clips = N + 3
            else:
                min_clips = N - 5
                max_clips = N + 5
            clip_range = f"{min_clips}-{max_clips}"
        else:
            clip_range = "15-60" if is_long_video else "10-30"

        # ── Step 4: Build prompt ─────────────────────────────────────────────
        transcript_dump = []
        for line in enriched_transcript:
            eng = f"|{line['engagement']:.2f}" if heatmap and line['engagement'] > 0 else ""
            transcript_dump.append(f"{line['start']:.1f}|{line['end']:.1f}{eng} {line['text']}")

        MAX_LINES = 2500 if is_long_video else 800
        if len(transcript_dump) > MAX_LINES:
            # Head-truncation silently dropped the tail of long videos (a 3h
            # source exceeds 2500 caption lines), so moments in the last hour
            # were never visible to the LLM. Keep the intro (context) plus an
            # even sample across the whole timeline instead.
            total = len(transcript_dump)
            keep_head = min(int(MAX_LINES * 0.15), 200)
            budget = MAX_LINES - keep_head
            rest = transcript_dump[keep_head:]
            step = len(rest) / budget
            sampled = [rest[int(i * step)] for i in range(budget)]
            transcript_dump = transcript_dump[:keep_head] + sampled
            logger.warning(f"Transcript {total} lines — keeping {keep_head} intro + even sample of {budget} across whole timeline.")
            del total, keep_head, budget, rest, step, sampled

        transcript_text = "\n".join(transcript_dump)
        dur_range   = {"15s": "10-20s", "30s": "20-40s", "60s": "45-75s"}.get(request.duration, "20-40s")
        heatmap_note = (
            "Columns: start|end|audience_interest(0-1). Higher = viewers actually REWATCHED that moment. "
            "Treat sustained high-interest peaks as strong evidence and prefer clip windows that contain them."
            if heatmap else
            "No audience interest data. Use content hooks, energy, and story arcs."
        )
        focus_instruction = ""
        if request.custom_prompt and request.custom_prompt.strip():
            focus_instruction = f"CRITICAL FOCUS: The user specifically wants you to find clips matching the following query/theme: \"{request.custom_prompt.strip()}\". Prioritize and tailor your selection of viral clips to fit this request, while still ensuring they make good standalone clips.\n\n"

        prompt = (
            f"You are a viral video clip finder.\n"
            f"Find {clip_range} short-form clip candidates from this YouTube transcript for TikTok/Reels/Shorts.\n\n"
            f"Title: {title}\n"
            f"Duration Range: {int(start_bound)}s to {int(end_bound)}s (Length: {int(duration)}s) | Target clip length: {dur_range}\n"
            f"{heatmap_note}\n"
            f"{focus_instruction}"
            f"Match output language to transcript language.\n\n"
            f"Transcript (start|end[|interest] text):\n---\n{transcript_text}\n---\n\n"
            f"SELECTION RUBRIC — proven viral-clip structure (relative weight):\n"
            f"1) Curiosity gap / open loop (25): the moment opens a question or promise the viewer must see resolved.\n"
            f"2) Punchline / emotional peak (20): joke payoff, strong opinion, surprise, high-arousal statement (the most-rewatched moments).\n"
            f"3) Self-contained (15): makes sense with ZERO context — no 'as I said earlier', no dangling pronouns.\n"
            f"4) Specificity (15): numbers, names, concrete claims — beats generalities.\n"
            f"5) Standalone value (15): a real insight, warning, or actionable tip even without visuals.\n"
            f"6) Contrast / pattern interrupt (10): 'but / actually / the problem is' shifts that break expected flow.\n"
            f"Score every candidate 1-100 against this rubric (virality_score).\n"
            f"Rules: use exact seconds from transcript; clips must start/end at sentence boundaries; do not overlap.\n"
            f"hook_time must equal the timestamp of the first sentence that creates the hook.\n"
            f"Return {clip_range} clips sorted by virality_score desc.\n"
        )

        requested_model = (request.model or 'gemini-2.5-flash').strip()
        if any(dep in requested_model.lower() for dep in ['gemini-1.0', 'gemini-pro-vision']):
            logger.info(f"Requested model '{requested_model}' is outdated. Upgrading to gemini-2.5-flash.")
            requested_model = 'gemini-2.5-flash'

        yield _sse({
            "step": 4,
            "step_progress": 10,
            "overall_progress": 72,
            "stage": "Context Assembly",
            "detail": f"Aligning {len(transcript_dump)} dialogue segments with engagement data for {requested_model}...",
            "model": requested_model,
            "message": f"Assembling prompt and engagement context for {requested_model}..."
        })

        # ── Step 4: AI call — Gemini (dynamic Flash fallback) or other provider ──
        if not gemini_key and not is_mock:
            # Reached the LLM stage without a key (concert mode + manual
            # setlist subtitles, or auto mode where the transcript path won).
            yield _sse({"error": "AI API key is required. Add it under AI Settings (⚙️), or configure it on the server via env.", "status": 400})
            return
        models_to_try = [requested_model]
        if provider == "gemini":
            client = genai.Client(api_key=gemini_key)

            # Discover all available Flash models for the user's API key
            discovered_flash = await asyncio.to_thread(get_flash_models_for_key, client)

            # Build models_to_try:
            # 1. Start with the requested model
            # 2. Append all discovered and known flash models in version descending order (e.g. 3.7, 3.6, 3.5, 2.5, 2.0, 1.5)
            #    so all available flash models are tried before giving up
            for fm in discovered_flash:
                if fm not in models_to_try:
                    models_to_try.append(fm)
            for km in KNOWN_FLASH_MODELS:
                if km not in models_to_try:
                    models_to_try.append(km)

        logger.info(f"Provider '{provider}' model chain prepared: {models_to_try}")

        response = None
        last_error = None
        encountered_quota_error = None
        analysis_data = None
        successful_model = None

        for idx, model_name in enumerate(models_to_try):
            next_model_hint = models_to_try[idx + 1] if idx + 1 < len(models_to_try) else None

            MAX_RETRIES = 2
            
            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    wait = 2
                    yield _sse({
                        "step": 4,
                        "step_progress": 25,
                        "overall_progress": 75,
                        "stage": "Transient Retry",
                        "detail": f"{model_name} busy — waiting {wait}s before retry ({attempt + 1}/{MAX_RETRIES})...",
                        "model": model_name,
                        "message": f"{model_name} is busy — waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}..."
                    })
                    await asyncio.sleep(wait)
                
                yield _sse({
                    "step": 4,
                    "step_progress": 18,
                    "overall_progress": 74,
                    "stage": "Neural Model Dispatch",
                    "detail": f"Dispatched {len(transcript_dump)} lines to {model_name} (attempt {attempt + 1})...",
                    "model": model_name,
                    "message": f"Calling {model_name} (attempt {attempt + 1}/{MAX_RETRIES})..."
                })
                
                # Execute the provider call (Gemini structured output, or raw-text
                # JSON from OpenAI / Anthropic / OpenAI-compatible endpoints)
                if provider == "gemini":
                    task = asyncio.create_task(asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=VideoAnalysis,
                            temperature=0.2,
                        )
                    ))
                else:
                    task = asyncio.create_task(asyncio.to_thread(
                        _provider_llm_call,
                        provider, model_name, prompt,
                        request.api_key or gemini_key,
                        request.base_url or None,
                    ))
                
                call_start = asyncio.get_event_loop().time()
                while not task.done():
                    done, _ = await asyncio.wait([task], timeout=2.0)
                    if not done:
                        elapsed = int(asyncio.get_event_loop().time() - call_start)
                        
                        if elapsed < 5:
                            stage = "Neural Context Loading"
                            detail = f"Transmitting {len(transcript_dump)} timestamped dialogue segments to {model_name}..."
                            step_prog = min(35, 12 + elapsed * 4)
                        elif elapsed < 12:
                            stage = "Retention Spike Cross-Analysis"
                            detail = f"Correlating viewer retention peaks against speaker dialogue to isolate viral moments..."
                            step_prog = min(55, 35 + int((elapsed - 5) * 3))
                        elif elapsed < 20:
                            stage = "Viral Hook & Curiosity Detection"
                            detail = f"Scanning transcript dialogue for opening hooks, punchlines, controversial takes & emotional peaks..."
                            step_prog = min(72, 55 + int((elapsed - 12) * 2.2))
                        elif elapsed < 30:
                            stage = "Coherence & Sentence Boundary Snapping"
                            detail = f"Ensuring clip candidates start and end naturally on sentence boundaries without mid-word cuts..."
                            step_prog = min(85, 72 + int((elapsed - 20) * 1.3))
                        elif elapsed < 42:
                            stage = "Virality Scoring & Selection"
                            detail = f"Calculating virality coefficients (1-100) and selecting the top {clip_range} highest potential clips..."
                            step_prog = min(92, 85 + int((elapsed - 30) * 0.7))
                        else:
                            stage = "Social Media Metadata Synthesis"
                            detail = f"Drafting attention-grabbing titles, social captions, and targeted hashtags ({elapsed}s)..."
                            step_prog = min(95, 92 + min(3, int((elapsed - 42) * 0.3)))

                        overall_prog = 70 + int(step_prog * 0.28)
                        yield _sse({
                            "step": 4,
                            "keepalive": True,
                            "step_progress": step_prog,
                            "overall_progress": overall_prog,
                            "stage": stage,
                            "detail": detail,
                            "model": model_name,
                            "elapsed": elapsed,
                            "message": f"[{model_name} | {elapsed}s] {stage}: {detail}"
                        })
                
                try:
                    resp_candidate = await task
                    last_error = None

                    # Parse structured response
                    parsed_data = None
                    if isinstance(resp_candidate, str):
                        parsed_data = _parse_json_response(resp_candidate)
                    elif hasattr(resp_candidate, 'parsed') and resp_candidate.parsed is not None:
                        parsed = resp_candidate.parsed
                        parsed_data = {
                            "summary": getattr(parsed, 'summary', ''),
                            "clips": [
                                {
                                    "title": getattr(c, 'title', ''),
                                    "start_time": getattr(c, 'start_time', 0.0),
                                    "end_time": getattr(c, 'end_time', 0.0),
                                    "hook_time": getattr(c, 'hook_time', None),
                                    "virality_score": getattr(c, 'virality_score', 0),
                                    "key_quotes": getattr(c, 'key_quotes', []),
                                    "title_suggestion": getattr(c, 'title_suggestion', ''),
                                    "caption_suggestion": getattr(c, 'caption_suggestion', ''),
                                    "hashtag_suggestion": getattr(c, 'hashtag_suggestion', ''),
                                }
                                for c in (getattr(parsed, 'clips', []) or [])
                            ]
                        }
                    elif resp_candidate.text:
                        raw_text = resp_candidate.text.strip()
                        if raw_text.startswith("```"):
                            raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
                            raw_text = re.sub(r"\n?```$", "", raw_text)
                        try:
                            parsed_data = json.loads(raw_text)
                        except Exception as json_err:
                            logger.warning(f"JSON parsing error from {model_name}: {json_err}")
                            parsed_data = None

                    if parsed_data is not None:
                        clips_found = len(parsed_data.get('clips', []))
                        if clips_found == 0 and next_model_hint is not None:
                            logger.warning(f"{model_name} returned 0 clips. Will try next flash model {next_model_hint}...")
                            yield _sse({
                                "step": 4,
                                "step_progress": 40,
                                "overall_progress": 78,
                                "stage": "Flash Model Fallback",
                                "detail": f"{model_name} returned 0 clips — switching to {next_model_hint} for deeper extraction...",
                                "model": next_model_hint,
                                "message": f"{model_name} returned 0 clips — switching to {next_model_hint}..."
                            })
                            last_error = Exception(f"{model_name} returned 0 clips")
                            break
                        
                        response = resp_candidate
                        analysis_data = parsed_data
                        successful_model = model_name
                        break
                    else:
                        last_error = Exception(f"{model_name} returned empty or unparseable response")
                        break
                        
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    logger.warning(f"Error from {model_name} (attempt {attempt + 1}): {e}")
                    
                    if any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                        encountered_quota_error = e
                        break

                    if any(x in err_str for x in ('404', 'not found', 'not supported')):
                        break
                    
                    is_server_busy = any(x in err_str for x in ('503', 'unavailable', 'overloaded', '500', 'internal'))
                    if not is_server_busy:
                        break
            
            if analysis_data is not None and response is not None:
                break
                
            if next_model_hint is not None:
                err_summary = "quota reached" if any(x in str(last_error).lower() for x in ('429', 'quota', 'rate limit')) else \
                              "not available or deprecated" if "404" in str(last_error) else \
                              "temporarily busy"
                yield _sse({
                    "step": 4,
                    "step_progress": 35,
                    "overall_progress": 76,
                    "stage": "Flash Fallback",
                    "detail": f"{model_name} {err_summary} — switching to fallback {next_model_hint}...",
                    "model": next_model_hint,
                    "message": f"{model_name} {err_summary} — switching to flash fallback model {next_model_hint}..."
                })

        if analysis_data is None:
            # If any model in the fallback chain suffered quota exhaustion, prioritize showing the quota explanation
            error_to_report = encountered_quota_error or last_error
            if error_to_report is not None:
                err_str = str(error_to_report).lower()
                if any(x in err_str for x in ('429', 'quota', 'resource exhausted', 'rate limit')):
                    yield _sse({
                        "error": "Quota limit reached across all available Gemini Flash models for this API key. Free keys have a request limit per minute. Please change your API key, generate a fresh free key at aistudio.google.com, or wait 30–60 seconds before trying again.",
                        "status": 429
                    })
                elif any(x in err_str for x in ('503', 'unavailable', 'overloaded')):
                    yield _sse({
                        "error": "Google Gemini servers are currently experiencing high demand across all Flash models. Please change to a different Gemini API key or wait a few moments and try again.",
                        "status": 503
                    })
                elif any(x in err_str for x in ('401', '403', 'api_key', 'invalid', 'permission')):
                    yield _sse({
                        "error": "Invalid or restricted Gemini API key. Please change your API key or generate a new free key at aistudio.google.com.",
                        "status": 401
                    })
                elif any(x in err_str for x in ('404', 'not found', 'not supported')):
                    models_preview = ', '.join(models_to_try[:3])
                    yield _sse({
                        "error": f"All tested Gemini Flash models ({models_preview}...) were unavailable or not supported for this API key. Please change your Gemini API key or generate a new one at aistudio.google.com.",
                        "status": 404
                    })
                else:
                    logger.error(f"Gemini error after all fallback models: {error_to_report}")
                    yield _sse({
                        "error": f"AI analysis failed across all available Flash models ({str(error_to_report)}). Please change your Gemini API key or try again in a few moments.",
                        "status": 500
                    })
            else:
                yield _sse({
                    "error": "No response received after trying all available Gemini Flash models. Please change your Gemini API key or try again in a few moments.",
                    "status": 500
                })
            return

        # Fallback clip synthesis if 0 clips were returned after all models
        if len(analysis_data.get('clips', [])) == 0 and enriched_transcript:
            logger.info("Generating fallback clips from heatmap and transcript segments...")
            sorted_lines = sorted(enriched_transcript, key=lambda l: l.get('engagement', 0.0), reverse=True)
            candidate_starts = []
            for l in sorted_lines:
                s = l['start']
                if not any(abs(s - existing) < 25.0 for existing in candidate_starts):
                    candidate_starts.append(s)
                if len(candidate_starts) >= 5:
                    break
            
            fallback_clips_list = []
            for i, st in enumerate(candidate_starts):
                target_len = 30.0 if request.duration == "30s" else 15.0 if request.duration == "15s" else 60.0
                et = min(duration, st + target_len)
                seg_lines = [l['text'] for l in enriched_transcript if max(l['start'], st) < min(l['end'], et)]
                seg_text = " ".join(seg_lines).strip()
                preview = seg_text[:60] + "..." if len(seg_text) > 60 else seg_text or f"Viral Highlight #{i+1}"
                fallback_clips_list.append({
                    "title": f"Key Highlight #{i+1}",
                    "start_time": st,
                    "end_time": et,
                    "hook_time": st,
                    "virality_score": max(70, int(95 - i * 5)),
                    "key_quotes": [seg_text[:80]] if seg_text else [],
                    "title_suggestion": f"Must Watch Moment #{i+1}",
                    "caption_suggestion": f"Key highlight from video: {preview} #viral #trending",
                    "hashtag_suggestion": "#viral #shorts #trending"
                })
            analysis_data['clips'] = fallback_clips_list
            if not analysis_data.get('summary'):
                analysis_data['summary'] = f"Analysis of \"{title}\" identifying {len(fallback_clips_list)} key segments. #viral #highlights"

        clip_count = len(analysis_data.get('clips', []))
        yield _sse({
            "step": 4,
            "step_progress": 98,
            "overall_progress": 98,
            "stage": "Clip Verification & Alignment",
            "detail": f"Verified {clip_count} clip segments with precise video timestamps and key quotes.",
            "model": successful_model or requested_model,
            "message": f"Found {clip_count} viral clip candidates with {successful_model or requested_model} — reconstructing transcripts..."
        })
        logger.info(f"Gemini analysis complete with {successful_model or requested_model}. Found {clip_count} clips.")
        logger.info(f"Gemini analysis complete with {successful_model or requested_model}. Found {clip_count} clips.")

        # Reconstruct clip transcripts from enriched_transcript
        final_clips = []
        for raw_clip in analysis_data.get('clips', []):
            start = raw_clip.get('start_time', 0.0)
            end   = raw_clip.get('end_time', 0.0)
            hook  = raw_clip.get('hook_time')
            if hook is None or not (start <= hook <= end):
                hook = start
            
            clip_lines = [
                line.get("text", "")
                for line in enriched_transcript
                if max(line.get("start", 0.0), start) < min(line.get("end", 0.0), end)
            ]
            
            # Ensure hashtags are always lowercase
            caption_sug = lowercase_hashtags_in_string(raw_clip.get('caption_suggestion', ''))
            hashtag_sug = lowercase_hashtags_in_string(raw_clip.get('hashtag_suggestion', ''))
            
            final_clips.append(ViralClip(
                title=raw_clip.get('title', ''),
                start_time=start,
                end_time=end,
                hook_time=hook,
                virality_score=raw_clip.get('virality_score', 0),
                key_quotes=raw_clip.get('key_quotes') or [],
                transcript=" ".join(clip_lines),
                title_suggestion=raw_clip.get('title_suggestion', ''),
                caption_suggestion=caption_sug,
                hashtag_suggestion=hashtag_sug
            ))

        # Hybrid evidence pass: label every clip with its signal source
        # (retention / text / both), attach heat+text evidence scores, and
        # append text-only candidates — moments with strong transcript
        # structure but no retention spike (flat-retention safety net).
        target_secs = {"15s": 18, "30s": 32, "60s": 60}.get(request.duration, 32)
        final_clips = annotate_and_extend_clips(
            final_clips, enriched_transcript, heatmap or [], target_secs
        )
        logger.info(f"Hybrid pass complete — {len(final_clips)} clips after evidence labeling + text-only candidates.")

        response_heatmap = [
            HeatmapPoint(
                start_time=float(pt.get('start_time', 0.0)),
                end_time=float(pt.get('end_time', 0.0)),
                value=float(pt.get('value', 0.0))
            )
            for pt in (heatmap or [])
        ]

        response_transcript = [
            TranscriptLine(
                start=float(line["start"]),
                end=float(line["end"]),
                text=line["text"],
                engagement=line.get("engagement")
            )
            for line in enriched_transcript
        ]

        # Ensure hashtags are lowercase in the overall summary
        clean_summary = lowercase_hashtags_in_string(analysis_data.get("summary", ""))

        final_result = AnalyzeResponse(
            video_id=video_id,
            title=title,
            duration=duration,
            heatmap=response_heatmap,
            summary=clean_summary,
            clips=final_clips,
            transcript=response_transcript,
            model=successful_model or requested_model
        )

        yield _sse({"done": True, "result": final_result.model_dump()})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


# ----------------------------------------------------------------
# Clip export: raw segment download (yt-dlp + ffmpeg stream-copy)
# ----------------------------------------------------------------
class ExportRequest(BaseModel):
    video_id: str = Field(..., description="YouTube video ID")
    start_time: float = Field(..., ge=0, description="Clip start in seconds")
    end_time: float = Field(..., gt=0, description="Clip end in seconds")
    title: Optional[str] = Field(default=None, description="Optional video title used for the download filename")


@app.post("/api/export")
async def export_clip(request: ExportRequest):
    """Cuts a RAW clip from the source video — stream-copy, no re-encode, no
    crop, original resolution/quality — and serves it as a normal mp4 download.

    Every export is padded with 2s of context BEFORE the requested start and
    2s AFTER the requested end (clamped to 0 at the video start). The user's
    editing phase (CapCut) does the precise frame-accurate trim, so the extra
    headroom guarantees the hook moment is never cut off by keyframe
    alignment (~1-2s). Stream-copy keeps the cut lossless and instant.

    CapCut exposes no public automation API, so the handoff is a plain file
    download that the user imports into CapCut manually.
    """
    if request.end_time <= request.start_time:
        raise HTTPException(status_code=400, detail="end_time must be greater than start_time.")
    if request.end_time - request.start_time > 3600:
        raise HTTPException(status_code=400, detail="Clip too long (max 60 minutes).")

    # ── Context padding (±2s) for the editing-phase finish ───────────────
    pad_pre = 2.0
    pad_post = 2.0
    cut_start = max(0.0, request.start_time - pad_pre)
    cut_end = request.end_time + pad_post
    logger.info(f"Export {request.video_id} requested {request.start_time:.1f}-{request.end_time:.1f}s "
                f"→ padded cut {cut_start:.1f}-{cut_end:.1f}s (+{pad_pre:.0f}s/+{pad_post:.0f}s).")

    import shutil
    import subprocess
    import tempfile
    import uuid
    from starlette.background import BackgroundTask

    tmpdir = _new_export_tmp_dir_or_500()

    def _run() -> str:
        import yt_dlp  # lazy: heavy scraping package
        url = f"https://www.youtube.com/watch?v={request.video_id}"
        dur = cut_end - cut_start
        out_mp4 = os.path.join(tmpdir, "clip.mp4")

        # ── Fast path: DASH fragment-range miner ─────────────────────────
        # True partial transfer: fetch only the fragments overlapping the
        # padded clip window (see _frag_miner_export). Falls back silently
        # to the full download below on ANY failure (no sidx, odd codecs,
        # bot-check, ffmpeg hiccup, ...). Wasted bytes on failure ≈ the
        # window slice only — never a second full download.
        try:
            mined = _frag_miner_export(url, tmpdir, cut_start, cut_end)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", mined],
                capture_output=True, text=True, timeout=60)
            try:
                out_dur = float(probe.stdout.strip())
            except ValueError:
                out_dur = 0.0
            # Superset semantics: the fragment cut must COVER the whole
            # padded window (never less), with headroom bounded by ~1 video
            # fragment per side (≈ +2-15s). Anything outside → legacy path.
            if out_dur >= dur * 0.9 and out_dur <= dur + 25.0:
                logger.info(f"Fragment-miner export OK — {out_dur:.1f}s covers {dur:.1f}s window.")
                return mined
            raise ValueError(f"miner output duration mismatch ({out_dur:.1f}s vs window {dur:.1f}s)")
        except Exception as e:
            logger.warning(f"Fragment-miner export failed ({str(e)[:120]}) — falling back to full download.")
            _clear_export_artifacts(tmpdir)

        # ── Fallback: full DASH download + local stream-copy cut ─────────
        # Used when the fragment miner cannot run (no sidx, unsupported
        # codec pair, bot-check during extraction, merge/trim failure).
        # Correct but heavy for long sources — downloads the WHOLE video
        # before cutting.
        dl_opts: Any = {
            "format": "bv*[height<=?1080]+ba/b[height<=?1080]/b",
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(tmpdir, "src.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 15,
            "retries": 3,
        }
        cf = get_yt_cookiefile()
        if cf:
            dl_opts["cookiefile"] = cf

        def _do_download(opts: Any) -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        try:
            _do_download(dl_opts)
        except Exception as e:
            if _botcheck_message(e):
                # Bot-check on the default web client — retry once with the
                # tv/android player client (often exempt, no login needed).
                logger.warning("Export hit YouTube bot-check — retrying with tv/android player client.")
                _do_download({
                    **dl_opts,
                    "extractor_args": {"youtube": ["player_client=tv,android"]},
                })
            else:
                raise
        src = None
        for f in sorted(os.listdir(tmpdir)):
            if f.startswith("src."):
                src = os.path.join(tmpdir, f)
                break
        if not src:
            raise ValueError("Could not download the source video.")

        cmd = [
            "ffmpeg", "-y", "-ss", f"{cut_start:.3f}", "-i", src,
            "-t", f"{dur:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-map", "0", out_mp4,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if proc.returncode != 0 or not os.path.exists(out_mp4) or os.path.getsize(out_mp4) == 0:
            # Frame-accurate fallback: re-encode the segment (slower, exact).
            cmd2 = [
                "ffmpeg", "-y", "-ss", f"{cut_start:.3f}", "-i", src,
                "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-c:a", "aac", "-b:a", "128k", "-map", "0", out_mp4,
            ]
            proc2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=1800)
            if proc2.returncode != 0 or not os.path.exists(out_mp4) or os.path.getsize(out_mp4) == 0:
                err = (proc2.stderr or proc.stderr or "unknown ffmpeg error")[-300:]
                raise ValueError(f"ffmpeg cut failed: {err}")
        return out_mp4

    try:
        out_mp4 = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001 — surface a clean API error to the client
        _finish_export_job(tmpdir)
        friendly = _botcheck_message(e)
        raise HTTPException(status_code=500, detail=f"Export failed: {friendly or e}")

    base = (request.title or "").strip() or f"clip-{request.video_id}"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()[:60] or f"clip-{request.video_id}"
    filename = f"{slug}-{int(request.start_time)}-{int(request.end_time)}s.mp4"
    return FileResponse(
        out_mp4,
        media_type="video/mp4",
        filename=filename,
        # Heartbeat keeps running until the response is fully streamed out;
        # _finish_export_job then stops it and removes the workspace.
        background=BackgroundTask(_finish_export_job, tmpdir),
    )

