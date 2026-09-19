"""Spade campaign -> raw-material planner.

Two jobs, both deliberately inference-free so the heavy lifting (yt-dlp
metadata, retention-peak mining, LLM copy) can stay in ``backend.main`` and be
injected:

1. ``parse_campaign`` pulls a campaign from Spade's PUBLIC API
   (``api.spadeclipping.com/public/campaigns/<id>`` — no auth, verified) and
   turns the free-text ``details`` HTML into a structured spec: source links,
   the timestamps each source carries, and the campaign's requirements
   (platforms, minimum clip length, positive-narrative rules, ...).
2. ``build_plan`` turns that spec into the CUT LIST — the raw material the user
   processes in their editor. Campaign-provided timestamps are sliced straight
   into clip windows (zero network, zero tokens); sources without timestamps
   fall back to retention-peak mining.

Kept stdlib-only on purpose: importable and unit-testable without yt-dlp,
FastAPI or an API key.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Optional, Tuple

SPADE_API = "https://api.spadeclipping.com/public/campaigns/"
DEFAULT_HEADERS = {
    "accept": "application/json",
    "origin": "https://app.spadeclipping.com",
    "referer": "https://app.spadeclipping.com/",
    "user-agent": "Mozilla/5.0 (compatible; HeatCut/1.0)",
}

ALLOWED_PLATFORMS = {
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "reels": "Instagram Reels",
    "youtube": "YouTube Shorts",
    "shorts": "YouTube Shorts",
    "twitter": "X",
    "x": "X",
    "snapchat": "Snapchat",
    "facebook": "Facebook",
    "twitch": "Twitch",
}


class CampaignError(Exception):
    """Raised for anything the caller should surface as a 4xx/5xx message."""


# ---------------------------------------------------------------- ids / fetch

def extract_campaign_id(value: str) -> Optional[str]:
    """Accepts a full Spade URL or a bare numeric campaign id."""
    if not value:
        return None
    raw = value.strip()
    if raw.isdigit() and len(raw) >= 6:
        return raw
    m = re.search(r"/(?:campaigns?)/([A-Za-z0-9_-]{6,})", raw)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z0-9_-]{10,})", raw)
    return m.group(1) if m else None


def fetch_campaign(campaign_id: str, timeout: int = 25) -> dict:
    import requests  # local import: keeps this module importable in odd envs

    url = f"{SPADE_API}{campaign_id}"
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — network layer, message matters
        raise CampaignError(f"Could not reach Spade ({exc})") from exc
    if resp.status_code == 404:
        raise CampaignError("Campaign not found — check the link is public and still live.")
    if resp.status_code >= 400:
        raise CampaignError(f"Spade returned HTTP {resp.status_code} for campaign {campaign_id}.")
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise CampaignError(f"Spade returned a non-JSON body ({exc})") from exc
    if not isinstance(data, dict) or not data.get("campaignId"):
        raise CampaignError("Unexpected campaign payload from Spade.")
    return data


# ---------------------------------------------------------------- html -> tree

class _Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str, attrs: Optional[dict] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List["_Node"] = []
        self.text: List[str] = []


_VOID = {"br", "img", "hr", "meta", "link", "input", "source"}


class _TreeBuilder(HTMLParser):
    """Minimal DOM: enough structure to keep nested ``<ul>`` timestamps with
    the ``<li>`` (and link) they belong to."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.stack: List[_Node] = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].text.append(data)


def html_to_tree(raw: str) -> _Node:
    parser = _TreeBuilder()
    parser.feed(raw or "")
    parser.close()
    return parser.root


def node_text(node: _Node, stop_tags: Iterable[str] = ("li", "ul", "ol")) -> str:
    """Text directly owned by ``node`` (nested list subtrees excluded)."""
    stop = set(stop_tags)
    parts = list(node.text)
    for child in node.children:
        if child.tag in stop:
            continue
        parts.append(node_text(child, stop_tags=()))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def node_links(node: _Node, _stop: Iterable[str] = ("li", "ul", "ol")) -> List[Tuple[str, str]]:
    """(href, anchor text) pairs owned by ``node`` (same subtree rule)."""
    stop = set(_stop)
    out: List[Tuple[str, str]] = []
    for child in node.children:
        if child.tag in stop:
            continue
        if child.tag == "a":
            out.append((child.attrs.get("href", ""), node_text(child, stop_tags=())))
        out.extend(node_links(child, _stop=()))
    return out


def html_to_plain(raw: str) -> str:
    tree = html_to_tree(raw)
    lines: List[str] = []

    def walk(node: _Node, depth: int = 0):
        if node.tag in ("h1", "h2", "h3", "h4"):
            t = node_text(node)
            if t:
                lines.append(f"\n## {t}")
        elif node.tag in ("li",):
            t = node_text(node)
            if t:
                lines.append(f"- {t}")
        elif node.tag in ("p", "div", "br", "strong", "em", "span"):
            t = node_text(node, stop_tags=())
            if t:
                lines.append(t)
        for child in node.children:
            walk(child, depth + 1)

    walk(tree)
    seen: List[str] = []
    for line in lines:
        if seen and seen[-1] == line:
            continue
        seen.append(line)
    return html.unescape("\n".join(seen)).strip()


# ---------------------------------------------------------------- timestamps

_TS = r"(\d{1,2}:\d{2}(?::\d{2})?)"
_TS_RANGE_RE = re.compile(_TS + r"\s*(?:-|–|—|to|s/d|sd)\s*" + _TS, re.IGNORECASE)


def parse_ts(value: str) -> Optional[float]:
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def find_timestamp_ranges(text: str) -> List[dict]:
    """All ``MM:SS - MM:SS`` ranges in a line, with a label when one precedes."""
    out: List[dict] = []
    for m in _TS_RANGE_RE.finditer(text):
        start, end = parse_ts(m.group(1)), parse_ts(m.group(2))
        if start is None or end is None or end <= start:
            continue
        before = text[: m.start()].strip(" \t-–—:()[]|")
        label = re.sub(r"\b(?:timestamp|timestamps|time)\b\s*:?\s*$", "", before, flags=re.I).strip()
        after = text[m.end():].strip(" \t-–—:()[]|")
        priority = bool(re.search(r"\bpriority\b", (label + " " + after), re.I))
        out.append({
            "start": start,
            "end": end,
            "label": label or f"{_fmt(start)} - {_fmt(end)}",
            "priority": priority,
            "note": after[:80],
        })
    return out


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- requirements

_PLATFORM_HINTS = ("tiktok", "instagram", "reels", "youtube", "shorts", "twitter", "snapchat", "facebook", "twitch")
_RULE_HINTS = (
    "must", "minimum", "min ", "no ", "do not", "don't", "keep", "only", "required",
    "ban", "avoid", "fail", "not allowed", "positive", "captions", "watermark", "repost",
)


def extract_min_duration(text: str) -> Optional[float]:
    patterns = [
        r"minimum\s+(?:clip\s+)?(?:length|duration)?\s*:?\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds\b)",
        r"at\s+least\s+(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds\b)",
        r"min(?:imum)?\s*(?:clip\s*)?(?:length|duration)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds\b)",
        r"(\d+)\s*(?:s\b|sec\b|secs\b|seconds\b)\s*(?:minimum|min)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def extract_max_duration(text: str) -> Optional[float]:
    for pat in (
        r"max(?:imum)?\s*(?:clip\s*)?(?:length|duration)\s*:?\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds\b|min\b|minutes\b)",
        r"no\s+(?:longer|more)\s+than\s+(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds\b|min\b|minutes\b)",
    ):
        m = re.search(pat, text, re.I)
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            if re.search(r"min", m.group(0), re.I) and "minute" in m.group(0).lower():
                val *= 60
            return val
    return None


def extract_requirements(details_html: str, plain: str, data: dict,
                         source_labels: Optional[Iterable[str]] = None) -> dict:
    platforms: List[str] = []
    for p in (data.get("allowedPlatforms") or []):
        label = ALLOWED_PLATFORMS.get(str(p).lower())
        if label and label not in platforms:
            platforms.append(label)
    for m in re.finditer("|".join(_PLATFORM_HINTS), plain, re.I):
        label = ALLOWED_PLATFORMS.get(m.group(0).lower())
        if label and label not in platforms:
            platforms.append(label)

    skip = {s.strip().lower() for s in (source_labels or []) if s and len(s.strip()) > 6}
    bullets = [b.lstrip("- ").strip() for b in re.split(r"\n", plain) if b.strip().startswith("-")]

    def is_rule(text: str) -> bool:
        low = text.lower()
        if "http" in low:
            return False
        if any(lbl in low for lbl in skip):
            return False
        return any(h in low for h in _RULE_HINTS)

    rules: List[str] = []
    for b in bullets:
        if not is_rule(b):
            continue
        key = re.sub(r"\W+", "", b.lower())
        if any(re.sub(r"\W+", "", r.lower()) == key for r in rules):
            continue
        rules.append(b)
    negatives = [r for r in rules if re.search(r"\bno\b|\bnot\b|ban|avoid|fail|never", r, re.I)]

    hashtags = sorted({h.lower() for h in re.findall(r"#[A-Za-z0-9_]{2,}", plain)})
    mentions = sorted({h for h in re.findall(r"@[A-Za-z0-9_.]{2,}", plain)})

    return {
        "platforms": platforms,
        "min_duration_sec": extract_min_duration(plain),
        "max_duration_sec": extract_max_duration(plain),
        "aspect_ratio": "9:16" if any(p in ("TikTok", "Instagram Reels", "YouTube Shorts") for p in platforms) else None,
        "rules": rules[:25],
        "negative_rules": negatives[:15],
        "hashtags": hashtags[:20],
        "mentions": mentions[:20],
        "raw_text": plain,
    }


# ---------------------------------------------------------------- sources

def _is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or "", re.I))


def extract_video_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"(?:v=|/shorts/|/embed/|youtu\.be/|/live/|/v/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else None


def normalize_youtube_url(url: str) -> str:
    """Strip playlist/radio/si noise so yt-dlp never tries to walk a playlist.

    Campaign briefs happily link `watch?v=ID&list=RDID&start_radio=1`; handing
    that to yt-dlp turns a 4s metadata fetch into a multi-minute radio
    extraction. Canonical single-video form instead.
    """
    vid = extract_video_id(url or "")
    if not vid:
        return url
    return f"https://www.youtube.com/watch?v={vid}"


def _walk_kind(node: _Node):
    """Yield ('li', node) and ('h', node) in document order."""
    for child in node.children:
        if child.tag in ("li", "h1", "h2", "h3", "h4"):
            yield (child.tag, child)
        yield from _walk_kind(child)


def parse_details(details_html: str) -> dict:
    """Sources (with their timestamps) + requirement text from ``details``."""
    tree = html_to_tree(details_html)
    plain = html_to_plain(details_html)

    sources: List[dict] = []
    current_source: Optional[dict] = None
    section = ""
    other_bullets: List[str] = []

    for tag, node in _walk_kind(tree):
        if tag.startswith("h"):
            section = node_text(node)
            current_source = None
            continue

        text = node_text(node)
        links = [(href, label) for href, label in node_links(node) if _is_youtube(href)]
        ranges = find_timestamp_ranges(text)

        if links:
            for href, label in links:
                vid = extract_video_id(href)
                if not vid:
                    continue
                src = {
                    "url": normalize_youtube_url(href),
                    "original_url": href,
                    "video_id": vid,
                    "label": (label or text or vid).strip()[:160],
                    "section": section,
                    "priority": bool(re.search(r"\bpriority\b", text, re.I)),
                    "timestamps": [],
                }
                sources.append(src)
                current_source = src
            if ranges and current_source is not None:
                current_source["timestamps"].extend(ranges)
            continue

        if ranges:
            target = current_source
            if target is None:
                # A timestamped block with no preceding link: keep it as a
                # section-level instruction (still useful for manual sources).
                for r in ranges:
                    other_bullets.append(f"{section or 'Timestamped section'}: {r['label']} ({_fmt(r['start'])} - {_fmt(r['end'])})")
                continue
            target["timestamps"].extend(ranges)
            if re.search(r"\bpriority\b", text, re.I):
                target["priority"] = True
                for r in target["timestamps"][-len(ranges):]:
                    r["priority"] = True
            continue

        if text and section and section.lower().startswith(("video requirement", "important", "platform requirement")):
            other_bullets.append(text)

    # Dedupe sources that repeat the same video id (a campaign often links one
    # video twice), merging their timestamps instead of listing it twice.
    merged: List[dict] = []
    for src in sources:
        hit = next((s for s in merged if s["video_id"] == src["video_id"]), None)
        if hit is None:
            merged.append(src)
        else:
            hit["priority"] = hit["priority"] or src["priority"]
            for ts in src["timestamps"]:
                if not any(abs(ts["start"] - e["start"]) < 0.5 for e in hit["timestamps"]):
                    hit["timestamps"].append(ts)

    for i, src in enumerate(merged):
        src["index"] = i
        src["timestamps"].sort(key=lambda t: t["start"])

    return {"sources": merged, "plain_text": plain, "loose_bullets": other_bullets}


# ---------------------------------------------------------------- public spec

def build_spec(data: dict, details_html: str) -> dict:
    parsed = parse_details(details_html)
    req = extract_requirements(
        details_html, parsed["plain_text"], data,
        source_labels=[s["label"] for s in parsed["sources"]],
    )
    loose: List[str] = []
    rule_keys = {re.sub(r"\W+", "", r.lower()) for r in req.get("rules", [])}
    for b in parsed["loose_bullets"]:
        if b in loose:
            continue
        # A brief note that is already listed under Requirements adds noise.
        if re.sub(r"\W+", "", b.lower()) in rule_keys:
            continue
        loose.append(b)
    rate = data.get("rate")
    rate_per_1k = round(float(rate) * 1000, 4) if rate else None
    return {
        "campaign_id": str(data.get("campaignId")),
        "name": data.get("name"),
        "public_name": data.get("publicName") or data.get("name"),
        "description": data.get("description"),
        "status": data.get("status"),
        "is_active": bool(data.get("isActive")),
        "image_url": data.get("imageUrl"),
        "niches": data.get("niches") or [],
        "rate_per_1k": rate_per_1k,
        "rate_per_100k": round(float(rate) * 100000, 2) if rate else None,
        "max_payout": data.get("maxPayout"),
        "budget": data.get("clientBudget"),
        "minimum_views": data.get("minimumViews"),
        "minimum_clip_views": data.get("minimumClipViews"),
        "max_posts_per_user": data.get("maxPostsPerUser"),
        "campaign_type": data.get("campaignType"),
        "announcements": data.get("announcementCount"),
        "end_date": data.get("endDate"),
        "requires_manual_approval": data.get("requiresManualApproval"),
        "sources": parsed["sources"],
        "requirements": req,
        "loose_bullets": loose,
        "details_html": details_html,
        "plain_details": parsed["plain_text"],
    }


def parse_campaign(url_or_id: str) -> dict:
    cid = extract_campaign_id(url_or_id)
    if not cid:
        raise CampaignError("Could not read a campaign id from that link.")
    data = fetch_campaign(cid)
    spec = build_spec(data, data.get("details") or "")
    if not spec["sources"]:
        spec["warnings"] = [
            "No YouTube source links were found in the campaign brief — "
            "add source videos manually or check the campaign page.",
        ]
    else:
        spec["warnings"] = []
    return spec


# ---------------------------------------------------------------- planner

def _slice_section(start: float, end: float, target: float, min_dur: float) -> List[Tuple[float, float]]:
    span = end - start
    if span <= 0:
        return []
    if span < min_dur:
        # Campaign sections are hard bounds; extend the tail to reach the
        # minimum length instead of dropping a priority moment.
        return [(start, start + min_dur)]
    n = max(1, int(round(span / max(target, 1.0))))
    n = max(n, 1)
    if target > 0 and span / n < min_dur:
        n = max(1, int(span // min_dur))
    step = span / n
    out: List[Tuple[float, float]] = []
    for i in range(n):
        s = start + i * step
        e = start + (i + 1) * step
        if e - s < min_dur and out:
            out[-1] = (out[-1][0], e)  # fold a short tail into the previous slice
        else:
            out.append((s, e))
    return out


def _even_windows(duration: float, target: float, count: int, skip_intro: float = 15.0) -> List[Tuple[float, float]]:
    if duration <= target or count <= 0:
        return [(0.0, min(duration, target or duration))]
    usable = max(duration - skip_intro, target)
    n = max(1, min(count, int(usable // target)))
    step = usable / n
    return [(skip_intro + i * step, skip_intro + i * step + target) for i in range(n)]


def build_plan(
    spec: dict,
    source_urls: List[str],
    target_duration: float = 30.0,
    per_source: int = 6,
    max_total: int = 15,
    metadata_fn: Optional[Callable[[str], dict]] = None,
    peaks_fn: Optional[Callable[[list, float, float, int], List[dict]]] = None,
    allow_network: bool = True,
) -> dict:
    """Turn a parsed campaign spec into the raw-material cut list."""
    req = spec.get("requirements") or {}
    min_dur = float(req.get("min_duration_sec") or 15.0)
    target = max(float(target_duration or 30.0), min_dur)
    max_dur = req.get("max_duration_sec")

    by_id = {s["video_id"]: s for s in spec.get("sources", [])}
    by_url: Dict[str, dict] = {}
    for s in spec.get("sources", []):
        for key in (s.get("url"), s.get("original_url")):
            if key:
                by_url[key] = s

    items: List[dict] = []
    warnings: List[str] = []
    source_meta: List[dict] = []

    for raw_url in source_urls or []:
        src = by_url.get(raw_url) or by_id.get(extract_video_id(raw_url) or "")
        if src is None:
            vid = extract_video_id(raw_url)
            if not vid:
                warnings.append(f"Skipped an unrecognized source URL: {raw_url}")
                continue
            src = {"url": normalize_youtube_url(raw_url), "video_id": vid, "label": vid,
                   "section": "", "priority": False, "timestamps": []}

        entry = {
            "video_id": src["video_id"], "url": src["url"], "label": src["label"],
            "priority": bool(src.get("priority")), "mode": None, "duration": None,
            "title": None, "clip_count": 0, "note": None, "heatmap_points": 0,
        }

        # --- 1) Campaign-defined timestamps: deterministic, no network, no AI
        if src.get("timestamps"):
            entry["mode"] = "campaign-timestamps"
            picked = 0
            for ts in sorted(src["timestamps"], key=lambda t: (not t.get("priority"), t["start"])):
                if picked >= per_source:
                    break
                for s, e in _slice_section(ts["start"], ts["end"], target, min_dur):
                    if picked >= per_source:
                        break
                    if max_dur and (e - s) > max_dur:
                        e = s + max_dur
                    items.append({
                        "video_id": src["video_id"],
                        "source_url": src["url"],
                        "source_label": src["label"],
                        "section_label": ts["label"],
                        "priority": bool(ts.get("priority")) or entry["priority"],
                        "start": round(s, 2),
                        "end": round(e, 2),
                        "duration": round(e - s, 2),
                        "evidence": "campaign-timestamp",
                        "heat": None,
                        "score": 1.0 if ts.get("priority") else 0.8,
                        "section_start": ts["start"],
                        "section_end": ts["end"],
                    })
                    picked += 1
            entry["clip_count"] = picked
            if picked == 0:
                entry["note"] = "Campaign timestamps were shorter than the minimum clip length."
            source_meta.append(entry)
            continue

        # --- 2) No timestamps: try retention peaks, else even spread
        meta = None
        if allow_network and metadata_fn is not None:
            try:
                meta = metadata_fn(src["url"])
            except Exception as exc:  # noqa: BLE001 — best effort by design
                entry["note"] = f"Metadata fetch failed: {exc}"
        duration = float((meta or {}).get("duration") or 0.0)
        heatmap = (meta or {}).get("heatmap") or []
        entry["duration"] = duration or None
        entry["title"] = (meta or {}).get("title")
        entry["heatmap_points"] = len(heatmap)

        windows: List[dict] = []
        if heatmap and peaks_fn is not None and duration > 0:
            try:
                windows = peaks_fn(heatmap, duration, target, per_source)
            except Exception as exc:  # noqa: BLE001
                entry["note"] = f"Peak mining failed: {exc}"
        if windows:
            entry["mode"] = "retention-peaks"
            for w in windows[:per_source]:
                s, e = float(w["start"]), float(w["end"])
                items.append({
                    "video_id": src["video_id"],
                    "source_url": src["url"],
                    "source_label": src["label"],
                    "section_label": f"retention peak @ {_fmt(w.get('hook', s))}",
                    "priority": entry["priority"],
                    "start": round(s, 2),
                    "end": round(e, 2),
                    "duration": round(e - s, 2),
                    "evidence": "retention-peak",
                    "heat": round(float(w.get("heat") or 0.0), 3),
                    "score": round(float(w.get("score") or 0.0), 3),
                })
        elif duration > 0:
            entry["mode"] = "even-spread"
            entry["note"] = entry["note"] or "No retention telemetry — windows spread evenly across the source."
            for s, e in _even_windows(duration, target, per_source):
                items.append({
                    "video_id": src["video_id"],
                    "source_url": src["url"],
                    "source_label": src["label"],
                    "section_label": "even spread",
                    "priority": entry["priority"],
                    "start": round(s, 2),
                    "end": round(e, 2),
                    "duration": round(e - s, 2),
                    "evidence": "no-telemetry",
                    "heat": None,
                    "score": 0.4,
                })
        else:
            entry["mode"] = "unavailable"
            entry["note"] = entry["note"] or "Could not read this source (blocked or unavailable)."
            warnings.append(f"“{src['label']}” could not be analyzed: {entry['note']}")
        entry["clip_count"] = sum(1 for it in items if it["video_id"] == src["video_id"])
        source_meta.append(entry)

    # Order: priority first, then campaign timestamps in source order, then
    # scored peaks. Keeps the "top priority for this campaign" note actionable.
    def sort_key(item: dict):
        return (
            0 if item["priority"] else 1,
            0 if item["evidence"] == "campaign-timestamp" else 1,
            -(item.get("score") or 0.0),
            item["start"],
        )

    items.sort(key=sort_key)
    if max_total and len(items) > max_total:
        dropped = len(items) - max_total
        items = items[:max_total]
        warnings.append(f"{dropped} extra window(s) dropped — campaign allows {max_total} posts per creator.")

    for i, it in enumerate(items):
        it["index"] = i
        it["id"] = f"{it['video_id']}:{it['start']:.1f}-{it['end']:.1f}"
        it["timestamp"] = f"{_fmt(it['start'])} - {_fmt(it['end'])}"
        it["title"] = _default_title(it)
        it["caption"] = _default_caption(it, spec)
        it["hashtags"] = " ".join(_default_hashtags(it, spec))
        it["reason"] = _reason(it)

    # Number repeated slices of the same section ("Negative — part 2 of 4") so
    # the default titles stay distinguishable without an LLM pass.
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for it in items:
        groups.setdefault((it["video_id"], it.get("section_label") or ""), []).append(it)
    for (_, _label), group in groups.items():
        if len(group) < 2:
            continue
        for n, it in enumerate(group, start=1):
            base = re.sub(r"\s*\([^)]*\)\s*$", "", it["title"]).strip()
            it["title"] = f"{base} — part {n}/{len(group)}"[:90]

    return {
        "items": items,
        "warnings": warnings,
        "sources": source_meta,
        "target_duration": target,
        "min_duration": min_dur,
        "max_total": max_total,
    }


def _default_title(item: dict) -> str:
    base = (item.get("section_label") or "").strip()
    label = item.get("source_label") or "Clip"
    # A section label that is nothing but a time range (or an auto tag) carries
    # no topic — fall back to the source's own name, which at least reads.
    if not base or re.fullmatch(r"[\d:\s\-–—]+", base) or base.lower() in ("even spread", "campaign timestamp"):
        base = label
    if item["evidence"] == "retention-peak":
        return f"{base} — hot moment"[:90]
    return f"{base} ({item['timestamp']})"[:90]


def _default_caption(item: dict, spec: dict) -> str:
    rules = (spec.get("requirements") or {})
    tags = _default_hashtags(item, spec)
    name = spec.get("public_name") or spec.get("name") or ""
    bits = [f"{item.get('section_label') or item.get('source_label')}"]
    if spec.get("campaign_id"):
        bits.append(f"@spadeclipping campaign #{spec['campaign_id']}")
    if rules.get("mentions"):
        bits.append(" ".join(rules["mentions"][:2]))
    text = " • ".join(b for b in bits if b)
    if name:
        text = f"{text} — {name}"
    return (text + " " + " ".join(tags)).strip()[:600]


def _default_hashtags(item: dict, spec: dict) -> List[str]:
    req = spec.get("requirements") or {}
    tags = list(req.get("hashtags") or [])
    for niche in (spec.get("niches") or [])[:3]:
        tags.append("#" + re.sub(r"[^a-z0-9]", "", niche.lower()))
    for extra in ("#clipping", "#reels", "#fyp"):
        tags.append(extra)
    out: List[str] = []
    for t in tags:
        t = t.lower()
        if t and t not in out:
            out.append(t)
    return out[:8]


def _reason(item: dict) -> str:
    if item["evidence"] == "campaign-timestamp":
        label = item.get("section_label") or "timestamped section"
        flag = " ⭐ priority section" if item.get("priority") else ""
        return f"Campaign-defined section “{label}” ({_fmt(item.get('section_start', item['start']))} - {_fmt(item.get('section_end', item['end']))}){flag}"
    if item["evidence"] == "retention-peak":
        return f"Retention peak inside {item.get('source_label')} (heat {item.get('heat')})"
    return f"No telemetry on {item.get('source_label')} — evenly spaced raw window"


# ---------------------------------------------------------------- brief

def build_brief_md(spec: dict, plan: dict, copy_note: str = "") -> str:
    req = spec.get("requirements") or {}
    lines: List[str] = []
    lines.append(f"# {spec.get('public_name') or spec.get('name')}")
    lines.append("")
    lines.append(f"- Campaign: `{spec.get('campaign_id')}` · status **{spec.get('status')}**")
    if spec.get("rate_per_100k"):
        lines.append(f"- Rate: **${spec['rate_per_100k']}/100K views**"
                     + (f" (budget ${spec.get('budget')})" if spec.get("budget") else ""))
    if req.get("platforms"):
        lines.append(f"- Platforms: {', '.join(req['platforms'])}")
    if spec.get("minimum_views"):
        lines.append(f"- Min views for payout: {spec['minimum_views']:,}")
    if spec.get("max_posts_per_user"):
        lines.append(f"- Max posts per creator: {spec['max_posts_per_user']}")
    if req.get("min_duration_sec"):
        lines.append(f"- Minimum clip length: {req['min_duration_sec']:.0f}s")
    if req.get("aspect_ratio"):
        lines.append(f"- Aspect: {req['aspect_ratio']} (vertical)")
    lines.append("")
    lines.append("## Requirements")
    for r in req.get("rules", []):
        lines.append(f"- {r}")
    if req.get("negative_rules"):
        lines.append("")
        lines.append("### Do NOT")
        for r in req["negative_rules"]:
            lines.append(f"- {r}")
    if spec.get("loose_bullets"):
        lines.append("")
        lines.append("### Notes from the brief")
        for r in spec["loose_bullets"][:12]:
            lines.append(f"- {r}")
    lines.append("")
    lines.append(f"## Raw cut list ({len(plan.get('items', []))} clips, target {plan.get('target_duration'):.0f}s each)")
    lines.append("")
    lines.append("Every window below is exported RAW (original quality, ±2s headroom) — trim frame-accurate in your editor.")
    lines.append("")
    current = None
    for item in plan.get("items", []):
        if item["video_id"] != current:
            current = item["video_id"]
            lines.append(f"### {item.get('source_label')}")
            lines.append(f"Source: {item.get('source_url')}")
            lines.append("")
            lines.append("| # | Window | Len | Section | Evidence | Title |")
            lines.append("|---|--------|-----|---------|----------|-------|")
        lines.append(
            f"| {item['index'] + 1} | {item['timestamp']} | {item['duration']:.0f}s | "
            f"{item.get('section_label') or ''} | {item['evidence']} | {item.get('title') or ''} |"
        )
    lines.append("")
    if plan.get("warnings"):
        lines.append("## Warnings")
        for w in plan["warnings"]:
            lines.append(f"- {w}")
        lines.append("")
    lines.append("## Copy per clip")
    for item in plan.get("items", []):
        lines.append("")
        lines.append(f"**{item['index'] + 1}. {item['timestamp']} — {item.get('title')}**")
        lines.append(f"- Source: {item.get('source_label')} ({item.get('source_url')})")
        lines.append(f"- Why: {item.get('reason')}")
        if item.get("caption"):
            lines.append(f"- Caption: {item['caption']}")
        if item.get("hashtags"):
            lines.append(f"- Hashtags: {item['hashtags']}")
    if copy_note:
        lines.append("")
        lines.append(f"> {copy_note}")
    lines.append("")
    lines.append("_Generated by HeatCut Campaign Prep — verify every claim against the campaign page before posting._")
    return "\n".join(lines)
