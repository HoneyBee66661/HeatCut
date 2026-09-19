// Raw-clip export helper shared by the campaign page.
//
// Mirrors the studio's download path: prefer the DEVICE WORKER (loopback or a
// tunneled remote worker — on cloud deploys the server itself cannot scrape
// YouTube), fall back to the server route `/api/export`.

const HEATMAP_WORKER_URL: string =
  (import.meta.env.VITE_HEATMAP_WORKER_URL as string | undefined) || 'http://127.0.0.1:8765';

const HEATMAP_WORKER_TOKEN: string =
  (import.meta.env.VITE_HEATMAP_WORKER_TOKEN as string | undefined) || '';

const IS_LOOPBACK_WORKER = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|$)/i.test(HEATMAP_WORKER_URL);
const WORKER_PROBE_TIMEOUT_MS = IS_LOOPBACK_WORKER ? 400 : 2500;

const workerHeaders = (): Record<string, string> =>
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
async function workerIsOnline(): Promise<boolean> {
  try {
    const probe = await fetch(`${HEATMAP_WORKER_URL}/health`, {
      signal: AbortSignal.timeout(WORKER_PROBE_TIMEOUT_MS),
    });
    return Boolean(probe.ok);
  } catch {
    return false;
  }
}

/** Fetch a RAW clip (stream-copy, original quality) and hand back the blob. */
export async function fetchRawClip(
  videoId: string,
  startTime: number,
  endTime: number,
  title: string,
): Promise<Blob> {
  let workerError: string | null = null;

  if (await workerIsOnline()) {
    const qs = new URLSearchParams({
      video_id: videoId,
      start_time: String(startTime),
      end_time: String(endTime),
      title,
    }).toString();
    try {
      const resp = await fetch(`${HEATMAP_WORKER_URL}/export?${qs}`, { headers: workerHeaders() });
      if (resp.ok) return resp.blob();
      // 401 = this build has no worker token; anything else may be a transient
      // worker problem — either way the server route is the fallback.
      const detail = await resp.json().catch(() => null);
      workerError = detail?.detail || `worker HTTP ${resp.status}`;
    } catch (err) {
      workerError = err instanceof Error ? err.message : String(err);
    }
  }

  const resp = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ video_id: videoId, start_time: startTime, end_time: endTime, title }),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => null);
    const serverMsg = detail?.detail || `HTTP ${resp.status}`;
    throw new Error(`Export failed (${serverMsg}${workerError ? ` — worker: ${workerError}` : ''})`);
  }
  return resp.blob();
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
