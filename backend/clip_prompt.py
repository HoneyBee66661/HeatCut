"""Clip-plan prompt — the user-editable parameter block behind a cut list.

Campaign prep in this app is deterministic on purpose, so "custom prompt" does
NOT mean "an LLM reads your prose and guesses". It means the user edits the
ACTUAL parameters the planner uses: the clip-selection method, clip length,
min/max bounds, per-source count, head/tail padding, the timestamp reading
rules, a manual timestamp list and the source-scrape command used when the
brief ships no video source at all.

The prompt is a small INI-ish block (the UI pre-fills it from the current
settings, so it always shows exactly what will run):

    # HeatCut clip plan
    method = requirement        # requirement | campaign | heatmap | even
    target = 30s                # clip length
    min = 15s                   # campaign minimum wins when it is higher
    max = 60s
    per_source = 6
    max_clips = 15
    pad_before = 2s             # extra head room before every window
    pad_after = 2s
    timestamp_format = auto     # auto | mm:ss | hh:mm:ss | seconds
    timestamp_source = both     # requirement | manual | both
    timestamps =                # your own windows, one per line
      12:30 - 13:45  chorus
      1:02:00 - 1:02:40
    source_search = ytsearch10: <what to scrape when the brief has no video>
    source_count = 5
    notes =                     # free direction for the copy pass

Rules that keep it safe to run for a non-technical user:

* an unknown key or an unusable value NEVER aborts a plan — it comes back as a
  warning and the previous/default value stays in force;
* nothing here is executed as a shell command: `source_search` is parsed into a
  YouTube search query and run through the app's own yt-dlp search path;
* the block is stored with the prep session, so a plan can always be traced
  back to the parameters that produced it.

Stdlib only, dual-importable (`backend.clip_prompt` / `clip_prompt`).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- methods

METHOD_DEFAULT = "requirement"
METHODS = ("requirement", "campaign", "heatmap", "even")

_METHOD_ALIASES = {
    "requirement": "requirement", "req": "requirement", "require": "requirement",
    "brief": "requirement", "requirements": "requirement", "text": "requirement",
    "auto": "requirement",
    "campaign": "campaign", "campaign-timestamps": "campaign", "timestamps": "campaign",
    "brief-timestamps": "campaign",
    "heatmap": "heatmap", "retention": "heatmap", "peaks": "heatmap", "peak": "heatmap",
    "hot": "heatmap", "telemetry": "heatmap",
    "even": "even", "even-spread": "even", "spread": "even", "flat": "even",
    "none": "even",
}

TIMESTAMP_SOURCES = ("both", "requirement", "manual")
TIMESTAMP_FORMATS = ("auto", "mm:ss", "hh:mm:ss", "seconds")

# ---------------------------------------------------------------- keys

_MULTILINE = {"timestamps", "notes", "source_search"}

_KEY_ALIASES = {
    "method": "method", "clip_method": "method", "selection": "method",
    "target": "target_duration", "target_duration": "target_duration",
    "clip_length": "target_duration", "length": "target_duration", "duration": "target_duration",
    "min": "min_duration", "min_duration": "min_duration", "minimum": "min_duration",
    "max": "max_duration", "max_duration": "max_duration", "maximum": "max_duration",
    "per_source": "per_source", "clips_per_source": "per_source",
    "max_clips": "max_clips", "max_total": "max_clips",
    "pad": "pad", "pad_before": "pad_before", "pad_after": "pad_after",
    "timestamp": "timestamps", "timestamps": "timestamps", "windows": "timestamps",
    "timestamp_format": "timestamp_format", "time_format": "timestamp_format",
    "timestamp_source": "timestamp_source", "timestamps_from": "timestamp_source",
    "source_search": "source_search", "scrape": "source_search", "search": "source_search",
    "command": "source_search", "scrape_command": "source_search", "source_command": "source_search",
    "source_count": "source_count", "results": "source_count", "source_limit": "source_count",
    "notes": "notes", "note": "notes", "extra": "notes", "instruction": "notes",
}

_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]{1,28})\s*[:=]\s*(.*)$")
_SECONDS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?\s*$", re.I)
_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
_MINUTES_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes)\s*$", re.I)

_SHELL_META = (";", "|", "&", "$", "`", ">", "<", "\n", "\r")
_YOUTUBE_SEARCH_RE = re.compile(r"\bytsearch(\d{1,2})\s*:\s*(.*)$", re.I)


def parse_seconds(value: Any) -> Optional[float]:
    """`30`, `30s`, `1m`, `0:30`, `1:02:00` → seconds. None when unusable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    m = _SECONDS_RE.match(text)
    if m:
        return float(m.group(1))
    m = _MINUTES_RE.match(text)
    if m:
        return float(m.group(1)) * 60.0
    m = _CLOCK_RE.match(text)
    if m:
        first, minutes = int(m.group(1)), int(m.group(2))
        if m.group(3) is None:
            # `M:SS` — the bare form is minutes:seconds (`0:30` = 30s)
            return first * 60.0 + minutes
        return first * 3600.0 + minutes * 60.0 + int(m.group(3))
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _strip_comment(value: str) -> str:
    idx = value.find(" #")
    return (value[:idx] if idx >= 0 else value).strip()


# ---------------------------------------------------------------- parse

def parse_prompt(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Split the block into raw `key -> value` pairs (multi-line values kept)."""
    raw: Dict[str, str] = {}
    warnings: List[str] = []
    last_key: Optional[str] = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            last_key = None
            continue
        if stripped.startswith("#"):
            continue
        asked = (not line[:1].isspace()) or stripped.startswith(("-", "*"))
        if stripped.startswith(("-", "*")):
            stripped = stripped.lstrip("-* ").strip()
            asked = False
        if asked and _KEY_RE.match(line):
            m = _KEY_RE.match(line)
            name = re.sub(r"[\s-]+", "_", m.group(1).strip().lower())
            canonical = _KEY_ALIASES.get(name)
            if canonical is None:
                warnings.append(f"Unknown setting “{m.group(1).strip()}” was ignored.")
                last_key = None
                continue
            value = m.group(2)
            if canonical not in _MULTILINE:
                value = _strip_comment(value)
            if canonical in raw and canonical not in _MULTILINE:
                warnings.append(f"“{canonical}” was set twice — the last value wins.")
            if canonical in _MULTILINE:
                raw[canonical] = (raw.get(canonical, "") + "\n" + value).strip() if canonical in raw else value
            else:
                raw[canonical] = value
            last_key = canonical
            continue
        if last_key in _MULTILINE:
            raw[last_key] = (raw.get(last_key, "") + "\n" + stripped).strip()
            continue
        warnings.append(f"Line “{stripped[:60]}” is not a setting (expected `name = value`) — ignored.")
    return raw, warnings


def resolve(text: str, defaults: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Prompt text → validated parameters. Warnings, never an exception."""
    base = dict(defaults or {})
    raw, warnings = parse_prompt(text)
    params: Dict[str, Any] = {}

    # ---- method
    method_raw = (raw.get("method") or "").strip().lower()
    if method_raw:
        method = _METHOD_ALIASES.get(method_raw.replace("_", "-"))
        if method is None:
            warnings.append(
                f"Clip method “{method_raw}” is not one of {', '.join(METHODS)} — "
                f"using “{base.get('method', METHOD_DEFAULT)}”."
            )
        else:
            params["method"] = method
    elif base.get("method"):
        params["method"] = base["method"]

    # ---- numeric settings
    def number(key: str, low: float, high: float, label: str) -> None:
        value = raw.get(key)
        if value is None or value == "":
            return
        secs = parse_seconds(value)
        if secs is None:
            warnings.append(f"{label} “{value.strip()[:30]}” is not a length in seconds — default kept.")
            return
        if secs < low or secs > high:
            warnings.append(f"{label} {secs:g}s is outside {low:g}-{high:g}s — clamped.")
            secs = _clamp(secs, low, high)
        params[key] = secs

    number("target_duration", 5, 600, "Clip length")
    number("min_duration", 3, 600, "Minimum clip length")
    number("max_duration", 5, 1800, "Maximum clip length")
    number("pad_before", 0, 120, "Lead-in padding")
    number("pad_after", 0, 120, "Tail padding")
    if "pad" in raw and raw["pad"]:
        pad = parse_seconds(raw["pad"])
        if pad is None:
            warnings.append("Padding is not a length in seconds — default kept.")
        else:
            pad = _clamp(pad, 0, 120)
            params["pad_before"] = pad
            params["pad_after"] = pad

    for key, label, high in (("per_source", "Clips per source", 30), ("max_clips", "Max clips", 60),
                             ("source_count", "Scraped sources", 8)):
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            count = int(float(str(value).strip().split()[0]))
        except (TypeError, ValueError):
            warnings.append(f"{label} “{value.strip()[:20]}” is not a number — default kept.")
            continue
        if count < 1 or count > high:
            warnings.append(f"{label} {count} is outside 1-{high} — clamped.")
            count = int(_clamp(count, 1, high))
        params[key] = count

    # ---- timestamp handling
    fmt = (raw.get("timestamp_format") or "").strip().lower()
    if fmt:
        if fmt not in TIMESTAMP_FORMATS:
            warnings.append(f"Timestamp format “{fmt}” is not one of {', '.join(TIMESTAMP_FORMATS)} — auto used.")
        else:
            params["timestamp_format"] = fmt

    src = (raw.get("timestamp_source") or "").strip().lower()
    if src:
        if src not in TIMESTAMP_SOURCES:
            warnings.append(f"Timestamp source “{src}” is not one of {', '.join(TIMESTAMP_SOURCES)} — both used.")
        else:
            params["timestamp_source"] = src

    manual_text = (raw.get("timestamps") or "").strip()
    if manual_text:
        stamps = parse_manual_timestamps(manual_text, params.get("timestamp_format", "auto"))
        if not stamps:
            warnings.append("No usable `HH:MM - HH:MM` window found under timestamps — ignored.")
        else:
            params["manual_timestamps"] = stamps

    # ---- source scrape command (never executed as a shell command)
    command = (raw.get("source_search") or "").strip()
    if command:
        params["source_search"] = command
        params["search"] = search_command(command)

    notes = (raw.get("notes") or "").strip()
    if notes:
        params["notes"] = notes

    return params, warnings


# ---------------------------------------------------------------- timestamps

_SECONDS_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?\s*(?:-|–|—|to|s/d)\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?",
    re.I,
)


def parse_manual_timestamps(text: str, fmt: str = "auto") -> List[Dict[str, Any]]:
    """Manual windows pasted by the user: `12:30 - 13:45  chorus`, one per line."""
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-* ").strip()
        if not line:
            continue
        ranges: List[Tuple[float, float]] = []
        if fmt == "seconds":
            m = _SECONDS_RANGE_RE.search(line)
            if m:
                ranges.append((float(m.group(1)), float(m.group(2))))
        else:
            try:  # reuse the brief's own parser so both read timestamps identically
                from . import campaign as _campaign  # type: ignore
            except ImportError:  # pragma: no cover — Vercel layout
                import campaign as _campaign  # type: ignore
            for hit in _campaign.find_timestamp_ranges(line):
                ranges.append((float(hit["start"]), float(hit["end"])))
        for start, end in ranges:
            if end <= start:
                continue
            label = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", "", line)
            label = re.sub(r"\s*(?:-|–|—|to|s/d|sd)\s*", " ", label)
            label = label.strip(" \t-–—:()[]|,.")
            if len(label) > 60:
                label = label[:60].strip()
            out.append({
                "start": round(start, 2),
                "end": round(end, 2),
                "label": label or "manual window",
                "priority": bool(re.search(r"\bpriority\b|⭐", line, re.I)),
                "origin": "manual",
            })
    # Dedupe identical windows, keep the first label seen.
    seen: List[Dict[str, Any]] = []
    for ts in out:
        if any(abs(ts["start"] - s["start"]) < 0.5 and abs(ts["end"] - s["end"]) < 0.5 for s in seen):
            continue
        seen.append(ts)
    return seen


# ---------------------------------------------------------------- scrape command

def search_command(command: str, fallback: str = "") -> Dict[str, Any]:
    """`ytsearch10:artist live` (or a yt-dlp line, or plain words) → a safe query.

    Returns ``{query, limit, echo, error}``. The command is NEVER handed to a
    shell: shell metacharacters are refused outright and only the search query
    and a result count survive, so a paste from a chat window can never run
    arbitrary code on the host.
    """
    text = (command or "").strip()
    if not text:
        text = (fallback or "").strip()
    echo = text
    if not text:
        return {"query": "", "limit": 5, "echo": "", "error": "No scrape command given."}
    flat = " ".join(text.split())
    for meta in _SHELL_META:
        if meta in flat:
            return {
                "query": "", "limit": 5, "echo": echo,
                "error": "Shell characters are not allowed in the scrape command — put the search words only.",
            }
    limit = 5
    m = _YOUTUBE_SEARCH_RE.search(flat)
    if m:
        limit = int(_clamp(float(m.group(1)), 1, 8))
        query = m.group(2).strip()
        query = re.split(r"[\"']", query)[0]          # a quoted query ends there
        query = re.sub(r"(^|\s)--?[A-Za-z][\w-]*(=\S+)?", " ", query)  # drop flags
        query = query.strip(" :\"'")
    else:
        query = flat
        # `yt-dlp "ytsearch5:..." --flags` and friends: drop the wrapper + flags.
        query = re.sub(r"^(?:yt-dlp|youtube-dl)\s+", "", query, flags=re.I)
        query = re.sub(r"(^|\s)--?[A-Za-z][\w-]*(=\S+)?", " ", query)
        query = query.strip(" \"'")
        query = re.sub(r"^(?:ytsearch\s*:?)\s*", "", query, flags=re.I).strip()
    if not query:
        return {"query": "", "limit": limit, "echo": echo, "error": "No search words in the scrape command."}
    return {"query": query, "limit": limit, "echo": echo, "error": None}


# ---------------------------------------------------------------- template

def render_template(params: Optional[Dict[str, Any]] = None,
                    notes: str = "") -> str:
    """The editable block, pre-filled with the CURRENT settings."""
    p = dict(params or {})
    method = p.get("method") or METHOD_DEFAULT
    target = p.get("target_duration")
    min_dur = p.get("min_duration")
    max_dur = p.get("max_duration")
    per_source = p.get("per_source")
    max_clips = p.get("max_clips")
    pad_before = p.get("pad_before") or 0
    pad_after = p.get("pad_after") or 0
    fmt = p.get("timestamp_format") or "auto"
    src = p.get("timestamp_source") or "both"
    search = p.get("source_search") or ""
    count = p.get("source_count") or 5
    manual = p.get("manual_timestamps") or []

    def row(key: str, value: str, comment: str = "") -> str:
        # 26-char value column so the comments line up — this block is meant to be
        # read and edited by a non-technical clipper, not parsed by eye.
        left = f"{key} = {value}"
        return f"{left.ljust(26)}# {comment}" if comment else left.rstrip()

    lines = [
        "# HeatCut clip plan — edit the values, the planner uses them literally.",
        row("method", method,
            "requirement (brief timestamps) | campaign (brief list only) | heatmap (retention, opt-in) | even (spread)"),
        row("target", f"{target:g}s" if target else "", "clip length in seconds"),
        row("min", f"{min_dur:g}s" if min_dur else "", "the campaign minimum wins when it is higher"),
        row("max", f"{max_dur:g}s" if max_dur else "", "blank = the campaign's own rule"),
        row("per_source", str(per_source) if per_source else "", "windows taken from each source"),
        row("max_clips", str(max_clips) if max_clips else "", "hard cap for this campaign"),
        row("pad_before", f"{pad_before:g}s", "extra seconds before each window"),
        row("pad_after", f"{pad_after:g}s", "extra seconds after each window"),
        row("timestamp_format", fmt, "auto | mm:ss | hh:mm:ss | seconds"),
        row("timestamp_source", src, "requirement | manual | both"),
    ]
    lines.append(row("timestamps", "", "your own windows, one per line (blank = none)"))
    for ts in manual[:20]:
        label = f"  {ts.get('label')}" if ts.get("label") else ""
        lines.append(f"  {_mmss(ts['start'])} - {_mmss(ts['end'])}{label}")
    if not manual:
        lines.append("  # 12:30 - 13:45  chorus")
    lines.append(row("source_search", search,
                     "scrape command used when the brief has no video (ytsearchN: words)"))
    lines.append(row("source_count", str(count), "how many scraped sources to keep (1-8)"))
    first_note = (notes or p.get("notes") or "").splitlines()
    lines.append(row("notes", (first_note[0] if first_note else ""), "free direction for the copy pass"))
    for extra in first_note[1:]:
        lines.append(f"  {extra}")
    return "\n".join(lines)


def _mmss(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def describe(params: Dict[str, Any], language: str = "en") -> List[str]:
    """Plain-language summary of the resolved plan (what the UI shows back)."""
    p = params or {}
    method = p.get("method") or METHOD_DEFAULT
    labels_en = {
        "requirement": "Timestamps from the brief's requirement text",
        "campaign": "Only the campaign's own timestamp list",
        "heatmap": "Retention heatmap (opt-in)",
        "even": "Evenly spread windows (no analysis)",
    }
    labels_id = {
        "requirement": "Timestamp dari teks requirement brief",
        "campaign": "Cuma daftar timestamp dari campaign",
        "heatmap": "Retention heatmap (opsional)",
        "even": "Sebar rata (tanpa analisa)",
    }
    labels = labels_id if language == "id" else labels_en
    out = [labels.get(method, method)]
    if p.get("target_duration"):
        out.append(f"clip {p['target_duration']:g}s")
    if p.get("per_source"):
        out.append(f"{int(p['per_source'])}/source")
    if p.get("pad_before") or p.get("pad_after"):
        out.append(f"pad -{p.get('pad_before') or 0:g}s/+{p.get('pad_after') or 0:g}s")
    if p.get("manual_timestamps"):
        out.append(f"{len(p['manual_timestamps'])} manual window(s)")
    if p.get("source_search"):
        out.append("scrape source armed")
    return out
