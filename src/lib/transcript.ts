// Per-window transcript (caption text + SRT) for the campaign page.
//
// Order of attack — the SERVER first, the device WORKER second:
//   * the server owns the caption ladder (yt-dlp player response → direct
//     fetch → Supadata) and, when HeatCut runs on your own machine, local
//     faster-whisper as well;
//   * on a cloud deploy the server has no ASR engine and answers
//     `source: "none"` with a reason — then the device worker (which always
//     runs next to the browser's own machine) does the transcription locally.
// The response shape is identical on both sides (backend/asr.py), so the UI
// never has to care which one answered.

import { HEATMAP_WORKER_URL, workerHeaders, workerIsOnline } from './rawExport';

const SERVER_TIMEOUT_MS = 300_000; // a Whisper window is 10-60 s of CPU
const WORKER_TIMEOUT_MS = 300_000;

export interface TranscriptLine {
  start: number;
  end: number;
  text: string;
  lang?: string;
}

export interface WindowTranscript {
  /** Where the words came from. `none` = no transcript available at all. */
  source: 'youtube' | 'whisper' | 'none';
  engine: string;
  model: string;
  language: string;
  lines: TranscriptLine[];
  /** Caption text of the window — one flowing paragraph. */
  text: string;
  /** SRT timed against the DOWNLOADED clip (which starts 2 s before the window). */
  srt: string;
  /** SRT timed against the requested window (0 = window start). */
  srt_window?: string;
  note: string;
  elapsed: number;
  cached?: boolean;
  video_id?: string;
  asr_seconds?: number;
}

export interface TranscriptOptions {
  model?: string;
  language?: string;
  allowWhisper?: boolean;
}

const empty = (note: string): WindowTranscript => ({
  source: 'none', engine: '', model: '', language: '', lines: [], text: '', srt: '',
  note, elapsed: 0,
});

/** POST JSON and parse JSON — the transcript routes answer one object, no SSE. */
async function postJson(
  url: string,
  body: unknown,
  timeoutMs: number,
  headers: Record<string, string> = {},
): Promise<WindowTranscript> {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = (data && (data.detail || data.note)) || `HTTP ${resp.status}`;
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
  }
  return data as WindowTranscript;
}

/**
 * Caption text + SRT for one clip window. Never throws for "no transcript":
 * a missing transcript is an answer (`source: 'none'` + `note`), not a crash —
 * the caller still hands over the jump link.
 */
export async function fetchWindowTranscript(
  videoId: string,
  start: number,
  end: number,
  opts: TranscriptOptions = {},
): Promise<WindowTranscript> {
  const body = {
    video_id: videoId,
    start,
    end,
    language: opts.language || '',
    model: opts.model || '',
    allow_whisper: opts.allowWhisper !== false,
  };
  let best = empty('');
  let lastError = '';

  try {
    const server = await postJson('/api/transcript/window', body, SERVER_TIMEOUT_MS);
    if (server.source !== 'none' && (server.lines || []).length) return server;
    best = server;
  } catch (err) {
    lastError = err instanceof Error ? err.message : String(err);
  }

  // Worker fallback: only worth asking when it is actually reachable. The
  // worker is the machine the BROWSER runs on, so on a cloud deploy this is
  // where the local Whisper engine lives.
  try {
    if (await workerIsOnline()) {
      const worker = await postJson(
        `${HEATMAP_WORKER_URL}/transcript/window`,
        body,
        WORKER_TIMEOUT_MS,
        workerHeaders(),
      );
      if (worker.source !== 'none' && (worker.lines || []).length) return worker;
      if (!best.note) best = worker;
    }
  } catch (err) {
    lastError = lastError || (err instanceof Error ? err.message : String(err));
  }

  if (best.source !== 'none' || best.note) return best;
  return empty(lastError || 'No transcript source could be reached.');
}

/** Filename-safe SRT name for a window: matches the clip's download name. */
export function srtFilename(label: string, start: number, end: number): string {
  const safe = (label || 'clip')
    .replace(/[^\p{L}\p{N} _.-]+/gu, '')
    .replace(/[\s_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 80) || 'clip';
  return `${safe}_${Math.floor(start)}-${Math.floor(end)}s.srt`;
}
