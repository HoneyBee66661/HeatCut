import { useEffect, useRef } from 'react';
import { useLanguage } from '../locales';

export type AppView = 'studio' | 'campaign' | 'downloader';

interface AppDrawerProps {
  open: boolean;
  view: AppView;
  onSelect: (view: AppView) => void;
  onClose: () => void;
}

/**
 * Slide-in navigation drawer (hamburger). Holds the app's screens — the studio,
 * campaign prep and the video downloader — so the header stays a header and new
 * pages cost one entry here instead of another button next to Support.
 */
export default function AppDrawer({ open, view, onSelect, onClose }: AppDrawerProps) {
  const { t } = useLanguage();
  const d = t.drawer;
  const closeRef = useRef<HTMLButtonElement | null>(null);

  // ESC closes; the body must not scroll behind the panel while it is open.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  const items: { id: AppView; icon: string; label: string; hint: string }[] = [
    { id: 'studio', icon: '⚡', label: d.studio, hint: d.studioHint },
    { id: 'campaign', icon: '🎯', label: d.campaign, hint: d.campaignHint },
    { id: 'downloader', icon: '⬇️', label: d.downloader, hint: d.downloaderHint },
  ];

  return (
    <div
      role="presentation"
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 3000,
        background: 'rgba(8, 6, 4, 0.62)',
        backdropFilter: 'blur(2px)',
        animation: 'hc-fade-in 0.16s ease-out',
      }}
    >
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={d.title}
        onClick={(event) => event.stopPropagation()}
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          bottom: 0,
          width: 'min(320px, 86vw)',
          display: 'flex',
          flexDirection: 'column',
          gap: '0.35rem',
          padding: '1.1rem 1rem 1.25rem',
          background: 'linear-gradient(180deg, #241c15 0%, #1a1410 100%)',
          borderRight: '1px solid var(--border-color)',
          boxShadow: '0 24px 60px rgba(0, 0, 0, 0.55)',
          overflowY: 'auto',
          animation: 'hc-drawer-in 0.2s ease-out',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.5rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.55rem' }}>
            <span style={{ fontSize: '1.5rem' }}>⚡</span>
            <strong style={{ fontSize: '1rem', letterSpacing: '0.04em' }}>HEATCUT</strong>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label={d.close}
            title={d.close}
            style={{
              width: '34px',
              height: '34px',
              borderRadius: '9px',
              border: '1px solid var(--border-color)',
              background: 'rgba(255, 255, 255, 0.04)',
              color: 'var(--text-primary)',
              fontSize: '1.05rem',
              cursor: 'pointer',
            }}
          >
            ✕
          </button>
        </div>

        <span
          style={{
            margin: '0.9rem 0 0.35rem',
            fontSize: '0.7rem',
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            color: 'var(--text-secondary)',
          }}
        >
          {d.title}
        </span>

        <nav style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
          {items.map((item) => {
            const active = view === item.id;
            return (
              <button
                key={item.id}
                type="button"
                data-testid={`drawer-${item.id}`}
                aria-current={active ? 'page' : undefined}
                onClick={() => {
                  onSelect(item.id);
                  onClose();
                }}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: '0.7rem',
                  width: '100%',
                  textAlign: 'left',
                  padding: '0.7rem 0.75rem',
                  borderRadius: '11px',
                  border: `1px solid ${active ? 'rgba(255, 94, 58, 0.5)' : 'var(--border-color)'}`,
                  background: active ? 'rgba(255, 94, 58, 0.16)' : 'rgba(255, 255, 255, 0.03)',
                  color: 'var(--text-primary)',
                  cursor: 'pointer',
                }}
              >
                <span style={{ fontSize: '1.15rem', lineHeight: 1.2 }}>{item.icon}</span>
                <span style={{ display: 'flex', flexDirection: 'column', gap: '0.15rem' }}>
                  <span style={{ fontSize: '0.92rem', fontWeight: 600 }}>
                    {item.label}
                    {active && (
                      <span style={{ marginLeft: '0.45rem', fontSize: '0.66rem', color: 'var(--secondary)' }}>
                        ● {d.active}
                      </span>
                    )}
                  </span>
                  <span style={{ fontSize: '0.74rem', color: 'var(--text-secondary)', lineHeight: 1.35 }}>
                    {item.hint}
                  </span>
                </span>
              </button>
            );
          })}
        </nav>

        <div style={{ marginTop: 'auto', paddingTop: '1rem' }}>
          <a
            href="https://tako.id/johansa"
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: 'block',
              textAlign: 'center',
              padding: '0.6rem 0.75rem',
              borderRadius: '10px',
              textDecoration: 'none',
              fontSize: '0.82rem',
              color: 'var(--text-primary)',
              background: 'linear-gradient(135deg, var(--secondary) 0%, #f43f5e 100%)',
            }}
          >
            🐈‍⬛ {d.support}
          </a>
        </div>
      </aside>
    </div>
  );
}
