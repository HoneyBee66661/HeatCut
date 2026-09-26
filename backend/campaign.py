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
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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


def fmt_time(sec: float) -> str:
    """Public `M:SS` / `H:MM:SS` formatter (prompt rendering + briefs)."""
    return _fmt(sec)


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


def youtube_deep_link(video_id: str, start: float) -> str:
    """Labeled jump link to the exact second of the source video.

    Manual-fallback path: when YouTube refuses to hand over the media (403 /
    PO-token / bot check) the automatic export dies, so the deliverable still
    has to carry something the editor can act on. A `&t=<sec>s` link does:
    open it, land on the window, cut it by hand.
    """
    if not video_id:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}&t={int(max(0.0, start or 0.0))}s"


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


def normalize_method(value: str) -> str:
    """Any accepted spelling of a clip method → its canonical name."""
    key = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "requirement": "requirement", "requirements": "requirement", "req": "requirement",
        "brief": "requirement", "brief-text": "requirement", "auto": "requirement", "text": "requirement",
        "campaign": "campaign", "campaign-timestamps": "campaign", "timestamps": "campaign",
        "heatmap": "heatmap", "retention": "heatmap", "peaks": "heatmap", "telemetry": "heatmap",
        "even": "even", "even-spread": "even", "spread": "even", "none": "even",
    }
    return aliases.get(key, "requirement")


def _known_ranges(spec: dict) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for src in spec.get("sources") or []:
        for ts in src.get("timestamps") or []:
            try:
                out.append((float(ts["start"]), float(ts["end"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _link_ids(text: str) -> List[str]:
    ids: List[str] = []
    for raw in re.findall(r"https?://\S+", text or ""):
        vid = extract_video_id(raw.strip(").,;"))
        if vid:
            ids.append(vid)
    return ids


# "Cut 1:02:00 - 1:02:40 for the priority moment" has the useful words AFTER the
# range; "Intro hook 5:00 - 5:30" has them before. Read both, drop the filler.
_LABEL_FILLER_RE = re.compile(
    r"^(?:cut|clip|clips|part|the part|post|use|take|make|best clip|best part|"
    r"timestamp|timestamps|time|section|sections|from|for|in|on|of|at|watch|here)\b[\s:,\-–—]*",
    re.I,
)


def _clean_fragment(text: Any) -> str:
    out = re.sub(r"[()\[\]]", " ", str(text or ""))
    out = re.sub(r"\b(?:please|thanks|ya|plis)\b", " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip(" .,-–—:;|")
    return out.strip()


def _readable_label(before: Any, after: Any) -> str:
    """Prefer whichever side of the range actually names the moment.

    "Cut 1:02:00 - 1:02:40 for the priority moment" carries the useful words
    AFTER the range; "Post the best part, the chorus at 12:30 - 13:45 works
    great" carries them BEFORE it, behind a filler verb — so a filler head falls
    back to its last clause ("the chorus") before giving up on the head.
    """
    head = _clean_fragment(before)
    if head and _LABEL_FILLER_RE.match(head):
        parts = [p.strip() for p in re.split(r"[,;:]", head) if p.strip()]
        if len(parts) > 1:
            head = re.sub(r"\b(?:at|on|in|from|for)\s*$", "", parts[-1]).strip(" ,.-–—:")
    tail = _clean_fragment(after)
    tail = _LABEL_FILLER_RE.sub("", tail).strip()
    if head and not _LABEL_FILLER_RE.match(head):
        if len(head.split()) > 1 or len(tail.split()) < 2:
            return head[:60]
    return (tail or head)[:60]


def scrape_requirement_timestamps(spec: dict) -> List[dict]:
    """Timestamps the brief ASKS FOR in words, not in its source list.

    Campaigns often write the window into the requirement itself — "post the
    part at 12:30 - 13:45", a rule line, a section header, the description — and
    never attach it to a video link. Those windows used to be lost: prep only
    looked at the structured per-source list, then fell back to the retention
    heatmap. This walks the whole brief (description, rules, notes and the raw
    details text) and returns every window the brief names, deduped against the
    per-source list and each other.

    Each hit carries `origin` (description | rule | note | brief), a
    `video_id_hint` when a source link shares its line, and a `source_hint` when
    the line names a source by label.
    """
    req = spec.get("requirements") or {}
    known = _known_ranges(spec)
    labels = [(s.get("label") or "").strip() for s in spec.get("sources") or []]
    found: List[dict] = []

    def hint_label(text: str) -> str:
        low = (text or "").lower()
        for lbl in labels:
            key = lbl.lower()
            if len(key) > 6 and key[:32] in low:
                return lbl
        return ""

    def add(text: str, origin: str, fallback_id: Optional[str] = None) -> None:
        if not text:
            return
        ids = _link_ids(text)
        hint_id = ids[-1] if ids else fallback_id
        hint = hint_label(text)
        for rng in find_timestamp_ranges(text):
            start, end = float(rng["start"]), float(rng["end"])
            if any(abs(start - k[0]) < 0.5 and abs(end - k[1]) < 0.5 for k in known):
                continue  # already listed under the source itself
            if any(abs(start - f["start"]) < 0.5 and abs(end - f["end"]) < 0.5 for f in found):
                continue
            found.append({
                "start": start,
                "end": end,
                "label": _readable_label(rng.get("label"), rng.get("note")) or f"{_fmt(start)} - {_fmt(end)}",
                "priority": bool(rng.get("priority")),
                "note": (rng.get("note") or "")[:80],
                "origin": origin,
                "video_id_hint": hint_id or None,
                "source_hint": hint,
            })

    add(spec.get("description") or "", "description")
    for rule in (req.get("rules") or []):
        if "http" in str(rule).lower():
            continue
        add(str(rule), "rule")
    for note in (spec.get("loose_bullets") or []):
        add(str(note), "note")

    # The raw details text, line by line: a timestamped line with no link belongs
    # to whatever video the brief mentioned last (that is how briefs are written).
    last_id: Optional[str] = None
    for line in (spec.get("plain_details") or "").splitlines():
        ids = _link_ids(line)
        if ids:
            last_id = ids[-1]
        if find_timestamp_ranges(line):
            add(line, "brief", last_id)
    return found


def attribute_windows(spec: dict, selected_ids: List[str], stamps: List[dict]) -> Dict[str, List[dict]]:
    """Decide which SELECTED source each loose window belongs to.

    Order of evidence: the video link on the same line → the source the line
    names → the only selected source → the primary selected source (flagged
    `attributed_by="primary"` so the plan can say so out loud).
    """
    by_sel = {s["video_id"]: s for s in spec.get("sources") or [] if s.get("video_id") in selected_ids}
    default_id: Optional[str] = None
    if selected_ids:
        hot = [vid for vid in selected_ids if (by_sel.get(vid) or {}).get("priority")]
        default_id = (hot or selected_ids)[0]

    out: Dict[str, List[dict]] = {vid: [] for vid in selected_ids}
    for raw in stamps or []:
        ts = dict(raw)
        target: Optional[str] = None
        how = ""
        hint_id = ts.get("video_id_hint")
        if hint_id and hint_id in out:
            target, how = hint_id, "link"
        if target is None and ts.get("source_hint"):
            key = str(ts["source_hint"]).strip().lower()[:32]
            if len(key) > 6:
                for vid in selected_ids:
                    label = ((by_sel.get(vid) or {}).get("label") or "").lower()
                    if key in label or (len(label) > 6 and label[:32] in key):
                        target, how = vid, "label"
                        break
        if target is None and len(selected_ids) == 1:
            target, how = selected_ids[0], "only-source"
        if target is None and default_id:
            target, how = default_id, "primary"
        if target is None:
            continue
        ts["attributed_to"] = target
        ts["attributed_by"] = how
        ts["source_label"] = (by_sel.get(target) or {}).get("label") or target
        out[target].append(ts)
    return out


def build_plan(
    spec: dict,
    source_urls: List[str],
    target_duration: float = 30.0,
    per_source: int = 6,
    max_total: int = 15,
    metadata_fn: Optional[Callable[[str], dict]] = None,
    peaks_fn: Optional[Callable[[list, float, float, int], List[dict]]] = None,
    allow_network: bool = True,
    method: str = "requirement",
    params: Optional[dict] = None,
) -> dict:
    """Turn a parsed campaign spec into the raw-material cut list.

    `method` decides WHERE the windows come from — it is NOT hard-wired to the
    retention heatmap, because most briefs never ship telemetry:

    * ``requirement`` (default) — the campaign's own per-source list, then every
      window the brief's REQUIREMENT/description/rule text asks for, then the
      manual windows from the clip-plan prompt.
    * ``campaign`` — the campaign's own timestamp list only (zero network).
    * ``heatmap`` — retention peaks; the OPT-IN method and the only one that
      reads telemetry from YouTube.
    * ``even`` — evenly spread raw windows, nothing else.

    Even spread stays the last-resort fallback for the first three, so a source
    never silently ends up with zero windows.
    """
    params = dict(params or {})
    method = normalize_method(params.get("method") or method or "requirement")
    req = spec.get("requirements") or {}
    min_dur = float(params.get("min_duration") or req.get("min_duration_sec") or 15.0)
    target = max(float(params.get("target_duration") or target_duration or 30.0), min_dur)
    max_dur = params.get("max_duration") or req.get("max_duration_sec")
    pad_before = max(0.0, float(params.get("pad_before") or 0.0))
    pad_after = max(0.0, float(params.get("pad_after") or 0.0))
    timestamp_source = str(params.get("timestamp_source") or "both").lower()
    manual_stamps = [dict(t) for t in (params.get("manual_timestamps") or [])]
    requirement_ts = scrape_requirement_timestamps(spec)

    by_id = {s["video_id"]: s for s in spec.get("sources", [])}
    by_url: Dict[str, dict] = {}
    for s in spec.get("sources", []):
        for key in (s.get("url"), s.get("original_url")):
            if key:
                by_url[key] = s

    items: List[dict] = []
    warnings: List[str] = []
    source_meta: List[dict] = []

    # Resolve every requested URL FIRST: the requirement-scraped timestamps have
    # to be attributed to a source before any window can be built.
    resolved: List[dict] = []
    for raw_url in source_urls or []:
        src = by_url.get(raw_url) or by_id.get(extract_video_id(raw_url) or "")
        if src is None:
            vid = extract_video_id(raw_url)
            if not vid:
                warnings.append(f"Skipped an unrecognized source URL: {raw_url}")
                continue
            src = {"url": normalize_youtube_url(raw_url), "video_id": vid, "label": vid,
                   "section": "", "priority": False, "timestamps": []}
        resolved.append(src)

    selected_ids = [s["video_id"] for s in resolved]
    attribution = attribute_windows(spec, selected_ids, requirement_ts)
    manual_by_source = attribute_windows(spec, selected_ids, manual_stamps)
    if len(selected_ids) > 1:
        for vid, hits in attribution.items():
            for hit in hits:
                if hit.get("attributed_by") == "primary":
                    warnings.append(
                        f"Window {_fmt(hit['start'])} - {_fmt(hit['end'])} from the brief is not tied to a "
                        f"specific video — applied to “{hit.get('source_label') or vid}”. Add its link next to "
                        "the timestamp to target another source."
                    )

    for src in resolved:
        entry = {
            "video_id": src["video_id"], "url": src["url"], "label": src["label"],
            "priority": bool(src.get("priority")), "mode": None, "duration": None,
            "title": None, "clip_count": 0, "note": None, "heatmap_points": 0,
            "requirement_hits": len(attribution.get(src["video_id"]) or []),
            "manual_hits": len(manual_by_source.get(src["video_id"]) or []),
        }
        picked: List[dict] = []
        meta_cache: Dict[str, dict] = {}

        def add_window(start: float, end: float, label: str, evidence: str, score: float,
                       priority: bool = False, heat=None, section: Optional[dict] = None,
                       duration: Optional[float] = None) -> None:
            s = max(0.0, float(start) - pad_before)
            e = float(end) + pad_after
            if duration:
                e = min(e, float(duration))
            if max_dur and (e - s) > float(max_dur):
                e = s + float(max_dur)
            if e - s <= 0:
                return
            item = {
                "video_id": src["video_id"],
                "source_url": src["url"],
                "source_label": src["label"],
                "section_label": label,
                "priority": bool(priority) or entry["priority"],
                "start": round(s, 2),
                "end": round(e, 2),
                "duration": round(e - s, 2),
                "evidence": evidence,
                "heat": heat,
                "score": score,
            }
            if section:
                item["section_start"] = section.get("start")
                item["section_end"] = section.get("end")
                item["origin"] = section.get("origin")
                item["attributed_by"] = section.get("attributed_by")
            items.append(item)
            picked.append(item)

        def slice_into(stamps: List[dict], evidence: str, base_score: float,
                       hot_score: float, exact: bool = False) -> int:
            """Slice every window of one stage into clips for THIS source.

            A window another stage already produced (same range ±0.5s) is
            skipped: the brief's own list wins over the same range pasted by
            hand, and a manual list never doubles a requirement window.

            `exact=True` keeps the range as ONE window: the campaign's sections
            are sliced to the requested length, but a window the user pasted by
            hand IS the window they asked for.
            """
            added = 0
            for ts in sorted(stamps, key=lambda t: (not t.get("priority"), t["start"])):
                if len(picked) >= per_source:
                    break
                if any(
                    abs(float(ts["start"]) - float(p.get("section_start", p["start"]))) < 0.5
                    and abs(float(ts["end"]) - float(p.get("section_end", p["end"]))) < 0.5
                    for p in picked
                ):
                    continue
                pieces = [(float(ts["start"]), float(ts["end"]))] if exact \
                    else _slice_section(ts["start"], ts["end"], target, min_dur)
                for s0, e0 in pieces:
                    if len(picked) >= per_source:
                        break
                    before = len(picked)
                    add_window(s0, e0, ts.get("label") or f"{_fmt(s0)} - {_fmt(e0)}", evidence,
                               hot_score if ts.get("priority") else base_score,
                               priority=bool(ts.get("priority")), section=ts)
                    added += len(picked) - before
            return added

        def ensure_meta() -> dict:
            """Duration + telemetry, fetched at most once per source."""
            if "meta" in meta_cache or not allow_network or metadata_fn is None:
                return meta_cache.get("meta") or {}
            try:
                meta_cache["meta"] = metadata_fn(src["url"]) or {}
            except Exception as exc:  # noqa: BLE001 — best effort by design
                entry["note"] = f"Metadata fetch failed: {exc}"
                meta_cache["meta"] = {}
            meta: dict = meta_cache.get("meta") or {}
            entry["duration"] = float(meta.get("duration") or 0.0) or None
            entry["title"] = meta.get("title")
            entry["heatmap_points"] = len(meta.get("heatmap") or [])
            return meta

        # --- 1) Campaign-defined timestamps: deterministic, no network, no AI.
        #     These are the brief's own list, so they win for every method except
        #     "heatmap" (an explicit opt-in to telemetry instead of the brief).
        if method in ("requirement", "campaign") and src.get("timestamps"):
            if slice_into(src["timestamps"], "campaign-timestamp", 0.8, 1.0):
                entry["mode"] = "campaign-timestamps"
            else:
                entry["note"] = "Campaign timestamps were shorter than the minimum clip length."

        # --- 2) Windows the brief asks for IN WORDS: the requirement text, the
        #     description, the rules — the timestamps campaigns write in a
        #     sentence instead of in the structured source list.
        if method == "requirement" and timestamp_source in ("requirement", "both"):
            hits = attribution.get(src["video_id"]) or []
            if hits and slice_into(hits, "requirement-timestamp", 0.85, 1.0):
                entry["mode"] = "requirement-timestamps"

        # --- 3) Manual windows pasted into the clip-plan prompt (your own list).
        #     `exact=True`: the window you typed IS the window, not a section to
        #     be re-sliced to the target length.
        if method == "requirement" and timestamp_source in ("manual", "both"):
            hits = manual_by_source.get(src["video_id"]) or []
            if hits and slice_into(hits, "manual-timestamp", 0.95, 1.0, exact=True):
                entry["mode"] = "manual-timestamps"

        # --- 4) Retention peaks — OPT-IN only (the one stage that reads YouTube).
        if method == "heatmap" and not picked:
            meta = ensure_meta()
            duration = float(meta.get("duration") or 0.0)
            heatmap = meta.get("heatmap") or []
            peaks: List[dict] = []
            if heatmap and peaks_fn is not None and duration > 0:
                try:
                    peaks = peaks_fn(heatmap, duration, target, per_source)
                except Exception as exc:  # noqa: BLE001
                    entry["note"] = f"Peak mining failed: {exc}"
                    peaks = []
            if peaks:
                entry["mode"] = "retention-peaks"
                for w in peaks[:per_source]:
                    s, e = float(w["start"]), float(w["end"])
                    add_window(
                        s, e, f"retention peak @ {_fmt(w.get('hook', s))}", "retention-peak",
                        round(float(w.get("score") or 0.0), 3),
                        heat=round(float(w.get("heat") or 0.0), 3), duration=duration,
                    )
            elif not entry["note"]:
                entry["note"] = ("No retention telemetry on this source — nothing to mine. "
                                 "Keep the brief's timestamps or paste your own.")

        # --- 5) Even spread: the last-resort fallback for EVERY method, and the
        #     only remaining stage that still needs the video duration.
        if not picked:
            meta = ensure_meta()
            duration = float(meta.get("duration") or 0.0)
            if duration > 0:
                entry["mode"] = "even-spread"
                if not entry["note"]:
                    entry["note"] = ("The brief has no timestamps for this source — windows spread evenly. "
                                     "Switch the clip method to “Retention heatmap” to mine viewer re-watch peaks.")
                for s0, e0 in _even_windows(duration, target, per_source):
                    if len(picked) >= per_source:
                        break
                    add_window(s0, e0, "even spread", "no-telemetry", 0.4, duration=duration)
            elif not entry["mode"]:
                if metadata_fn is None or not allow_network:
                    entry["mode"] = "needs-metadata"
                    entry["note"] = ("This source has no timestamps in the brief — its window plan needs the "
                                     "video duration, which the preview does not fetch.")
                else:
                    entry["mode"] = "unavailable"
                    entry["note"] = entry["note"] or "Could not read this source (blocked or unavailable)."
                    warnings.append(f"“{src['label']}” could not be analyzed: {entry['note']}")
        entry["clip_count"] = len(picked)
        source_meta.append(entry)

    # Order: priority first, then campaign timestamps in source order, then the
    # windows the brief asked for in words, then manual ones, then scored peaks.
    # Keeps the "top priority for this campaign" note actionable.
    _EVIDENCE_RANK = {
        "campaign-timestamp": 0,
        "requirement-timestamp": 1,
        "manual-timestamp": 1,
        "retention-peak": 2,
        "no-telemetry": 3,
    }

    def sort_key(item: dict):
        return (
            0 if item["priority"] else 1,
            _EVIDENCE_RANK.get(item["evidence"], 2),
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
        it["youtube_url"] = youtube_deep_link(it["video_id"], it["start"])
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
        "method": method,
        "params": {
            "method": method,
            "target_duration": target,
            "min_duration": min_dur,
            "max_duration": max_dur,
            "per_source": per_source,
            "max_clips": max_total,
            "pad_before": pad_before,
            "pad_after": pad_after,
            "timestamp_source": timestamp_source,
            "source_search": params.get("source_search") or "",
            "source_count": params.get("source_count") or 0,
            "notes": params.get("notes") or "",
        },
        "requirement_timestamps": [dict(t) for hits in attribution.values() for t in hits],
        "manual_timestamps": [dict(t) for hits in manual_by_source.values() for t in hits],
    }


def _default_title(item: dict) -> str:
    base = (item.get("section_label") or "").strip()
    label = item.get("source_label") or "Clip"
    # A section label that is nothing but a time range (or an auto tag) carries
    # no topic — fall back to the source's own name, which at least reads.
    if not base or re.fullmatch(r"[\d:\s\-–—]+", base) or base.lower() in (
            "even spread", "campaign timestamp", "manual window"):
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
    evidence = item.get("evidence")
    if evidence == "campaign-timestamp":
        label = item.get("section_label") or "timestamped section"
        flag = " ⭐ priority section" if item.get("priority") else ""
        return f"Campaign-defined section “{label}” ({_fmt(item.get('section_start', item['start']))} - {_fmt(item.get('section_end', item['end']))}){flag}"
    if evidence == "requirement-timestamp":
        where = {
            "rule": "a campaign rule", "description": "the campaign description",
            "note": "a brief note", "brief": "the brief's requirement text",
        }.get(item.get("origin") or "", "the brief's requirement text")
        flag = " ⭐ priority section" if item.get("priority") else ""
        return (f"The brief asks for “{item.get('section_label')}” "
                f"({_fmt(item.get('section_start', item['start']))} - {_fmt(item.get('section_end', item['end']))}) "
                f"in {where}{flag}")
    if evidence == "manual-timestamp":
        return (f"Your own window from the clip plan prompt "
                f"({_fmt(item.get('section_start', item['start']))} - {_fmt(item.get('section_end', item['end']))})")
    if evidence == "retention-peak":
        return f"Retention peak inside {item.get('source_label')} (heat {item.get('heat')})"
    return f"No telemetry on {item.get('source_label')} — evenly spaced raw window"


# ---------------------------------------------------------------- brief

_METHOD_LABEL = {
    "requirement": "timestamps from the brief (structured list → windows named in the requirement text → manual)",
    "campaign": "the campaign's own timestamp list only",
    "heatmap": "retention heatmap (opt-in)",
    "even": "evenly spread windows (no analysis)",
}


def plan_preview(spec: dict, source_urls: List[str], method: str = "requirement",
                 params: Optional[dict] = None, target_duration: float = 30.0,
                 per_source: int = 6, max_total: int = 15) -> dict:
    """What the CURRENT settings would cut, WITHOUT touching the network.

    Same staging as `build_plan` (campaign list → requirement text → manual →
    even spread), so the preview the UI shows can never disagree with the real
    run. Sources that would need the video duration or the heatmap come back as
    `needs-metadata` instead of guessing.
    """
    return build_plan(
        spec, source_urls, target_duration, per_source, max_total,
        None, None, False, method, params,
    )


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
    _method = plan.get("method")
    _params = plan.get("params") or {}
    if _method:
        lines.append("## Clip plan")
        lines.append(f"- Method: **{_METHOD_LABEL.get(_method, _method)}**")
        bits: List[str] = []
        if _params.get("target_duration"):
            bits.append(f"clip {_params['target_duration']:.0f}s")
        if _params.get("min_duration"):
            bits.append(f"min {_params['min_duration']:.0f}s")
        if _params.get("max_duration"):
            bits.append(f"max {_params['max_duration']:.0f}s")
        if _params.get("per_source"):
            bits.append(f"{int(_params['per_source'])} window(s)/source")
        if _params.get("pad_before") or _params.get("pad_after"):
            bits.append(f"extra pad -{_params.get('pad_before') or 0:.0f}s/+{_params.get('pad_after') or 0:.0f}s")
        if bits:
            lines.append(f"- Parameters: {', '.join(bits)}")
        req_ts = plan.get("requirement_timestamps") or []
        if req_ts:
            lines.append(f"- Windows the brief asks for in its own text ({len(req_ts)}):")
            for t in req_ts:
                label = f" · {t['label']}" if t.get("label") else ""
                lines.append(
                    f"  - {_fmt(t['start'])} - {_fmt(t['end'])}{label} · from the {t.get('origin')} → "
                    f"{t.get('source_label')} ({t.get('attributed_by')})"
                )
        if _params.get("source_search"):
            lines.append(f"- Source scrape command: `{_params['source_search']}`")
        if _params.get("notes"):
            lines.append(f"- Direction: {_params['notes']}")
        for warn in (plan.get("prompt_warnings") or []):
            lines.append(f"  - ⚠ {warn}")
        lines.append("")
    lines.append(f"## Raw cut list ({len(plan.get('items', []))} clips, target {plan.get('target_duration'):.0f}s each)")
    lines.append("")
    lines.append("Every window below is exported RAW (original quality, ±2s headroom) — trim frame-accurate in your editor.")
    lines.append("")
    lines.append("If a source blocks the automatic download (YouTube 403 / bot check), every window still carries a **jump link** to the exact second: open it, cut by hand. Same links in the copy-per-clip section.")
    lines.append("")
    current = None
    for item in plan.get("items", []):
        if item["video_id"] != current:
            current = item["video_id"]
            lines.append(f"### {item.get('source_label')}")
            lines.append(f"Source: {item.get('source_url')}")
            lines.append("")
            lines.append("| # | Window | Len | Section | Evidence | Title | Jump |")
            lines.append("|---|--------|-----|---------|----------|-------|------|")
        lines.append(
            f"| {item['index'] + 1} | {item['timestamp']} | {item['duration']:.0f}s | "
            f"{item.get('section_label') or ''} | {item['evidence']} | {item.get('title') or ''} | "
            f"[▶ open]({item.get('youtube_url') or item.get('source_url')}) |"
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
        if item.get("youtube_url"):
            lines.append(f"- Jump link: {item['youtube_url']}")
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
