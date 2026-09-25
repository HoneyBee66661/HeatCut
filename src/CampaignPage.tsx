import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLanguage } from './locales';
import { fetchRawClip, requestRawClip, safeFilename, saveBlob, youtubeLink } from './lib/rawExport';
import { ExportFallbackPanel } from './components/ExportFallbackPanel';
import type { ExportFallbackTarget } from './components/ExportFallbackPanel';
import MaterialFinder from './components/MaterialFinder';
import type { MaterialPersist } from './components/MaterialFinder';
import { buildZip, zipEntry, type ZipEntry } from './lib/zipBundle';
import { fetchWindowTranscript, srtFilename, type WindowTranscript } from './lib/transcript';
import {
  clearSessions, deleteSession, readIndex, readLastUrl, readSession, writeLastUrl, writeSession,
  type PrepSession, type SessionSummary,
} from './lib/campaignStore';

// ---------------------------------------------------------------- types

export interface CampaignTimestamp {
  start: number;
  end: number;
  label: string;
  priority: boolean;
  note?: string;
}

export interface CampaignSource {
  index: number;
  url: string;
  original_url?: string;
  video_id: string;
  label: string;
  section?: string;
  priority: boolean;
  timestamps: CampaignTimestamp[];
}

export interface CampaignRequirements {
  platforms: string[];
  min_duration_sec: number | null;
  max_duration_sec: number | null;
  aspect_ratio: string | null;
  rules: string[];
  negative_rules: string[];
  hashtags: string[];
  mentions: string[];
}

export interface CampaignSpec {
  campaign_id: string;
  name: string;
  public_name: string;
  description?: string;
  status?: string;
  image_url?: string | null;
  niches?: string[];
  rate_per_100k?: number | null;
  budget?: number | null;
  minimum_views?: number | null;
  max_posts_per_user?: number | null;
  sources: CampaignSource[];
  requirements: CampaignRequirements;
  loose_bullets?: string[];
  warnings?: string[];
}

export interface PlanItem {
  index: number;
  id: string;
  video_id: string;
  source_url: string;
  source_label: string;
  section_label?: string;
  priority: boolean;
  start: number;
  end: number;
  duration: number;
  timestamp: string;
  evidence: string;
  heat?: number | null;
  score?: number | null;
  title: string;
  caption: string;
  hashtags: string;
  reason: string;
  /** Deep link to the window's start second — the manual-fallback deliverable. */
  youtube_url?: string;
}

export interface PrepPlan {
  items: PlanItem[];
  warnings: string[];
  sources: { label: string; mode: string | null; clip_count: number; duration: number | null; heatmap_points?: number; note?: string | null }[];
  target_duration: number;
  min_duration: number;
  copy_note?: string;
  brief_md: string;
  campaign_id?: string;
  campaign_name?: string;
}

export interface StrategyIdea {
  id: string;
  angle: string;
  angle_key: string;
  topic: string;
  platform: string;
  aspect_ratio: string;
  duration_sec: number;
  hook: string;
  title: string;
  title_alt: string;
  caption: string;
  hashtags: string[];
  why_it_works: string;
  retention_device: string;
  cta: string;
  shot_list: string[];
  text_overlay: string[];
}

export interface StrategyMarketing {
  positioning?: { brand?: string; audience?: string; niches?: string[]; angle_mix?: string[]; tone?: string };
  hook_window_sec?: number;
  retention_target_pct?: number;
  loopability?: string;
  caption_rules?: string[];
  hashtag_mix?: { branded?: string[]; niche?: string[]; broad?: string[]; rule?: string };
  posting?: {
    per_account_limit?: number | null;
    cadence?: string;
    best_windows?: Record<string, string[]>;
    batch_rule?: string;
  };
  sound?: string;
  cover_frame?: string;
  text_overlay?: string;
  kpi?: Record<string, string | number | null>;
  ab_test?: Record<string, string | number>;
  optimization_loop?: string[];
  asset_guidance?: string[];
  llm_tactics?: string[];
}

export interface CreativeStrategy {
  version: number;
  source: string;
  model?: string;
  campaign_id?: string;
  campaign_name?: string;
  has_timed_sources: boolean;
  topics: string[];
  prompt: string;
  language: string;
  ideas: StrategyIdea[];
  marketing: StrategyMarketing;
  compliance: { rules: string[]; do_not: string[]; checklist: string[]; prompt_conflicts: string[] };
  notes: string[];
}

export interface StrategyResponse {
  campaign_id?: string;
  strategy: CreativeStrategy;
  strategy_md: string;
  copy_note?: string;
  model: string;
}

interface CampaignPageProps {
  apiKey: string;
  provider: string;
  model: string;
  baseUrl: string;
  onToast: (message: string | null) => void;
  /** Jump back to the Studio with a URL pre-filled (material → analyze hand-off). */
  onSendToStudio?: (url: string) => void;
}

/** One archive is one download — cap it so the tab never has to hold a huge pack. */
const ZIP_MAX_BYTES = 1.5 * 1024 * 1024 * 1024;

/**
 * Clock read behind a module-scope helper: the React Compiler's purity rule
 * flags a bare `Date.now()` anywhere reachable from render, and this component
 * legitimately stamps sessions and cache ages on user actions.
 */
const nowMs = (): number => Date.now();

const fmt = (sec: number): string => {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 3600) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

export default function CampaignPage({ apiKey, provider, model, baseUrl, onToast, onSendToStudio }: CampaignPageProps) {
  const { t, language } = useLanguage();
  const c = t.campaign;

  const [url, setUrl] = useState(readLastUrl);
  const [spec, setSpec] = useState<CampaignSpec | null>(null);
  const [plan, setPlan] = useState<PrepPlan | null>(null);
  const [parsing, setParsing] = useState(false);
  const [prepping, setPrepping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Saved prep sessions (the studio's history feature, campaign-shaped).
  const [sessions, setSessions] = useState<SessionSummary[]>(readIndex);
  const [showHistory, setShowHistory] = useState(true);
  const [historyQuery, setHistoryQuery] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  // Caption text + SRT per window, plus what is being transcribed right now.
  const [transcripts, setTranscripts] = useState<Record<string, WindowTranscript>>({});
  const [captionsBusy, setCaptionsBusy] = useState<string | null>(null);
  const [bulkCaptions, setBulkCaptions] = useState<{ done: number; total: number } | null>(null);
  /** item id → epoch ms of the last successful export (session bookkeeping). */
  const [exported, setExported] = useState<Record<string, number>>({});
  /** item id → caption body expanded (long transcripts start collapsed). */
  const [openCaptions, setOpenCaptions] = useState<Record<string, boolean>>({});
  const sessionAt = useRef<number>(0);

  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [targetDuration, setTargetDuration] = useState<15 | 30 | 60>(30);
  const [perSource, setPerSource] = useState(4);
  const [maxTotal, setMaxTotal] = useState(15);
  const [aiCopy, setAiCopy] = useState(false);
  // Creative consultant (the brief is the data source, especially when the
  // campaign ships no timestamped videos to cut).
  const [strategyPrompt, setStrategyPrompt] = useState('');
  const [strategyTone, setStrategyTone] = useState('auto');
  const [strategyAudience, setStrategyAudience] = useState('');
  const [strategyIdeaCount, setStrategyIdeaCount] = useState(10);
  const [strategy, setStrategy] = useState<CreativeStrategy | null>(null);
  const [strategyMd, setStrategyMd] = useState('');
  const [strategyBusy, setStrategyBusy] = useState(false);
  // Simple vs advanced in the consultant: the average user gets the guided
  // material finder; the marketing board (KPI/A-B/cadence) hides behind a toggle.
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [materialState, setMaterialState] = useState<MaterialPersist | null>(null);
  /** Bumped when a saved session is loaded so the finder remounts with its data. */
  const [materialKey, setMaterialKey] = useState(0);
  const [exportingKey, setExportingKey] = useState<string | null>(null);
  const [bulk, setBulk] = useState<{ done: number; total: number } | null>(null);
  const [bulkZip, setBulkZip] = useState(false);
  // item id -> why the automatic download failed. Non-empty = that window is
  // handed over as a MANUAL cut (labeled jump link to the source second).
  const [manual, setManual] = useState<Record<string, string>>({});
  // Window whose automatic export YouTube refused → the choice panel is open.
  const [fallback, setFallback] = useState<ExportFallbackTarget | null>(null);

  const toast = useCallback((msg: string | null, ms = 3500) => {
    onToast(msg);
    if (msg) setTimeout(() => onToast(null), ms);
  }, [onToast]);

  const minDuration = spec?.requirements?.min_duration_sec || 15;

  const selectedUrls = useMemo(
    () => (spec?.sources || []).filter(s => selected[s.video_id]).map(s => s.url),
    [spec, selected],
  );

  const groupedItems = useMemo(() => {
    if (!plan) return [];
    const order: string[] = [];
    const map: Record<string, { label: string; url: string; items: PlanItem[] }> = {};
    plan.items.forEach(it => {
      if (!map[it.video_id]) {
        map[it.video_id] = { label: it.source_label, url: it.source_url, items: [] };
        order.push(it.video_id);
      }
      map[it.video_id].items.push(it);
    });
    return order.map(id => ({ videoId: id, ...map[id] }));
  }, [plan]);

  // ---------------------------------------------------------------- actions

  const handleParse = async () => {
    if (!url.trim() || parsing) return;
    setParsing(true);
    setError(null);
    setPlan(null);
    try {
      const resp = await fetch('/api/campaign/parse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url.trim() }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || `Server error ${resp.status}`);
      const parsed = data as CampaignSpec;
      setSpec(parsed);
      const sel: Record<string, boolean> = {};
      (parsed.sources || []).forEach(s => { sel[s.video_id] = true; });
      setSelected(sel);
      setMaxTotal(parsed.max_posts_per_user || 15);
      const minDur = parsed.requirements?.min_duration_sec || 15;
      setTargetDuration(minDur > 30 ? 60 : minDur > 15 ? 30 : 15);
      writeLastUrl(url.trim());
      // A fresh parse of this campaign starts a FRESH session timestamp; the
      // stored session itself is written by the auto-save effect below.
      sessionAt.current = nowMs();
      setExported({});
      setTranscripts({});
      setManual({});
      setPlan(null);
      // The board belongs to the campaign it was built from.
      setStrategy(null);
      setStrategyMd('');
      if (!(parsed.sources || []).length) {
        toast(parsed.warnings?.[0] || c.noClips, 6000);
      } else {
        toast(c.parsed);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setParsing(false);
    }
  };

  const handlePrep = async () => {
    if (!spec || prepping) return;
    if (!selectedUrls.length) {
      toast('Pick at least one source', 4000);
      return;
    }
    setPrepping(true);
    setError(null);
    try {
      const resp = await fetch('/api/campaign/prep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          spec,
          source_urls: selectedUrls,
          target_duration: targetDuration,
          per_source: perSource,
          max_clips: maxTotal,
          allow_network: true,
          generate_copy: aiCopy,
          api_key: apiKey.trim() || undefined,
          provider: aiCopy ? provider : undefined,
          model: aiCopy ? model : undefined,
          base_url: aiCopy && provider === 'openai-compatible' ? baseUrl.trim() || undefined : undefined,
        }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || `Server error ${resp.status}`);
      if (data?.detail) throw new Error(data.detail);
      const prepared = data as PrepPlan;
      setPlan(prepared);
      if (prepared.copy_note) toast(prepared.copy_note, 6000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPrepping(false);
    }
  };

  // ------------------------------------------------------ creative consultant

  /** Does the brief give us anything to actually cut? */
  const timedSources = useMemo(
    () => (spec?.sources || []).some(s => (s.timestamps || []).length > 0),
    [spec],
  );

  /**
   * Turn the BRIEF into a creative board: angles, per-idea hook/title(A/B)/
   * caption/hashtags, the digital-marketing levers to optimize and a compliance
   * checklist. Deterministic server-side; the optional AI polish reuses the same
   * AI settings as the copy pass.
   */
  const handleStrategy = async () => {
    if (!spec || strategyBusy) return;
    setStrategyBusy(true);
    setError(null);
    try {
      const resp = await fetch('/api/campaign/strategy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          spec,
          prompt: strategyPrompt.trim() || undefined,
          language,
          tone: strategyTone,
          audience: strategyAudience.trim() || undefined,
          idea_count: strategyIdeaCount,
          generate_copy: aiCopy,
          api_key: aiCopy ? apiKey.trim() || undefined : undefined,
          provider: aiCopy ? provider : undefined,
          model: aiCopy ? model : undefined,
          base_url: aiCopy && provider === 'openai-compatible' ? baseUrl.trim() || undefined : undefined,
        }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || `Server error ${resp.status}`);
      const parsed = data as StrategyResponse;
      setStrategy(parsed.strategy);
      setStrategyMd(parsed.strategy_md || '');
      if (parsed.copy_note) toast(parsed.copy_note, 6000);
      else toast(c.strategyReady((parsed.strategy?.ideas || []).length), 4000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStrategyBusy(false);
    }
  };

  /** One idea as plain text — the clipboard hand-off the editor pastes into CapCut. */
  const ideaText = (idea: StrategyIdea): string => [
    `#${idea.id.replace('idea-', '')} · ${idea.angle} · ${idea.platform} · ~${idea.duration_sec}s ${idea.aspect_ratio}`,
    `${c.strategyHookLabel}: ${idea.hook}`,
    `${c.strategyTitleALabel}: ${idea.title}`,
    `${c.strategyTitleBLabel}: ${idea.title_alt}`,
    `${c.strategyCaptionLabel}: ${idea.caption}`,
    `${c.strategyHashtagLabel}: ${idea.hashtags.join(' ')}`,
    `${c.strategyWhyLabel}: ${idea.why_it_works}`,
  ].join('\n');

  const copyIdeas = () => {
    const text = (strategy?.ideas || []).map(ideaText).join('\n\n');
    if (text) copyText(text, c.strategyIdeasTitle);
    else toast(c.strategyEmpty, 4000);
  };

  const copyStrategyMd = () => {
    if (strategyMd) copyText(strategyMd, c.strategyTitle);
    else toast(c.strategyEmpty, 4000);
  };

  const downloadStrategyMd = () => {
    if (!strategyMd) {
      toast(c.strategyEmpty, 4000);
      return;
    }
    saveBlob(new Blob([strategyMd], { type: 'text/markdown' }),
             `heatcut_campaign_${spec?.campaign_id || 'strategy'}_strategy.md`);
  };

  const markManual = (key: string, message: string) =>
    setManual(prev => ({ ...prev, [key]: message }));

  const clearManual = (key: string) =>
    setManual(prev => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });

  // ------------------------------------------------------------- sessions
  // The studio rebuilds its history by scanning `cheat_clip_cache_*`; the
  // campaign page stores one session per campaign (see lib/campaignStore) and
  // reloads the WHOLE session — windows, downloads, captions and all.

  const refreshSessions = useCallback(() => setSessions(readIndex()), []);

  const currentSession = useCallback((): PrepSession | null => {
    if (!spec) return null;
    return {
      key: spec.campaign_id,
      label: spec.public_name || spec.name || spec.campaign_id,
      url: url.trim(),
      at: sessionAt.current || nowMs(),
      updated_at: nowMs(),
      spec, plan, selected,
      target_duration: targetDuration,
      per_source: perSource,
      max_clips: maxTotal,
      ai_copy: aiCopy,
      manual, exported, transcripts,
      strategy, strategy_md: strategyMd,
      strategy_prompt: strategyPrompt, strategy_tone: strategyTone,
      strategy_audience: strategyAudience, strategy_ideas: strategyIdeaCount,
      material: materialState?.material || null,
      material_md: materialState?.material_md || '',
      material_subject: materialState?.subject || '',
      material_artist: materialState?.artist || '',
      material_lyrics: materialState?.lyrics || '',
      material_vibe: materialState?.vibe || 'auto',
      material_style: materialState?.style || 'auto',
    };
  }, [spec, plan, selected, url, targetDuration, perSource, maxTotal, aiCopy, manual, exported, transcripts,
      strategy, strategyMd, strategyPrompt, strategyTone, strategyAudience, strategyIdeaCount, materialState]);

  // Debounced auto-save: a new plan, a finished download or a fresh caption
  // lands in localStorage within a second of the UI going idle.
  useEffect(() => {
    const session = currentSession();
    if (!session) return;
    const id = setTimeout(() => {
      const ok = writeSession(session);
      setSavedAt(ok ? session.updated_at : 0);
      if (ok) refreshSessions();
    }, 700);
    return () => clearTimeout(id);
  }, [currentSession, refreshSessions]);

  const loadSessionEntry = (key: string) => {
    const session = readSession(key);
    if (!session) {
      refreshSessions();
      toast(c.sessionMissing, 6000);
      return;
    }
    sessionAt.current = session.at || nowMs();
    setUrl(session.url || '');
    setSpec(session.spec || null);
    setPlan(session.plan || null);
    setSelected(session.selected || {});
    setTargetDuration((session.target_duration || 30) as 15 | 30 | 60);
    setPerSource(session.per_source || 4);
    setMaxTotal(session.max_clips || 15);
    setAiCopy(!!session.ai_copy);
    setManual(session.manual || {});
    setExported(session.exported || {});
    setTranscripts(session.transcripts || {});
    setStrategy(session.strategy || null);
    setStrategyMd(session.strategy_md || '');
    setStrategyPrompt(session.strategy_prompt || '');
    setStrategyTone(session.strategy_tone || 'auto');
    setStrategyAudience(session.strategy_audience || '');
    setStrategyIdeaCount(session.strategy_ideas || 10);
    setMaterialState(session.material || session.material_md ? {
      material: session.material || null,
      material_md: session.material_md || '',
      subject: session.material_subject || '',
      artist: session.material_artist || '',
      lyrics: session.material_lyrics || '',
      vibe: session.material_vibe || 'auto',
      style: session.material_style || 'auto',
    } : null);
    setMaterialKey(k => k + 1);
    setFallback(null);
    setError(null);
    toast(c.sessionLoaded(session.label), 6000);
  };

  const removeSessionEntry = (key: string, label: string) => {
    deleteSession(key);
    refreshSessions();
    toast(c.sessionRemoved(label), 4000);
  };

  const clearAllSessions = () => {
    if (!confirmClear) {
      setConfirmClear(true);
      setTimeout(() => setConfirmClear(false), 4000);
      return;
    }
    clearSessions();
    refreshSessions();
    setConfirmClear(false);
    toast(c.sessionCleared, 4000);
  };

  const filteredSessions = useMemo(() => {
    const q = historyQuery.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(s => `${s.label} ${s.key}`.toLowerCase().includes(q));
  }, [sessions, historyQuery]);

  const relativeTime = useCallback((ts: number): string => {
    const mins = Math.floor((nowMs() - (ts || 0)) / 60000);
    if (mins < 1) return t.relativeTime.justNow;
    if (mins < 60) return t.relativeTime.minsAgo(mins);
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return t.relativeTime.hrsAgo(hrs);
    return t.relativeTime.daysAgo(Math.floor(hrs / 24));
  }, [t]);

  const downloadItem = async (item: PlanItem) => {
    const key = item.id;
    if (exportingKey) return;
    setExportingKey(key);
    try {
      // mode 'auto' = cheapest partial route on the backend; when YouTube
      // refuses BOTH partial routes the answer is a 409 and we show the user
      // their choice instead of failing the row outright.
      const result = await requestRawClip(item.video_id, item.start, item.end, item.source_label, { mode: 'auto' });
      if (result.kind === 'denied') {
        clearManual(key);
        setFallback({
          videoId: item.video_id,
          startTime: item.start,
          endTime: item.end,
          title: item.source_label,
          denied: result.denied,
        });
        return;
      }
      saveBlob(result.blob, `heatcut_${safeFilename(item.source_label)}_${Math.floor(item.start)}-${Math.floor(item.end)}s.mp4`);
      clearManual(key);
      setExported(prev => ({ ...prev, [key]: nowMs() }));
      toast(null);
    } catch (err) {
      // Not a dead end: the window stays in the pack as a MANUAL cut with a
      // labeled jump link to the exact second.
      const message = err instanceof Error ? err.message : c.exportFailed;
      markManual(key, message);
      toast(c.manualFallback(message), 6000);
    } finally {
      setExportingKey(null);
    }
  };

  const downloadAll = async () => {
    if (!plan || bulk || exportingKey) return;
    const items = plan.items;
    setBulk({ done: 0, total: items.length });
    let ok = 0;
    let failed = 0;
    for (let i = 0; i < items.length; i += 1) {
      setBulk({ done: i, total: items.length });
      try {
        const blob = await fetchRawClip(items[i].video_id, items[i].start, items[i].end, items[i].source_label);
        saveBlob(blob, `heatcut_${safeFilename(items[i].source_label)}_${Math.floor(items[i].start)}-${Math.floor(items[i].end)}s.mp4`);
        clearManual(items[i].id);
        setExported(prev => ({ ...prev, [items[i].id]: nowMs() }));
        ok += 1;
      } catch (err) {
        // Keep going: one blocked source must not cancel the rest of the pack.
        markManual(items[i].id, err instanceof Error ? err.message : c.exportFailed);
        failed += 1;
      }
      // Give the browser a beat between downloads so it does not drop them.
      await new Promise(r => setTimeout(r, 600));
    }
    setBulk({ done: items.length, total: items.length });
    toast(c.downloadAllDone(ok, failed), 7000);
    setTimeout(() => setBulk(null), 1200);
  };

  const copyText = (text: string, label: string) => {
    navigator.clipboard.writeText(text).then(() => toast(c.copyDone(label)));
  };

  const copyItemCopy = (item: PlanItem) => {
    const text = [
      `${item.title}`,
      item.caption,
      item.hashtags,
      `--- ${item.source_label} ${item.timestamp} (${item.evidence})`,
    ].filter(Boolean).join('\n\n');
    copyText(text, c.copyCopied);
  };

  const copyTimestamps = () => {
    if (!plan) return;
    copyText(plan.items.map(i => `${fmt(i.start)} - ${fmt(i.end)}`).join('\n'), c.timestampsCopied);
  };

  const evidenceLabel = (key: string) =>
    key === 'campaign-timestamp' ? c.evidenceCampaign : key === 'retention-peak' ? c.evidencePeak : c.evidenceSpread;

  // ------------------------------------------------- manual fallback (403 etc.)

  /** Jump link for a window, from the brief when present, computed otherwise. */
  const itemLink = (item: PlanItem): string => item.youtube_url || youtubeLink(item.video_id, item.start);

  const manualItems = useMemo(
    () => (plan?.items || []).filter(i => manual[i.id]),
    [plan, manual],
  );

  /** Markdown hand-off for the windows YouTube would not hand over. */
  const manualListMd = (items: PlanItem[]): string => {
    if (!items.length) return '';
    const lines = [`## ${c.manualListTitle}`, '', c.manualHint, ''];
    items.forEach(i => {
      lines.push(`- **${i.timestamp}** · ${i.source_label}${i.section_label ? ` · ${i.section_label}` : ''}`);
      lines.push(`  - ${i.title}`);
      lines.push(`  - Jump: ${itemLink(i)}`);
    });
    lines.push('');
    return lines.join('\n');
  };

  const copyManualList = () => copyText(manualListMd(manualItems.length ? manualItems : plan?.items || []), c.manualListCopied);

  const downloadManualList = () => {
    const md = manualListMd(manualItems.length ? manualItems : plan?.items || []);
    if (md) saveBlob(new Blob([md], { type: 'text/markdown' }), `heatcut_campaign_${plan?.campaign_id || 'manual'}_manual.md`);
  };

  // One archive = one download = no "allow multiple downloads" gate. Same fetch
  // path as the per-clip button, plus BRIEF.md / MANUAL_CUTS.md riding along.
  const downloadZipAll = async () => {
    if (!plan || bulk || exportingKey) return;
    const items = plan.items;
    const entries: ZipEntry[] = [];
    const blocked: PlanItem[] = [];
    let bytes = 0;
    setBulk({ done: 0, total: items.length });
    setBulkZip(true);
    // `for … of entries()` instead of an index loop: the React Compiler flags a
    // mutated `let i` inside this async body (react-hooks/immutability).
    for (const [i, item] of items.entries()) {
      setBulk({ done: i, total: items.length });
      try {
        const blob = await fetchRawClip(item.video_id, item.start, item.end, item.source_label);
        const entry = await zipEntry(
          `heatcut_${safeFilename(item.source_label)}_${Math.floor(item.start)}-${Math.floor(item.end)}s.mp4`,
          blob,
        );
        bytes += entry.size;
        if (bytes > ZIP_MAX_BYTES) {
          setBulk(null);
          setBulkZip(false);
          toast(c.zipTooBig, 8000);
          return;
        }
        entries.push(entry);
        clearManual(item.id);
        setExported(prev => ({ ...prev, [item.id]: nowMs() }));
      } catch (err) {
        markManual(item.id, err instanceof Error ? err.message : c.exportFailed);
        blocked.push(item);
      }
      await new Promise(r => setTimeout(r, 120));
    }
    if (!entries.length) {
      setBulk(null);
      setBulkZip(false);
      toast(c.zipEmpty, 8000);
      return;
    }
    entries.push(await zipEntry('BRIEF.md', new Blob([plan.brief_md], { type: 'text/markdown' })));
    if (blocked.length) {
      entries.push(await zipEntry('MANUAL_CUTS.md', new Blob([manualListMd(blocked)], { type: 'text/markdown' })));
    }
    // Captions ride along: one .srt per clip that has one, plus CAPTIONS.md.
    let srtCount = 0;
    for (const item of items) {
      const srt = transcripts[item.id]?.srt;
      if (!srt) continue;
      entries.push(await zipEntry(`heatcut_${srtFilename(item.source_label, item.start, item.end)}`,
                                  new Blob([srt], { type: 'application/x-subrip' })));
      srtCount += 1;
    }
    const captions = captionsMd(items);
    if (captions) {
      entries.push(await zipEntry('CAPTIONS.md', new Blob([captions], { type: 'text/markdown' })));
    }
    // The creative board rides along when the user built one.
    if (strategyMd) {
      entries.push(await zipEntry('STRATEGY.md', new Blob([strategyMd], { type: 'text/markdown' })));
    }
    // ...and so does the material plan (beats, links, steps).
    const materialMd = materialState?.material_md || '';
    if (materialMd) {
      entries.push(await zipEntry('MATERIAL.md', new Blob([materialMd], { type: 'text/markdown' })));
    }
    saveBlob(buildZip(entries), `heatcut_campaign_${plan.campaign_id || 'pack'}_raw.zip`);
    setBulk(null);
    setBulkZip(false);
    toast(c.zipDone(entries.filter(e => e.name.endsWith('.mp4')).length, blocked.length, srtCount), 9000);
  };

  /**
   * Source-less briefs have no clips to fetch, so `downloadZipAll` can never run
   * for them. The board is still a deliverable: one archive with STRATEGY.md (+
   * BRIEF.md once a plan exists) — still ONE download, no permission gate.
   */
  const downloadStrategyPack = async () => {
    if ((!strategyMd && !materialState?.material_md) || bulkZip) return;
    setBulkZip(true);
    try {
      const entries: ZipEntry[] = [];
      if (strategyMd) {
        entries.push(await zipEntry('STRATEGY.md', new Blob([strategyMd], { type: 'text/markdown' })));
      }
      if (materialState?.material_md) {
        entries.push(await zipEntry('MATERIAL.md', new Blob([materialState.material_md], { type: 'text/markdown' })));
      }
      if (plan?.brief_md) {
        entries.push(await zipEntry('BRIEF.md', new Blob([plan.brief_md], { type: 'text/markdown' })));
      }
      if (strategy) {
        const ideas = strategy.ideas.map(ideaText).join('\n\n');
        entries.push(await zipEntry('IDEAS.txt', new Blob([ideas], { type: 'text/plain' })));
      }
      saveBlob(buildZip(entries), `heatcut_campaign_${spec?.campaign_id || 'strategy'}_strategy.zip`);
      toast(c.strategyZipDone(entries.length), 8000);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBulkZip(false);
    }
  };

  // ------------------------------------------------- captions (text + SRT)
  // One window at a time: the caption ladder is cheap (server-side scraped
  // captions) but the Whisper fallback is 10-60 s of CPU on one core, so a bulk
  // run is sequential and reports progress instead of firing N requests.

  const captionOne = async (item: PlanItem): Promise<WindowTranscript | null> => {
    if (captionsBusy) return null;
    setCaptionsBusy(item.id);
    try {
      const result = await fetchWindowTranscript(item.video_id, item.start, item.end);
      setTranscripts(prev => ({ ...prev, [item.id]: result }));
      if (result.source === 'none') {
        toast(c.captionNoneNote(result.note || ''), 7000);
      } else {
        toast(c.captionReady(result.source === 'whisper' ? c.captionSourceWhisper : c.captionSourceYt,
                            (result.lines || []).length), 5000);
      }
      return result;
    } catch (err) {
      toast(c.captionNoneNote(err instanceof Error ? err.message : String(err)), 7000);
      return null;
    } finally {
      setCaptionsBusy(null);
    }
  };

  const captionAll = async () => {
    if (!plan || bulkCaptions || captionsBusy) return;
    const items = plan.items;
    setBulkCaptions({ done: 0, total: items.length });
    let ok = 0;
    let none = 0;
    for (let i = 0; i < items.length; i += 1) {
      setBulkCaptions({ done: i, total: items.length });
      const result = await captionOne(items[i]);
      if (result && result.source !== 'none' && (result.lines || []).length) ok += 1;
      else none += 1;
    }
    setBulkCaptions({ done: items.length, total: items.length });
    toast(c.captionAllDone(ok, none), 9000);
    setTimeout(() => setBulkCaptions(null), 1500);
  };

  const downloadSrt = (item: PlanItem) => {
    const srt = transcripts[item.id]?.srt;
    if (!srt) return;
    saveBlob(new Blob([srt], { type: 'application/x-subrip' }),
             `heatcut_${srtFilename(item.source_label, item.start, item.end)}`);
  };

  const captionsCount = useMemo(
    () => (plan?.items || []).filter(i => (transcripts[i.id]?.lines || []).length).length,
    [plan, transcripts],
  );

  /** CAPTIONS.md — every window's words with its jump link, for the pack. */
  const captionsMd = (items: PlanItem[]): string => {
    const rows = items.filter(i => (transcripts[i.id]?.text || '').trim());
    if (!rows.length) return '';
    const lines = [`## ${c.captionMdTitle}`, '', c.captionMdHint, ''];
    rows.forEach(item => {
      const tr = transcripts[item.id];
      const engine = tr.source === 'whisper' ? c.captionSourceWhisper : c.captionSourceYt;
      lines.push(`### ${item.timestamp} · ${item.source_label}${item.section_label ? ` · ${item.section_label}` : ''}`);
      lines.push(`- ${item.title}`);
      lines.push(`- Engine: ${engine}${tr.model ? ` (${tr.model})` : ''}${tr.language ? ` · ${tr.language}` : ''}`);
      lines.push(`- Jump: ${itemLink(item)}`);
      lines.push('');
      lines.push(tr.text);
      lines.push('');
    });
    return lines.join('\n');
  };

  const copyCaptions = () => {
    const md = captionsMd(plan?.items || []);
    if (md) copyText(md, c.captionMdTitle);
    else toast(c.captionNoneYet, 4000);
  };

  const downloadCaptionsMd = () => {
    const md = captionsMd(plan?.items || []);
    if (md) saveBlob(new Blob([md], { type: 'text/markdown' }),
                     `heatcut_campaign_${plan?.campaign_id || 'captions'}_captions.md`);
  };

  // ---------------------------------------------------------------- render help

  const statChip = (label: string, value: string) => (
    <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 110 }}>
      <span style={{ fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{label}</span>
      <span style={{ fontSize: '0.95rem', fontWeight: 700, color: 'var(--text-primary)' }}>{value}</span>
    </div>
  );

  /** `hook_window_sec` → `Hook window sec` — keeps the marketing table readable
   *  without a locale key per metric the rules engine emits. */
  const kvLabel = (key: string): string =>
    key.replace(/_/g, ' ').replace(/\bpct\b/gi, '(%)').replace(/^./, (ch) => ch.toUpperCase());

  const kvRows = (obj?: Record<string, unknown>) =>
    Object.entries(obj || {})
      .filter(([, v]) => v !== null && v !== undefined && String(v).length > 0)
      .map(([k, v]) => (
        <div key={k} data-testid={`strategy-kv-${k}`} style={{ display: 'flex', gap: '0.6rem', fontSize: '0.76rem', color: 'var(--text-secondary)' }}>
          <span style={{ minWidth: 172, color: 'var(--text-muted)', fontWeight: 600 }}>{kvLabel(k)}</span>
          <span style={{ flex: 1 }}>{Array.isArray(v) ? (v as unknown[]).join(', ') : String(v)}</span>
        </div>
      ));

  const bulletList = (items?: string[], color?: string) => (
    <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.78rem', color: color || 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
      {(items || []).map((item, i) => <li key={i}>{item}</li>)}
    </ul>
  );

  const chipRow = (items?: string[], tone: 'meta' | 'high' = 'meta') => (
    <div style={{ display: 'flex', gap: '0.3rem', flexWrap: 'wrap' }}>
      {(items || []).map((item, i) => (
        <span key={i} className={`score-badge score-${tone}`} style={{ fontSize: '0.66rem' }}>{item}</span>
      ))}
    </div>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      {/* Hero / input */}
      <section className="glass-panel" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.75rem', flexWrap: 'wrap' }}>
          <h2 className="text-gradient" style={{ margin: 0, fontSize: '1.5rem' }}>🎯 {c.title}</h2>
          <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)', maxWidth: 760 }}>{c.subtitle}</p>
        </div>

        <form
          className="form-main-input-row"
          onSubmit={(e) => { e.preventDefault(); handleParse(); }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', flex: 1 }}>
            <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--text-secondary)' }}>{c.urlLabel}</label>
            <input
              type="text"
              className="form-input"
              placeholder={c.urlPlaceholder}
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={parsing}
            />
          </div>
          <button
            type="submit"
            className="glowing-btn"
            disabled={parsing || !url.trim()}
            style={{ height: 48, padding: '0 2rem' }}
          >
            {parsing ? c.parsing : `📄 ${c.parseBtn}`}
          </button>
        </form>
        <p style={{ margin: 0, fontSize: '0.75rem', color: 'var(--text-muted)' }}>{c.parseHint}</p>

        {sessions.length > 0 && (
          <div
            style={{
              display: 'flex', flexDirection: 'column', gap: '0.55rem', padding: '0.7rem 0.85rem',
              borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
              <span style={{ fontSize: '0.72rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-muted)' }}>
                🗂️ {c.sessionTitle}
              </span>
              <span className="score-badge score-meta" style={{ fontSize: '0.66rem' }}>{sessions.length}</span>
              <input
                className="form-input"
                style={{ width: 190, padding: '0.3rem 0.6rem', fontSize: '0.75rem' }}
                placeholder={c.historySearch}
                value={historyQuery}
                onChange={(e) => setHistoryQuery(e.target.value)}
              />
              <button
                type="button"
                onClick={() => setShowHistory(v => !v)}
                style={{ background: 'none', border: 'none', color: 'var(--accent)', fontSize: '0.72rem', cursor: 'pointer', fontWeight: 600 }}
              >
                {showHistory ? c.historyHide : c.historyShow}
              </button>
              <button
                type="button"
                onClick={clearAllSessions}
                style={{ background: 'none', border: 'none', color: confirmClear ? '#fca5a5' : 'var(--text-muted)', fontSize: '0.72rem', cursor: 'pointer', textDecoration: 'underline' }}
              >
                {confirmClear ? c.historyConfirmClear : c.historyClearAll}
              </button>
            </div>
            <p style={{ margin: 0, fontSize: '0.72rem', color: 'var(--text-muted)' }}>{c.historyHint}</p>

            {showHistory && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '0.5rem' }}>
                {filteredSessions.map(s => (
                  <div
                    key={s.key}
                    style={{
                      display: 'flex', gap: '0.6rem', padding: '0.6rem 0.7rem', borderRadius: 10,
                      background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)',
                    }}
                  >
                    {s.thumbnail
                      ? <img src={s.thumbnail} alt="" style={{ width: 62, height: 40, borderRadius: 8, objectFit: 'cover' }} />
                      : <div style={{ width: 62, height: 40, borderRadius: 8, background: 'rgba(255,255,255,0.05)' }} />}
                    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
                      <span style={{ fontSize: '0.82rem', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.label}>
                        {s.label}
                      </span>
                      <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                        {relativeTime(s.updated_at)} · {c.historyItems(s.items)} · {c.historyExported(s.exported)}
                        {s.captions > 0 ? ` · ${c.historyCaptions(s.captions)}` : ''}
                      </span>
                      <div style={{ display: 'flex', gap: '0.4rem', marginTop: 3 }}>
                        <button
                          type="button"
                          className="form-input"
                          style={{ width: 'auto', padding: '0.25rem 0.6rem', fontSize: '0.72rem', cursor: 'pointer', background: 'transparent' }}
                          onClick={() => loadSessionEntry(s.key)}
                          title={c.sessionHint}
                        >
                          ↺ {c.historyLoad}
                        </button>
                        <button
                          type="button"
                          onClick={() => removeSessionEntry(s.key, s.label)}
                          style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.72rem', cursor: 'pointer', textDecoration: 'underline' }}
                        >
                          {c.historyDelete}
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
                {!filteredSessions.length && (
                  <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>{c.historyNoMatch}</span>
                )}
              </div>
            )}
          </div>
        )}

        {error && (
          <div style={{ padding: '0.7rem 0.9rem', borderRadius: 10, background: 'rgba(239, 68, 68, 0.08)', border: '1px solid rgba(239, 68, 68, 0.3)', color: '#fca5a5', fontSize: '0.85rem' }}>
            {error}
          </div>
        )}
      </section>

      {/* Campaign brief */}
      {spec && (
        <section className="glass-panel" style={{ display: 'flex', flexDirection: 'column', gap: '1.1rem' }}>
          <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
            {spec.image_url && (
              <img src={spec.image_url} alt="" style={{ width: 96, height: 96, borderRadius: 12, objectFit: 'cover', border: '1px solid rgba(255,255,255,0.08)' }} />
            )}
            <div style={{ flex: 1, minWidth: 260 }}>
              <h3 style={{ margin: '0 0 0.35rem', fontSize: '1.15rem' }}>{spec.public_name || spec.name}</h3>
              {spec.description && <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{spec.description}</p>}
              <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.5rem', flexWrap: 'wrap' }}>
                <span className="score-badge score-high" style={{ fontSize: '0.7rem' }}>{spec.status}</span>
                {(spec.niches || []).slice(0, 5).map(n => (
                  <span key={n} className="score-badge score-meta" style={{ fontSize: '0.7rem' }}>{n}</span>
                ))}
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', gap: '1.25rem', flexWrap: 'wrap', padding: '0.85rem', borderRadius: 12, background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)' }}>
            {spec.rate_per_100k != null && statChip(c.rate, `$${spec.rate_per_100k} ${c.perHundredK}`)}
            {spec.budget != null && statChip(c.budget, `$${spec.budget.toLocaleString()}`)}
            {spec.minimum_views != null && statChip(c.minViews, spec.minimum_views.toLocaleString())}
            {spec.max_posts_per_user != null && statChip(c.maxPosts, String(spec.max_posts_per_user))}
            {spec.requirements?.min_duration_sec != null && statChip(c.minClipLen, `${spec.requirements.min_duration_sec}s`)}
            {spec.requirements?.max_duration_sec != null && statChip(c.maxClipLen, `${spec.requirements.max_duration_sec}s`)}
            {!!spec.requirements?.platforms?.length && statChip(c.platforms, spec.requirements.platforms.join(' · '))}
            {spec.requirements?.aspect_ratio && statChip(c.aspect, spec.requirements.aspect_ratio)}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '0.9rem' }}>
            <div>
              <h4 style={{ margin: '0 0 0.4rem', fontSize: '0.78rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)' }}>{c.briefTitle}</h4>
              <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.82rem', color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
                {(spec.requirements?.rules || []).map((r, i) => <li key={i}>{r}</li>)}
                {(spec.requirements?.rules || []).length === 0 && <li style={{ listStyle: 'none', color: 'var(--text-muted)' }}>—</li>}
              </ul>
            </div>
            <div>
              {!!spec.requirements?.negative_rules?.length && (
                <>
                  <h4 style={{ margin: '0 0 0.4rem', fontSize: '0.78rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: '#fca5a5' }}>{c.doNotTitle}</h4>
                  <ul style={{ margin: '0 0 0.6rem', paddingLeft: '1.1rem', fontSize: '0.82rem', color: '#fca5a5', display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
                    {spec.requirements.negative_rules.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </>
              )}
              {!!(spec.loose_bullets || []).length && (
                <>
                  <h4 style={{ margin: '0 0 0.4rem', fontSize: '0.78rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)' }}>{c.notesTitle}</h4>
                  <ul style={{ margin: 0, paddingLeft: '1.1rem', fontSize: '0.8rem', color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                    {(spec.loose_bullets || []).slice(0, 8).map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </>
              )}
            </div>
          </div>
        </section>
      )}

      {/* Sources */}
      {spec && (
        <section className="glass-panel" style={{ display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
          <div>
            <h3 style={{ margin: 0, fontSize: '1.05rem' }}>🎬 {c.sourcesTitle}</h3>
            <p style={{ margin: '0.25rem 0 0', fontSize: '0.78rem', color: 'var(--text-muted)' }}>{c.sourcesHint}</p>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.55rem' }}>
            {(spec.sources || []).map(s => (
              <label
                key={s.video_id}
                style={{
                  display: 'flex', gap: '0.75rem', alignItems: 'flex-start', padding: '0.75rem 0.85rem',
                  borderRadius: 12, cursor: 'pointer',
                  background: selected[s.video_id] ? 'rgba(255, 94, 58, 0.06)' : 'rgba(255,255,255,0.02)',
                  border: `1px solid ${selected[s.video_id] ? 'rgba(255, 94, 58, 0.35)' : 'rgba(255,255,255,0.06)'}`,
                }}
              >
                <input
                  type="checkbox"
                  checked={!!selected[s.video_id]}
                  onChange={(e) => setSelected(prev => ({ ...prev, [s.video_id]: e.target.checked }))}
                  style={{ marginTop: 3, accentColor: 'var(--primary)' }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                    <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>{s.label}</span>
                    {s.priority && (
                      <span className="score-badge score-high" style={{ fontSize: '0.65rem' }}>⭐ {c.priorityBadge}</span>
                    )}
                    <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                      {s.timestamps.length ? c.sourceTimestamps(s.timestamps.length) : c.untimedSource}
                    </span>
                  </div>
                  <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap', marginTop: '0.4rem' }}>
                    {s.timestamps.map((ts, i) => (
                      <span
                        key={i}
                        className="score-badge score-meta"
                        style={{ fontSize: '0.68rem', color: ts.priority ? '#fbbf24' : undefined }}
                      >
                        {ts.label} · {fmt(ts.start)}-{fmt(ts.end)}{ts.priority ? ' ⭐' : ''}
                      </span>
                    ))}
                  </div>
                </div>
                <a
                  href={`https://www.youtube.com/watch?v=${s.video_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: '0.72rem', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}
                >
                  {c.openSource} ↗
                </a>
              </label>
            ))}
          </div>
        </section>
      )}

      {/* Creative consultant — when the brief has no cuttable sources, the brief
          itself is the data: angles, copy, marketing levers, compliance. */}
      {spec && (
        <section className="glass-panel" data-testid="strategy-card" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-start', flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 320px' }}>
              <h3 style={{ margin: 0, fontSize: '1.05rem' }}>🧠 {c.strategyTitle}</h3>
              <p style={{ margin: '0.25rem 0 0', fontSize: '0.78rem', color: 'var(--text-muted)' }}>{c.strategySubtitle}</p>
            </div>
            <button
              type="button"
              data-testid="strategy-advanced-toggle"
              onClick={() => setShowAdvanced(v => !v)}
              style={{ padding: '0.45rem 0.8rem', borderRadius: 10, cursor: 'pointer', fontSize: '0.76rem', fontWeight: 600, border: '1px solid rgba(255,255,255,0.14)', background: showAdvanced ? 'rgba(255,107,53,0.16)' : 'transparent', color: showAdvanced ? 'var(--primary)' : 'var(--text-secondary)' }}
            >
              {showAdvanced ? c.materialAdvancedOff : c.materialAdvancedOn}
            </button>
          </div>

          <MaterialFinder
            key={`material-${materialKey}`}
            c={c}
            language={language}
            spec={spec}
            initial={materialState}
            durationSec={targetDuration}
            aiReady={aiCopy && !!apiKey.trim()}
            apiKey={apiKey}
            provider={provider}
            model={model}
            baseUrl={baseUrl}
            onToast={onToast}
            onSendToStudio={onSendToStudio || (() => undefined)}
            onPersist={setMaterialState}
          />

          {showAdvanced ? (
          <>
          {!timedSources && (
            <div
              data-testid="strategy-no-sources"
              style={{ padding: '0.6rem 0.85rem', borderRadius: 10, fontSize: '0.8rem', background: 'rgba(255, 94, 58, 0.08)', border: '1px solid rgba(255, 94, 58, 0.3)', color: 'var(--secondary)' }}
            >
              ⚠️ {c.strategyNoSources}
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-secondary)' }}>{c.strategyPromptLabel}</label>
            <textarea
              data-testid="strategy-prompt"
              className="form-input"
              rows={3}
              style={{ resize: 'vertical', fontFamily: 'inherit' }}
              placeholder={c.strategyPromptPlaceholder}
              value={strategyPrompt}
              onChange={(e) => setStrategyPrompt(e.target.value)}
            />
          </div>

          <div style={{ display: 'flex', gap: '0.9rem', flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
              <label style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyToneLabel}</label>
              <select
                data-testid="strategy-tone"
                className="form-input"
                value={strategyTone}
                onChange={(e) => setStrategyTone(e.target.value)}
                style={{ padding: '0.4rem 0.6rem', fontSize: '0.82rem' }}
              >
                <option value="auto">{c.toneAuto}</option>
                <option value="punchy">{c.tonePunchy}</option>
                <option value="story">{c.toneStory}</option>
                <option value="educational">{c.toneEducational}</option>
                <option value="funny">{c.toneFunny}</option>
              </select>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem', flex: 1, minWidth: 220 }}>
              <label style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyAudienceLabel}</label>
              <input
                type="text"
                data-testid="strategy-audience"
                className="form-input"
                placeholder={c.strategyAudiencePlaceholder}
                value={strategyAudience}
                onChange={(e) => setStrategyAudience(e.target.value)}
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
              <label style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyIdeasLabel}</label>
              <input
                type="number"
                data-testid="strategy-idea-count"
                className="form-input"
                min={4}
                max={20}
                value={strategyIdeaCount}
                onChange={(e) => setStrategyIdeaCount(Math.max(4, Math.min(20, Number(e.target.value) || 10)))}
                style={{ width: 92 }}
              />
            </div>

            <button
              type="button"
              className="glowing-btn"
              data-testid="strategy-generate"
              onClick={handleStrategy}
              disabled={strategyBusy}
              style={{ height: 44, padding: '0 1.6rem' }}
            >
              {strategyBusy ? c.strategyGenerating : c.strategyGenerate}
            </button>
          </div>

          <p style={{ margin: 0, fontSize: '0.72rem', color: 'var(--text-muted)' }}>
            {c.strategyHint} {c.strategyAiNote}
          </p>

          {strategy && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
              <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'center' }}>
                <span className="score-badge score-high" style={{ fontSize: '0.68rem' }}>
                  {strategy.source === 'rules+llm' ? c.strategySourceLlm : c.strategySourceRules}
                </span>
                {strategy.model && <span className="score-badge score-meta" style={{ fontSize: '0.68rem' }}>{strategy.model}</span>}
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  {c.strategyAudienceOut}: {strategy.marketing?.positioning?.audience}
                </span>
              </div>

              {!!(strategy.topics || []).length && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
                  <span style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyTopics}</span>
                  {chipRow(strategy.topics)}
                </div>
              )}

              {!!(strategy.notes || []).length && (
                <div data-testid="strategy-notes" style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                  {strategy.notes.map((n, i) => <div key={i}>ℹ️ {n}</div>)}
                </div>
              )}

              <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
                <h4 style={{ margin: 0, fontSize: '0.9rem' }}>🎬 {c.strategyIdeasTitle} ({strategy.ideas.length})</h4>
                <button type="button" onClick={copyIdeas} style={{ background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.78rem' }}>
                  {c.strategyCopyIdeas}
                </button>
                <span style={{ color: 'var(--border-color)' }}>|</span>
                <button type="button" onClick={copyStrategyMd} style={{ background: 'none', border: 'none', color: 'var(--secondary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.78rem' }}>
                  {c.strategyCopyMd}
                </button>
                <span style={{ color: 'var(--border-color)' }}>|</span>
                <button type="button" data-testid="strategy-download-md" onClick={downloadStrategyMd} style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontWeight: 600, fontSize: '0.78rem' }}>
                  ⬇ {c.strategyDownloadMd}
                </button>
                <span style={{ color: 'var(--border-color)' }}>|</span>
                <button type="button" data-testid="strategy-download-zip" onClick={downloadStrategyPack} disabled={bulkZip} style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontWeight: 600, fontSize: '0.78rem' }}>
                  📦 {c.strategyZip}
                </button>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '0.8rem' }}>
                {strategy.ideas.map((idea) => (
                  <div
                    key={idea.id}
                    data-testid={`strategy-concept-${idea.id}`}
                    style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem', padding: '0.8rem 0.9rem', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
                  >
                    <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap', alignItems: 'center' }}>
                      <span className="score-badge score-high" style={{ fontSize: '0.66rem' }}>{idea.angle}</span>
                      <span className="score-badge score-meta" style={{ fontSize: '0.66rem' }}>{idea.platform}</span>
                      <span style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>~{idea.duration_sec}s · {idea.aspect_ratio}</span>
                    </div>
                    <div style={{ fontSize: '0.82rem', color: 'var(--text-primary)' }}>
                      <strong style={{ color: 'var(--secondary)' }}>{c.strategyHookLabel}:</strong> {idea.hook}
                    </div>
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                      <strong>{c.strategyTitleALabel}:</strong> {idea.title}
                    </div>
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>
                      <strong>{c.strategyTitleBLabel}:</strong> {idea.title_alt}
                    </div>
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{idea.caption}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--primary)' }}>{idea.hashtags.join(' ')}</div>
                    <div style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>💡 {idea.why_it_works}</div>
                    <div style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>⏱️ {idea.retention_device}</div>
                    <details>
                      <summary style={{ fontSize: '0.74rem', color: 'var(--text-muted)', cursor: 'pointer' }}>{c.strategyShotListLabel}</summary>
                      <div style={{ marginTop: '0.3rem' }}>{bulletList(idea.shot_list)}</div>
                    </details>
                    <button
                      type="button"
                      onClick={() => copyText(ideaText(idea), `${c.strategyTitle} ${idea.id}`)}
                      style={{ alignSelf: 'flex-start', background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.74rem' }}
                    >
                      📋 {c.strategyCopyIdea}
                    </button>
                  </div>
                ))}
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '0.9rem' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', padding: '0.85rem', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.82rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--secondary)' }}>📈 {c.strategyMarketingTitle}</h4>
                  {kvRows({
                    hook_window_sec: strategy.marketing?.hook_window_sec,
                    retention_target_pct: strategy.marketing?.retention_target_pct,
                    loopability: strategy.marketing?.loopability,
                    sound: strategy.marketing?.sound,
                    cover_frame: strategy.marketing?.cover_frame,
                    text_overlay: strategy.marketing?.text_overlay,
                  })}
                  <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyCaptionRules}</div>
                  {bulletList(strategy.marketing?.caption_rules)}
                  <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyHashtagMix}</div>
                  {kvRows({
                    branded: strategy.marketing?.hashtag_mix?.branded,
                    niche: strategy.marketing?.hashtag_mix?.niche,
                    broad: strategy.marketing?.hashtag_mix?.broad,
                    rule: strategy.marketing?.hashtag_mix?.rule,
                  })}
                  <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyPosting}</div>
                  {kvRows({
                    per_account_limit: strategy.marketing?.posting?.per_account_limit,
                    cadence: strategy.marketing?.posting?.cadence,
                    best_windows: strategy.marketing?.posting?.best_windows
                      ? Object.entries(strategy.marketing.posting.best_windows).map(([p, w]) => `${p}: ${(w || []).join(', ')}`)
                      : undefined,
                    batch_rule: strategy.marketing?.posting?.batch_rule,
                  })}
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', padding: '0.85rem', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.82rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--accent)' }}>🎯 {c.strategyKpi}</h4>
                  {kvRows(strategy.marketing?.kpi as Record<string, unknown>)}
                  <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyAbTest}</div>
                  {kvRows(strategy.marketing?.ab_test as Record<string, unknown>)}
                  <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyLoop}</div>
                  {bulletList(strategy.marketing?.optimization_loop)}
                  {!!(strategy.marketing?.llm_tactics || []).length && (
                    <>
                      <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyTactics}</div>
                      {bulletList(strategy.marketing?.llm_tactics)}
                    </>
                  )}
                  <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyAssets}</div>
                  {bulletList(strategy.marketing?.asset_guidance)}
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', padding: '0.85rem', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)' }}>
                  <h4 style={{ margin: 0, fontSize: '0.82rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--secondary)' }}>🛡️ {c.strategyComplianceTitle}</h4>
                  <div style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.strategyChecklist}</div>
                  {bulletList(strategy.compliance?.checklist)}
                  {!!(strategy.compliance?.do_not || []).length && (
                    <>
                      <div style={{ marginTop: '0.3rem', fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: '#fca5a5', fontWeight: 700 }}>{c.strategyDoNot}</div>
                      {bulletList(strategy.compliance.do_not, '#fca5a5')}
                    </>
                  )}
                  <div
                    data-testid="strategy-conflicts"
                    style={{ marginTop: '0.35rem', fontSize: '0.76rem', color: (strategy.compliance?.prompt_conflicts || []).length ? '#fca5a5' : 'var(--text-muted)' }}
                  >
                    {(strategy.compliance?.prompt_conflicts || []).length
                      ? <>⚠️ {c.strategyConflicts}: {strategy.compliance.prompt_conflicts.join(' · ')}</>
                      : <>✅ {c.strategyNoConflicts}</>}
                  </div>
                </div>
              </div>
            </div>
          )}
          </>
          ) : (
            <div data-testid="material-advanced-hint" style={{ fontSize: '0.76rem', color: 'var(--text-muted)', borderTop: '1px solid var(--border-color)', paddingTop: '0.75rem' }}>
              💡 {c.materialAdvancedHint}
            </div>
          )}
        </section>
      )}

      {/* Controls */}
      {spec && (
        <section className="glass-panel" style={{ display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
          <h3 style={{ margin: 0, fontSize: '1.05rem' }}>⚙️ {c.controlsTitle}</h3>

          <div style={{ display: 'flex', gap: '1.5rem', flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
              <span style={{ fontSize: '0.74rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-muted)' }}>{c.clipLength}</span>
              <div className="duration-selector">
                {([15, 30, 60] as const).map(d => (
                  <button
                    key={d}
                    type="button"
                    className={`duration-btn ${targetDuration === d ? 'active' : ''}`}
                    disabled={d < minDuration}
                    onClick={() => setTargetDuration(d)}
                    title={d < minDuration ? `${c.minClipLen}: ${minDuration}s` : undefined}
                    style={d < minDuration ? { opacity: 0.4, cursor: 'not-allowed' } : undefined}
                  >
                    {d}s
                  </button>
                ))}
              </div>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
              <span style={{ fontSize: '0.74rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-muted)' }}>{c.clipsPerSource}</span>
              <input
                type="number"
                min={1}
                max={30}
                className="form-input"
                style={{ width: 90 }}
                value={perSource}
                onChange={(e) => setPerSource(Math.max(1, Math.min(30, Number(e.target.value) || 1)))}
              />
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
              <span style={{ fontSize: '0.74rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-muted)' }}>{c.maxTotal}</span>
              <input
                type="number"
                min={1}
                max={60}
                className="form-input"
                style={{ width: 90 }}
                value={maxTotal}
                onChange={(e) => setMaxTotal(Math.max(1, Math.min(60, Number(e.target.value) || 1)))}
              />
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer', fontSize: '0.82rem' }}>
              <input type="checkbox" checked={aiCopy} onChange={(e) => setAiCopy(e.target.checked)} style={{ accentColor: 'var(--primary)' }} />
              <span>{c.aiCopy}</span>
              <span style={{ color: 'var(--text-muted)', fontSize: '0.74rem' }}>{aiCopy ? c.aiCopyOn : c.aiCopyOff}</span>
            </label>

            <button
              type="button"
              className="glowing-btn"
              disabled={prepping || !selectedUrls.length}
              onClick={handlePrep}
              style={{ height: 44, padding: '0 1.75rem', marginLeft: 'auto' }}
            >
              {prepping ? c.generating : `⚡ ${c.generate}`}
            </button>
          </div>
          {prepping && <p style={{ margin: 0, fontSize: '0.75rem', color: 'var(--text-muted)' }}>{c.generatingHint}</p>}
        </section>
      )}

      {/* Results */}
      {plan && (
        <section className="glass-panel" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', flexWrap: 'wrap' }}>
            <h3 style={{ margin: 0, fontSize: '1.05rem' }}>📦 {c.resultsTitle}</h3>
            <span className="score-badge score-meta" style={{ fontSize: '0.72rem' }}>
              {c.resultSummary(plan.items.length, groupedItems.length)}
            </span>
            {plan.copy_note && (
              <span style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>{c.copyNoteLabel}: {plan.copy_note}</span>
            )}
            {!!savedAt && (
              <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }} title={c.historyHint}>
                💾 {c.sessionSaved(relativeTime(savedAt))}
              </span>
            )}
            <div style={{ display: 'flex', gap: '0.4rem', marginLeft: 'auto', flexWrap: 'wrap' }}>
              <button type="button" className="form-input" style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent' }} onClick={copyTimestamps}>
                ⏱️ {c.copyTimestamps}
              </button>
              <button type="button" className="form-input" style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent' }} onClick={() => copyText(plan.brief_md, c.briefCopied)}>
                📋 {c.copyBrief}
              </button>
              <button
                type="button"
                className="form-input"
                style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent' }}
                onClick={() => saveBlob(new Blob([plan.brief_md], { type: 'text/markdown' }), `heatcut_campaign_${plan.campaign_id || 'brief'}.md`)}
              >
                📥 {c.downloadBrief}
              </button>
              <button
                type="button"
                className="form-input"
                style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent', opacity: captionsCount ? 1 : 0.5 }}
                onClick={copyCaptions}
                title={c.captionHint}
                disabled={!captionsCount}
              >
                💬 {c.captionCopyAll(captionsCount)}
              </button>
              <button
                type="button"
                className="form-input"
                style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent', opacity: captionsCount ? 1 : 0.5 }}
                onClick={downloadCaptionsMd}
                title={c.captionHint}
                disabled={!captionsCount}
              >
                📄 {c.captionDownloadMd}
              </button>
              <button
                type="button"
                className="glowing-btn"
                style={{ padding: '0.45rem 1rem', fontSize: '0.78rem' }}
                disabled={!plan.items.length || !!bulk || !!bulkCaptions || !!captionsBusy}
                onClick={captionAll}
                title={c.captionHint}
              >
                {bulkCaptions ? c.captionAllProgress(bulkCaptions.done, bulkCaptions.total) : `🎙️ ${c.captionForAll}`}
              </button>
              <button
                type="button"
                className="form-input"
                style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent' }}
                onClick={copyManualList}
                title={c.manualHint}
              >
                🔗 {manualItems.length ? c.manualCopyListCount(manualItems.length) : c.manualCopyList}
              </button>
              <button
                type="button"
                className="form-input"
                style={{ width: 'auto', padding: '0.45rem 0.85rem', fontSize: '0.78rem', cursor: 'pointer', background: 'transparent' }}
                onClick={downloadManualList}
                title={c.manualHint}
              >
                📄 {c.manualDownloadList}
              </button>
              <button
                type="button"
                className="glowing-btn"
                style={{ padding: '0.45rem 1rem', fontSize: '0.78rem' }}
                disabled={!plan.items.length || !!bulk || !!exportingKey}
                onClick={downloadAll}
                title={c.bulkHint}
              >
                {bulk && !bulkZip ? c.downloadingAll(bulk.done, bulk.total) : `⬇ ${c.downloadAll}`}
              </button>
              <button
                type="button"
                className="glowing-btn"
                style={{ padding: '0.45rem 1rem', fontSize: '0.78rem' }}
                disabled={!plan.items.length || !!bulk || !!exportingKey}
                onClick={downloadZipAll}
                title={c.bulkHint}
              >
                {bulk && bulkZip ? c.zippingAll(bulk.done, bulk.total) : `🧩 ${c.zipAll}`}
              </button>
            </div>
          </div>

          <p style={{ margin: 0, fontSize: '0.72rem', color: 'var(--text-muted)' }}>{c.bulkHint}</p>
          <p style={{ margin: 0, fontSize: '0.72rem', color: 'var(--text-muted)' }}>{c.captionHint}</p>

          {!!plan.warnings?.length && (
            <div style={{ padding: '0.65rem 0.85rem', borderRadius: 10, background: 'rgba(251, 191, 36, 0.07)', border: '1px solid rgba(251, 191, 36, 0.28)', fontSize: '0.8rem', color: '#fcd34d' }}>
              <strong>{c.warningsTitle}: </strong>
              {plan.warnings.join(' · ')}
            </div>
          )}

          {manualItems.length > 0 && (
            <div
              style={{ padding: '0.75rem 0.9rem', borderRadius: 10, background: 'rgba(251, 191, 36, 0.07)', border: '1px solid rgba(251, 191, 36, 0.32)', display: 'flex', flexDirection: 'column', gap: '0.55rem' }}
            >
              <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.5rem', flexWrap: 'wrap' }}>
                <strong style={{ fontSize: '0.88rem', color: '#fcd34d' }}>⚠️ {c.manualTitle(manualItems.length)}</strong>
                <span style={{ fontSize: '0.74rem', color: 'var(--text-muted)', maxWidth: 780 }}>{c.manualHint}</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
                {manualItems.map(item => (
                  <div key={item.id} style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap', fontSize: '0.78rem' }}>
                    <span style={{ fontFamily: 'monospace', fontWeight: 700 }}>{item.timestamp}</span>
                    <span>{item.source_label}</span>
                    <a href={itemLink(item)} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--primary)', fontWeight: 600 }}>
                      🔗 {c.openAtTime} ↗
                    </a>
                    <button
                      type="button"
                      onClick={() => copyText(itemLink(item), c.linkCopied)}
                      style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.74rem', cursor: 'pointer', textDecoration: 'underline' }}
                    >
                      {c.copyLink}
                    </button>
                    <span style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{manual[item.id].slice(0, 140)}</span>
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="form-input"
                  style={{ width: 'auto', padding: '0.4rem 0.8rem', fontSize: '0.75rem', cursor: 'pointer', background: 'transparent' }}
                  onClick={copyManualList}
                >
                  🔗 {c.manualCopyList}
                </button>
                <button
                  type="button"
                  className="form-input"
                  style={{ width: 'auto', padding: '0.4rem 0.8rem', fontSize: '0.75rem', cursor: 'pointer', background: 'transparent' }}
                  onClick={downloadManualList}
                >
                  📄 {c.manualDownloadList}
                </button>
              </div>
            </div>
          )}

          {!plan.items.length && <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{c.noClips}</p>}

          {groupedItems.map(group => (
            <div key={group.videoId} style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap' }}>
                <h4 style={{ margin: 0, fontSize: '0.92rem' }}>{group.label}</h4>
                <a href={group.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                  {c.openSource} ↗
                </a>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {group.items.map(item => (
                  <div key={item.id} className="clip-card" style={{ padding: '0.75rem 0.9rem' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap' }}>
                      <span className="step-circle" style={{ width: 22, height: 22, fontSize: '0.7rem' }}>{item.index + 1}</span>
                      <span style={{ fontFamily: 'monospace', fontSize: '0.85rem', fontWeight: 700 }}>{item.timestamp}</span>
                      <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{item.duration.toFixed(0)}s</span>
                      <span className={`score-badge ${item.evidence === 'retention-peak' ? 'score-high' : item.priority ? 'score-high' : 'score-meta'}`} style={{ fontSize: '0.65rem' }}>
                        {item.priority ? `⭐ ${c.priorityBadge} · ` : ''}{evidenceLabel(item.evidence)}
                      </span>
                      {item.heat != null && (
                        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>heat {item.heat}</span>
                      )}
                      {manual[item.id] && (
                        <span
                          className="score-badge"
                          style={{ fontSize: '0.65rem', background: 'rgba(251, 191, 36, 0.14)', color: '#fcd34d' }}
                          title={manual[item.id]}
                        >
                          ⚠️ {c.manualBadge}
                        </span>
                      )}
                      <div style={{ display: 'flex', gap: '0.4rem', marginLeft: 'auto' }}>
                        <a
                          href={itemLink(item)}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="form-input"
                          style={{ width: 'auto', padding: '0.4rem 0.75rem', fontSize: '0.75rem', textDecoration: 'none', textAlign: 'center' }}
                          title={itemLink(item)}
                        >
                          🔗 {c.openAtTime}
                        </a>
                        <button
                          type="button"
                          className="form-input"
                          style={{ width: 'auto', padding: '0.4rem 0.75rem', fontSize: '0.75rem', cursor: 'pointer', background: 'transparent' }}
                          onClick={() => copyItemCopy(item)}
                        >
                          📝 {c.copyCopy}
                        </button>
                        <button
                          type="button"
                          className="glowing-btn"
                          style={{ padding: '0.4rem 0.9rem', fontSize: '0.75rem' }}
                          disabled={!!exportingKey || !!bulk}
                          onClick={() => downloadItem(item)}
                        >
                          {exportingKey === item.id ? c.downloading : manual[item.id] ? `🔁 ${c.retryDownload}` : `⬇ ${c.downloadRaw}`}
                        </button>
                      </div>
                    </div>

                    <div style={{ marginTop: '0.5rem', fontSize: '0.82rem' }}>
                      <strong>{item.title}</strong>
                      {item.caption && <div style={{ color: 'var(--text-secondary)', marginTop: 2 }}>{item.caption}</div>}
                      {item.hashtags && <div style={{ color: 'var(--primary)', marginTop: 2, fontSize: '0.78rem' }}>{item.hashtags}</div>}
                      <div style={{ color: 'var(--text-muted)', fontSize: '0.72rem', marginTop: 4 }}>{c.whyLabel}: {item.reason}</div>
                      {manual[item.id] && (
                        <div style={{ color: '#fcd34d', fontSize: '0.72rem', marginTop: 4 }}>
                          {c.manualBadge}: {manual[item.id]} · <a href={itemLink(item)} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--primary)' }}>{c.openAtTime} ↗</a>
                        </div>
                      )}
                    </div>

                    {/* Caption text + SRT for this window */}
                    {(() => {
                      const tr = transcripts[item.id];
                      const busy = captionsBusy === item.id;
                      const hasWords = !!tr && tr.source !== 'none' && (tr.lines || []).length > 0;
                      const open = !!openCaptions[item.id];
                      const body = tr?.text || '';
                      const short = body.length > 260 && !open ? `${body.slice(0, 260)}…` : body;
                      return (
                        <div style={{ marginTop: '0.6rem', paddingTop: '0.55rem', borderTop: '1px dashed rgba(255,255,255,0.09)' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                            <span style={{ fontSize: '0.72rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)' }}>
                              💬 {c.captionSection}
                            </span>
                            {hasWords && (
                              <span
                                className={`score-badge ${tr.source === 'whisper' ? 'score-meta' : 'score-high'}`}
                                style={{ fontSize: '0.65rem', whiteSpace: 'nowrap' }}
                                title={tr.note || undefined}
                              >
                                {tr.source === 'whisper' ? `🎙️ ${c.captionSourceWhisper}` : `📺 ${c.captionSourceYt}`}
                                {tr.model ? ` · ${tr.model}` : ''}{tr.language ? ` · ${tr.language}` : ''}
                                {tr.cached ? ` · ${c.captionCached}` : ''}
                                {` · ${tr.lines.length} ${c.captionLines}`}
                              </span>
                            )}
                            <div style={{ display: 'flex', gap: '0.4rem', marginLeft: 'auto', flexWrap: 'wrap' }}>
                              {hasWords && (
                                <>
                                  <button
                                    type="button"
                                    className="form-input"
                                    style={{ width: 'auto', padding: '0.35rem 0.7rem', fontSize: '0.74rem', cursor: 'pointer', background: 'transparent' }}
                                    onClick={() => copyText(tr.text, c.captionCopyLabel)}
                                  >
                                    📋 {c.captionCopy}
                                  </button>
                                  <button
                                    type="button"
                                    className="form-input"
                                    style={{ width: 'auto', padding: '0.35rem 0.7rem', fontSize: '0.74rem', cursor: 'pointer', background: 'transparent' }}
                                    onClick={() => downloadSrt(item)}
                                    title={c.captionSrtNote}
                                  >
                                    📥 {c.captionDownloadSrt}
                                  </button>
                                </>
                              )}
                              <button
                                type="button"
                                className="form-input"
                                style={{ width: 'auto', padding: '0.35rem 0.7rem', fontSize: '0.74rem', cursor: 'pointer', background: 'transparent' }}
                                disabled={busy || !!bulkCaptions}
                                onClick={() => captionOne(item)}
                                title={c.captionHint}
                              >
                                {busy
                                  ? `⏳ ${c.captionWorking}`
                                  : tr ? `🔁 ${c.captionRetry}` : `🎙️ ${c.captionGet}`}
                              </button>
                            </div>
                          </div>

                          {busy && (
                            <div style={{ marginTop: 5, fontSize: '0.74rem', color: 'var(--text-muted)' }}>
                              ⏳ {c.captionWorkingHint}
                            </div>
                          )}

                          {!busy && hasWords && (
                            <div style={{ marginTop: 6 }}>
                              <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', fontStyle: 'italic', lineHeight: 1.5 }}>
                                “{short}”
                              </div>
                              {body.length > 260 && (
                                <button
                                  type="button"
                                  onClick={() => setOpenCaptions(prev => ({ ...prev, [item.id]: !open }))}
                                  style={{ background: 'none', border: 'none', color: 'var(--accent)', fontSize: '0.72rem', cursor: 'pointer', padding: 0, marginTop: 3 }}
                                >
                                  {open ? c.captionShowLess : c.captionShowMore}
                                </button>
                              )}
                            </div>
                          )}

                          {!busy && tr && !hasWords && (
                            <div style={{ marginTop: 5, fontSize: '0.74rem', color: 'var(--text-muted)' }}>
                              {tr.source === 'none'
                                ? `⚠️ ${c.captionNone}${tr.note ? ` — ${tr.note}` : ''}`
                                : `⚠️ ${c.captionNoWords}`}
                            </div>
                          )}
                        </div>
                      );
                    })()}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </section>
      )}

      {fallback && (
        <ExportFallbackPanel
          key={`${fallback.videoId}-${fallback.startTime}-${fallback.endTime}`}
          target={fallback}
          onClose={() => setFallback(null)}
          onClip={(blob, filename) => saveBlob(blob, filename)}
        />
      )}
    </div>
  );
}
