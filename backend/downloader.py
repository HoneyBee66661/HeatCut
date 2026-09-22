"""Video-downloader core — stdlib only, no yt-dlp/FastAPI imports.

Used by the server (`backend/main.py`) AND mirrored by the device worker
(`heatcut_worker.py`, which is a standalone single file and therefore carries
its own copy of the tiny route glue). Everything here is pure: URL/platform
classification, timestamp parsing, format selection and filename building — so
it can be unit-tested without a network, a scraper or a framework.

Why a separate module: the DOWNLOAD paths themselves reuse the studio's
expensive media plumbing (DASH fragment-range miner `_frag_miner_export` → HLS
segment miner `_hls_segment_export` → whole-file download) which lives in
`backend/main.py`. Keeping the *decision* logic pure keeps that reuse honest —
the same windows, formats and filenames come out regardless of which backend
answers the request.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- platforms

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com")
TIKTOK_HOSTS = ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")
INSTAGRAM_HOSTS = ("instagram.com", "instagr.am")

PLATFORM_LABEL = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "other": "Other",
}

# Extractor quirks worth surfacing in the UI instead of a bare failure.
PLATFORM_NOTE = {
    "youtube": "Partial (timestamp) windows come from the same DASH/HLS miners the studio uses.",
    "tiktok": "TikTok usually serves one progressive mp4 — full downloads are fast; timestamps fall back to HLS segments.",
    "instagram": "Instagram needs a logged-in cookies.txt on the host doing the download (posts/reels only).",
    "other": "Any site yt-dlp supports works; partial windows need HLS or DASH media.",
}


def _host(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", (url or "").strip())
    host = (m.group(1) if m else (url or "").strip()).lower()
    return host.split("@")[-1].split(":")[0]


def classify_url(url: str) -> Dict[str, Any]:
    """{'platform', 'label', 'url', 'note'} for a pasted link."""
    raw = (url or "").strip()
    host = _host(raw)
    platform = "other"
    if any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS):
        platform = "youtube"
    elif any(host == h or host.endswith("." + h) for h in TIKTOK_HOSTS):
        platform = "tiktok"
    elif any(host == h or host.endswith("." + h) for h in INSTAGRAM_HOSTS):
        platform = "instagram"
    return {
        "platform": platform,
        "label": PLATFORM_LABEL.get(platform, "Other"),
        "url": raw,
        "note": PLATFORM_NOTE.get(platform, ""),
    }


def is_supported_url(url: str) -> bool:
    """A paste-able http(s) link (yt-dlp decides the rest)."""
    return bool(re.match(r"^https?://[^\s]+$", (url or "").strip()))


# ---------------------------------------------------------------- timestamps

_TIME_RE = re.compile(r"^(\d{1,3}(?::\d{1,2}){0,2})(?:[.,](\d{1,3}))?$")


def parse_time_token(token: str) -> Optional[float]:
    """'90' | '1:30' | '01:02:03.5' -> seconds. None when it is not a time."""
    tok = (token or "").strip()
    if not tok:
        return None
    m = _TIME_RE.match(tok)
    if not m:
        # '90s' / '2m' / '1h30m' style labels
        m2 = re.match(r"^(?:(\d+(?:[.,]\d+)?)h)?(?:(\d+(?:[.,]\d+)?)m)?(?:(\d+(?:[.,]\d+)?)s)?$", tok, re.I)
        if not m2 or not any(m2.groups()):
            return None
        total = 0.0
        for value, factor in zip(m2.groups(), (3600.0, 60.0, 1.0)):
            if value:
                total += float(value.replace(",", ".")) * factor
        return total
    parts = m.group(1).split(":")
    frac = float("0." + m.group(2)) if m.group(2) else 0.0
    total = 0.0
    for p in parts:
        total = total * 60.0 + float(p)
    return total + frac


def _looks_like_clock(token: str) -> bool:
    """True when a bare number is really a clock value written without 'm'.

    '1:30' is unambiguous; the caller only needs this for the separator-free
    '1:30 2:00' shorthand, which we deliberately do NOT support (see
    parse_windows).
    """
    return ":" in token.strip()


def parse_window_text(text: str) -> Optional[Dict[str, Any]]:
    """One window out of '1:20-2:05' | '80 125' | '1:20 2:05' | '1:20~2:05'.

    Returns {'start', 'end'} or None when the text is not a window.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.match(r"^(.*?)\s*(?:-|–|—|~|to)\s*(.+)$", raw)
    if m and parse_time_token(m.group(1)) is not None and parse_time_token(m.group(2)) is not None:
        start, end = parse_time_token(m.group(1)), parse_time_token(m.group(2))
    else:
        # '1:20 2:05' — whitespace separated, both sides must be timed tokens.
        parts = re.split(r"\s+", raw)
        if len(parts) != 2:
            return None
        start, end = parse_time_token(parts[0]), parse_time_token(parts[1])
        if start is None or end is None:
            return None
    if start is None or end is None:
        return None
    return {"start": max(0.0, start), "end": end}


def parse_windows(text: str, duration: float = 0.0, max_windows: int = 20) -> List[Dict[str, Any]]:
    """Parse a pasted timestamp list into ordered, validated windows.

    Accepts one window per line or several comma/semicolon separated:
        1:20-2:05, 3:00-3:40
        0:10 0:45
        90-150
    Raises ValueError with a human message on anything else — the API maps it
    to HTTP 400, so the UI never has to guess.
    """
    chunks: List[str] = []
    for line in (text or "").splitlines():
        for piece in re.split(r"[;,\n]", line):
            if piece.strip():
                chunks.append(piece.strip())
    if not chunks:
        raise ValueError("No windows given — write one per line, e.g. 1:20-2:05")
    if len(chunks) > max_windows:
        raise ValueError(f"Too many windows ({len(chunks)}) — max {max_windows} per request.")

    out: List[Dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, 1):
        win = parse_window_text(chunk)
        if win is None:
            raise ValueError(f"'{chunk}' is not a window — use start-end, e.g. 1:20-2:05")
        start, end = float(win["start"]), float(win["end"])
        if end <= start:
            raise ValueError(f"'{chunk}': end must be after start")
        if duration and start >= duration:
            raise ValueError(f"'{chunk}': starts past the end of the video ({format_clock(duration)})")
        if duration:
            end = min(end, duration)
        if end - start < 0.5:
            raise ValueError(f"'{chunk}': window shorter than 0.5s")
        out.append({
            "index": idx,
            "start": round(start, 3),
            "end": round(end, 3),
            "label": f"{format_clock(start)}-{format_clock(end)}",
        })
    return out


def format_clock(seconds: float) -> str:
    """142 -> '2:22', 3725 -> '1:02:05'."""
    total = max(0, int(float(seconds or 0)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- formats

AUDIO_EXT_PREF = ("m4a", "mp4", "aac", "mp3", "opus", "webm")


def _is_audio_only(f: dict) -> bool:
    return f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")


def _is_video_only(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")


def _is_muxed(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")


CAP = 1080
VIDEO_EXTS = ("mp4", "webm", "mkv", "mov", "m4v", "flv", "3gp")


def _is_real_video(f: dict) -> bool:
    """A downloadable video rendition — not a storyboard/mhtml thumbnail sheet."""
    fid = str(f.get("format_id") or "")
    if fid.startswith("sb"):
        return False
    if str(f.get("protocol") or "").startswith("mhtml"):
        return False
    return str(f.get("ext") or "") in VIDEO_EXTS


def quality_menu(formats: List[dict], cap: int = CAP) -> List[Dict[str, Any]]:
    """Distinct video heights a user can ask for, best first (cap=1080 rule).

    The 1080p cap is deliberate and shared with the miner: uncapped pickers
    choose 2160p when it exists and multiply the bytes by ~4. Storyboard
    (`sb*`/mhtml) entries are dropped — they are thumbnail sheets, not video.
    """
    best: Dict[int, Dict[str, Any]] = {}
    for f in formats or []:
        if _is_audio_only(f) or not f.get("url") or not _is_real_video(f):
            continue
        h = int(f.get("height") or 0)
        if h <= 0 or h > cap:
            continue
        cur = best.get(h)
        score = (f.get("tbr") or 0, 1 if _is_muxed(f) else 0)
        if cur is None or score > (cur.get("tbr") or 0, 1 if cur.get("_muxed") else 0):
            best[h] = {
                "height": h,
                "label": f"{h}p",
                "ext": f.get("ext"),
                "tbr": f.get("tbr"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "format_id": str(f.get("format_id")),
                "_muxed": _is_muxed(f),
            }
    out = sorted(best.values(), key=lambda x: x["height"], reverse=True)
    for item in out:
        item.pop("_muxed", None)
    return out


def audio_menu(formats: List[dict]) -> List[Dict[str, Any]]:
    """Audio-only streams (best first) for the probe payload."""
    items = [f for f in (formats or []) if f.get("url") and _is_audio_only(f)]
    items.sort(key=lambda f: (AUDIO_EXT_PREF.index(f.get("ext")) if f.get("ext") in AUDIO_EXT_PREF
                              else len(AUDIO_EXT_PREF), -(f.get("abr") or f.get("tbr") or 0)))
    return [{
        "ext": f.get("ext"),
        "abr": f.get("abr") or f.get("tbr"),
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "format_id": str(f.get("format_id")),
    } for f in items[:5]]


def has_hls(formats: List[dict]) -> bool:
    return any("m3u8" in str(f.get("protocol") or "") and f.get("url") for f in formats or [])


def has_rangeable_dash(formats: List[dict]) -> bool:
    """A pair the DASH fragment miner could cut (https fMP4 video + audio)."""
    v = any(_is_video_only(f) and f.get("protocol") == "https" and f.get("ext") == "mp4"
            and f.get("url") and 0 < (f.get("height") or 0) <= 1080 for f in formats or [])
    a = any(_is_audio_only(f) and f.get("protocol") == "https" and f.get("ext") in ("m4a", "mp4")
            and f.get("url") for f in formats or [])
    return bool(v and a)


def probe_payload(info: dict, url: str) -> Dict[str, Any]:
    """UI-facing description of a pasted link (no media is fetched)."""
    platform = classify_url(url)
    formats = (info or {}).get("formats") or []
    dur = (info or {}).get("duration") or 0
    try:
        dur = float(dur or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {
        "platform": platform["platform"],
        "platform_label": platform["label"],
        "url": (info or {}).get("webpage_url") or url,
        "input_url": url,
        "id": (info or {}).get("id"),
        "title": (info or {}).get("title") or "",
        "uploader": (info or {}).get("uploader") or (info or {}).get("channel") or "",
        "duration": round(dur, 3),
        "duration_label": format_clock(dur) if dur else "",
        "thumbnail": (info or {}).get("thumbnail") or "",
        "is_live": bool((info or {}).get("is_live")),
        "qualities": quality_menu(formats),
        "audio_streams": audio_menu(formats),
        "has_audio": any(f.get("acodec") not in (None, "none") for f in formats),
        "hls": has_hls(formats),
        "dash": has_rangeable_dash(formats),
        "partial_supported": has_hls(formats) or has_rangeable_dash(formats),
        "note": platform["note"],
    }


# ---------------------------------------------------------------- filenames

def safe_slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return slug[:limit] or "download"


def filename_for(title: str, kind: str, ext: str, label: str = "") -> str:
    """'<title>[-<window>].<ext>' — extension decides the container, always."""
    parts = [safe_slug(title), safe_slug(label) if label else ""]
    stem = "-".join(p for p in parts if p)
    return f"{stem}.{ext.lstrip('.')}"


# ---------------------------------------------------------------- messaging

_BOILERPLATE = (
    r"\s*please report this issue on\s*https?://\S+.*$",
    r"\s*Confirm you are on the latest version.*$",
    r"\s*filling out the appropriate issue template\.?",
    r"\s*See\s+https?://github\.com/yt-dlp/\S+.*$",
    r"\s*You might want to use --?\S+.*$",
)

# Measured on this host (2026-09): a datacenter IP gets the same wall the
# YouTube bot check throws, per platform. Say what to actually do about it.
PLATFORM_FIX_HINT = {
    "tiktok": ("TikTok refused this request from the server's IP. A worker running on your own "
               "device/connection usually gets through — keep the device worker online and retry."),
    "instagram": ("Instagram only serves media to a logged-in session: put a cookies.txt "
                  "(logged in) next to the worker/backend, or retry from your own device worker."),
    "youtube": ("YouTube is asking for a human check from the server's IP. Cookies on the host "
                "doing the download (yt_cookies.txt) or your own device worker fixes it."),
}


def friendly_error(message: str, platform: str = "") -> str:
    """A user-facing one-liner: yt-dlp's tracker boilerplate stripped, hint added.

    Users should never read "please report this issue on github" — that is our
    problem, not theirs — and a wall needs a next step, not a stack trace.
    """
    text = (message or "").strip()
    for pattern in _BOILERPLATE:
        text = re.sub(pattern, "", text, flags=re.I | re.S).strip()
    text = re.sub(r"\s{2,}", " ", text)
    lowered = text.lower()
    hint = ""
    if platform in PLATFORM_FIX_HINT and any(
            word in lowered for word in ("unexpected response", "empty media", "bot", "cookies",
                                         "sign in", "login", "403", "forbidden")):
        hint = " " + PLATFORM_FIX_HINT[platform]
    return f"{text or 'the site refused the request.'}{hint}"
