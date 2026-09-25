// Campaign prep SESSIONS — the campaign page's equivalent of the studio's
// analysis history.
//
// The studio rebuilds its history by scanning `cheat_clip_cache_*` keys in
// localStorage; the campaign page has a different shape of work (a brief, a cut
// list, downloads and transcripts), so the same pattern is implemented
// explicitly: one key per session plus an index of summaries.
//
//   heatcut_campaign_session_<campaign_id>  → the whole session (JSON)
//   heatcut_campaign_sessions               → [{key, label, …, items, exported,
//                                              captions, sourceIds, thumbnail}]
//
// Quota discipline: a session can hold 15 windows of transcripts (a few KB
// each) but never the media, so writes stay small; when the browser still
// refuses (quota exceeded) the OLDEST sessions are dropped and the write is
// retried once — an old session must never block the current one.

import type { CampaignSpec, CreativeStrategy, PrepPlan } from '../CampaignPage';
import type { WindowTranscript } from './transcript';

const SESSION_PREFIX = 'heatcut_campaign_session_';
const INDEX_KEY = 'heatcut_campaign_sessions';
const URL_KEY = 'heatcut_campaign_url';
const MAX_SESSIONS = 8;

export interface PrepSession {
  key: string;
  label: string;
  url: string;
  at: number;
  updated_at: number;
  spec: CampaignSpec | null;
  plan: PrepPlan | null;
  selected: Record<string, boolean>;
  target_duration: number;
  per_source: number;
  max_clips: number;
  ai_copy: boolean;
  /** item id → why the automatic export failed (manual-cut hand-off). */
  manual: Record<string, string>;
  /** item id → epoch ms of the last successful export. */
  exported: Record<string, number>;
  /** item id → caption text + SRT. */
  transcripts: Record<string, WindowTranscript>;
  /** Creative board for this brief (the consultant's output, if built). */
  strategy?: CreativeStrategy | null;
  strategy_md?: string;
  strategy_prompt?: string;
  strategy_tone?: string;
  strategy_audience?: string;
  strategy_ideas?: number;
}

export interface SessionSummary {
  key: string;
  label: string;
  url: string;
  at: number;
  updated_at: number;
  items: number;
  exported: number;
  captions: number;
  sourceIds: string[];
  thumbnail: string;
}

const safeParse = <T,>(raw: string | null, fallback: T): T => {
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    return (parsed ?? fallback) as T;
  } catch {
    return fallback;
  }
};

export const readIndex = (): SessionSummary[] =>
  safeParse<SessionSummary[]>(localStorage.getItem(INDEX_KEY), []).slice(0, MAX_SESSIONS);

const writeIndex = (entries: SessionSummary[]): void => {
  try {
    localStorage.setItem(INDEX_KEY, JSON.stringify(entries.slice(0, MAX_SESSIONS)));
  } catch {
    /* index is a convenience; a failed write only costs the panel */
  }
};

export const summarize = (session: PrepSession): SessionSummary => {
  const items = session.plan?.items || [];
  const sourceIds: string[] = [];
  items.forEach(it => {
    if (!sourceIds.includes(it.video_id)) sourceIds.push(it.video_id);
  });
  const captions = Object.values(session.transcripts || {}).filter(t => t && t.source !== 'none').length;
  return {
    key: session.key,
    label: session.label,
    url: session.url,
    at: session.at,
    updated_at: session.updated_at,
    items: items.length,
    exported: Object.keys(session.exported || {}).length,
    captions,
    sourceIds: sourceIds.slice(0, 4),
    thumbnail: sourceIds[0] ? `https://img.youtube.com/vi/${sourceIds[0]}/mqdefault.jpg` : '',
  };
};

/** Persist one session. Returns false when the browser refused even after pruning. */
export const writeSession = (session: PrepSession): boolean => {
  const payload = JSON.stringify(session);
  const persist = (): boolean => {
    try {
      localStorage.setItem(`${SESSION_PREFIX}${session.key}`, payload);
      return true;
    } catch {
      return false;
    }
  };
  if (persist()) {
    const summary = summarize(session);
    const rest = readIndex().filter(s => s.key !== session.key);
    writeIndex([summary, ...rest]);
    return true;
  }
  // Quota: drop the oldest sessions (never the one being written) and retry once.
  const index = readIndex().filter(s => s.key !== session.key);
  const oldest = index[index.length - 1];
  if (oldest) {
    localStorage.removeItem(`${SESSION_PREFIX}${oldest.key}`);
    writeIndex(index.slice(0, -1));
  }
  const ok = persist();
  if (ok) writeIndex([summarize(session), ...index.slice(0, -1)]);
  return ok;
};

export const readSession = (key: string): PrepSession | null =>
  safeParse<PrepSession | null>(localStorage.getItem(`${SESSION_PREFIX}${key}`), null);

export const deleteSession = (key: string): void => {
  localStorage.removeItem(`${SESSION_PREFIX}${key}`);
  writeIndex(readIndex().filter(s => s.key !== key));
};

export const clearSessions = (): void => {
  readIndex().forEach(s => localStorage.removeItem(`${SESSION_PREFIX}${s.key}`));
  localStorage.removeItem(INDEX_KEY);
};

/** Last campaign URL typed (the input keeps its value across reloads). */
export const readLastUrl = (): string => localStorage.getItem(URL_KEY) || '';
export const writeLastUrl = (url: string): void => {
  try {
    localStorage.setItem(URL_KEY, url);
  } catch {
    /* non-fatal */
  }
};

/** SRT body for one window, or '' when there is nothing to write. */
export const sessionSrt = (t: WindowTranscript | undefined): string =>
  t && t.source !== 'none' ? (t.srt || '') : '';
