"""Creative consultant for clipping campaigns — stdlib only, deterministic.

WHY THIS EXISTS
---------------
Spade briefs often ship WITHOUT concrete source videos (some campaigns only
describe the product, the platforms, the rules and the payout). The prep planner
then has nothing to cut. But the BRIEF ITSELF is data: niches, description,
platform list, min/max clip length, hard rules, banned claims, existing hashtags
& mentions, payout rate, per-account post cap.

This module turns that into what a creative/marketing team would hand to an
editor: content angles, per-idea title (A/B), caption, hook line and hashtag mix,
the digital-marketing levers worth optimizing (hook window, retention, posting
cadence, sound, cover frame, CTA, KPI targets), an A/B plan per idea and a
compliance checklist that quotes the campaign's own rules.

No LLM is required — the deterministic board is complete on its own, which keeps
the feature usable with a client-side key (or none). `backend/main.py` can layer
an optional LLM polish on top (`POST /api/campaign/strategy` with
`generate_copy=True`), the same best-effort pattern as the campaign copy pass.

Language: templates exist in English and Indonesian; `language` picks the set so
the deterministic output matches the UI the user is reading.
"""

import re
from typing import Dict, Iterable, List, Optional, Tuple

CREATIVE_VERSION = 1

# ---------------------------------------------------------------- text helpers

STOPWORDS = {
    # english
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "our",
    "their", "they", "have", "has", "are", "was", "were", "will", "must", "not",
    "any", "all", "can", "should", "post", "posts", "video", "videos", "clip",
    "clips", "content", "please", "make", "sure", "using", "use", "into", "over",
    "about", "than", "then", "them", "also", "only", "more", "most", "some",
    # indonesian
    "yang", "dan", "untuk", "dengan", "dari", "tidak", "harus", "bisa", "ada",
    "ini", "itu", "kamu", "anda", "kita", "akan", "juga", "saja", "wajib",
    "konten", "klip", "posting", "gunakan", "menggunakan", "dalam",
    "pada", "agar", "supaya", "atau", "jika", "kalau", "bikin", "buat",
    # campaign boilerplate
    "https", "http", "www", "com", "youtube", "tiktok", "instagram", "reels",
    "shorts", "campaign", "kampanye", "creator", "creators", "clipper", "clippers",
    "brand", "brands", "product", "products", "produk", "bisnis", "business",
    "client", "clients", "klien", "viewer", "viewers", "penonton", "audience",
    "hashtag", "hashtags", "mention", "mentions", "logo", "logos", "second",
    "seconds", "detik", "duration", "durasi", "minimum", "maximum", "habit",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]{2,}")


def _lang(language: Optional[str]) -> str:
    return "id" if str(language or "").lower().startswith("id") else "en"


def _count_words(text: str, counts: Dict[str, int], weight: int = 1,
                 exclude: Iterable[str] = ()) -> None:
    excluded = {str(x).lower() for x in exclude}
    for w in WORD_RE.findall(text or ""):
        low = w.lower()
        if low in STOPWORDS or low in excluded or len(low) < 4:
            continue
        counts[low] = counts.get(low, 0) + weight


def _rank(counts: Dict[str, int], limit: int) -> List[str]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def keywords(text: str, limit: int = 10) -> List[str]:
    """Frequency-ranked topic words, stopwords and platform noise removed."""
    counts: Dict[str, int] = {}
    _count_words(text, counts, 1)
    return _rank(counts, limit)


def _title_case(value: str) -> str:
    return " ".join(part[:1].upper() + part[1:] for part in str(value or "").split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 100:
        return f"${value:,.0f}"
    return f"${value:,.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- angle library
# Each angle carries BOTH languages. {topic} / {brand} / {audience} are filled in
# per idea; `why` explains the mechanic, `retention` the watch-time device.

ANGLES: List[dict] = [
    {
        "key": "problem_solution",
        "en": {
            "label": "Problem → payoff",
            "hook": "If your {topic} keeps failing, this is why.",
            "titles": ["{topic} Keeps Failing? Do This", "The Fix For {topic}", "{brand}: {topic} Done Right"],
            "caption": "The mistake almost everyone makes with {topic} — and the 10-second fix. Saves this one.",
            "cta": "Save it for the next time {topic} goes wrong.",
            "why": "A named pain in the first two seconds makes the viewer self-identify; the payoff answers the question the hook opened.",
            "retention": "Curiosity gap in 0-2s, payoff delivered at ~60% of the clip.",
        },
        "id": {
            "label": "Masalah → solusi",
            "hook": "Kalau {topic} kamu sering gagal, ini penyebabnya.",
            "titles": ["{topic} Sering Gagal? Coba Ini", "Solusi {topic} yang Jarang Dibahas", "{brand}: {topic} yang Benar"],
            "caption": "Kesalahan paling umum soal {topic} — dan solusinya cuma 10 detik. Simpan dulu.",
            "cta": "Simpan buat pas {topic} bermasalah lagi.",
            "why": "Masalah yang disebut jelas di 2 detik pertama bikin penonton merasa 'ini gue'; solusinya menutup rasa penasaran itu.",
            "retention": "Celah penasaran di 0-2 detik, jawaban di ~60% durasi.",
        },
    },
    {
        "key": "myth_fact",
        "en": {
            "label": "Myth vs fact",
            "hook": "Stop believing this about {topic}.",
            "titles": ["The {topic} Myth That Costs You", "Nobody Told You This About {topic}", "{topic}: Myth Or Fact?"],
            "caption": "Everyone repeats this about {topic}. Here is what actually happens. 👇",
            "cta": "Follow for the next myth, debunked in 20 seconds.",
            "why": "Contradicting a belief the audience already holds forces a stop-and-verify reaction; comments arrive as people defend or agree.",
            "retention": "Direct contradiction in 0-1s makes the viewer stay to be proven wrong.",
        },
        "id": {
            "label": "Mitos vs fakta",
            "hook": "Berhenti percaya ini soal {topic}.",
            "titles": ["Mitos {topic} yang Bikin Rugi", "Fakta {topic} yang Jarang Dibahas", "{topic}: Mitos atau Fakta?"],
            "caption": "Semua orang ngulang mitos {topic} ini. Faktanya beda. 👇",
            "cta": "Follow buat mitos berikutnya, dibahas 20 detik.",
            "why": "Membantah keyakinan yang sudah dipegang memaksa orang berhenti dan verifikasi; komentar datang dari yang setuju maupun tidak.",
            "retention": "Bantahan langsung di 0-1 detik bikin penonton nunggu pembuktian.",
        },
    },
    {
        "key": "before_after",
        "en": {
            "label": "Before / after proof",
            "hook": "This is what {topic} looked like before.",
            "titles": ["30 Days Of {topic}: The Difference", "Before And After {topic}", "What {topic} Actually Changes"],
            "caption": "Same effort, different result — the difference with {topic}. Which one are you?",
            "cta": "Comment which side you are on today.",
            "why": "Visual proof of change answers 'does this even work?' without arguing; the comparison frame is a natural loop point.",
            "retention": "Reveal at ~50%, then the loop back to the 'before' frame for rewatches.",
        },
        "id": {
            "label": "Sebelum / sesudah",
            "hook": "Segini dulu hasilnya sebelum pakai {topic}.",
            "titles": ["30 Hari Pakai {topic}: Bedanya", "Sebelum vs Sesudah {topic}", "{topic} Beneran Ngubah Apa?"],
            "caption": "Usaha sama, hasil beda — gara-gara {topic}. Kamu tim mana?",
            "cta": "Komen kamu ada di sisi mana hari ini.",
            "why": "Bukti visual perubahan menjawab 'ini beneran ngefek?' tanpa debat; frame perbandingan jadi titik loop alami.",
            "retention": "Reveal di ~50%, lalu balik ke frame 'sebelum' untuk rewatch.",
        },
    },
    {
        "key": "listicle",
        "en": {
            "label": "Listicle (3 quick wins)",
            "hook": "Three things about {topic} that take one minute each.",
            "titles": ["3 {topic} Wins In 60 Seconds", "{topic}: 3 Things To Try Today", "Quick {topic} Wins"],
            "caption": "1️⃣ 2️⃣ 3️⃣ — do them in order. The third one is the one people skip.",
            "cta": "Which one are you trying first?",
            "why": "A numbered promise sets an explicit finish line, and the 'third one' tease keeps people past the halfway drop-off.",
            "retention": "Numbered structure + hold-back on the last item.",
        },
        "id": {
            "label": "Listikel (3 hal cepat)",
            "hook": "Tiga hal soal {topic} yang cuma butuh satu menit.",
            "titles": ["3 Trik {topic} Dalam 60 Detik", "{topic}: 3 Hal Yang Bisa Dicoba", "{topic} Cepat & Praktis"],
            "caption": "1️⃣ 2️⃣ 3️⃣ — kerjakan berurutan. Yang ketiga paling sering dilewatkan.",
            "cta": "Mau coba yang mana dulu?",
            "why": "Janji bernomor memberi garis akhir yang jelas; teaser 'yang ketiga' menahan penonton melewati titik drop-off.",
            "retention": "Struktur bernomor + tahan item terakhir.",
        },
    },
    {
        "key": "behind_scenes",
        "en": {
            "label": "Behind the scenes",
            "hook": "What actually happens behind {topic}.",
            "titles": ["Inside {topic}: The Real Process", "How {topic} Is Really Done", "{brand} Behind The Scenes"],
            "caption": "The part of {topic} nobody films. Raw, unpolished, honest.",
            "cta": "Ask me anything about this in the comments.",
            "why": "Process content feels exclusive and low-polish, which reads as authentic — and authenticity lifts comment and share rates.",
            "retention": "Open on the least expected step, not the start of the process.",
        },
        "id": {
            "label": "Behind the scenes",
            "hook": "Ini yang sebenarnya terjadi di balik {topic}.",
            "titles": ["Isi Dapur {topic}", "Proses {topic} yang Sebenarnya", "{brand} Di Balik Layar"],
            "caption": "Bagian {topic} yang gak pernah difilmkan. Apa adanya.",
            "cta": "Tanya apa saja soal ini di komentar.",
            "why": "Konten proses terasa eksklusif dan apa adanya — itu menaikkan komentar dan share.",
            "retention": "Buka dari langkah yang paling tak terduga, bukan dari awal.",
        },
    },
    {
        "key": "tutorial",
        "en": {
            "label": "How-to in one take",
            "hook": "Here is exactly how to do {topic}.",
            "titles": ["{topic} In One Take", "Do {topic} Like This", "{topic}: Step By Step, Fast"],
            "caption": "No filler, no intro. Save it before you need it.",
            "cta": "Save now, thank yourself later.",
            "why": "Utility content earns saves, and saves are the strongest signal the platform's ranking uses for non-follower reach.",
            "retention": "Start mid-action; the 'no intro' promise keeps the first 3 seconds tight.",
        },
        "id": {
            "label": "Tutorial satu take",
            "hook": "Begini cara tepat melakukan {topic}.",
            "titles": ["{topic} Dalam Satu Take", "Cara {topic} Yang Benar", "{topic}: Langkah Cepat"],
            "caption": "Tanpa basa-basi. Simpan sebelum butuh.",
            "cta": "Simpan sekarang, berguna nanti.",
            "why": "Konten berguna memancing save, dan save adalah sinyal terkuat untuk jangkauan non-follower.",
            "retention": "Mulai langsung di tengah aksi; janji 'tanpa intro' menjaga 3 detik pertama.",
        },
    },
    {
        "key": "social_proof",
        "en": {
            "label": "Result & testimonial",
            "hook": "This is what people say after trying {topic}.",
            "titles": ["Real Results With {topic}", "They Tried {topic} — Here Is What Happened", "{topic}: The Feedback"],
            "caption": "Straight from the people who tried it. No script, just outcomes.",
            "cta": "Tag someone who needs to see this.",
            "why": "Third-party proof lowers skepticism; tagging behaviour multiplies reach without paid spend.",
            "retention": "Front-load the strongest quote, then the numbers.",
        },
        "id": {
            "label": "Hasil & testimoni",
            "hook": "Ini kata mereka setelah coba {topic}.",
            "titles": ["Hasil Nyata Pakai {topic}", "Mereka Coba {topic} — Ini Jadinya", "{topic}: Kata Pengguna"],
            "caption": "Langsung dari yang sudah coba. Tanpa script, cuma hasil.",
            "cta": "Tag orang yang perlu lihat ini.",
            "why": "Bukti pihak ketiga menurunkan keraguan; perilaku tagging memperbesar jangkauan tanpa biaya iklan.",
            "retention": "Taruh kutipan terkuat di depan, angka menyusul.",
        },
    },
    {
        "key": "hot_take",
        "en": {
            "label": "Hot take / reaction",
            "hook": "Unpopular opinion about {topic}.",
            "titles": ["Unpopular {topic} Opinion", "Am I Wrong About {topic}?", "The {topic} Take Nobody Likes"],
            "caption": "Say it with me: {topic} does not work the way people think. Tell me I am wrong.",
            "cta": "Disagree? The comments are open.",
            "why": "A defensible opinion invites disagreement; comment volume and reply threads are cheap engagement the algorithm rewards.",
            "retention": "State the take, then immediately show the evidence so it does not read as rage bait.",
        },
        "id": {
            "label": "Opini / reaksi",
            "hook": "Opini tidak populer soal {topic}.",
            "titles": ["Opini {topic} Yang Tidak Populer", "Salah Gak Sih Soal {topic}?", "Pandangan {topic} Yang Dihindari"],
            "caption": "Jujur: {topic} gak jalan seperti yang orang pikir. Bilang kalau gue salah.",
            "cta": "Gak setuju? Komentar terbuka.",
            "why": "Opini yang bisa dibantah mengundang diskusi; volume komentar adalah engagement murah yang disukai algoritma.",
            "retention": "Sebut opininya, langsung tunjukkan buktinya biar gak terkesan rage bait.",
        },
    },
]

PLATFORM_WINDOWS = {
    "tiktok": ["11:30-13:00", "19:00-22:00"],
    "instagram reels": ["12:00-13:00", "19:00-21:00"],
    "youtube shorts": ["07:00-09:00", "17:00-20:00"],
    "facebook reels": ["08:00-10:00", "20:00-22:00"],
    "snapchat": ["15:00-18:00", "20:00-23:00"],
    "x": ["09:00-11:00", "18:00-20:00"],
}


# ---------------------------------------------------------------- inputs

def _platforms(spec: dict) -> List[str]:
    req = spec.get("requirements") or {}
    platforms = [str(p) for p in (req.get("platforms") or []) if p]
    return platforms or ["TikTok", "Instagram Reels", "YouTube Shorts"]


def _duration_bounds(spec: dict) -> Tuple[float, float]:
    req = spec.get("requirements") or {}
    low = req.get("min_duration_sec")
    high = req.get("max_duration_sec")
    low = float(low) if low else 15.0
    high = float(high) if high else 60.0
    if high < low:
        high = low
    return low, high


def _angle_lang(angle: dict, language: str) -> dict:
    return angle.get(language) or angle.get("en") or {}


def _fill(template: str, topic: str, brand: str, audience: str) -> str:
    return (template or "").replace("{topic}", topic).replace("{brand}", brand).replace("{audience}", audience)


def _prompt_conflicts(prompt: str, negative_rules: Iterable[str]) -> List[str]:
    """Banned ideas the user's own prompt asks for (e.g. prompt says 'giveaway'
    while the campaign bans promotions). Shown as a warning so the copy never
    ships against the rules."""
    conflicts: List[str] = []
    low = (prompt or "").lower()
    if not low:
        return conflicts
    for rule in negative_rules or []:
        rule_low = str(rule).lower()
        # the meaningful token of the rule: 'no giveaway posts' -> 'giveaway'
        tokens = [t for t in re.findall(r"[a-z]{4,}", rule_low)
                  if t not in ("posts", "post", "content", "clip", "clips", "video", "videos",
                               "create", "creating", "never", "avoid", "must", "your", "with",
                               "also", "they", "them", "that", "this", "from")]
        for token in tokens:
            if token in low and rule not in conflicts:
                conflicts.append(rule)
                break
    return conflicts


# ---------------------------------------------------------------- the board

def build_strategy(
    spec: dict,
    prompt: str = "",
    language: str = "en",
    tone: str = "auto",
    niche: Optional[str] = None,
    audience: Optional[str] = None,
    platform_focus: Optional[List[str]] = None,
    idea_count: int = 10,
) -> dict:
    """Deterministic creative board for a campaign brief. No network, no LLM."""
    lang = _lang(language)
    req = spec.get("requirements") or {}
    brand = (spec.get("public_name") or spec.get("name") or "the brand").strip()
    niches = [str(n) for n in (spec.get("niches") or []) if n]
    platforms = [str(p) for p in (platform_focus or []) if p] or _platforms(spec)
    min_dur, max_dur = _duration_bounds(spec)
    target_dur = _clamp(min_dur + (max_dur - min_dur) * 0.35, min_dur, max_dur)
    aspect = req.get("aspect_ratio") or ("9:16" if any(
        p.lower() in ("tiktok", "instagram reels", "youtube shorts", "facebook reels", "snapchat")
        for p in platforms) else "9:16")

    # Topic seeds: the brief's OWN vocabulary weighs most; the user's prompt is
    # counted last and with its banned asks removed (a campaign that bans
    # giveaways must not end up with "giveaway" as a topic).
    banned_tokens = {
        t for rule in (req.get("negative_rules") or [])
        for t in re.findall(r"[a-z]{4,}", str(rule).lower())
    }
    counts: Dict[str, int] = {}
    _count_words(str(spec.get("public_name") or ""), counts, 3)
    _count_words(str(spec.get("name") or ""), counts, 3)
    _count_words(" ".join(niches), counts, 3)
    _count_words(str(spec.get("description") or ""), counts, 2)
    _count_words(str(spec.get("plain_details") or "")[:4000], counts, 1)
    _count_words(niche or "", counts, 3)
    _count_words(audience or "", counts, 1)
    _count_words(prompt or "", counts, 1, exclude=banned_tokens)
    topics = _rank(counts, 10)
    if not topics:
        topics = [niches[0] if niches else brand]
    audience_text = (
        (audience or "").strip()
        or (f"people following {'/'.join(niches[:3])}" if niches else "short-form viewers in this category")
    )

    # ---- idea board -------------------------------------------------------
    broad = ["#fyp", "#foryou"] if any(p.lower() == "tiktok" for p in platforms) else ["#reels", "#shorts"]
    ideas: List[dict] = []
    count = int(_clamp(int(idea_count or 10), 4, 20))
    for i in range(count):
        angle = ANGLES[i % len(ANGLES)]
        copy_lang = _angle_lang(angle, lang)
        raw_topic = topics[i % len(topics)]
        topic = _title_case(raw_topic) if lang == "en" else raw_topic
        filled_titles = [_fill(t, topic, brand, audience_text) for t in copy_lang.get("titles", [])]
        filled_titles = [t[:1].upper() + t[1:] if t else t for t in filled_titles]
        title = filled_titles[0] if filled_titles else f"{topic}"
        title_alt = filled_titles[1] if len(filled_titles) > 1 else f"{title} (A/B: shorter cut)"
        hook = _fill(copy_lang.get("hook", ""), topic, brand, audience_text)
        hook = hook[:1].upper() + hook[1:] if hook else hook
        caption_core = _fill(copy_lang.get("caption", ""), topic, brand, audience_text)
        cta = _fill(copy_lang.get("cta", ""), topic, brand, audience_text)

        # hashtags: campaign's own first (they are already lowercase), then the
        # topic, then the niche, then generic reach tags for the platform set.
        tags: List[str] = []
        for tag in (req.get("hashtags") or [])[:3]:
            tags.append(str(tag).lower())
        tags.append("#" + _slug(raw_topic))
        for n in niches[:2]:
            slug = _slug(n)
            if slug:
                tags.append("#" + slug)
        tags.extend(broad)
        seen: List[str] = []
        for t in tags:
            if t and t not in seen and len(t) > 2:
                seen.append(t)
        hashtags = seen[:8]

        mentions = " ".join((req.get("mentions") or [])[:2])
        caption = " ".join(x for x in [caption_core, cta, mentions, " ".join(hashtags[:5])] if x).strip()

        ideas.append({
            "id": f"idea-{i + 1}",
            "angle": copy_lang.get("label") or angle["key"],
            "angle_key": angle["key"],
            "topic": topic,
            "platform": platforms[i % len(platforms)],
            "aspect_ratio": aspect,
            "duration_sec": round(target_dur, 1),
            "hook": hook,
            "title": title,
            "title_alt": title_alt,
            "caption": caption,
            "hashtags": hashtags,
            "why_it_works": copy_lang.get("why", ""),
            "retention_device": copy_lang.get("retention", ""),
            "cta": cta,
            "shot_list": [
                _fill("0-2s: state the hook about {topic} straight to camera, no intro", topic, brand, audience_text),
                _fill("2-6s: show the proof/demo of {topic} (screen, product, or hands-on)", topic, brand, audience_text),
                _fill("6-{d}s: payoff + one-line CTA".replace("{d}", str(int(target_dur))), topic, brand, audience_text),
            ],
            "text_overlay": [
                _fill("{topic}", topic, brand, audience_text),
                cta,
            ],
        })

    conflicts = _prompt_conflicts(prompt, req.get("negative_rules") or [])

    # ---- digital-marketing levers ----------------------------------------
    rate = spec.get("rate_per_1k")
    payout_line = (
        f"Each qualifying post pays about {_fmt_usd(rate)} per 1,000 views "
        f"({_fmt_usd(spec.get('rate_per_100k'))} per 100k)."
        if rate else "No payout rate published in the brief."
    )
    marketing = {
        "positioning": {
            "brand": brand,
            "audience": audience_text,
            "niches": niches[:5],
            "angle_mix": [a["key"] for a in ANGLES[:min(len(ANGLES), max(3, count // 2))]],
            "tone": tone if tone and tone != "auto" else ("casual, spoken, fast-paced" if lang == "id" else "casual, spoken, fast-paced"),
        },
        "hook_window_sec": 2,
        "retention_target_pct": 65,
        "loopability": "End on the same frame the clip opens with so the replay reads as a loop (rewatch is the cheapest retention signal).",
        "caption_rules": [
            "First line under 90 characters — it is the only line visible before 'more'.",
            "3-4 short lines, one idea each; emojis only as separators.",
            "CTA on the last line, one action only (save / comment / tag).",
        ],
        "hashtag_mix": {
            "branded": [str(h).lower() for h in (req.get("hashtags") or [])[:3]],
            "niche": ["#" + _slug(n) for n in niches[:3] if _slug(n)],
            "broad": broad,
            "rule": "3 niche + 2 broad + campaign tags; never 15+ tags, it dilutes the topic signal.",
        },
        "posting": {
            "per_account_limit": spec.get("max_posts_per_user"),
            "cadence": (
                f"max {spec.get('max_posts_per_user')} posts per account — split them across days, not hours;"
                " a post that flops still consumes a slot"
            ) if spec.get("max_posts_per_user") else "1 post per day per platform, 2 on the strongest platform",
            "best_windows": {p.lower(): PLATFORM_WINDOWS.get(p.lower(), ["12:00-13:00", "19:00-21:00"]) for p in platforms},
            "batch_rule": "Prepare 3 variants per idea before posting; test the opening 2 seconds first.",
        },
        "sound": "Trending audio under 30% volume, voice-forward mix; keep the first beat aligned with the hook frame.",
        "cover_frame": "Pick a frame with a readable face or product + overlay text of the title; the cover decides the non-follower tap.",
        "text_overlay": "3-5 words on screen at the hook, then one overlay per beat — never paragraphs.",
        "kpi": {
            "payout_basis": payout_line,
            "min_clip_views": spec.get("minimum_clip_views"),
            "min_account_views": spec.get("minimum_views"),
            "target_watch_through_pct": 65,
            "target_completion_pct": 40,
            "saves_per_1k_views": 8,
            "share_rate_pct": 1.5,
            "comment_rate_pct": 0.8,
        },
        "ab_test": {
            "variable": "opening 2 seconds (question hook vs result-first) and the cover frame",
            "variants": 2,
            "metric": "watch-through % over the first 500 views",
            "window": "48 hours",
            "decision": "Keep the winner's pattern for the next 3 posts, then re-test.",
        },
        "optimization_loop": [
            "Post 1: baseline — publish the strongest angle, log views/watch-through at 24h.",
            "Post 2: change ONLY the hook (same topic, other angle from the board).",
            "Post 3: change ONLY the cover frame + caption first line.",
            "Day 4: compare watch-through, keep the winning pattern, drop the losing angle.",
            "Day 5-7: scale the winner into the remaining allowed slots per account.",
        ],
        "asset_guidance": [
            "The brief ships no source videos: shoot product/B-roll yourself, reuse campaign-provided assets, or license stock — never lift other creators' clips.",
            "Screen recordings of the product/app count as original footage and are the fastest asset to produce.",
            "Keep a 3-clip buffer ready so a day is never skipped because the edit is not finished.",
        ],
    }

    compliance = {
        "rules": [str(r) for r in (req.get("rules") or [])],
        "do_not": [str(r) for r in (req.get("negative_rules") or [])],
        "checklist": [
            "Every claim in the copy is visible in the footage (show it, do not only say it).",
            "Clip length inside the campaign window " + f"({int(min_dur)}s-{int(max_dur)}s).",
            "Aspect ratio " + str(aspect) + ", no platform watermark from another app.",
            "Caption/hashtag set matches the campaign tags; no competitor tags.",
            "Paid-partnership or disclosure requirement applied if the brief asks for it.",
            "No banned claim from the Do-Not list, in speech or on-screen text.",
        ],
        "prompt_conflicts": conflicts,
    }

    board = {
        "version": CREATIVE_VERSION,
        "source": "rules",
        "campaign_id": spec.get("campaign_id"),
        "campaign_name": spec.get("public_name") or spec.get("name"),
        "has_timed_sources": any((s.get("timestamps") or []) for s in (spec.get("sources") or [])),
        "topics": topics,
        "prompt": (prompt or "").strip(),
        "language": lang,
        "ideas": ideas,
        "marketing": marketing,
        "compliance": compliance,
        "notes": [],
    }
    if not board["has_timed_sources"]:
        board["notes"].append(
            "This campaign brief provides no timestamped source videos, so the board is "
            "asset-free: it describes WHAT to shoot/hook, not which second to cut."
        )
    if conflicts:
        board["notes"].append(
            f"Your brief prompt conflicts with {len(conflicts)} campaign rule(s) — see compliance.prompt_conflicts."
        )
    board["strategy_md"] = build_strategy_md(board, spec)
    return board


def build_strategy_md(board: dict, spec: Optional[dict] = None) -> str:
    """Markdown hand-off for the pack (STRATEGY.md)."""
    spec = spec or {}
    m = board.get("marketing") or {}
    pos = m.get("positioning") or {}
    lines: List[str] = []
    lines.append(f"# Creative strategy — {board.get('campaign_name') or 'campaign'}")
    lines.append("")
    lines.append(f"- Campaign id: `{board.get('campaign_id')}`")
    lines.append(f"- Generated by: {board.get('source')} engine v{board.get('version')}"
                 + (f" ({board.get('model')})" if board.get("model") else ""))
    lines.append(f"- Timestamped sources in brief: {'yes' if board.get('has_timed_sources') else 'no'}")
    if board.get("prompt"):
        lines.append(f"- Creative direction given: {board['prompt']}")
    lines.append("")

    if board.get("notes"):
        lines.append("## Notes")
        for n in board["notes"]:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("## Positioning")
    lines.append("")
    lines.append(f"- Brand: **{pos.get('brand')}**")
    lines.append(f"- Audience: {pos.get('audience')}")
    if pos.get("niches"):
        lines.append(f"- Niches: {', '.join(pos['niches'])}")
    if pos.get("angle_mix"):
        lines.append(f"- Angle mix: {', '.join(pos['angle_mix'])}")
    if board.get("topics"):
        lines.append(f"- Topic seeds mined from the brief: {', '.join(board['topics'][:8])}")
    lines.append("")

    lines.append("## Idea board")
    lines.append("")
    for idea in board.get("ideas") or []:
        lines.append(f"### {idea['id']} · {idea['angle']} · {idea['platform']}")
        lines.append("")
        lines.append(f"- **Hook (first 2s):** {idea['hook']}")
        lines.append(f"- **Title A:** {idea['title']}")
        lines.append(f"- **Title B (A/B):** {idea['title_alt']}")
        lines.append(f"- **Caption:** {idea['caption']}")
        lines.append(f"- **Hashtags:** {' '.join(idea['hashtags'])}")
        lines.append(f"- **Format:** {idea['aspect_ratio']}, ~{idea['duration_sec']}s")
        lines.append(f"- **Why it works:** {idea['why_it_works']}")
        lines.append(f"- **Retention device:** {idea['retention_device']}")
        lines.append("- **Shot list:**")
        for shot in idea.get("shot_list") or []:
            lines.append(f"  - {shot}")
        lines.append("")

    lines.append("## Digital-marketing parameters to optimize")
    lines.append("")
    for key, label in (
        ("hook_window_sec", "Hook window (seconds)"),
        ("retention_target_pct", "Target watch-through (%)"),
        ("loopability", "Loopability"),
        ("sound", "Sound"),
        ("cover_frame", "Cover frame"),
        ("text_overlay", "On-screen text"),
    ):
        lines.append(f"- **{label}:** {m.get(key)}")
    lines.append("")
    lines.append("**Caption rules**")
    for r in m.get("caption_rules") or []:
        lines.append(f"- {r}")
    lines.append("")
    mix = m.get("hashtag_mix") or {}
    lines.append("**Hashtag mix**")
    lines.append(f"- Branded: {' '.join(mix.get('branded') or []) or '—'}")
    lines.append(f"- Niche: {' '.join(mix.get('niche') or []) or '—'}")
    lines.append(f"- Broad: {' '.join(mix.get('broad') or []) or '—'}")
    lines.append(f"- Rule: {mix.get('rule')}")
    lines.append("")
    posting = m.get("posting") or {}
    lines.append("**Posting plan**")
    lines.append(f"- {posting.get('cadence')}")
    lines.append(f"- {posting.get('batch_rule')}")
    for platform, windows in (posting.get("best_windows") or {}).items():
        lines.append(f"- {platform}: {', '.join(windows)}")
    lines.append("")
    kpi = m.get("kpi") or {}
    lines.append("**KPI targets**")
    lines.append(f"- {kpi.get('payout_basis')}")
    for key, label in (
        ("min_clip_views", "Minimum views for a qualifying clip"),
        ("min_account_views", "Minimum views per account"),
        ("target_watch_through_pct", "Watch-through target (%)"),
        ("target_completion_pct", "Completion target (%)"),
        ("saves_per_1k_views", "Saves per 1k views"),
        ("share_rate_pct", "Share rate (%)"),
        ("comment_rate_pct", "Comment rate (%)"),
    ):
        if kpi.get(key) is not None:
            lines.append(f"- {label}: {kpi.get(key)}")
    lines.append("")
    ab = m.get("ab_test") or {}
    lines.append("**A/B plan**")
    lines.append(f"- Variable: {ab.get('variable')}")
    lines.append(f"- Variants: {ab.get('variants')} · Metric: {ab.get('metric')} · Window: {ab.get('window')}")
    lines.append(f"- Decision rule: {ab.get('decision')}")
    lines.append("")
    lines.append("**Optimization loop**")
    for step in m.get("optimization_loop") or []:
        lines.append(f"- {step}")
    lines.append("")
    lines.append("**Asset guidance (no source videos in this brief)**")
    for a in m.get("asset_guidance") or []:
        lines.append(f"- {a}")
    lines.append("")

    comp = board.get("compliance") or {}
    lines.append("## Compliance")
    lines.append("")
    lines.append("**Campaign rules**")
    for r in comp.get("rules") or []:
        lines.append(f"- {r}")
    if not comp.get("rules"):
        lines.append("- (none published)")
    lines.append("")
    lines.append("**Do not**")
    for r in comp.get("do_not") or []:
        lines.append(f"- {r}")
    if not comp.get("do_not"):
        lines.append("- (none published)")
    lines.append("")
    lines.append("**Checklist**")
    for r in comp.get("checklist") or []:
        lines.append(f"- [ ] {r}")
    if comp.get("prompt_conflicts"):
        lines.append("")
        lines.append("**Prompt conflicts (fix before shooting)**")
        for r in comp["prompt_conflicts"]:
            lines.append(f"- ⚠️ your prompt contradicts: {r}")
    lines.append("")
    return "\n".join(lines)


def strategy_prompt(board: dict, spec: dict, language: str = "en") -> str:
    """Prompt for the OPTIONAL LLM polish pass: keep the structure, upgrade the words."""
    lang_name = "Bahasa Indonesia" if _lang(language) == "id" else "English"
    idea_lines = "\n".join(
        f"{i['id']}|{i['angle_key']}|{i['topic']}|{i['platform']}|{i['duration_sec']}s"
        for i in board.get("ideas") or []
    )
    req = spec.get("requirements") or {}
    rules = "\n".join(f"- {r}" for r in (req.get("rules") or [])[:12]) or "- (none published)"
    donts = "\n".join(f"- {r}" for r in (req.get("negative_rules") or [])[:10]) or "- (none published)"
    return (
        "You are the creative director on a PAID short-form clipping campaign.\n"
        f"Write in {lang_name}. Never use first person (no 'I', 'me', 'my', 'we', 'saya'); "
        "frame the brand, the product or the viewer.\n\n"
        f"Campaign: {spec.get('public_name') or spec.get('name')}\n"
        f"Description: {str(spec.get('description') or '')[:600]}\n"
        f"Niches: {', '.join(spec.get('niches') or [])}\n"
        f"Platforms: {', '.join(_platforms(spec))}\n"
        f"Clip window: {int(_duration_bounds(spec)[0])}s-{int(_duration_bounds(spec)[1])}s, "
        f"aspect {req.get('aspect_ratio') or '9:16'}\n"
        f"Campaign rules:\n{rules}\nBanned:\n{donts}\n\n"
        f"Brief note (no timestamped source videos): the concepts must be shootable as original footage.\n"
        f"User's creative direction: {board.get('prompt') or '(none — surprise me)'}\n"
        f"Audience: {(board.get('marketing') or {}).get('positioning', {}).get('audience')}\n\n"
        "Idea slots (id|angle|topic|platform|target length):\n"
        f"{idea_lines}\n\n"
        "For EVERY idea return: a scroll-stopping hook line for the first 2 seconds, a title under "
        "9 words, one A/B alternative title, a 2-3 line caption ending in ONE call to action, and "
        "3-6 lowercase hashtags (campaign tags included, no banned ones). Also improve the audience "
        "line and add 4 concrete growth tactics for this campaign.\n"
        'Return ONLY JSON: {"audience": "...", "tactics": ["..."], '
        '"ideas": [{"id": "idea-1", "hook": "...", "title": "...", "title_alt": "...", '
        '"caption": "...", "hashtags": "#a #b"}]}'
    )


def apply_llm_strategy(board: dict, parsed: dict, model: str) -> int:
    """Merge an LLM answer into the deterministic board. Returns ideas touched."""
    applied = 0
    ideas = {i["id"]: i for i in board.get("ideas") or []}
    for entry in parsed.get("ideas") or []:
        if not isinstance(entry, dict):
            continue
        target = ideas.get(str(entry.get("id")))
        if target is None:
            continue
        for key, max_len in (("hook", 220), ("title", 120), ("title_alt", 120), ("caption", 600)):
            if entry.get(key):
                target[key] = str(entry[key]).strip()[:max_len]
        if entry.get("hashtags"):
            tags = [t for t in re.split(r"\s+", str(entry["hashtags"]).strip()) if t.startswith("#")]
            if tags:
                target["hashtags"] = [t.lower() for t in tags][:8]
        target["caption"] = _rebuild_caption(target, board)
        applied += 1

    audience = str(parsed.get("audience") or "").strip()
    if audience:
        board.setdefault("marketing", {}).setdefault("positioning", {})["audience"] = audience[:300]
    tactics = [str(t).strip()[:300] for t in (parsed.get("tactics") or []) if str(t).strip()]
    if tactics:
        board["marketing"]["llm_tactics"] = tactics[:8]
    if applied or audience or tactics:
        board["source"] = "rules+llm"
        board["model"] = model
    board["strategy_md"] = build_strategy_md(board)
    return applied


def _rebuild_caption(idea: dict, board: dict) -> str:
    """Re-attach the campaign mention/hashtag tail after the LLM rewrote the copy."""
    caption = str(idea.get("caption") or "").strip()
    tail: List[str] = []
    if board.get("campaign_id"):
        mentions = ((board.get("marketing") or {}).get("hashtag_mix") or {}).get("branded") or []
        tail = [m for m in mentions[:2]]
    hashtags = " ".join((idea.get("hashtags") or [])[:5])
    return " ".join(x for x in [caption, " ".join(tail), hashtags] if x).strip()
