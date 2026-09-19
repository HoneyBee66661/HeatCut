"""Local speech-to-text for a clip window — the fallback when YouTube has no captions.

This module is deliberately STANDALONE and stdlib-only: it never imports
faster-whisper into the API process. Transcription runs in a CHILD process
(`whisper_runner.py`, invoked with the same interpreter) because the model
needs ~0.6 GB RSS on `small`, and this box is a 2 vCPU / 2 GB instance that
also serves the API, the worker and vite. A child process keeps the peak out
of the API's address space and makes a crash cost one request, not the server.

Nothing here touches yt-dlp or FastAPI: the caller injects
  * `captions_fn(video_id) -> list[dict]`  — the caption ladder (yt-dlp, direct
    fetch, Supadata) already implemented by the backend/worker, and
  * `audio_fn(video_id, cut_start, cut_end) -> str` — the audio-window fetcher,
which keeps this module unit-testable without network, keys or ffmpeg.

Wire contract of `window_transcript()` (shared verbatim by the server route
and the device worker so the UIs never have to know which one answered):

    {"source": "youtube"|"whisper"|"none", "engine": "captions"|"faster-whisper",
     "model": "small"|"", "language": "en", "lines": [{start,end,text}, ...],
     "text": "...", "srt": "...", "srt_window": "...",
     "cut_start": 28.0, "pad_pre": 2.0, "note": "...", "elapsed": 12.4,
     "cached": false}

`lines` carry ABSOLUTE source times. `srt` is timed against the DOWNLOADED
clip file (which the export route pads ±2 s), `srt_window` against the
requested window — an editor importing both the clip and its .srt gets
subtitle sync without a manual offset.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

# Bump when the result shape or the runner contract changes: cached entries
# from an older version are then ignored instead of served half-stale.
ASR_VERSION = 3

DEFAULT_MODEL = os.environ.get("HEATCUT_WHISPER_MODEL", "small")
DEFAULT_TIMEOUT = float(os.environ.get("HEATCUT_WHISPER_TIMEOUT", "600"))
MAX_WINDOW_SECONDS = float(os.environ.get("HEATCUT_WHISPER_MAX_WINDOW", "600"))

# The export route pads every window; the SRT is anchored on the same cut.
DEFAULT_PAD_PRE = 2.0
DEFAULT_PAD_POST = 2.0

_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper_runner.py")
_AVAILABLE: Optional[tuple] = None


class AsrError(Exception):
    """Local transcription failed (no engine, refused media, runner error)."""


# --------------------------------------------------------------------- helpers

def cache_root() -> str:
    """Where transcripts and the audio scratch live (env-overridable)."""
    root = os.environ.get("HEATCUT_ASR_TMP") or os.path.join(
        os.path.expanduser("~"), ".heatcut", "asr")
    return root


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def srt_timestamp(seconds: float) -> str:
    total_ms = int(round(max(0.0, float(seconds or 0.0)) * 1000))
    h, rem = divmod(total_ms, 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    sec, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def normalize_lines(raw: Iterable[dict]) -> List[dict]:
    """Accept {start,end} or {start,duration} (the caption shape) → one shape."""
    out: List[dict] = []
    for line in raw or []:
        try:
            start = float(line.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
        if "end" in line and line.get("end") is not None:
            try:
                end = float(line["end"])
            except (TypeError, ValueError):
                end = start
        else:
            try:
                end = start + float(line.get("duration") or 0.0)
            except (TypeError, ValueError):
                end = start
        text = (line.get("text") or "").strip()
        if not text:
            continue
        if end <= start:
            end = start + 0.01
        item = {"start": round(start, 3), "end": round(end, 3), "text": text}
        if line.get("lang"):
            item["lang"] = line["lang"]
        out.append(item)
    out.sort(key=lambda x: x["start"])
    return out


def window_slice(lines: Iterable[dict], start: float, end: float,
                 min_overlap: float = 0.25) -> List[dict]:
    """Caption lines covering [start, end].

    A line counts when it OVERLAPS the window by more than `min_overlap`
    seconds — captions bleed across the cut, and dropping a line that starts
    0.2 s before the window loses the first word of the clip.
    """
    picked = []
    for line in lines:
        overlap = min(line["end"], end) - max(line["start"], start)
        if overlap > min_overlap or (line["start"] >= start and line["start"] < end):
            picked.append(dict(line))
    return picked


def lines_to_text(lines: Iterable[dict]) -> str:
    """Caption text of a window: one flowing paragraph, single spaces."""
    parts = [(l["text"] or "").strip() for l in lines if (l.get("text") or "").strip()]
    return " ".join(" ".join(parts).split())


def lines_to_srt(lines: Iterable[dict], shift: float = 0.0) -> str:
    """SRT body with `shift` seconds ADDED to every timestamp.

    `shift = -cut_start` re-bases absolute caption times onto a clip file that
    starts at `cut_start`; whisper segments already come clip-relative.
    """
    blocks: List[str] = []
    for i, line in enumerate(lines, start=1):
        ts = max(0.0, line["start"] + shift)
        te = max(ts + 0.05, line["end"] + shift)
        blocks.append(f"{i}\n{srt_timestamp(ts)} --> {srt_timestamp(te)}\n{line['text']}\n")
    return "\n".join(blocks)


def build_result(lines: List[dict], *, source: str, engine: str, model: str = "",
                 language: str = "", cut_start: float = 0.0, pad_pre: float = DEFAULT_PAD_PRE,
                 window_start: float = 0.0, note: str = "", elapsed: float = 0.0,
                 cached: bool = False, extra: Optional[Dict[str, Any]] = None) -> dict:
    """Assemble the wire contract from absolute-time lines."""
    result: Dict[str, Any] = {
        "source": source,
        "engine": engine,
        "model": model,
        "language": language or "",
        "lines": lines,
        "text": lines_to_text(lines),
        # Clip-anchored SRT (matches the padded export) + window-anchored one.
        "srt": lines_to_srt(lines, -cut_start) if cut_start else lines_to_srt(lines),
        "srt_window": lines_to_srt(lines, -window_start),
        "cut_start": round(cut_start, 3),
        "pad_pre": pad_pre,
        "note": note or "",
        "elapsed": round(elapsed, 2),
        "cached": cached,
        "version": ASR_VERSION,
    }
    if extra:
        result.update(extra)
    return result


# ----------------------------------------------------------------- engine: whisper

def whisper_available() -> tuple:
    """(available, reason) — probed ONCE per process (import probe is ~1 s)."""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    if not os.path.exists(_RUNNER):
        _AVAILABLE = (False, "whisper_runner.py is missing next to backend/asr.py")
        return _AVAILABLE
    try:
        probe = subprocess.run([sys.executable, "-c", "import faster_whisper"],
                               capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        _AVAILABLE = (False, f"whisper probe failed: {str(e)[:120]}")
        return _AVAILABLE
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        _AVAILABLE = (False, f"faster-whisper not installed ({detail[-1][:110] if detail else 'import error'})")
        return _AVAILABLE
    _AVAILABLE = (True, "")
    return _AVAILABLE


def transcribe_wav(wav_path: str, model: str = "", language: str = "",
                   timeout: float = 0.0, threads: int = 0) -> dict:
    """Run the whisper child process on a wav file; returns its JSON payload."""
    ok, reason = whisper_available()
    if not ok:
        raise AsrError(reason)
    cmd = [sys.executable, _RUNNER, "--wav", wav_path, "--model", model or DEFAULT_MODEL]
    if language:
        cmd += ["--language", language]
    if threads:
        cmd += ["--threads", str(threads)]
    env = dict(os.environ)
    # Keep the (2 vCPU) box responsive while a transcription runs.
    env.setdefault("OMP_NUM_THREADS", str(threads or 2))
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=timeout or DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AsrError(f"transcription timed out after {int(timeout or DEFAULT_TIMEOUT)}s")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise AsrError(f"whisper failed: {detail[-1][:160] if detail else 'unknown error'}")
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AsrError("whisper returned no parsable JSON")
    if not payload.get("ok"):
        raise AsrError(str(payload.get("error") or "whisper reported a failure")[:180])
    return payload


# -------------------------------------------------------------------------- cache

def _cache_key(video_id: str, start: float, end: float, kind: str, model: str,
               language: str) -> str:
    raw = f"v{ASR_VERSION}|{video_id}|{start:.3f}|{end:.3f}|{kind}|{model}|{language}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def read_cache(key: str) -> Optional[dict]:
    path = os.path.join(cache_root(), "cache", f"{key}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_cache(key: str, payload: dict) -> None:
    try:
        directory = _ensure_dir(os.path.join(cache_root(), "cache"))
        tmp = os.path.join(directory, f"{key}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(directory, f"{key}.json"))
    except OSError:
        pass  # cache is an optimization, never a failure


# --------------------------------------------------------------------- orchestrator

def window_transcript(video_id: str, start: float, end: float, *,
                      captions_fn: Optional[Callable[[str], List[dict]]] = None,
                      audio_fn: Optional[Callable[[str, float, float], str]] = None,
                      model: str = "", language: str = "", use_captions: bool = True,
                      allow_whisper: bool = True, use_cache: bool = True,
                      pad_pre: float = DEFAULT_PAD_PRE,
                      pad_post: float = DEFAULT_PAD_POST) -> dict:
    """Captions when the video has them, local Whisper when it does not.

    `captions_fn` may raise (the caption ladder raises HTTP errors for videos
    without subtitles) — that is the whisper trigger, not a failure.
    `audio_fn(video_id, cut_start, cut_end)` must return a 16 kHz mono wav that
    STARTS at cut_start (the padded clip start), so whisper's own segment times
    line up with the exported clip.
    """
    start = max(0.0, float(start))
    end = float(end)
    if end <= start:
        raise AsrError("end must be greater than start")
    if end - start > MAX_WINDOW_SECONDS:
        raise AsrError(f"window too long for local transcription (max {int(MAX_WINDOW_SECONDS)}s)")
    cut_start = max(0.0, start - pad_pre)
    model = (model or DEFAULT_MODEL).strip()
    language = (language or "").strip()
    t0 = time.time()
    notes: List[str] = []

    # ── 1) captions ────────────────────────────────────────────────────────────
    if use_captions and captions_fn is not None:
        key = _cache_key(video_id, start, end, "captions", "", "")
        if use_cache:
            hit = read_cache(key)
            if hit:
                hit["cached"] = True
                return hit
        try:
            raw = captions_fn(video_id) or []
        except Exception as e:  # noqa: BLE001 — missing captions is a normal outcome
            raw = []
            notes.append(f"captions unavailable ({str(e)[:110]})")
        lines = window_slice(normalize_lines(raw), start, end)
        if lines:
            result = build_result(lines, source="youtube", engine="captions",
                                  cut_start=cut_start, pad_pre=pad_pre,
                                  window_start=start, elapsed=time.time() - t0,
                                  note=" | ".join(notes),
                                  extra={"language": lines[0].get("lang", "")})
            if use_cache:
                write_cache(key, result)
            return result
        if raw:
            notes.append("captions exist for this video but no words fall inside the window")

    # ── 2) local whisper ───────────────────────────────────────────────────────
    if allow_whisper and audio_fn is not None:
        ok, reason = whisper_available()
        if not ok:
            notes.append(reason)
        else:
            key = _cache_key(video_id, start, end, "whisper", model, language)
            if use_cache:
                hit = read_cache(key)
                if hit:
                    hit["cached"] = True
                    return hit
            try:
                wav = audio_fn(video_id, cut_start, end + pad_post)
                payload = transcribe_wav(wav, model=model, language=language)
                # Whisper times are relative to the AUDIO FILE, which starts at
                # cut_start → re-base them onto source time before building the
                # result (otherwise the clip-anchored SRT collapses to 0.000).
                lines = normalize_lines([
                    {"start": (s.get("start") or 0.0) + cut_start,
                     "end": (s.get("end") or 0.0) + cut_start,
                     "text": s.get("text")}
                    for s in payload.get("segments") or []
                ])
                result = build_result(
                    lines, source="whisper", engine="faster-whisper", model=payload.get("model") or model,
                    language=payload.get("language") or language, cut_start=cut_start,
                    pad_pre=pad_pre, window_start=start, elapsed=time.time() - t0,
                    note=" | ".join(notes),
                    extra={"asr_seconds": payload.get("asr_s"), "audio_seconds": payload.get("duration")})
                if lines and use_cache:
                    write_cache(key, result)
                return result
            except AsrError as e:
                notes.append(str(e)[:180])
            except Exception as e:  # noqa: BLE001 — media refused, ffmpeg hiccup, ...
                notes.append(f"local transcription failed ({str(e)[:140]})")

    # ── 3) nothing ─────────────────────────────────────────────────────────────
    return build_result([], source="none", engine="", model=model, language=language,
                        cut_start=cut_start, pad_pre=pad_pre, window_start=start,
                        elapsed=time.time() - t0, note=" | ".join(notes) or "no transcript source")


def scratch_dir() -> str:
    """Per-call temp dir for the audio window (cleaned by the caller)."""
    return tempfile.mkdtemp(prefix="asr_", dir=_ensure_dir(cache_root()))
