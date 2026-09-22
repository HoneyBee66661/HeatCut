// Video-downloader client (YouTube / TikTok / Instagram → file).
//
// Same shape as the raw-clip helper: the DEVICE/REMOTE WORKER answers first
// (it owns the residential IP, the cookies and the miners), and the app server's
// `/api/downloader/*` routes are the fallback — they exist on any host, but on a
// cloud deploy only the worker can actually scrape.
//
// Timestamp parsing is NOT re-implemented here: the page sends the pasted text
// to `/api/downloader/parse` (a stdlib-only route, present on every deployment)
// so the windows the UI shows are exactly the windows the cut will use.

import {
  HEATMAP_WORKER_URL, safeFilename, workerHeaders, workerIsOnline,
} from './rawExport';
import { buildZip, zipEntry, type ZipEntry } from './zipBundle';

// ---------------------------------------------------------------- types

export type DownloaderPlatform = 'youtube' | 'tiktok' | 'instagram' | 'other';

export interface QualityOption {
  height: number;
  label: string;
  ext?: string | null;
  tbr?: number | null;
  filesize?: number | null;
  format_id?: string;
}

export interface AudioStreamOption {
  ext?: string | null;
  abr?: number | null;
  filesize?: number | null;
  format_id?: string;
}

export interface ProbeResult {
  platform: DownloaderPlatform;
  platform_label: string;
  url: string;
  input_url: string;
  id?: string | null;
  title: string;
  uploader: string;
  duration: number;
  duration_label: string;
  thumbnail: string;
  is_live: boolean;
  qualities: QualityOption[];
  audio_streams: AudioStreamOption[];
  has_audio: boolean;
  hls: boolean;
  dash: boolean;
  partial_supported: boolean;
  note: string;
}

export interface DownloadWindow {
  index: number;
  start: number;
  end: number;
  label: string;
}

export interface ParseResult {
  windows: DownloadWindow[];
  count: number;
  total_seconds: number;
  total_label: string;
}

export interface DownloadRequest {
  url: string;
  kind: 'video' | 'audio';
  /** 'full' = the whole media, 'window' = start_time/end_time only. */
  mode: 'full' | 'window';
  startTime?: number;
  endTime?: number;
  /** 'best' | '1080' | '720' … (video only). */
  quality?: string;
  audioFormat?: 'm4a' | 'mp3';
  title?: string;
  /** The user chose the whole-file route after a refused partial fetch. */
  fallback?: boolean;
}

export interface DownloadDenied {
  code: string;
  estSeconds: number;
  sourceUrl: string;
  message?: string;
  platform?: string;
}

export type DownloadOutcome =
  | { kind: 'file'; blob: Blob; filename: string }
  | { kind: 'denied'; denied: DownloadDenied };

// ---------------------------------------------------------------- platform

const HOST_RULES: { platform: DownloaderPlatform; hosts: string[] }[] = [
  { platform: 'youtube', hosts: ['youtube.com', 'youtu.be', 'youtube-nocookie.com', 'music.youtube.com'] },
  { platform: 'tiktok', hosts: ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'] },
  { platform: 'instagram', hosts: ['instagram.com', 'instagr.am'] },
];

const PLATFORM_LABELS: Record<DownloaderPlatform, string> = {
  youtube: 'YouTube',
  tiktok: 'TikTok',
  instagram: 'Instagram',
  other: 'Other',
};

const hostOf = (url: string): string =>
  (url || '').trim().replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '').split('/')[0]
    .split('@').pop()!.split(':')[0].toLowerCase();

/** Client-side guess of the platform — instant feedback before the probe lands. */
export function guessPlatform(url: string): DownloaderPlatform {
  const host = hostOf(url);
  for (const rule of HOST_RULES) {
    if (rule.hosts.some((h) => host === h || host.endsWith(`.${h}`))) return rule.platform;
  }
  return 'other';
}

export const platformLabel = (platform: DownloaderPlatform): string => PLATFORM_LABELS[platform];

/** A link a platform page opens at a given second (manual-fallback hand-off). */
export function sourceLinkAtTime(url: string, seconds: number): string {
  const t = Math.max(0, Math.floor(seconds || 0));
  if (!url) return '';
  if (guessPlatform(url) === 'youtube') {
    const base = url.split('&t=')[0].split('#')[0];
    return `${base}${base.includes('?') ? '&' : '?'}t=${t}s`;
  }
  return url;
}

// ---------------------------------------------------------------- parse

/** Server-side timestamp parsing (single source of truth for the windows). */
export async function parseWindows(parts: string, duration: number): Promise<ParseResult> {
  const resp = await fetch('/api/downloader/parse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parts, duration: duration || 0 }),
  });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`);
  }
  return body as ParseResult;
}

// ---------------------------------------------------------------- probe

const detailMessage = (detail: unknown, fallback: string): string => {
  if (typeof detail === 'string' && detail) return detail;
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    if (typeof d.message === 'string' && d.message) return d.message;
  }
  return fallback;
};

/** Describe a link: worker first (it can reach sites the server can't), then server. */
export async function probeLink(url: string): Promise<ProbeResult> {
  let workerError: string | null = null;

  if (await workerIsOnline()) {
    try {
      const resp = await fetch(`${HEATMAP_WORKER_URL}/probe?${new URLSearchParams({ url }).toString()}`, {
        headers: workerHeaders(),
      });
      if (resp.ok) return (await resp.json()) as ProbeResult;
      const body = await resp.json().catch(() => null);
      workerError = detailMessage(body?.detail, `worker HTTP ${resp.status}`);
    } catch (err) {
      workerError = err instanceof Error ? err.message : String(err);
    }
  }

  const resp = await fetch('/api/downloader/probe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const message = detailMessage(body?.detail, `HTTP ${resp.status}`);
    throw new Error(`${message}${workerError ? ` — worker: ${workerError}` : ''}`);
  }
  return body as ProbeResult;
}

// ---------------------------------------------------------------- download

const filenameFromHeader = (resp: Response, fallback: string): string => {
  const disposition = resp.headers.get('content-disposition') || '';
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (utf8) {
    try {
      return decodeURIComponent(utf8[1].trim().replace(/^"|"$/g, ''));
    } catch {
      /* fall through to the plain filename */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : fallback;
};

const asDenied = (detail: unknown, url: string): DownloadDenied => {
  const d = (detail && typeof detail === 'object' ? detail : {}) as Record<string, unknown>;
  const est = Number(d.est_seconds);
  return {
    code: typeof d.code === 'string' && d.code ? d.code : 'partial_blocked',
    estSeconds: Number.isFinite(est) && est > 0 ? Math.round(est) : 90,
    sourceUrl: typeof d.source_url === 'string' && d.source_url ? d.source_url : url,
    message: typeof d.message === 'string' ? d.message : undefined,
    platform: typeof d.platform === 'string' ? d.platform : undefined,
  };
};

export interface DownloadExtra {
  /** Filename to fall back on when the response carries no Content-Disposition. */
  filename?: string;
}

/** One file per call: the whole media, or the requested window. */
export async function requestDownload(
  request: DownloadRequest,
  extra: DownloadExtra = {},
): Promise<DownloadOutcome> {
  const ext = request.kind === 'audio' ? (request.audioFormat || 'm4a') : 'mp4';
  const filename = extra.filename || `${safeFilename(request.title || 'download')}.${ext}`;
  let workerError: string | null = null;

  if (await workerIsOnline()) {
    const qs = new URLSearchParams({
      url: request.url,
      kind: request.kind,
      mode: request.mode,
      start: String(request.startTime ?? 0),
      end: String(request.endTime ?? 0),
      quality: request.quality || '',
      audio_format: request.audioFormat || 'm4a',
      title: request.title || '',
      fallback: request.fallback ? '1' : '0',
    }).toString();
    try {
      const resp = await fetch(`${HEATMAP_WORKER_URL}/download?${qs}`, { headers: workerHeaders() });
      if (resp.ok) {
        return { kind: 'file', blob: await resp.blob(), filename: filenameFromHeader(resp, filename) };
      }
      const body = await resp.json().catch(() => null);
      if (resp.status === 409) return { kind: 'denied', denied: asDenied(body?.detail, request.url) };
      if (resp.status === 400) throw new Error(detailMessage(body?.detail, 'The request was rejected.'));
      workerError = detailMessage(body?.detail, `worker HTTP ${resp.status}`);
    } catch (err) {
      if (err instanceof Error && err.message && !err.message.startsWith('worker HTTP')) throw err;
      workerError = err instanceof Error ? err.message : String(err);
    }
  }

  const resp = await fetch('/api/downloader/fetch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: request.url,
      kind: request.kind,
      mode: request.mode,
      start_time: request.startTime ?? 0,
      end_time: request.endTime ?? 0,
      quality: request.quality || null,
      audio_format: request.audioFormat || 'm4a',
      title: request.title || null,
      fallback: Boolean(request.fallback),
    }),
  });
  if (resp.status === 409) {
    const body = await resp.json().catch(() => null);
    return { kind: 'denied', denied: asDenied(body?.detail, request.url) };
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    const message = detailMessage(body?.detail, `HTTP ${resp.status}`);
    throw new Error(`${message}${workerError ? ` — worker: ${workerError}` : ''}`);
  }
  return { kind: 'file', blob: await resp.blob(), filename: filenameFromHeader(resp, filename) };
}

/** Pack several parts into ONE archive — one download always saves (see zipBundle). */
export async function zipParts(files: { name: string; blob: Blob }[]): Promise<Blob> {
  const entries: ZipEntry[] = [];
  for (const file of files) entries.push(await zipEntry(file.name, file.blob));
  return buildZip(entries);
}

/** Browser download for a blob (shared with the studio's clip downloads). */
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

export { safeFilename };
