# UPSTREAM AUDIT — galihjuansaputra/cheat-clip vs fork HoneyBee66661/HeatCut

Audited 2026-09-25. Upstream HEAD = `fb9e605` (2026-09-22 "feat: add option for copy timestamp").
Compare API: upstream master vs our master = diverged, **we are 47 ahead / 11 behind**
(most of the 11 "behind" commits are a history rewrite on upstream — same features, new SHAs).

Upstream endpoints: `/api/health`, `/api/supadata-usage`, `/api/video-title`, `/api/models`, `/api/analyze`.
No `/api/export` at all — upstream hands off by *copying timestamps only*.

Locale key census: upstream `en.ts` = 188 keys, fork = 385 keys.
Upstream-only user-visible keys = 25 (Supadata quota panel 17, copy-scope "marked" 8, `errors.tryAgain`).

---

## A. UPSTREAM-ONLY — code we genuinely do not have

### A1. Seven-tier transcript pipeline (backend/main.py `fetch_transcript`, L860–1110)
Tiers in order: 1 Supadata → 2 youtube-transcript-api **via proxy** (direct lang → list manual+auto → **translate any track to id/en**) → 3 same via **CLI subprocess** → 4 yt-dlp native via proxy → 5–7 the same three without proxy.
Remarks: biggest single upstream feature. Our fork has 4 strategies (direct fetch → list → yt-dlp → supadata) and **no proxy path, no subprocess isolation, no translation fallback**.
Supporting pieces: `get_youtube_transcript_proxy_config()` (WebshareProxyConfig / GenericProxyConfig), `create_http_client()` (browser-like headers + shared cookie jar), `fetch_transcript_cli()` (`python -m youtube_transcript_api --format json` in a subprocess).
Failure UX: after all tiers it raises one HTTP 400 whose `detail` lists **every method tried, the probable cause** (TranscriptsDisabled / AgeRestricted / VideoUnavailable / IpBlocked) **and 3 recommended fixes** — vs our one-line "No subtitles could be retrieved".
Env: `WEBSHARE_USERNAME/PASSWORD/LOCATIONS/RETRIES`, `WEBSHARE_PROXY`, `PROXY_URL` (documented in upstream `backend/.env.template`).

### A2. Supadata quota monitor — `GET /api/supadata-usage` (L1184) + `check_single_supadata_key` / `get_supadata_usage_data` (L696–805)
Per-key parallel check of `https://api.supadata.ai/v1/me`, 30 s cache, masked key, `max/used/remaining credits`, plan, status active|exhausted|error.
Remarks: **endpoint is orphaned today** — the 17 `form.supadata*` locale keys still exist but upstream's current `App.tsx` never renders the panel (grep: zero `supadata` hits). Our `fetch_transcript_supadata` rotates keys blind; `/api/health` only reports `supadata_keys_count`.

### A3. `GET /api/video-title?video_id=` — oEmbed title resolver (L497 `get_youtube_oembed_title`, L1189)
`youtube.com/oembed` first, then an HTML `<title>` scrape; App.tsx uses it to asynchronously backfill placeholder titles in history (L355).
Remarks: cheap (no yt-dlp extract, no bot-check exposure) — our history/campaign-session titles come from full scrapes.

### A4. Copy-timestamps SCOPE: All | Marked only (App.tsx L1015–1160, L2562–2810)
Scope toggle (All / 🔖 Marked n) + greyed-out formats when 0 marked + marked-specific toasts, menu opens from BOTH the toolbar and the results-overview button (`copyTimestampMenuTarget: 'toolbar' | 'overview'`).
Remarks: we have the marked-clips state and a marked filter/sort, but `handleCopyAllTimestampsFormat` (App.tsx L1151) always copies `result.clips`.

### A5. Third-person title rule in the prompt
`ViralClip.title_suggestion` field description: "Catchy alternative title suggestion in third-person (no 'I'/'me'/'my'/'saya', frame around speaker or topic)" (L69/L80). Same rule applies to our local `ViralClipGemini` schema + titling pass.

### A6. `/live/` URL regex (L260–263)
`extract_video_id` accepts `v= | /v/ | embed/ | shorts/ | live/ | youtu.be/ | watch?v=` in one pattern.
Remarks: small robustness win for live/stream URLs pasted into the studio or the downloader page.

---

## B. PARITY — upstream features we already have (no action)

- Retention heatmap + `HeatmapTimeline.tsx` visualisation.
- Gemini analysis of heat + dialogue → viral clip candidates, SSE streaming progress.
- `/api/models` dynamic listing (flash-first, newest-first, junk-model filter) + automatic fallback chain across all flash models + "Free Tier Resilience" tip + actionable API-key/quota guidance panel when all models fail.
- Clip customisation: 15/30/60 s, target clip count, free-text focus prompt, custom analysis range (MM:SS / HH:MM:SS / raw seconds).
- Manual subtitle upload (.srt/.txt) + the Vercel/downsub.com hint.
- History + search across titles/links/quotes/clips, remove-one, confirm-clear-all, local caching.
- Copy timestamps in 3 formats (only / with title / YouTube chapters), per-clip copy title, caption, timestamp, details, Copy All (MD).
- Bilingual EN/ID with a type-checked `Translations` interface.
- Client-supplied Gemini key in localStorage (`mock` = demo mode), in-app player with clip auto-loop.

## C. FORK-ONLY — we are well ahead (upstream has nothing to compare)

Export (`/api/export`) + DASH fragment-range miner → HLS segment ladder → 409 + ExportFallbackPanel;
zip bundle pack; loopback heatmap worker + Cloudflare tunnel + token auth; analysis modes auto/podcast/concert,
heatmap-peak mining, heatmap-peak titling pass; multi-provider LLM (openai / anthropic / openai-compatible);
campaign prep page (Spade brief parser, deterministic windows, AI copy pass, manual-cut fallback);
caption text + SRT per window with local faster-whisper fallback; campaign saved sessions;
drawer navigation + standalone video/audio downloader (whole file / timestamp windows / audio-only);
A/V-sync-correct miner anchoring; export-tmp liveness cleaner.

---

## D. SUGGESTED ADAPTATIONS (ranked value / effort)

| # | Feature to port | Effort | Why it pays off here |
|---|---|---|---|
| 1 | Copy-scope **Marked only** (A4) — ✅ **PORTED, PR #20** (`feat/copy-marked-scope-oembed-live-urls`) | XS–S | State (`markedClips`) + filter already exist; only `handleCopyAllTimestampsFormat` + menu UI + 8 locale keys. Direct workflow win: mark the winners, paste only those into CapCut/YouTube. |
| 2 | `/api/video-title` oEmbed (A3) — ✅ **PORTED, PR #20** | XS | One 20-line helper + one route; kills a full yt-dlp scrape on history/session title backfill; also lets the downloader page show a title before a probe. |
| 3 | Third-person title rule (A5) — ✅ **PORTED, PR #20** | XS | One-line prompt/schema change in the Gemini schema + titling pass prompt → copy reads less "I did this", more broadcast-friendly. |
| 4 | `/live/` URL regex (A6) — ✅ **PORTED, PR #20** | XS | Fold into our `extract_video_id`; live/stream links currently fall out early. |
| 5 | Transcript **translate fallback** to id/en (part of A1) | S–M | We already use youtube-transcript-api v1.2 (`api.list()`); adds `.translate()` for tracks with no id/en variant → far better coverage on non-EN/non-ID sources in campaign + downloader flows. No proxy account required. |
| 6 | Transcript **tier diagnostics** (part of A1) | S–M | Our hard-fail is a one-liner; the "methods attempted + probable cause + 3 fixes" 400 body shortens every "why no captions?" debug round (and feeds the existing UI error panel verbatim). |
| 7 | Proxy tiers (Webshare/Generic) (A1) | M | Only needed if we ever run analysis on a cloud/datacenter host; on this VPS the residential-ish path + cookies already work, so treat it as the "cloud deploy" prerequisite, not a daily need. |
| 8 | Supadata quota card (A2) | M | Our campaign batch can silently exhaust the 100/mo free keys mid-run; a keys-connected/ready/depleted badge + Refresh button makes the ceiling visible. Backend route is ~60 lines of stdlib `requests`. |

### Porting notes
- A4/A5/A6 touch `src/App.tsx` + `backend/main.py`: patch surgically, keep `src/locales/{en,id}.ts` key-identical (`tsc -b` gate).
- A1/A2 additions must stay stdlib-only in `backend/downloader.py`-style purity if they are to be reused by `heatcut_worker.py` (the worker carries its own copy of these helpers) — but the transcript ladder itself exists only server-side today; the worker's `/transcript/window` is Whisper-only, so no mirror is needed.
- Upstream's transcript ladder assumes youtube-transcript-api ≥1.2 (`proxy_config=`, `api.list()`, `t.information`); we are on the same major, so the tier code can be lifted with the `_shared_cookie_jar` / `create_http_client` helpers.
- Do NOT port upstream's history rewriting / re-init commits; our lineage already contains their functionality plus 225 extra locale keys.
