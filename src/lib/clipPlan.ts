// Clip-plan client — where the campaign page's clip windows come from.
//
// Campaign prep used to have exactly one answer for a source without a campaign
// timestamp: mine the retention heatmap, then spread evenly when there is no
// telemetry. Two calls make that a CHOICE, and neither of them is an LLM:
//
//   * resolveClipPrompt — the editable parameter prompt + the current settings →
//     validated parameters, plain-language warnings and the exact windows they
//     would cut. The preview runs the SAME staging as the real prep (on the
//     server), so what the card shows can never disagree with the plan that
//     produces the clips.
//   * scrapeSources — a user-typed scrape command ("ytsearch10: <artist> live")
//     → real candidate videos, for the briefs that ship no source at all. The
//     command is parsed server-side and never executed as a shell command.

import type { CampaignSpec } from '../CampaignPage';

export interface ClipPlanParams {
  method?: string;
  target_duration?: number;
  min_duration?: number;
  max_duration?: number | null;
  per_source?: number;
  max_clips?: number;
  pad_before?: number;
  pad_after?: number;
  timestamp_source?: string;
  source_search?: string;
  source_count?: number;
  notes?: string;
}

export interface RequirementTimestamp {
  start: number;
  end: number;
  label: string;
  priority?: boolean;
  origin?: string;
  attributed_to?: string;
  attributed_by?: string;
  source_label?: string;
}

export interface ClipPlanSourceState {
  video_id: string;
  label: string;
  mode: string | null;
  clip_count: number;
  duration?: number | null;
  heatmap_points?: number;
  requirement_hits?: number;
  manual_hits?: number;
  note?: string | null;
}

export interface PreviewWindow {
  video_id: string;
  start: number;
  end: number;
  section_label?: string;
  evidence?: string;
  priority?: boolean;
}

export interface ClipPlanPreview {
  method: string;
  params: ClipPlanParams;
  warnings: string[];
  plan_warnings: string[];
  template: string;
  summary: string[];
  sources: ClipPlanSourceState[];
  items: PreviewWindow[];
  total_windows: number;
  requirement_timestamps: RequirementTimestamp[];
  manual_timestamps: RequirementTimestamp[];
  search: { query?: string; limit?: number; error?: string | null };
}

export interface ScrapeCandidate {
  video_id: string;
  title: string;
  url: string;
  channel?: string;
  duration?: number | null;
  duration_label?: string;
  thumbnail?: string;
}

export interface ScrapeResult {
  command: string;
  query: string;
  limit: number;
  candidates: ScrapeCandidate[];
  count: number;
  note: string;
}

const postJson = async <T,>(path: string, body: unknown): Promise<T> => {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new Error(data?.detail || `Server error ${resp.status}`);
  return data as T;
};

/** What the prompt + current settings would cut — no LLM, no YouTube fetch. */
export const resolveClipPrompt = (body: {
  spec: CampaignSpec;
  prompt: string;
  source_urls?: string[];
  target_duration: number;
  per_source: number;
  max_clips: number;
  language: string;
}): Promise<ClipPlanPreview> => postJson<ClipPlanPreview>('/api/campaign/clip-prompt', body);

/** Run a scrape command and get REAL candidate videos back. */
export const scrapeSources = (body: {
  command: string;
  subject?: string;
  spec?: CampaignSpec | null;
  limit?: number;
}): Promise<ScrapeResult> => postJson<ScrapeResult>('/api/campaign/sources/scrape', body);

/** The default block the card starts from, when the brief carries none. */
export const DEFAULT_CLIP_PROMPT = [
  '# HeatCut clip plan — edit the values, the planner uses them literally.',
  'method = requirement',
  'target = 30s',
  'per_source = 6',
  'timestamps =',
].join('\n');
