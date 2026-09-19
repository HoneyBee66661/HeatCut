"""Whisper child process: transcribe ONE wav file, print one JSON line.

Invoked by `backend/asr.py` (and by the device worker) with the same
interpreter, so the model never lives in the API's address space — `small`
peaks around 0.6 GB RSS on this 2 vCPU / 2 GB host.

    python whisper_runner.py --wav /path/win.wav --model small [--language en]

Output (stdout, last line, always a single JSON object):

    {"ok": true, "model": "small", "language": "en", "language_probability": 1.0,
     "duration": 34.0, "load_s": 2.4, "asr_s": 11.1, "device": "cpu", "compute_type": "int8",
     "segments": [{"start": 0.0, "end": 2.1, "text": "..."}, ...]}

Failures answer {"ok": false, "error": "..."} on stdout with exit code 1, so the
caller can surface a real reason instead of a bare non-zero status.

WHY these settings (measured on this host, 2 vCPU):
  * `vad_filter=False` — the default Silero VAD dropped an ENTIRE loud music
    window (0 segments, 0.2 s "transcription") on the first probe, and music/
    concert clips are exactly what this app cuts. Never enable it here.
  * `beam_size=1` + `condition_on_previous_text=False` — ~8x realtime on
    `small` for a 30 s window; a beam of 5 costs ~3x that for a marginal gain
    on short clips. Override with HEATCUT_WHISPER_BEAM if copy needs polish.
  * `compute_type=int8` — CPU-only host; int8 keeps `small` at ~0.6 GB RSS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def emit(payload: dict, code: int = 0) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--model", default=os.environ.get("HEATCUT_WHISPER_MODEL", "small"))
    ap.add_argument("--language", default=os.environ.get("HEATCUT_WHISPER_LANGUAGE", ""))
    ap.add_argument("--threads", type=int, default=int(os.environ.get("HEATCUT_WHISPER_THREADS", "2")))
    ap.add_argument("--beam", type=int, default=int(os.environ.get("HEATCUT_WHISPER_BEAM", "1")))
    ap.add_argument("--max-seconds", type=float,
                    default=float(os.environ.get("HEATCUT_WHISPER_MAX_WINDOW", "600")))
    args = ap.parse_args()

    if not os.path.exists(args.wav):
        return emit({"ok": False, "error": f"wav not found: {args.wav}"}, 1)

    t0 = time.time()
    try:
        from faster_whisper import WhisperModel
    except Exception as e:  # noqa: BLE001 — the honest answer: engine missing
        return emit({"ok": False, "error": f"faster-whisper unavailable ({e})"}, 1)

    try:
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             cpu_threads=max(1, args.threads))
    except Exception as e:  # noqa: BLE001 — bad model name, download failure, OOM
        return emit({"ok": False, "error": f"model {args.model} could not be loaded ({str(e)[:200]})"}, 1)
    load_s = time.time() - t0

    t1 = time.time()
    try:
        segments, info = model.transcribe(
            args.wav,
            beam_size=max(1, args.beam),
            vad_filter=False,               # see the module docstring — VAD eats music
            language=args.language or None,
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        rows = [
            {"start": round(float(s.start), 3), "end": round(float(s.end), 3),
             "text": (s.text or "").strip()}
            for s in segments if (s.text or "").strip()
        ]
    except Exception as e:  # noqa: BLE001
        return emit({"ok": False, "error": f"transcription failed ({str(e)[:200]})"}, 1)
    asr_s = time.time() - t1

    return emit({
        "ok": True,
        "model": args.model,
        "language": getattr(info, "language", "") or "",
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
        "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
        "load_s": round(load_s, 2),
        "asr_s": round(asr_s, 2),
        "device": "cpu",
        "compute_type": "int8",
        "segments": rows,
    }, 0)


if __name__ == "__main__":
    sys.exit(main())
