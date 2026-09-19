// Raw-clip export helper shared by the studio and the campaign page.
//
// Mirrors the studio's download path: prefer the DEVICE WORKER (loopback or a
// tunneled remote worker — on cloud deploys the server itself cannot scrape
// YouTube), fall back to the server route `/api/export`.
//
// Export modes (see backend `_auto_partial_export`):
//   auto — the backend takes the cheapest partial route it can find (DASH byte
//          ranges, then HLS segments). When YouTube refuses BOTH, it answers
//          HTTP 409 with {code, est_seconds, source_url} instead of silently
//          downloading the whole video: we hand that to the UI so the user can
//          choose "whole video (with an ETA)" or "download the source".
//   full — the whole-video route, only on the user's explicit request.

export const HEATMAP_WORKER_URL: string =
  (import.meta.env.VITE_HEATMAP_WORKER_URL as string | undefined) || 'http://127.0.0.1:8765';

const HEATMAP_WORKER_TOKEN: string =
  (import.meta.env.VITE_HEATMAP_WORKER_TOKEN as string | undefined) || '';

const IS_LOOPBACK_WORKER = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|$)/i.test(HEATMAP_WORKER_URL);
const WORKER_PROBE_TIMEOUT_MS = IS_LOOPBACK_WORKER ? 400 : 2500;

export const workerHeaders = (): Record<string, string> =>
  HEATMAP_WORKER_TOKEN ? { 'X-Heatcut-Token': HEATMAP_WORKER_TOKEN } : {};

export const safeFilename = (raw: string): string =>
  (raw || 'clip')
    .replace(/[^\p{L}\p{N} _.-]+/gu, '')
    .replace(/[\s_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 80) || 'clip';

/**
 * Labeled jump link to the exact second of a source video.
 *
 * Manual-fallback deliverable: when the automatic export is refused (YouTube
 * 403 / bot check / worker offline) the editor must still get something
 * actionable — a link that opens the source AT the window's start second.
 */
export const youtubeLink = (videoId: string, startTime: number): string =>
  videoId ? `https://www.youtube.com/watch?v=${videoId}&t=${Math.max(0, Math.floor(startTime || 0))}s` : '';

/** True when a device/tunneled worker answers the loopback-style health probe. */
export async function workerIsOnline(): Promise<boolean> {
  try {
    const probe = await fetch(`${HEATMAP_WORKER_URL}/health`, {
      signal: AbortSignal.timeout(WORKER_PROBE_TIMEOUT_MS),
    });
    return Boolean(probe.ok);
  } catch {
    return false;
  }
}

export type RawClipMode = 'auto' | 'full';

/** The backends' "partial fetch refused" answer (HTTP 409). */
export type RawClipDenied = {
  code: string;
  estSeconds: number;
  sourceUrl: string;
  message?: string;
};

export type RawClipResult =
  | { kind: 'clip'; blob: Blob }
  | { kind: 'denied'; denied: RawClipDenied };

const DEFAULT_EST_SECONDS = 60;

const asDenied = (detail: unknown, videoId: string, startTime: number): RawClipDenied => {
  const d = (detail && typeof detail === 'object' ? detail : {}) as Record<string, unknown>;
  const est = Number(d.est_seconds);
  return {
    code: typeof d.code === 'string' && d.code ? d.code : 'yt_partial_blocked',
    estSeconds: Number.isFinite(est) && est > 0 ? Math.round(est) : DEFAULT_EST_SECONDS,
    sourceUrl: typeof d.source_url === 'string' && d.source_url ? d.source_url : youtubeLink(videoId, startTime),
    message: typeof d.message === 'string' ? d.message : undefined,
  };
};

/**
 * Ask for a RAW clip and report WHICH kind of answer came back.
 *
 * `denied` is not an error: YouTube refused the partial fetch, and the caller
 * shows the user their two options (whole video with a countdown, or the source
 * itself). A 409 from the worker is NOT retried against the server — same
 * scraper, same refusal, and the user would just wait twice.
 */
export async function requestRawClip(
  videoId: string,
  startTime: number,
  endTime: number,
  title: string,
  opts: { mode?: RawClipMode; signal?: AbortSignal } = {},
): Promise<RawClipResult> {
  const mode: RawClipMode = opts.mode || 'auto';
  let workerError: string | null = null;

  if (await workerIsOnline()) {
    const qs = new URLSearchParams({
      video_id: videoId,
      start_time: String(startTime),
      end_time: String(endTime),
      title,
      mode,
    }).toString();
    try {
      const resp = await fetch(`${HEATMAP_WORKER_URL}/export?${qs}`, {
        headers: workerHeaders(),
        signal: opts.signal,
      });
      if (resp.ok) return { kind: 'clip', blob: await resp.blob() };
      const detail = await resp.json().catch(() => null);
      if (resp.status === 409) {
        return { kind: 'denied', denied: asDenied(detail?.detail, videoId, startTime) };
      }
      // 401 = this build has no worker token; anything else may be a transient
      // worker problem — either way the server route is the fallback.
      workerError = detail?.detail || `worker HTTP ${resp.status}`;
    } catch (err) {
      if (opts.signal?.aborted) throw err;
      workerError = err instanceof Error ? err.message : String(err);
    }
  }

  const resp = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ video_id: videoId, start_time: startTime, end_time: endTime, title, mode }),
    signal: opts.signal,
  });
  if (resp.status === 409) {
    const detail = await resp.json().catch(() => null);
    return { kind: 'denied', denied: asDenied(detail?.detail, videoId, startTime) };
  }
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    const serverMsg = typeof detail?.detail === 'string' ? detail.detail : `HTTP ${resp.status}`;
    throw new Error(`Export failed (${serverMsg}${workerError ? ` — worker: ${workerError}` : ''})`);
  }
  return { kind: 'clip', blob: await resp.blob() };
}

/**
 * Blob-only wrapper for the flows that have nowhere to show a choice (bulk
 * "download all", zip packing): a refused partial is a hard error there, and
 * the window stays in the manual-cut list as before.
 */
export async function fetchRawClip(
  videoId: string,
  startTime: number,
  endTime: number,
  title: string,
): Promise<Blob> {
  const result = await requestRawClip(videoId, startTime, endTime, title, { mode: 'auto' });
  if (result.kind === 'denied') {
    throw new Error(result.denied.message || 'YouTube refused the partial fetch for this window.');
  }
  return result.blob;
}

/** Browser download for a blob (used for clips and the markdown brief). */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
