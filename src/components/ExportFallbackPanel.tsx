import { useEffect, useRef, useState } from 'react';
import { useLanguage } from '../locales';
import { requestRawClip, safeFilename } from '../lib/rawExport';
import type { RawClipDenied } from '../lib/rawExport';

// Shown when the automatic (partial) export is refused by YouTube.
//
// Not an error dialog: the clip can still be produced, it just costs the whole
// video download, so the user decides — download the source and cut it by hand,
// or let HeatCut take the whole video while a countdown shows the wait. The
// countdown is the estimated remaining time; the heavy export starts on click,
// so the user never waits twice.

export type ExportFallbackTarget = {
  videoId: string;
  startTime: number;
  endTime: number;
  title: string;
  denied: RawClipDenied;
};

type Props = {
  target: ExportFallbackTarget;
  onClose: () => void;
  /** Receives the finished clip so the caller saves it with its own naming. */
  onClip: (blob: Blob, filename: string) => void;
};

export function ExportFallbackPanel({ target, onClose, onClip }: Props) {
  const { t } = useLanguage();
  const f = t.exportFallback;
  // The caller keys this component per window, so the initial state below is
  // already the right one for a newly opened panel — no reset effect needed.
  const [remaining, setRemaining] = useState(target.denied.estSeconds);
  const [phase, setPhase] = useState<'idle' | 'running' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    abortRef.current?.abort();
  }, []);

  const startFullExport = async () => {
    if (phase === 'running') return;
    setPhase('running');
    setError(null);
    setRemaining(target.denied.estSeconds);
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    timerRef.current = window.setInterval(() => {
      setRemaining(prev => (prev > 0 ? prev - 1 : 0));
    }, 1000);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const result = await requestRawClip(
        target.videoId, target.startTime, target.endTime, target.title,
        { mode: 'full', signal: controller.signal },
      );
      if (result.kind === 'denied') {
        // Should not happen in mode=full, but never dead-end the user.
        setError(result.denied.message || f.failedFallback);
        setPhase('error');
        return;
      }
      const filename = `heatcut_${safeFilename(target.title)}_${Math.floor(target.startTime)}-${Math.floor(target.endTime)}s.mp4`;
      onClip(result.blob, filename);
      onClose();
    } catch (err) {
      if (controller.signal.aborted) {
        setPhase('idle');
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
      setPhase('error');
    } finally {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      abortRef.current = null;
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setPhase('idle');
  };

  const reason =
    target.denied.code === 'yt_bot_check' ? f.reasonBotCheck
      : target.denied.code === 'yt_partial_blocked' ? f.reasonPartialBlocked
        : f.reasonUnknown;

  const running = phase === 'running';
  const btn = (disabled = false): React.CSSProperties => ({
    padding: '0.65rem 1.1rem',
    borderRadius: 10,
    border: '1px solid var(--border-color)',
    background: 'rgba(255, 255, 255, 0.05)',
    color: 'var(--text-primary)',
    fontFamily: 'inherit',
    fontWeight: 600,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.55 : 1,
  });

  return (
    <div
      role="dialog"
      aria-modal="true"
      style={{
        position: 'fixed', inset: 0, zIndex: 60,
        background: 'rgba(6, 8, 15, 0.72)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '1rem',
      }}
    >
      <div className="glass-panel" style={{ maxWidth: 560, display: 'flex', flexDirection: 'column', gap: '0.9rem' }}>
        <div style={{ fontWeight: 700, fontSize: '1.05rem' }}>{f.title}</div>
        <div style={{ color: 'var(--text-secondary, #b9c0d4)', lineHeight: 1.5 }}>{reason}</div>

        {running && (
          <div style={{ fontSize: '0.95rem', color: '#ffb3a3' }}>
            {remaining > 0 ? f.processingIn(remaining) : f.finishing}
          </div>
        )}
        {error && (
          <div style={{ fontSize: '0.9rem', color: '#ff9b8a', wordBreak: 'break-word' }}>
            {f.failed(error)}
          </div>
        )}

        <div style={{ display: 'flex', gap: '0.6rem', flexWrap: 'wrap' }}>
          <button
            type="button"
            style={btn()}
            onClick={() => window.open(target.denied.sourceUrl, '_blank', 'noopener')}
          >
            ⬇ {f.downloadSource}
          </button>
          <button
            type="button"
            style={{ ...btn(running), background: 'rgba(255, 94, 58, 0.16)', borderColor: 'rgba(255, 94, 58, 0.35)' }}
            onClick={startFullExport}
            disabled={running}
          >
            {running
              ? (remaining > 0 ? f.processingInShort(remaining) : f.finishing)
              : f.processFull(target.denied.estSeconds)}
          </button>
          {running ? (
            <button type="button" style={btn()} onClick={cancel}>{f.cancel}</button>
          ) : (
            <button type="button" style={btn()} onClick={onClose}>{f.close}</button>
          )}
        </div>
      </div>
    </div>
  );
}

export default ExportFallbackPanel;
