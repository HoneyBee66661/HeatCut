import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLanguage } from './locales';
import {
  guessPlatform, parseWindows, platformLabel, probeLink, requestDownload, saveBlob,
  safeFilename, sourceLinkAtTime, zipParts,
  type DownloadDenied, type DownloadRequest, type ParseResult, type ProbeResult,
} from './lib/downloader';

interface VideoDownloaderPageProps {
  onToast: (message: string | null) => void;
}

/** One requested part and what came back for it. */
interface PartState {
  key: string;
  label: string;
  start: number;
  end: number;
  status: 'pending' | 'working' | 'ok' | 'blocked' | 'failed';
  filename?: string;
  denied?: DownloadDenied;
  error?: string;
}

const PAD = '0.85rem';
const cardStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: '0.9rem',
  padding: PAD,
};
const rowStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: '0.6rem', alignItems: 'center' };
const chipStyle = (active: boolean): React.CSSProperties => ({
  padding: '0.5rem 0.85rem',
  borderRadius: '10px',
  border: `1px solid ${active ? 'rgba(255, 94, 58, 0.5)' : 'var(--border-color)'}`,
  background: active ? 'rgba(255, 94, 58, 0.16)' : 'rgba(255, 255, 255, 0.03)',
  color: 'var(--text-primary)',
  fontSize: '0.82rem',
  fontWeight: active ? 600 : 500,
  cursor: 'pointer',
});
const badgeStyle = (tone: 'ok' | 'warn' | 'bad' | 'muted'): React.CSSProperties => {
  const colors = {
    ok: ['rgba(52, 199, 89, 0.16)', '#7ce29a'],
    warn: ['rgba(242, 181, 138, 0.16)', 'var(--secondary)'],
    bad: ['rgba(255, 94, 58, 0.16)', '#ff9d7a'],
    muted: ['rgba(255, 255, 255, 0.05)', 'var(--text-secondary)'],
  }[tone];
  return {
    padding: '0.22rem 0.55rem',
    borderRadius: '999px',
    fontSize: '0.72rem',
    background: colors[0],
    color: colors[1],
    border: '1px solid var(--border-color)',
  };
};

/**
 * Standalone downloader page: any yt-dlp link (YouTube, TikTok, Instagram) →
 * one file, whole or windowed. Windows reuse the studio's DASH fragment-range
 * miner / HLS segment miner on the backend, so a 30 s part of an 80-minute
 * source moves a few MB — not the whole upload. Audio-only parts ride the audio
 * DASH stream directly.
 */
export default function VideoDownloaderPage({ onToast }: VideoDownloaderPageProps) {
  const { t } = useLanguage();
  const d = t.downloader;

  /** The app's toast is a plain state setter with no auto-hide — clear it here. */
  const toast = useCallback((message: string, ms = 4500) => {
    onToast(message);
    setTimeout(() => onToast(null), ms);
  }, [onToast]);

  const [url, setUrl] = useState('');
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);

  const [kind, setKind] = useState<'video' | 'audio'>('video');
  const [scope, setScope] = useState<'full' | 'parts'>('full');
  const [quality, setQuality] = useState('');
  const [audioFormat, setAudioFormat] = useState<'m4a' | 'mp3'>('m4a');

  const [parts, setParts] = useState('');
  const [parsed, setParsed] = useState<ParseResult | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);

  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ current: number; total: number } | null>(null);
  const [partStates, setPartStates] = useState<PartState[]>([]);
  const [wholeBusy, setWholeBusy] = useState(false);

  const guessed = useMemo(() => guessPlatform(url), [url]);
  const probedHere = probe && probe.input_url === url.trim();

  // ------------------------------------------------------------ probe

  const runProbe = useCallback(async (target: string) => {
    const link = (target || '').trim();
    if (!link) {
      toast(d.needUrl);
      return;
    }
    setProbing(true);
    setProbeError(null);
    try {
      const result = await probeLink(link);
      setProbe(result);
      setQuality('');
      setProbeError(null);
    } catch (err) {
      setProbe(null);
      setProbeError(err instanceof Error ? err.message : String(err));
    } finally {
      setProbing(false);
    }
  }, [d.needUrl, toast]);

  // A new link invalidates the previous probe (title/duration feed the filenames).
  const onUrlChange = (value: string) => {
    setUrl(value);
    if (probe && probe.input_url !== value.trim()) setProbe(null);
    setProbeError(null);
  };

  // ------------------------------------------------------------ parts preview

  useEffect(() => {
    if (scope !== 'parts') return;
    const text = parts.trim();
    let cancelled = false;
    // Everything (including the empty-text reset) happens inside the debounce
    // callback: a synchronous setState in the effect body would cascade renders.
    const handle = setTimeout(() => {
      if (cancelled) return;
      if (!text) {
        setParsed(null);
        setParseError(null);
        return;
      }
      parseWindows(text, probe?.duration || 0)
        .then((result) => {
          if (cancelled) return;
          setParsed(result);
          setParseError(null);
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          setParsed(null);
          setParseError(err instanceof Error ? err.message : String(err));
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [parts, scope, probe?.duration]);

  // ------------------------------------------------------------ downloads

  const baseRequest = (): Omit<DownloadRequest, 'mode'> => ({
    url: (probe?.url || url).trim(),
    kind,
    quality,
    audioFormat,
    title: probe?.title || '',
  });

  /** One window → one file (or a refusal). `fallback` = whole file, cut locally. */
  const fetchPart = async (
    window: { start: number; end: number; label: string },
    fallback: boolean,
  ): Promise<{ status: 'ok'; name: string; blob: Blob }
    | { status: 'blocked'; denied: DownloadDenied }
    | { status: 'failed'; error: string }> => {
    const ext = kind === 'audio' ? audioFormat : 'mp4';
    const suggested = `${safeFilename(probe?.title || 'download')}-${safeFilename(window.label.replace(/:/g, '-'))}.${ext}`;
    try {
      const outcome = await requestDownload({
        ...baseRequest(),
        mode: 'window',
        startTime: window.start,
        endTime: window.end,
        fallback,
      }, { filename: suggested });
      if (outcome.kind === 'file') return { status: 'ok', name: outcome.filename || suggested, blob: outcome.blob };
      return { status: 'blocked', denied: outcome.denied };
    } catch (err) {
      return { status: 'failed', error: err instanceof Error ? err.message : String(err) };
    }
  };

  const downloadWhole = async () => {
    if (!probe) {
      toast(d.checkFirst);
      return;
    }
    setWholeBusy(true);
    setProgress(null);
    try {
      const outcome = await requestDownload({ ...baseRequest(), mode: 'full' });
      if (outcome.kind === 'file') {
        saveBlob(outcome.blob, outcome.filename);
        toast(d.savedOne(outcome.filename));
      } else {
        toast(d.failed(outcome.denied.message || d.probeFailed));
      }
    } catch (err) {
      toast(d.failed(err instanceof Error ? err.message : String(err)));
    } finally {
      setWholeBusy(false);
    }
  };

  const downloadParts = async () => {
    if (!probe) {
      toast(d.checkFirst);
      return;
    }
    const windows = parsed?.windows || [];
    if (!windows.length) {
      toast(parseError ? d.partsInvalid(parseError) : d.noParts);
      return;
    }
    setRunning(true);
    setPartStates(windows.map((w) => ({
      key: `${w.index}`, label: w.label, start: w.start, end: w.end, status: 'pending' as const,
    })));

    const files: { name: string; blob: Blob }[] = [];
    let ok = 0;
    let blocked = 0;
    let failed = 0;

    for (let i = 0; i < windows.length; i += 1) {
      const w = windows[i];
      setProgress({ current: i + 1, total: windows.length });
      setPartStates((prev) => prev.map((p, idx) => (idx === i ? { ...p, status: 'working' } : p)));
      const result = await fetchPart(w, false);
      if (result.status === 'ok') {
        ok += 1;
        files.push({ name: result.name, blob: result.blob });
        setPartStates((prev) => prev.map((p, idx) => (idx === i ? { ...p, status: 'ok', filename: result.name } : p)));
      } else if (result.status === 'blocked') {
        // Refused partial: keep going — one blocked window must never cancel
        // the rest of the pack (same rule as the campaign page's bulk run).
        blocked += 1;
        setPartStates((prev) => prev.map((p, idx) => (idx === i ? { ...p, status: 'blocked', denied: result.denied } : p)));
      } else {
        failed += 1;
        setPartStates((prev) => prev.map((p, idx) => (idx === i ? { ...p, status: 'failed', error: result.error } : p)));
      }
    }

    setProgress(null);
    setRunning(false);

    if (ok === 1) {
      saveBlob(files[0].blob, files[0].name);
      toast(d.savedOne(files[0].name));
    } else if (ok > 1) {
      const archive = `${safeFilename(probe.title || 'download')}-${d.zipName}.zip`;
      const blob = await zipParts(files);
      saveBlob(blob, archive);
      toast(d.savedZip(ok, archive));
    } else if (blocked || failed) {
      toast(d.failed(`${blocked + failed} ${d.blockedBadge}`));
    }
  };

  /** Re-request ONE part (the row's Retry) — never the whole pack again. */
  const retryPart = async (part: PartState) => {
    if (!probe || running) return;
    setPartStates((prev) => prev.map((p) => (p.key === part.key ? { ...p, status: 'working', error: undefined } : p)));
    const result = await fetchPart(part, false);
    if (result.status === 'ok') {
      saveBlob(result.blob, result.name);
      setPartStates((prev) => prev.map((p) => (
        p.key === part.key ? { ...p, status: 'ok', filename: result.name, error: undefined } : p
      )));
      toast(d.savedOne(result.name));
    } else if (result.status === 'blocked') {
      setPartStates((prev) => prev.map((p) => (
        p.key === part.key ? { ...p, status: 'blocked', denied: result.denied, error: undefined } : p
      )));
    } else {
      setPartStates((prev) => prev.map((p) => (
        p.key === part.key ? { ...p, status: 'failed', error: result.error } : p
      )));
      toast(d.failed(result.error));
    }
  };

  /** "Whole file instead" for ONE blocked window: pull everything, cut locally. */
  const recoverBlocked = async (part: PartState) => {
    if (!probe || running) return;
    setPartStates((prev) => prev.map((p) => (p.key === part.key ? { ...p, status: 'working' } : p)));
    const result = await fetchPart(part, true);
    if (result.status === 'ok') {
      saveBlob(result.blob, result.name);
      setPartStates((prev) => prev.map((p) => (
        p.key === part.key ? { ...p, status: 'ok', filename: result.name, denied: undefined } : p
      )));
      toast(d.savedOne(result.name));
    } else if (result.status === 'blocked') {
      setPartStates((prev) => prev.map((p) => (
        p.key === part.key ? { ...p, status: 'blocked', denied: result.denied } : p
      )));
    } else {
      setPartStates((prev) => prev.map((p) => (
        p.key === part.key ? { ...p, status: 'failed', error: result.error } : p
      )));
      toast(d.failed(result.error));
    }
  };

  // ------------------------------------------------------------ render

  const blockedParts = partStates.filter((p) => p.status === 'blocked');
  const canDownloadParts = Boolean(probe && parsed && parsed.count > 0 && !running);
  const busy = running || wholeBusy;

  return (
    <section className="glass-panel" style={cardStyle} data-testid="downloader-page">
      <div>
        <h2 style={{ margin: 0, fontSize: '1.15rem' }}>⬇️ {d.title}</h2>
        <p style={{ margin: '0.35rem 0 0', fontSize: '0.84rem', color: 'var(--text-secondary)' }}>{d.subtitle}</p>
      </div>

      {/* ------------------------------------------------ link + probe */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.45rem' }}>
        <label htmlFor="downloader-url" style={{ fontSize: '0.82rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
          {d.urlLabel}
        </label>
        <div style={rowStyle}>
          <input
            id="downloader-url"
            type="text"
            className="form-input"
            style={{ flex: '1 1 320px' }}
            placeholder={d.urlPlaceholder}
            value={url}
            onChange={(event) => onUrlChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void runProbe(url);
              }
            }}
            disabled={busy}
          />
          <button
            type="button"
            className="glowing-btn"
            data-testid="downloader-check"
            onClick={() => void runProbe(url)}
            disabled={probing || busy || !url.trim()}
            style={{ padding: '0 1.3rem', height: '42px' }}
          >
            {probing ? d.checking : d.check}
          </button>
        </div>
        {url.trim() && (
          <div style={rowStyle}>
            <span style={badgeStyle('muted')}>{d.detected(platformLabel(probedHere ? probe!.platform : guessed))}</span>
            {!probedHere && guessed === 'other' && <span style={badgeStyle('warn')}>{d.unsupportedHost}</span>}
          </div>
        )}
        {probeError && (
          <p style={{ margin: 0, fontSize: '0.8rem', color: '#ff9d7a' }} data-testid="downloader-probe-error">
            {d.probeFailed} {probeError}
          </p>
        )}
      </div>

      {/* ------------------------------------------------ probe result */}
      {probedHere && probe && (
        <div
          data-testid="downloader-probe"
          style={{
            display: 'flex',
            gap: '0.85rem',
            padding: PAD,
            borderRadius: '12px',
            border: '1px solid var(--border-color)',
            background: 'rgba(255, 255, 255, 0.03)',
          }}
        >
          {probe.thumbnail && (
            <img
              src={probe.thumbnail}
              alt=""
              style={{ width: '128px', height: '72px', objectFit: 'cover', borderRadius: '9px', flex: '0 0 auto' }}
            />
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem', minWidth: 0 }}>
            <strong style={{ fontSize: '0.92rem' }}>{probe.title}</strong>
            <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
              {[probe.uploader, probe.duration_label].filter(Boolean).join(' · ')}
            </span>
            <div style={rowStyle}>
              <span style={badgeStyle(probe.partial_supported ? 'ok' : 'warn')}>
                {probe.partial_supported ? `✓ ${d.partialYes}` : d.partialNo}
              </span>
              {probe.hls && <span style={badgeStyle('muted')}>HLS</span>}
              {probe.dash && <span style={badgeStyle('muted')}>DASH</span>}
              {!probe.has_audio && <span style={badgeStyle('bad')}>{d.kindAudio}: ✕</span>}
            </div>
            {probe.is_live && <span style={badgeStyle('bad')}>{d.liveNotice}</span>}
            <span style={{ fontSize: '0.74rem', color: 'var(--text-secondary)' }}>{probe.note}</span>
          </div>
        </div>
      )}

      {/* ------------------------------------------------ options */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.7rem' }}>
        <strong style={{ fontSize: '0.88rem' }}>{d.formatTitle}</strong>

        <div style={rowStyle}>
          <button type="button" data-testid="downloader-kind-video" style={chipStyle(kind === 'video')}
                  onClick={() => setKind('video')} disabled={busy}>
            🎬 {d.kindVideo}
          </button>
          <button type="button" data-testid="downloader-kind-audio" style={chipStyle(kind === 'audio')}
                  onClick={() => setKind('audio')} disabled={busy}>
            🎵 {d.kindAudio}
          </button>
        </div>

        <div style={rowStyle}>
          <button type="button" data-testid="downloader-scope-full" style={chipStyle(scope === 'full')}
                  onClick={() => setScope('full')} disabled={busy}>
            {d.scopeFull}
          </button>
          <button type="button" data-testid="downloader-scope-parts" style={chipStyle(scope === 'parts')}
                  onClick={() => setScope('parts')} disabled={busy}>
            ✂️ {d.scopeParts}
          </button>
        </div>

        {kind === 'video' && (
          <div style={rowStyle}>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{d.qualityLabel}</span>
            <select
              className="form-input"
              data-testid="downloader-quality"
              style={{ maxWidth: '260px' }}
              value={quality}
              onChange={(event) => setQuality(event.target.value)}
              disabled={busy}
            >
              <option value="">{d.qualityBest}</option>
              {(probe?.qualities || []).map((q) => (
                <option key={q.height} value={String(q.height)}>
                  {q.label}{q.filesize ? ` · ${(q.filesize / 1e6).toFixed(1)} MB` : ''}
                </option>
              ))}
            </select>
          </div>
        )}

        {kind === 'audio' && (
          <div style={rowStyle}>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{d.audioFormatLabel}</span>
            <button type="button" data-testid="downloader-audio-m4a" style={chipStyle(audioFormat === 'm4a')}
                    onClick={() => setAudioFormat('m4a')} disabled={busy}>
              {d.audioM4a}
            </button>
            <button type="button" data-testid="downloader-audio-mp3" style={chipStyle(audioFormat === 'mp3')}
                    onClick={() => setAudioFormat('mp3')} disabled={busy}>
              {d.audioMp3}
            </button>
          </div>
        )}

        {scope === 'parts' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
            <label htmlFor="downloader-parts" style={{ fontSize: '0.82rem', fontWeight: 600, color: 'var(--text-secondary)' }}>
              {d.partsLabel}
            </label>
            <textarea
              id="downloader-parts"
              className="form-input"
              data-testid="downloader-parts"
              rows={4}
              placeholder={d.partsPlaceholder}
              value={parts}
              onChange={(event) => setParts(event.target.value)}
              disabled={busy}
              style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', resize: 'vertical' }}
            />
            <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{d.partsHint}</span>
            {kind === 'audio' && <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{d.audioNote}</span>}
            {parseError && (
              <span style={{ fontSize: '0.78rem', color: '#ff9d7a' }} data-testid="downloader-parts-error">
                {d.partsInvalid(parseError)}
              </span>
            )}
            {parsed && !parseError && (
              <span style={{ fontSize: '0.78rem', color: 'var(--secondary)' }} data-testid="downloader-parts-preview">
                {d.partsPreview(parsed.count, parsed.total_label)}
              </span>
            )}
          </div>
        )}

        <div style={rowStyle}>
          {scope === 'full' ? (
            <button
              type="button"
              className="glowing-btn"
              data-testid="downloader-submit"
              onClick={() => void downloadWhole()}
              disabled={busy || !probe}
              style={{ padding: '0 1.6rem', height: '44px' }}
            >
              {wholeBusy ? d.working : `⬇️ ${d.downloadWhole}`}
            </button>
          ) : (
            <button
              type="button"
              className="glowing-btn"
              data-testid="downloader-submit"
              onClick={() => void downloadParts()}
              disabled={busy || !canDownloadParts}
              style={{ padding: '0 1.6rem', height: '44px' }}
            >
              {running ? d.working : `✂️ ${d.downloadParts(parsed?.count || 0)}`}
            </button>
          )}
          {!probe && (
            <span style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>{d.checkFirst}</span>
          )}
        </div>

        {progress && (
          <span style={{ fontSize: '0.82rem', color: 'var(--secondary)' }} data-testid="downloader-progress">
            {d.progress(progress.current, progress.total)}
          </span>
        )}
      </div>

      {/* ------------------------------------------------ per-part status */}
      {partStates.length > 0 && (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: '0.4rem' }}
            data-testid="downloader-parts-list">
          {partStates.map((part) => (
            <li
              key={part.key}
              data-testid={`downloader-part-${part.status}`}
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: '0.5rem',
                alignItems: 'center',
                padding: '0.5rem 0.65rem',
                borderRadius: '10px',
                border: '1px solid var(--border-color)',
                background: 'rgba(255, 255, 255, 0.03)',
                fontSize: '0.8rem',
              }}
            >
              <strong style={{ minWidth: '110px', fontFamily: 'ui-monospace, monospace' }}>{part.label}</strong>
              <span style={badgeStyle(
                part.status === 'ok' ? 'ok'
                  : part.status === 'blocked' ? 'bad'
                    : part.status === 'failed' ? 'bad' : 'muted',
              )}>
                {part.status === 'ok' ? `✓ ${part.filename || ''}`
                  : part.status === 'blocked' ? d.blockedBadge
                    : part.status === 'failed' ? `✕ ${part.error || ''}`
                      : part.status === 'working' ? d.working : '…'}
              </span>
              {part.status === 'blocked' && part.denied && (
                <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                  {part.denied.message || part.denied.code}
                </span>
              )}
              <span style={{ marginLeft: 'auto', display: 'flex', gap: '0.4rem' }}>
                {(part.status === 'blocked' || part.status === 'failed') && (
                  <button
                    type="button"
                    className="glowing-btn"
                    style={{ padding: '0.35rem 0.7rem', fontSize: '0.75rem', boxShadow: 'none' }}
                    onClick={() => void recoverBlocked(part)}
                    disabled={busy}
                  >
                    {d.downloadWholeInstead}
                  </button>
                )}
                {(part.status === 'blocked' || part.status === 'failed') && (
                  <button
                    type="button"
                    style={{
                      padding: '0.35rem 0.7rem',
                      fontSize: '0.75rem',
                      borderRadius: '9px',
                      border: '1px solid var(--border-color)',
                      background: 'rgba(255, 255, 255, 0.04)',
                      color: 'var(--text-primary)',
                      cursor: 'pointer',
                    }}
                    onClick={() => void retryPart(part)}
                    disabled={busy}
                  >
                    {d.retry}
                  </button>
                )}
                <a
                  href={sourceLinkAtTime(probe?.url || url, part.start)}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ alignSelf: 'center', fontSize: '0.75rem', color: 'var(--secondary)' }}
                >
                  🔗 {d.openAtTime}
                </a>
              </span>
            </li>
          ))}
        </ul>
      )}

      {/* ------------------------------------------------ blocked panel */}
      {blockedParts.length > 0 && (
        <div
          data-testid="downloader-blocked-panel"
          style={{
            padding: PAD,
            borderRadius: '12px',
            border: '1px solid rgba(255, 94, 58, 0.35)',
            background: 'rgba(255, 94, 58, 0.08)',
            display: 'flex',
            flexDirection: 'column',
            gap: '0.45rem',
          }}
        >
          <strong style={{ fontSize: '0.88rem' }}>⚠️ {d.blockedTitle}</strong>
          <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>{d.blockedBody}</span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
            {blockedParts.map((part) => (
              <div key={part.key} style={{ display: 'flex', gap: '0.6rem', alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: '0.8rem' }}>{part.label}</span>
                {part.denied?.estSeconds ? (
                  <span style={badgeStyle('muted')}>{d.estimate(part.denied.estSeconds)}</span>
                ) : null}
                <button
                  type="button"
                  className="glowing-btn"
                  style={{ padding: '0.35rem 0.7rem', fontSize: '0.75rem', boxShadow: 'none' }}
                  onClick={() => void recoverBlocked(part)}
                  disabled={busy}
                >
                  {d.downloadWholeInstead}
                </button>
                <a
                  href={sourceLinkAtTime(probe?.url || url, part.start)}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ fontSize: '0.75rem', color: 'var(--secondary)' }}
                >
                  🔗 {d.openAtTime}
                </a>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
