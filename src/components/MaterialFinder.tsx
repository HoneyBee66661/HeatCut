// MATERIAL FINDER — the simple, average-user half of the creative consultant.
//
// The creative board (`/api/campaign/strategy`) answers "what should I post?".
// This answers the question a clipper actually gets stuck on:
//   "…and where do I get the footage?"
//
// Flow (three inputs, one button):
//   1. what is being promoted (+ artist),
//   2. the lyrics, pasted one line per line (optional but it makes beats exact),
//   3. feel + video style chips.
// Out come: a numbered non-technical recipe, the asset list, and one card per
// LYRIC BEAT carrying the shot idea, a real YouTube search link, and — on
// request — actual videos found by the live search, each with a one-click
// hand-off to the Studio ("Analyze in Studio" for a movie scene, say).
//
// Everything is stateful on its own and reports up through `onPersist`, so the
// campaign page can save it in the prep session without owning the state.

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { Translations } from '../locales/en';
import { saveBlob } from '../lib/rawExport';

export interface MaterialSearch {
  kind: string;
  query: string;
  url: string;
  ai?: boolean;
}

export interface MaterialResult {
  video_id: string;
  title: string;
  url: string;
  channel: string;
  duration?: number | null;
  duration_label?: string;
  thumbnail?: string;
}

export interface MaterialBeat {
  id: string;
  index: number;
  line: string;
  line_full: string;
  essence: string;
  repeat: number;
  is_chorus: boolean;
  mood: string;
  motif: string;
  visual: string;
  pace_sec: number | null;
  shot: string;
  why: string;
  searches: MaterialSearch[];
  ai_scene?: { film: string; scene: string; why: string } | null;
  results: MaterialResult[];
}

export interface MaterialStep {
  n: number;
  title: string;
  detail: string;
  minutes: number;
}

export interface MaterialPack {
  version: number;
  source: string;
  model?: string;
  campaign_id?: string;
  language: string;
  subject: string;
  artist: string;
  style: string;
  style_label: string;
  style_hint: string;
  vibe: string;
  vibe_label: string;
  mood: string;
  pace_sec: number;
  pace_hint: string;
  duration_sec: number;
  clip_count: number;
  summary: string;
  beats: MaterialBeat[];
  lyric_map: { line: string; beat_id: string | null; repeat: number }[];
  searches: MaterialSearch[];
  asset_plan: { item: string; count: number }[];
  steps: MaterialStep[];
  watch_outs: { level: string; text: string }[];
  notes: string[];
  material_md?: string;
}

/** Everything the page needs to store this feature in a prep session. */
export interface MaterialPersist {
  material: MaterialPack | null;
  material_md: string;
  subject: string;
  artist: string;
  lyrics: string;
  vibe: string;
  style: string;
}

export interface MaterialFinderProps {
  c: Translations['campaign'];
  language: string;
  /** Parsed campaign brief, forwarded so the pack can read its rules/platforms. */
  spec?: unknown;
  initial?: MaterialPersist | null;
  durationSec: number;
  /** The AI toggle + a key: gates the optional film-scene pass. */
  aiReady: boolean;
  apiKey: string;
  provider: string;
  model: string;
  baseUrl: string;
  onToast: (message: string | null) => void;
  /** Jump to the Studio with this URL pre-filled (fast hand-off). */
  onSendToStudio: (url: string) => void;
  onPersist: (state: MaterialPersist) => void;
}

const VIBES = ['auto', 'hype', 'sad', 'romantic', 'nostalgic', 'bold', 'chill'] as const;
const STYLES = ['auto', 'movie_edit', 'lyric_video', 'story', 'product_demo', 'broll'] as const;

const VIBE_LABEL: Record<string, { en: string; id: string }> = {
  auto: { en: 'Match the song', id: 'Ikut lagunya' },
  hype: { en: 'Hype', id: 'Hype' },
  sad: { en: 'Sad', id: 'Sedih' },
  romantic: { en: 'Romantic', id: 'Romantis' },
  nostalgic: { en: 'Nostalgic', id: 'Nostalgia' },
  bold: { en: 'Bold', id: 'Tegas' },
  chill: { en: 'Chill', id: 'Santai' },
};

const STYLE_LABEL: Record<string, { en: string; id: string }> = {
  auto: { en: 'Pick for me', id: 'Pilihkan' },
  movie_edit: { en: 'Movie-scene edit', id: 'Edit potongan film' },
  lyric_video: { en: 'Lyric / text video', id: 'Video lirik teks' },
  story: { en: 'Story / vlog', id: 'Cerita / vlog' },
  product_demo: { en: 'Product demo', id: 'Demo produk' },
  broll: { en: 'Cinematic b-roll', id: 'B-roll sinematik' },
};

const KIND_LABEL: Record<string, { en: string; id: string }> = {
  scene: { en: 'Film / scene reference', id: 'Referensi film / adegan' },
  ai_scene: { en: 'AI scene idea', id: 'Ide adegan AI' },
  edit_ref: { en: 'Edit reference — watch how it is cut', id: 'Referensi editing — lihat cara potongnya' },
  broll: { en: 'Cinematic b-roll / stock', id: 'B-roll sinematik / stok' },
  backdrop: { en: 'Moving backdrop', id: 'Latar bergerak' },
  product: { en: 'Product shot reference', id: 'Referensi shot produk' },
  audio: { en: 'Official song audio', id: 'Audio lagu resmi' },
};

const label = (kind: string, lang: string): string => {
  const entry = KIND_LABEL[kind];
  if (!entry) return kind;
  return lang === 'id' ? entry.id : entry.en;
};

export default function MaterialFinder({
  c, language, spec, initial, durationSec, aiReady, apiKey, provider, model, baseUrl,
  onToast, onSendToStudio, onPersist,
}: MaterialFinderProps) {
  const lang = language === 'id' ? 'id' : 'en';
  const [subject, setSubject] = useState(initial?.subject || '');
  const [artist, setArtist] = useState(initial?.artist || '');
  const [lyrics, setLyrics] = useState(initial?.lyrics || '');
  const [vibe, setVibe] = useState(initial?.vibe || 'auto');
  const [style, setStyle] = useState(initial?.style || 'auto');
  const [showLyrics, setShowLyrics] = useState(!!(initial?.lyrics || '').trim());
  const [material, setMaterial] = useState<MaterialPack | null>(initial?.material || null);
  const [materialMd, setMaterialMd] = useState(initial?.material_md || '');
  const [busy, setBusy] = useState(false);
  const [liveBusy, setLiveBusy] = useState<string | null>(null);
  const [liveOn, setLiveOn] = useState(true);
  const [scenesOn, setScenesOn] = useState(false);
  const [showLyricMap, setShowLyricMap] = useState(false);
  const [openBeats, setOpenBeats] = useState<Record<string, boolean>>({});

  // The parent's callback identity must never re-trigger the persist effect.
  const persistRef = useRef(onPersist);
  useEffect(() => { persistRef.current = onPersist; }, [onPersist]);

  useEffect(() => {
    persistRef.current({ material, material_md: materialMd, subject, artist, lyrics, vibe, style });
  }, [material, materialMd, subject, artist, lyrics, vibe, style]);

  const lyricLines = lyrics.trim() ? lyrics.trim().split('\n').filter(l => l.trim()).length : 0;

  const toast = useCallback((msg: string) => onToast(msg), [onToast]);

  /** Merge live-search hits back into the beats that asked for them. */
  const mergeResults = useCallback((found: Record<string, MaterialResult[]>) => {
    setMaterial(prev => {
      if (!prev) return prev;
      return {
        ...prev,
        beats: (prev.beats || []).map(beat => {
          const query = beat.searches?.[0]?.query;
          const hits = query ? found[query] : undefined;
          return hits ? { ...beat, results: hits } : beat;
        }),
      };
    });
  }, []);

  const searchQueries = useCallback(async (queries: string[], key: string) => {
    const list = queries.filter(Boolean).slice(0, 8);
    if (!list.length) return;
    setLiveBusy(key);
    try {
      const resp = await fetch('/api/campaign/material/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ queries: list, limit: 4 }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || `Server error ${resp.status}`);
      const found = (data?.results || {}) as Record<string, MaterialResult[]>;
      mergeResults(found);
      const total = list.reduce((n, q) => n + (found[q]?.length || 0), 0);
      toast(total ? c.materialLiveFound(total) : c.materialLiveNone);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err));
    } finally {
      setLiveBusy(null);
    }
  }, [c, mergeResults, toast]);

  /** One beat's own primary query (the scene search) — the per-card button. */
  const searchBeat = useCallback((beat: MaterialBeat) => {
    const query = beat.searches?.[0]?.query;
    if (!query) return;
    void searchQueries([query], beat.id);
  }, [searchQueries]);

  const searchAll = useCallback((pack: MaterialPack) => {
    const queries = (pack.beats || []).map(b => b.searches?.[0]?.query || '').filter(Boolean);
    if (!queries.length) { toast(c.materialLiveNone); return; }
    void searchQueries(queries, 'all');
  }, [c, searchQueries, toast]);

  const handleBuild = async () => {
    if (busy) return;
    if (!subject.trim() && !lyrics.trim()) { toast(c.materialNeedSubject); return; }
    setBusy(true);
    try {
      const resp = await fetch('/api/campaign/material', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          spec,
          subject: subject.trim() || undefined,
          artist: artist.trim() || undefined,
          lyrics: lyrics.trim() || undefined,
          language,
          vibe,
          style,
          duration_sec: durationSec,
          beat_count: 6,
          generate_scenes: scenesOn && aiReady,
          api_key: scenesOn && aiReady ? apiKey.trim() || undefined : undefined,
          provider: scenesOn && aiReady ? provider : undefined,
          model: scenesOn && aiReady ? model : undefined,
          base_url: scenesOn && aiReady && provider === 'openai-compatible' ? baseUrl.trim() || undefined : undefined,
        }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.detail || `Server error ${resp.status}`);
      const pack = (data?.material || null) as MaterialPack | null;
      const md = (data?.material_md || '') as string;
      setMaterial(pack);
      setMaterialMd(md);
      if (pack) {
        setOpenBeats({ [(pack.beats || [])[0]?.id || 'b1']: true });
        toast(c.materialReady((pack.steps || []).length, (pack.beats || []).length));
        if (liveOn) searchAll(pack);
      }
      const note = (data?.scene_note || '') as string;
      if (note) toast(note);
    } catch (err) {
      toast(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const planText = (): string => {
    if (!material) return '';
    const lines: string[] = [];
    lines.push(`# ${material.subject}${material.artist ? ` — ${material.artist}` : ''}`);
    lines.push('');
    lines.push(material.summary);
    lines.push('');
    lines.push(`## ${c.materialStepsTitle}`);
    (material.steps || []).forEach(s => lines.push(`${s.n}. ${s.title} — ${s.detail}`));
    lines.push('');
    lines.push(`## ${c.materialBeatsTitle}`);
    (material.beats || []).forEach(b => {
      lines.push('');
      lines.push(`[${b.id}] ${b.line_full || b.line}${b.is_chorus ? ` (${c.materialChorus})` : ''}`);
      lines.push(`  ${c.materialEssence}: ${b.essence}`);
      lines.push(`  ${c.materialShot}: ${b.shot}`);
      (b.searches || []).forEach(s => lines.push(`  - ${label(s.kind, lang)}: ${s.url}`));
      (b.results || []).forEach(r => lines.push(`  ▶ ${r.title} — ${r.url}`));
    });
    return lines.join('\n');
  };

  const copyPlan = () => {
    const text = planText();
    if (!text) { toast(c.materialEmpty); return; }
    navigator.clipboard.writeText(text).then(() => toast(c.copyDone(c.materialTitle)));
  };

  const copySteps = () => {
    if (!material) { toast(c.materialEmpty); return; }
    const text = (material.steps || []).map(s => `${s.n}. ${s.title} — ${s.detail}`).join('\n');
    navigator.clipboard.writeText(text).then(() => toast(c.copyDone(c.materialStepsTitle)));
  };

  const downloadMd = () => {
    if (!materialMd) { toast(c.materialEmpty); return; }
    saveBlob(new Blob([materialMd], { type: 'text/markdown' }), 'MATERIAL.md');
  };

  const chip = (active: boolean): CSSProperties => ({
    padding: '0.3rem 0.6rem',
    borderRadius: 999,
    cursor: 'pointer',
    fontSize: '0.72rem',
    fontWeight: 600,
    border: `1px solid ${active ? 'var(--primary)' : 'rgba(255,255,255,0.12)'}`,
    background: active ? 'rgba(255,107,53,0.16)' : 'transparent',
    color: active ? 'var(--primary)' : 'var(--text-secondary)',
  });

  const statChip = (key: string, value: string) => (
    <span key={key} className="score-badge score-meta" style={{ fontSize: '0.68rem' }}>{value}</span>
  );

  return (
    <div data-testid="material-finder" style={{ display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
      <div>
        <h4 style={{ margin: 0, fontSize: '0.95rem' }}>🎬 {c.materialTitle}</h4>
        <p style={{ margin: '0.25rem 0 0', fontSize: '0.78rem', color: 'var(--text-muted)' }}>{c.materialSubtitle}</p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0.6rem' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
          <label style={{ fontSize: '0.74rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)' }}>
            {c.materialSubjectLabel}
          </label>
          <input
            data-testid="material-subject"
            className="form-input"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder={c.materialSubjectPlaceholder}
          />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
          <label style={{ fontSize: '0.74rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)' }}>
            {c.materialArtistLabel}
          </label>
          <input
            data-testid="material-artist"
            className="form-input"
            value={artist}
            onChange={(e) => setArtist(e.target.value)}
            placeholder={c.materialArtistPlaceholder}
          />
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
        <button
          type="button"
          data-testid="material-lyrics-toggle"
          onClick={() => setShowLyrics(v => !v)}
          style={{ alignSelf: 'flex-start', background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.78rem', padding: 0 }}
        >
          {showLyrics ? '▾' : '▸'} {c.materialLyricsLabel}
          {lyricLines > 0 && <span style={{ marginLeft: 6, color: 'var(--text-muted)', fontWeight: 500 }}>· {c.materialLyricsCount(lyricLines)}</span>}
        </button>
        {showLyrics && (
          <textarea
            data-testid="material-lyrics"
            className="form-input"
            rows={6}
            value={lyrics}
            onChange={(e) => setLyrics(e.target.value)}
            placeholder={c.materialLyricsPlaceholder}
            style={{ fontFamily: 'inherit', resize: 'vertical' }}
          />
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
        <span style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.materialVibeLabel}</span>
        <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
          {VIBES.map(v => (
            <button
              key={v}
              type="button"
              data-testid={`material-vibe-${v}`}
              onClick={() => setVibe(v)}
              style={chip(vibe === v)}
            >
              {VIBE_LABEL[v]?.[lang] || v}
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
        <span style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--text-muted)', fontWeight: 700 }}>{c.materialStyleLabel}</span>
        <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
          {STYLES.map(s => (
            <button
              key={s}
              type="button"
              data-testid={`material-style-${s}`}
              onClick={() => setStyle(s)}
              style={chip(style === s)}
            >
              {STYLE_LABEL[s]?.[lang] || s}
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', fontSize: '0.76rem', color: 'var(--text-secondary)' }}>
          <input type="checkbox" data-testid="material-live-toggle" checked={liveOn} onChange={(e) => setLiveOn(e.target.checked)} />
          {c.materialLiveToggle}
        </label>
        {aiReady && (
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', fontSize: '0.76rem', color: 'var(--text-secondary)' }}>
            <input type="checkbox" data-testid="material-scenes-toggle" checked={scenesOn} onChange={(e) => setScenesOn(e.target.checked)} />
            {c.materialScenesToggle}
          </label>
        )}
      </div>

      <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <button
          type="button"
          className="glowing-btn"
          data-testid="material-generate"
          onClick={handleBuild}
          disabled={busy}
          style={{ padding: '0.6rem 1.1rem', fontSize: '0.85rem', borderRadius: 10 }}
        >
          {busy ? c.materialGenerating : c.materialGenerate}
        </button>
        {!material && <span style={{ fontSize: '0.74rem', color: 'var(--text-muted)' }}>{c.materialStartHint}</span>}
      </div>

      {material && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', borderTop: '1px solid var(--border-color)', paddingTop: '1rem' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', padding: '0.8rem 0.9rem', borderRadius: 12, background: 'rgba(255,107,53,0.07)', border: '1px solid rgba(255,107,53,0.18)' }}>
            <div style={{ fontSize: '0.85rem', fontWeight: 600 }} data-testid="material-summary">{material.summary}</div>
            <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
              {statChip('style', `${c.materialSummaryStyle}: ${material.style_label}`)}
              {statChip('feel', `${c.materialSummaryFeel}: ${material.vibe_label}`)}
              {statChip('pace', `${c.materialSummaryPace}: ${c.materialPaceUnit(material.pace_sec)}`)}
              {statChip('clips', `${c.materialSummaryClips}: ${material.clip_count}`)}
              {statChip('len', `${c.materialSummaryLen}: ${Math.round(material.duration_sec)}s`)}
              {material.source === 'rules+llm' && statChip('llm', material.model || 'AI')}
            </div>
            <div style={{ display: 'flex', gap: '0.7rem', flexWrap: 'wrap', alignItems: 'center' }}>
              <button type="button" data-testid="material-copy-steps" onClick={copySteps} style={{ background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.76rem', padding: 0 }}>
                {c.materialCopySteps}
              </button>
              <span style={{ color: 'var(--border-color)' }}>|</span>
              <button type="button" data-testid="material-copy-plan" onClick={copyPlan} style={{ background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.76rem', padding: 0 }}>
                {c.materialCopyPlan}
              </button>
              <span style={{ color: 'var(--border-color)' }}>|</span>
              <button type="button" data-testid="material-download-md" onClick={downloadMd} style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontWeight: 600, fontSize: '0.76rem', padding: 0 }}>
                {c.materialDownloadMd}
              </button>
              <span style={{ color: 'var(--border-color)' }}>|</span>
              <button
                type="button"
                data-testid="material-live-all"
                onClick={() => searchAll(material)}
                disabled={liveBusy === 'all'}
                style={{ background: 'none', border: 'none', color: 'var(--secondary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.76rem', padding: 0 }}
              >
                {liveBusy === 'all' ? c.materialLiveSearching : c.materialLiveAll}
              </button>
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            <h4 style={{ margin: 0, fontSize: '0.9rem' }}>📋 {c.materialStepsTitle}</h4>
            {(material.steps || []).map(step => (
              <div
                key={step.n}
                data-testid={`material-step-${step.n}`}
                style={{ display: 'flex', gap: '0.65rem', padding: '0.7rem 0.8rem', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
              >
                <span style={{ flex: '0 0 26px', height: 26, borderRadius: 999, display: 'grid', placeItems: 'center', background: 'rgba(255,107,53,0.16)', color: 'var(--primary)', fontWeight: 700, fontSize: '0.78rem' }}>{step.n}</span>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                  <div style={{ display: 'flex', gap: '0.45rem', alignItems: 'center', flexWrap: 'wrap' }}>
                    <strong style={{ fontSize: '0.84rem' }}>{step.title}</strong>
                    <span className="score-badge score-meta" style={{ fontSize: '0.64rem' }}>⏱ {c.materialMinutes(step.minutes)}</span>
                  </div>
                  <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{step.detail}</div>
                </div>
              </div>
            ))}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
            <h4 style={{ margin: 0, fontSize: '0.9rem' }}>🧰 {c.materialNeedTitle}</h4>
            <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>
              {(material.asset_plan || []).map((item, i) => (
                <span key={i} className="score-badge score-high" style={{ fontSize: '0.68rem' }}>{item.count} × {item.item}</span>
              ))}
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
            <h4 style={{ margin: 0, fontSize: '0.9rem' }}>🎞️ {c.materialBeatsTitle}</h4>
            {(material.beats || []).map(beat => {
              const open = !!openBeats[beat.id];
              const primary = beat.searches?.[0];
              return (
                <div
                  key={beat.id}
                  data-testid={`material-beat-${beat.id}`}
                  style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem', padding: '0.8rem 0.9rem', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
                >
                  <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', flexWrap: 'wrap' }}>
                    <span className="score-badge score-high" style={{ fontSize: '0.66rem' }}>{beat.id}</span>
                    {beat.is_chorus && <span className="score-badge score-meta" style={{ fontSize: '0.64rem' }}>🔁 {c.materialChorus} ×{beat.repeat}</span>}
                    <span className="score-badge score-meta" style={{ fontSize: '0.64rem' }}>{beat.mood}</span>
                    <button
                      type="button"
                      onClick={() => setOpenBeats(prev => ({ ...prev, [beat.id]: !open }))}
                      style={{ marginLeft: 'auto', background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '0.72rem' }}
                    >
                      {open ? '▾' : '▸'}
                    </button>
                  </div>
                  <div style={{ fontSize: '0.85rem', fontWeight: 600 }}>“{beat.line_full || beat.line}”</div>
                  <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                    <strong>{c.materialEssence}:</strong> {beat.essence}
                  </div>
                  {open && (
                    <>
                      <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                        <strong>{c.materialShot}:</strong> {beat.shot}
                      </div>
                      {beat.ai_scene && (
                        <div data-testid={`material-ai-scene-${beat.id}`} style={{ fontSize: '0.78rem', color: 'var(--secondary)' }}>
                          🎥 <strong>{c.materialAiScene}:</strong> {beat.ai_scene.film} — {beat.ai_scene.scene}
                          {beat.ai_scene.why ? ` (${beat.ai_scene.why})` : ''}
                        </div>
                      )}
                      <div style={{ fontSize: '0.74rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 700 }}>{c.materialSearchHint}</div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                        {(beat.searches || []).map((s, i) => (
                          <a
                            key={i}
                            href={s.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={{ fontSize: '0.76rem', color: 'var(--primary)', textDecoration: 'none' }}
                          >
                            ↗ {label(s.kind, lang)} — <span style={{ color: 'var(--text-muted)' }}>{s.query}</span>
                          </a>
                        ))}
                      </div>
                      <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center', flexWrap: 'wrap' }}>
                        <button
                          type="button"
                          data-testid={`material-live-${beat.id}`}
                          onClick={() => searchBeat(beat)}
                          disabled={liveBusy === beat.id || !primary}
                          style={{ background: 'none', border: 'none', color: 'var(--secondary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.74rem', padding: 0 }}
                        >
                          {liveBusy === beat.id ? c.materialLiveSearching : c.materialLiveBtn}
                        </button>
                      </div>
                      {(beat.results || []).length > 0 && (
                        <div data-testid={`material-results-${beat.id}`} style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
                          {(beat.results || []).map(r => (
                            <div
                              key={r.video_id}
                              style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap', padding: '0.4rem 0.5rem', borderRadius: 8, background: 'rgba(255,255,255,0.03)' }}
                            >
                              <a href={r.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: '0.76rem', color: 'var(--primary)', textDecoration: 'none', flex: '1 1 220px' }}>
                                ▶ {r.title}
                              </a>
                              <span style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
                                {r.channel}{r.duration_label ? ` · ${r.duration_label}` : ''}
                              </span>
                              <button
                                type="button"
                                onClick={() => onSendToStudio(r.url)}
                                style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontWeight: 600, fontSize: '0.72rem', padding: 0 }}
                              >
                                {c.materialOpenStudio}
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>

          {!!(material.lyric_map || []).length && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
              <button
                type="button"
                data-testid="material-lyric-map-toggle"
                onClick={() => setShowLyricMap(v => !v)}
                style={{ alignSelf: 'flex-start', background: 'none', border: 'none', color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, fontSize: '0.8rem', padding: 0 }}
              >
                {showLyricMap ? '▾' : '▸'} {c.materialLyricMapTitle}
              </button>
              {showLyricMap && (
                <div data-testid="material-lyric-map" style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem', fontSize: '0.76rem', color: 'var(--text-secondary)' }}>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{c.materialLyricMapHint}</div>
                  {(material.lyric_map || []).map((row, i) => (
                    <div key={i} style={{ display: 'flex', gap: '0.5rem' }}>
                      <span className="score-badge score-meta" style={{ fontSize: '0.62rem', opacity: row.beat_id ? 1 : 0.45 }}>{row.beat_id || '—'}</span>
                      <span>{row.line}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {!!(material.watch_outs || []).length && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
              <h4 style={{ margin: 0, fontSize: '0.9rem' }}>⚠️ {c.materialWatchTitle}</h4>
              {(material.watch_outs || []).map((w, i) => (
                <div key={i} style={{ fontSize: '0.76rem', color: w.level === 'warn' ? '#fca5a5' : 'var(--text-secondary)' }}>
                  {w.level === 'warn' ? '⚠️' : 'ℹ️'} {w.text}
                </div>
              ))}
            </div>
          )}

          {!!(material.notes || []).length && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem', fontSize: '0.76rem', color: 'var(--text-secondary)' }}>
              {(material.notes || []).map((n, i) => <div key={i}>ℹ️ {n}</div>)}
            </div>
          )}

          <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>📦 {c.materialZipAdded}</div>
        </div>
      )}
    </div>
  );
}
