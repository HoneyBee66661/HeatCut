import { useCallback, useMemo, useState } from 'react';
import { useLanguage } from './locales';
import { fetchRawClip, safeFilename, saveBlob, youtubeLink } from './lib/rawExport';
import { buildZip, zipEntry, type ZipEntry } from './lib/zipBundle';

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

interface CampaignPageProps {
  apiKey: string;
  provider: string;
  model: string;
  baseUrl: string;
  onToast: (message: string | null) => void;
}

interface HistoryEntry {
  campaign_id: string;
  label: string;
  url: string;
  at: number;
}

const HISTORY_KEY = 'heatcut_campaign_history';
const URL_KEY = 'heatcut_campaign_url';

/** One archive is one download — cap it so the tab never has to hold a huge pack. */
const ZIP_MAX_BYTES = 1.5 * 1024 * 1024 * 1024;

const fmt = (sec: number): string => {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 3600) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};

const readHistory = (): HistoryEntry[] => {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.slice(0, 6) : [];
  } catch {
    return [];
  }
};

export default function CampaignPage({ apiKey, provider, model, baseUrl, onToast }: CampaignPageProps) {
  const { t } = useLanguage();
  const c = t.campaign;

  const [url, setUrl] = useState(() => localStorage.getItem(URL_KEY) || '');
  const [spec, setSpec] = useState<CampaignSpec | null>(null);
  const [plan, setPlan] = useState<PrepPlan | null>(null);
  const [parsing, setParsing] = useState(false);
  const [prepping, setPrepping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>(readHistory);

  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [targetDuration, setTargetDuration] = useState<15 | 30 | 60>(30);
  const [perSource, setPerSource] = useState(4);
  const [maxTotal, setMaxTotal] = useState(15);
  const [aiCopy, setAiCopy] = useState(false);
  const [exportingKey, setExportingKey] = useState<string | null>(null);
  const [bulk, setBulk] = useState<{ done: number; total: number } | null>(null);
  const [bulkZip, setBulkZip] = useState(false);
  // item id -> why the automatic download failed. Non-empty = that window is
  // handed over as a MANUAL cut (labeled jump link to the source second).
  const [manual, setManual] = useState<Record<string, string>>({});

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
      localStorage.setItem(URL_KEY, url.trim());
      const entry: HistoryEntry = {
        campaign_id: parsed.campaign_id,
        label: parsed.public_name || parsed.name || parsed.campaign_id,
        url: url.trim(),
        at: Date.now(),
      };
      const next = [entry, ...readHistory().filter(h => h.campaign_id !== entry.campaign_id)].slice(0, 6);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
      setHistory(next);
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

  const markManual = (key: string, message: string) =>
    setManual(prev => ({ ...prev, [key]: message }));

  const clearManual = (key: string) =>
    setManual(prev => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });

  const downloadItem = async (item: PlanItem, quiet = false) => {
    const key = item.id;
    if (exportingKey) return;
    setExportingKey(key);
    try {
      const blob = await fetchRawClip(item.video_id, item.start, item.end, item.source_label);
      saveBlob(blob, `heatcut_${safeFilename(item.source_label)}_${Math.floor(item.start)}-${Math.floor(item.end)}s.mp4`);
      clearManual(key);
      if (!quiet) toast(null);
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
    for (let i = 0; i < items.length; i += 1) {
      setBulk({ done: i, total: items.length });
      try {
        const blob = await fetchRawClip(items[i].video_id, items[i].start, items[i].end, items[i].source_label);
        const entry = await zipEntry(
          `heatcut_${safeFilename(items[i].source_label)}_${Math.floor(items[i].start)}-${Math.floor(items[i].end)}s.mp4`,
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
        clearManual(items[i].id);
      } catch (err) {
        markManual(items[i].id, err instanceof Error ? err.message : c.exportFailed);
        blocked.push(items[i]);
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
    saveBlob(buildZip(entries), `heatcut_campaign_${plan.campaign_id || 'pack'}_raw.zip`);
    setBulk(null);
    setBulkZip(false);
    toast(c.zipDone(entries.filter(e => e.name.endsWith('.mp4')).length, blocked.length), 8000);
  };

  // ---------------------------------------------------------------- render help

  const statChip = (label: string, value: string) => (
    <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 110 }}>
      <span style={{ fontSize: '0.68rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{label}</span>
      <span style={{ fontSize: '0.95rem', fontWeight: 700, color: 'var(--text-primary)' }}>{value}</span>
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

        {history.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span style={{ fontSize: '0.72rem', fontWeight: 700, textTransform: 'uppercase', color: 'var(--text-muted)' }}>{c.sessionTitle}</span>
              <button
                type="button"
                onClick={() => { localStorage.removeItem(HISTORY_KEY); setHistory([]); }}
                style={{ background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: '0.72rem', cursor: 'pointer', textDecoration: 'underline' }}
              >
                {c.clearSession}
              </button>
            </div>
            <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
              {history.map(h => (
                <button
                  key={h.campaign_id}
                  type="button"
                  className="duration-btn"
                  onClick={() => { setUrl(h.url); }}
                  title={c.sessionHint}
                >
                  {h.label.slice(0, 42)}
                </button>
              ))}
            </div>
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
                  </div>
                ))}
              </div>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
