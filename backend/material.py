"""Material finder — WHAT to cut, WHERE to get it, and HOW, in plain language.

WHY THIS EXISTS
---------------
The creative consultant (`backend/creative.py`) answers "what should I post?".
That is only half the job for an average clipper: they still have to guess WHERE
the footage comes from, and a concept they cannot shoot is a concept they never
post. This module answers the other half.

Input: the campaign (its own subject/product/song) + optional creative direction
+ optional LYRICS. Output, all deterministic (no network, no LLM, no key):

- `beats[]`  the content browsed BY LYRICS: every meaningful lyric line becomes a
  beat with the plain-language "essence" of the line, a mood, a visual motif
  (rain on a window, city at night, a chase) translated into what to actually
  put on screen, the cut rhythm to use, and ready-to-click YouTube searches.
- `lyric_map[]`  the full lyric sheet with each line tagged by the beat it feeds
  (so the clipper reads down the song and sees where each clip lands).
- `searches[]`  every query with a real `youtube.com/results?search_query=` URL.
  The route can then upgrade them to ACTUAL video results (live search).
- `asset_plan[]` how many clips of what kind are needed.
- `steps[]`  a numbered, non-technical, time-boxed execution recipe for someone
  who has never run a campaign.
- `watch_outs[]` licensing + campaign-rule warnings in plain words.

Language: templates exist in English and Indonesian; `language` picks the set.
`build_material_md()` is the hand-off (MATERIAL.md).
"""

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

MATERIAL_VERSION = 1


def _lang(language: Optional[str]) -> str:
    return "id" if str(language or "").lower().startswith("id") else "en"


def _pick(entry: dict, lang: str) -> str:
    return str(entry.get(lang) or entry.get("en") or "").strip()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def search_url(query: str) -> str:
    """A YouTube search deep link — always valid, never 404s, needs no API key."""
    return "https://www.youtube.com/results?search_query=" + quote_plus(str(query or "").strip())


def watch_url(video_id: str, start: Optional[float] = None) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    if start and start > 0:
        return f"{base}&t={int(start)}s"
    return base


# ---------------------------------------------------------------- mood + pacing
# A mood decides the CUT RHYTHM and the emotional colour of the edit. `pace` is
# seconds per shot: the single number an average editor actually needs.

MOODS: List[dict] = [
    {"key": "auto", "en": "Match the song", "id": "Ikut lagunya", "pace": 1.6},
    {"key": "hype", "en": "Hype / high energy", "id": "Hype / energik", "pace": 0.8},
    {"key": "sad", "en": "Sad / emotional", "id": "Sedih / emosional", "pace": 3.2},
    {"key": "romantic", "en": "Romantic", "id": "Romantis", "pace": 2.6},
    {"key": "nostalgic", "en": "Nostalgic", "id": "Nostalgia", "pace": 2.4},
    {"key": "bold", "en": "Bold / statement", "id": "Tegas / statement", "pace": 1.1},
    {"key": "chill", "en": "Chill / lo-fi", "id": "Santai / lo-fi", "pace": 3.6},
]

MOOD_BY_KEY: Dict[str, dict] = {m["key"]: m for m in MOODS}

_FAST = "fast, hard cuts on the beat"
_SLOW = "slow, let each shot breathe"
_PACE_WORDS = {
    "en": {0.8: _FAST, 1.1: _FAST, 1.6: "medium cuts on the beat", 2.4: "unhurried cuts", 2.6: "unhurried cuts", 3.2: _SLOW, 3.6: _SLOW},
    "id": {0.8: "cepat, potong tepat di beat", 1.1: "cepat, potong tepat di beat", 1.6: "sedang, potong di beat",
           2.4: "potongan santai", 2.6: "potongan santai", 3.2: "lambat, biarkan tiap shot bernafas",
           3.6: "lambat, biarkan tiap shot bernafas"},
}

# Words that reveal the mood of a line when no motif matches.
MOOD_WORDS: Dict[str, List[str]] = {
    "hype": ["run", "running", "jump", "fire", "fight", "win", "wins", "top", "loud", "power",
             "burn", "rise", "gaspol", "berlari", "menang", "bakar", "atas", "tonight"],
    "sad": ["cry", "crying", "tears", "lost", "alone", "goodbye", "hurt", "broken", "empty", "rain",
            "sorry", "why", "fall", "fade", "menangis", "hilang", "pergi", "sakit", "hancur", "sepi",
            "maaf", "rindu", "tuhan"],
    "romantic": ["love", "kiss", "heart", "baby", "stay", "forever", "hold", "close",
                 "cinta", "sayang", "peluk", "selamanya", "hati"],
    "nostalgic": ["remember", "memory", "memories", "back", "used", "young", "time", "times", "photo",
                  "kenangan", "dulu", "masa", "foto", "waktu", "kecil"],
    "bold": ["never", "enough", "stop", "money", "boss", "king", "proof", "mad", "nyali",
             "berani", "cukup", "jangan", "diam", "sendiri"],
    "chill": ["slow", "easy", "float", "smoke", "night", "drive", "chill", "dream", "santai",
              "pelan", "malam", "jalan", "tidur"],
}

_MOOD_RE = {mood: re.compile("|".join(rf"(?<![a-z]){re.escape(w)}(?![a-z])" for w in words))
            for mood, words in MOOD_WORDS.items()}

# ---------------------------------------------------------------- visual motifs
# Each motif is a SHOT IDEA, not a keyword list: the average user needs to know
# what to put on screen. `q` are the YouTube query seeds (English works best for
# stock/film footage; `qid` rides along for local-language material).

MOTIFS: List[dict] = [
    {"key": "rain", "mood": "sad",
     "words": ["rain", "hujan", "storm", "badai", "tears", "tear", "cry", "crying", "nangis", "menangis", "wet"],
     "en": "rain running down a window / someone standing in the rain",
     "id": "air hujan menetes di kaca jendela / seseorang berdiri di bawah hujan",
     "q": ["rain on window cinematic", "standing in the rain movie scene"],
     "qid": ["hujan sedih sinematik"]},
    {"key": "fire", "mood": "hype",
     "words": ["fire", "flame", "burn", "burning", "smoke", "bakar", "api", "hot", "ignite", "ash", "abu"],
     "en": "fire, sparks or smoke rising in slow motion",
     "id": "api, percikan atau asap naik dalam gerakan lambat",
     "q": ["fire sparks slow motion", "burning smoke cinematic"],
     "qid": ["api asap sinematik"]},
    {"key": "city_night", "mood": "chill",
     "words": ["city", "night", "lights", "street", "neon", "downtown", "kota", "malam", "lampu", "jalan", "jakarta"],
     "en": "city at night — neon, traffic trails, a lit skyline",
     "id": "kota malam hari — neon, lampu lalu lintas, gedung bertingkat",
     "q": ["city at night neon cinematic b-roll", "tokyo night street walk"],
     "qid": ["kota malam indonesia b-roll"]},
    {"key": "drive", "mood": "nostalgic",
     "words": ["drive", "driving", "road", "highway", "car", "wheels", "jalan", "mobil", "motor", "perjalanan", "jauh"],
     "en": "driving at night, window down, headlights on the road",
     "id": "menyetir malam-malam, jendela terbuka, lampu menyorot jalan",
     "q": ["driving at night cinematic pov", "car lights highway b-roll"],
     "qid": ["perjalanan malam mobil sinematik"]},
    {"key": "ocean", "mood": "chill",
     "words": ["ocean", "sea", "wave", "waves", "water", "beach", "shore", "laut", "pantai", "ombak", "air"],
     "en": "waves hitting the shore, wide ocean horizon",
     "id": "ombak menghantam pantai, garis laut lepas",
     "q": ["ocean waves cinematic b-roll", "beach aerial drone"],
     "qid": ["pantai ombak sinematik"]},
    {"key": "stars", "mood": "nostalgic",
     "words": ["star", "stars", "sky", "moon", "universe", "dream", "bintang", "langit", "bulan", "mimpi", "terang"],
     "en": "night sky, stars, slow drift over the horizon",
     "id": "langit malam, bintang, gerakan lambat di atas horizon",
     "q": ["night sky stars timelapse", "moon cinematic b-roll"],
     "qid": ["langit malam bintang sinematik"]},
    {"key": "dance", "mood": "hype",
     "words": ["dance", "dancing", "move", "beat", "party", "club", "menari", "dansa", "pesta", "goyang"],
     "en": "dancing bodies, moving crowd, club lighting",
     "id": "orang menari, kerumunan bergerak, lampu klub",
     "q": ["dance crowd party cinematic", "dancing silhouette club lights"],
     "qid": ["menari pesta lampu klub"]},
    {"key": "fight", "mood": "bold",
     "words": ["fight", "war", "battle", "hit", "punch", "strong", "enemy", "law", "lawan", "berkelahi", "perang", "kuat"],
     "en": "a fight scene / someone training, sweat and impact",
     "id": "adegan perkelahian / seseorang berlatih, keringat dan benturan",
     "q": ["fight scene cinematic movie", "boxing training slow motion"],
     "qid": ["adegan berkelahi film"]},
    {"key": "kiss", "mood": "romantic",
     "words": ["kiss", "kissed", "lips", "touch", "hold", "hand", "cinta", "cium", "peluk", "sayang", "hati"],
     "en": "a near-kiss, hands touching, two people close",
     "id": "hampir berciuman, tangan bersentuhan, dua orang berdekatan",
     "q": ["romantic movie scene kiss", "couple cinematic slow motion"],
     "qid": ["adegan romantis film"]},
    {"key": "mirror", "mood": "bold",
     "words": ["mirror", "mask", "cermin", "wajah", "diri", "topeng", "reflection"],
     "en": "a face in a mirror, half-lit, staring back",
     "id": "wajah di cermin, setengah cahaya, menatap balik",
     "q": ["mirror reflection cinematic portrait", "moody portrait lighting"],
     "qid": ["cermin potret sinematik"]},
    {"key": "phone", "mood": "sad",
     "words": ["phone", "call", "message", "text", "miss", "unread", "telepon", "pesan", "rindu", "telfon", "wa"],
     "en": "an unread message on a glowing phone screen in the dark",
     "id": "pesan belum dibaca di layar HP yang menyala dalam gelap",
     "q": ["phone screen dark room cinematic", "text message typing close up"],
     "qid": ["hp layar gelap sinematik"]},
    {"key": "running", "mood": "hype",
     "words": ["run", "running", "chase", "escape", "faster", "berlari", "kejar", "lari", "cepat", "kabur"],
     "en": "someone running — a chase through streets or forest",
     "id": "seseorang berlari — dikejar di jalan atau hutan",
     "q": ["running chase scene cinematic", "running in slow motion street"],
     "qid": ["adegan berlari sinematik"]},
    {"key": "sunrise", "mood": "nostalgic",
     "words": ["sun", "sunrise", "morning", "light", "hope", "new", "matahari", "pagi", "cahaya", "harapan", "baru"],
     "en": "sunrise breaking over a horizon, lens flare",
     "id": "matahari terbit di horizon, kilau lensa",
     "q": ["sunrise timelapse cinematic", "golden hour lens flare b-roll"],
     "qid": ["matahari terbit sinematik"]},
    {"key": "empty_room", "mood": "sad",
     "words": ["empty", "alone", "room", "home", "left", "gone", "kosong", "sendiri", "rumah", "pergi", "sepi"],
     "en": "an empty room, a chair by a window, nobody there",
     "id": "ruangan kosong, kursi di dekat jendela, tak ada orang",
     "q": ["empty room cinematic sad", "abandoned house interior moody"],
     "qid": ["ruangan kosong sedih sinematik"]},
    {"key": "crowd", "mood": "bold",
     "words": ["crowd", "people", "everyone", "orang", "kerumunan", "semua"],
     "en": "a crowd moving in time-lapse while one person stands still",
     "id": "kerumunan bergerak cepat sementara satu orang diam",
     "q": ["crowd timelapse people walking", "lone person standing in crowd"],
     "qid": ["kerumunan orang timelapse"]},
    {"key": "money", "mood": "bold",
     "words": ["money", "cash", "paid", "rich", "gold", "diamond", "duit", "uang", "kaya", "emas", "harga"],
     "en": "cash, gold or luxury detail shots, hard light",
     "id": "tumpukan uang, emas, atau detail barang mewah, cahaya keras",
     "q": ["money cash cinematic close up", "luxury gold detail b-roll"],
     "qid": ["uang tunai sinematik"]},
    {"key": "memory", "mood": "nostalgic",
     "words": ["remember", "memory", "memories", "photo", "old", "child", "young", "kenangan", "foto", "dulu", "kecil", "masa"],
     "en": "old photos, a projector, faded home video look",
     "id": "foto lama, proyektor, tampilan video rumah yang pudar",
     "q": ["old photos memories cinematic", "vintage film grain super 8"],
     "qid": ["kenangan foto lama sinematik"]},
    {"key": "cold", "mood": "sad",
     "words": ["cold", "winter", "snow", "freeze", "ice", "dingin", "salju", "beku", "sejuk"],
     "en": "breath fogging in cold air, snow falling",
     "id": "uap nafas di udara dingin, salju turun",
     "q": ["snow falling cinematic b-roll", "cold breath winter moody"],
     "qid": ["salju turun sinematik"]},
    {"key": "party", "mood": "hype",
     "words": ["party", "bottle", "celebration", "toast", "pesta", "minum", "selebrasi", "ramai"],
     "en": "friends celebrating, bottles, sparklers, confetti",
     "id": "teman-teman selebrasi, botol, kembang api, confetti",
     "q": ["party celebration cinematic slow motion", "confetti sparkler friends"],
     "qid": ["pesta selebrasi teman"]},
    {"key": "silence", "mood": "chill",
     "words": ["quiet", "silent", "breathe", "peace", "diam", "tenang", "hening", "pelan", "damai"],
     "en": "one still wide shot — field, roof, or rooftop at dusk",
     "id": "satu shot lebar diam — lapangan, atap, atau rooftop saat senja",
     "q": ["quiet wide shot field dusk", "rooftop dusk cinematic"],
     "qid": ["pemandangan tenang senja"]},
    {"key": "prayer", "mood": "sad",
     "words": ["god", "pray", "prayer", "faith", "soul", "heaven", "tuhan", "doa", "berdoa", "iman", "surga", "nyawa"],
     "en": "hands together, a candle, light through a window",
     "id": "tangan menengadah, lilin, cahaya masuk lewat jendela",
     "q": ["candle light cinematic praying hands", "church light window moody"],
     "qid": ["doa cahaya lilin sinematik"]},
    {"key": "stage", "mood": "hype",
     "words": ["stage", "concert", "microphone", "sing", "audience", "panggung", "konser", "nyanyi", "suara", "mic"],
     "en": "stage lights, a mic, silhouettes of a singing crowd",
     "id": "lampu panggung, mic, siluet penonton bernyanyi",
     "q": ["concert stage lights silhouette", "singer microphone live crowd"],
     "qid": ["konser panggung lampu"]},
]

MOTIF_BY_KEY: Dict[str, dict] = {m["key"]: m for m in MOTIFS}
_MOTIF_INDEX: List[Tuple[str, dict]] = [(w, m) for m in MOTIFS for w in m["words"]]

GENERIC_MOTIFS = ["city_night", "crowd", "sunrise", "drive"]
assert all(k in MOTIF_BY_KEY for k in GENERIC_MOTIFS), "GENERIC_MOTIFS must name real motifs"

# When a lyric line names no motif, or two lines name the SAME motif, the pack
# rotates through a style-appropriate pool — two beats in a row must never get
# the identical shot idea.
GENERIC_BY_STYLE: Dict[str, List[str]] = {
    "movie_edit": ["city_night", "crowd", "drive", "rain", "sunrise"],
    "lyric_video": ["stars", "ocean", "city_night", "rain", "sunrise"],
    "story": ["city_night", "drive", "crowd", "silence", "sunrise"],
    "product_demo": ["sunrise", "mirror", "city_night", "crowd", "silence"],
    "broll": ["city_night", "sunrise", "ocean", "drive", "crowd"],
}
for _style_key, _pool in GENERIC_BY_STYLE.items():  # import-time sanity check
    assert all(k in MOTIF_BY_KEY for k in _pool), _style_key

# ---------------------------------------------------------------- styles
# The edit style decides which KINDS of material are searched for.

STYLES: List[dict] = [
    {"key": "movie_edit", "en": "Movie-scene edit", "id": "Edit potongan film",
     "en_hint": "Cut film/scene footage to the song so the lyric tells the story.",
     "id_hint": "Potong adegan film ke lagu supaya liriknya jadi cerita."},
    {"key": "lyric_video", "en": "Lyric / text video", "id": "Video lirik teks",
     "en_hint": "The words on screen carry it, behind a moving backdrop.",
     "id_hint": "Kata-kata di layar jadi utama, latar bergerak."},
    {"key": "story", "en": "Story / vlog", "id": "Cerita / vlog",
     "en_hint": "Hold a camera and tell a small true story about it.",
     "id_hint": "Pegang kamera dan ceritakan kisah kecil yang nyata."},
    {"key": "product_demo", "en": "Product / service demo", "id": "Demo produk / jasa",
     "en_hint": "Show the product solving a real problem in one shot per step.",
     "id_hint": "Tunjukkan produk menyelesaikan masalah nyata, satu shot per langkah."},
    {"key": "broll", "en": "Cinematic b-roll", "id": "B-roll sinematik",
     "en_hint": "Mood shots only, voice-over or song on top.",
     "id_hint": "Hanya shot suasana, voice-over atau lagu di atasnya."},
]

STYLE_BY_KEY: Dict[str, dict] = {s["key"]: s for s in STYLES}

STYLE_QUERY: Dict[str, List[str]] = {
    "movie_edit": ["{m} movie scene", "{m} edit reference"],
    "lyric_video": ["{m} aesthetic", "{m} backdrop loop"],
    "story": ["{m} cinematic b-roll", "{m} daily life vlog"],
    "product_demo": ["{m} product b-roll", "{m} commercial reference"],
    "broll": ["{m} cinematic b-roll", "{m} free stock footage"],
}

# How the pack numbers itself: an average clipper can only juggle so many
# distinct clips before the edit stalls, so the plan never asks for 25 of them.
MIN_CLIPS = 4
MAX_CLIPS = 14

# Plain-language bullet labels per kind of material.
KIND_LABEL = {
    "scene": {"en": "Film / scene reference", "id": "Referensi film / adegan"},
    "edit_ref": {"en": "Edit reference (watch how it is cut)", "id": "Referensi editing (lihat cara potongnya)"},
    "broll": {"en": "Cinematic b-roll / stock", "id": "B-roll sinematik / stok"},
    "backdrop": {"en": "Moving backdrop", "id": "Latar bergerak"},
    "product": {"en": "Product shot reference", "id": "Referensi shot produk"},
    "audio": {"en": "Official song audio (upload with permission / your own)", "id": "Audio lagu resmi (unggah kalau dizinkan / milik sendiri)"},
    "ai_scene": {"en": "AI-suggested scene (verify it exists)", "id": "Saran adegan AI (pastikan ada)"},
}

# ---------------------------------------------------------------- lyric handling

_TS_TAG_RE = re.compile(r"[\[(](?:chorus|verse|bridge|intro|outro|hook|pre-?chorus|refrain|reff|reffrain|interlude|spoken|x\d+)[^\])]*[\])]",
                        re.IGNORECASE)
_LINE_NOISE_RE = re.compile(r"[^\w\s'\-]", re.UNICODE)
_STOP = {
    "the", "and", "you", "your", "for", "with", "that", "this", "from", "have", "has", "are", "was",
    "were", "will", "not", "but", "all", "can", "just", "like", "when", "what", "who", "how", "why",
    "yeah", "oh", "ooh", "uh", "na", "la", "ah", "eh", "mmm", "got", "get", "got", "aint", "ain't",
    "yang", "dan", "untuk", "dengan", "dari", "tidak", "tak", "ini", "itu", "kamu", "kau", "aku",
    "kita", "akan", "juga", "saja", "ada", "sama", "ke", "di", "the",
}


def _norm_line(line: str) -> str:
    return " ".join(str(line or "").split())


def clean_lyric_lines(text: str, limit: int = 120) -> List[str]:
    """Drop section tags, empty lines and repeated lines (a chorus repeats is
    ONE lyric idea, not four)."""
    out: List[str] = []
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = _TS_TAG_RE.sub(" ", raw)
        line = _norm_line(line)
        if len(line) < 3:
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        if out and out[-1].lower() == line.lower():
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _tokens(line: str) -> List[str]:
    return [t for t in re.findall(r"[A-Za-z']+", (line or "").lower()) if len(t) > 2]


def motif_for_line(line: str) -> Optional[dict]:
    """Highest-specificity motif whose keyword appears in the line."""
    low = " " + re.sub(r"[^a-z0-9\s]", " ", (line or "").lower()) + " "
    best: Optional[tuple] = None
    for word, motif in _MOTIF_INDEX:
        if f" {word} " in low or f" {word}s " in low:
            score = len(word)
            if best is None or score > best[0]:
                best = (score, motif)
    return best[1] if best else None


def mood_for_line(line: str, fallback: str = "auto") -> str:
    """The mood a single line carries, from EXACT token matches (a prefix match
    once read "now" as "no" and turned a neutral line into "bold")."""
    low = (line or "").lower()
    scores: Dict[str, int] = {}
    for mood, pattern in _MOOD_RE.items():
        hits = len(pattern.findall(low))
        if hits:
            scores[mood] = hits
    if not scores:
        return fallback
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _count_repeats(lines: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for line in lines:
        key = line.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def beat_score(line: str, repeats: Dict[str, int]) -> float:
    """How well a line works as an on-screen moment: repeated lines are the
    chorus (the part people replay), motif lines are visually shootable, very
    long lines are hard to cut to."""
    words = _tokens(line)
    if len(words) < 2:
        return 0.0
    score = 1.0
    score += 2.5 * (repeats.get(line.lower(), 1) - 1)
    score += 1.5 if motif_for_line(line) else 0.0
    score += 0.5 if re.search(r"\b(i|me|my|aku|ku|kamu|you|we|kita)\b", line.lower()) else 0.0
    score += 0.4 if len(words) <= 8 else -0.4
    return round(score, 2)


def _short_essence(line: str) -> str:
    words = [w for w in line.split() if w]
    if len(words) <= 9:
        return line.strip().rstrip(",;")
    return " ".join(words[:9]).rstrip(",;") + "…"


def _generic_essence(lang: str, subject: str, idx: int, style: str = "broll") -> str:
    """Placeholder beats when no lyrics were pasted — song-shaped for music work,
    product-shaped otherwise."""
    song = {
        "en": [
            f"Show the world of {subject} — where this happens.",
            f"Show the feeling {subject} speaks about.",
            "Show the turn: the moment it changes.",
            f"Payoff — the strongest image of {subject}.",
            f"Close on {subject} alone: the after-feeling.",
            "Extra texture shot for the middle of the song.",
        ],
        "id": [
            f"Tunjukkan dunia {subject} — di mana ini terjadi.",
            f"Tunjukkan perasaan yang dibicarakan {subject}.",
            "Tunjukkan titik balik: momen ketika semuanya berubah.",
            f"Payoff — gambar paling kuat dari {subject}.",
            f"Tutup dengan {subject} sendirian: sisa perasaannya.",
            "Shot tambahan untuk bagian tengah lagu.",
        ],
    }
    product = {
        "en": [
            f"Where {subject} fits into a normal day.",
            "Show the problem it solves — the annoying moment.",
            "The switch: before versus after.",
            "The payoff — the result, close up.",
            "One honest reaction shot from a real user.",
            "Extra detail shot for texture (hands, screen, product).",
        ],
        "id": [
            f"Di mana {subject} masuk ke hari-hari biasa.",
            "Tunjukkan masalah yang diselesaikan — momen menyebalkannya.",
            "Perubahannya: sebelum versus sesudah.",
            "Payoff — hasilnya, close up.",
            "Satu reaksi jujur dari pengguna nyata.",
            "Shot detail tambahan (tangan, layar, produk).",
        ],
    }
    arr = (song if style in ("movie_edit", "lyric_video", "broll") else product)[lang]
    return arr[idx % len(arr)]


# ---------------------------------------------------------------- searches

def _kind_of(template: str) -> str:
    t = template.lower()
    if "movie" in t or "scene" in t:
        return "scene"
    if "reference" in t or "edit" in t:
        return "edit_ref"
    if "aesthetic" in t or "backdrop" in t:
        return "backdrop"
    if "product" in t or "commercial" in t:
        return "product"
    return "broll"


def _query_kinds(style: str, motif: dict, lang: str) -> List[dict]:
    """Per-beat material searches: a film/scene reference and an edit reference
    (plus a local-language pass), each with a real YouTube search URL."""
    seed_en = (motif.get("q") or [motif["key"]])[0]
    seed_id = (motif.get("qid") or [seed_en])[0]
    out: List[dict] = []
    for template in STYLE_QUERY.get(style) or STYLE_QUERY["broll"]:
        query = template.replace("{m}", seed_en).strip()
        out.append({"kind": _kind_of(template), "query": query, "url": search_url(query)})
    local = seed_id if " " in seed_id else f"{seed_id} klip"
    if local and local not in [o["query"] for o in out]:
        out.append({"kind": "broll", "query": local, "url": search_url(local)})
    return out


# ---------------------------------------------------------------- the pack

BEAT_TEMPLATES = {
    "en": {
        "shot": "Show {visual}. Hold the shot about {pace_s} seconds, then cut.",
        "why": "Put this on screen exactly where the line is sung, so the picture and the words say the same thing.",
    },
    "id": {
        "shot": "Tampilkan {visual}. Tahan shotnya sekitar {pace_s} detik, lalu potong.",
        "why": "Pasang ini tepat di bagian lagu saat baris itu dinyanyikan, supaya gambar dan kata-katanya seirama.",
    },
}

# What a line MEANS on screen, in plain words — the part an average clipper
# cannot be expected to translate from a lyric by themselves. Several phrasings
# per mood so a whole song does not repeat one sentence beat after beat.
ESSENCE_BY_MOOD = {
    "hype": {"en": "Peak energy — this is the loudest, biggest moment of the story.",
             "id": "Puncak energi — momen paling keras dan paling besar di cerita ini."},
    "sad": {"en": "The heavy, quiet moment — the low point of the story.",
            "id": "Momen berat dan sunyi — titik terendah ceritanya."},
    "romantic": {"en": "Close and intimate — two people with almost no space between them.",
                 "id": "Dekat dan intim — dua orang dengan jarak yang hampir tidak ada."},
    "nostalgic": {"en": "Looking back — past tense, softer light, memory.",
                  "id": "Melihat ke belakang — masa lalu, cahaya lebih lembut, kenangan."},
    "bold": {"en": "A statement moment — chin up, straight to the camera.",
             "id": "Momen statement — dagu naik, langsung ke kamera."},
    "chill": {"en": "Breathing room between the loud parts — a calm, wide shot.",
              "id": "Ruang nafas di antara bagian ramai — shot lebar yang tenang."},
}

ESSENCE_VARIANTS: Dict[str, List[dict]] = {
    "hype": [
        {"en": "The drop lands here — cut the fastest right on this line.",
         "id": "Bagian paling nendang ada di sini — potong paling cepat di baris ini."},
        {"en": "Everybody moves on this line — crowd, motion, impact.",
         "id": "Semua orang bergerak di baris ini — kerumunan, gerakan, benturan."},
    ],
    "sad": [
        {"en": "Loss sits here — show the space the person left behind.",
         "id": "Kehilangan ada di sini — tunjukkan ruang yang ditinggalkan."},
        {"en": "This is where it hurts — keep the camera still and let it sink.",
         "id": "Di sini bagian yang menyakitkan — tahan kameranya, biarkan meresap."},
    ],
    "romantic": [
        {"en": "Warmth and nearness — hands, breath, a small distance closing.",
         "id": "Hangat dan dekat — tangan, nafas, jarak kecil yang menutup."},
        {"en": "Softer light, closer frame — the moment they choose each other.",
         "id": "Cahaya lebih lembut, frame lebih dekat — momen mereka saling memilih."},
    ],
    "nostalgic": [
        {"en": "Grainy, warmer, older — this is a memory, not the present.",
         "id": "Lebih grain, lebih hangat, lebih lama — ini kenangan, bukan sekarang."},
        {"en": "A before-and-now beat — show then, cut to now.",
         "id": "Beat sebelum-dan-sekarang — tunjukkan dulu, potong ke sekarang."},
    ],
    "bold": [
        {"en": "A line with attitude — hard light, straight lines, no clutter.",
         "id": "Baris berkarakter — cahaya keras, garis tegas, tanpa gangguan."},
        {"en": "Draw the boundary here — the shot says no alongside the words.",
         "id": "Tarik batasnya di sini — gambarnya ikut bilang tidak."},
    ],
    "chill": [
        {"en": "Let it drift — one long shot, no rush to the next cut.",
         "id": "Biarkan mengalir — satu shot panjang, tak perlu buru-buru."},
        {"en": "A wide, calm frame to reset the eye before the next build.",
         "id": "Frame lebar dan tenang untuk menyegarkan mata sebelum naik lagi."},
    ],
}


def _essence_variant(mood: str, pos: int, motif_key: str, lang: str) -> str:
    """Deterministic variety: same mood, a different sentence per beat."""
    base = ESSENCE_BY_MOOD.get(mood) or ESSENCE_BY_MOOD["chill"]
    variants = ESSENCE_VARIANTS.get(mood) or []
    if pos <= 1 or not variants:
        return base[lang]
    return variants[(pos + len(motif_key)) % len(variants)][lang]


def _song_mood(lines: List[str]) -> str:
    """The mood of the whole song — the fallback when a single line says nothing."""
    tally: Dict[str, int] = {}
    for line in lines:
        mood = mood_for_line(line)
        if mood != "auto":
            tally[mood] = tally.get(mood, 0) + 1
    if not tally:
        return "chill"
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def build_material(
    spec: Optional[dict] = None,
    subject: str = "",
    artist: str = "",
    lyrics: str = "",
    prompt: str = "",
    language: str = "en",
    vibe: str = "auto",
    style: str = "auto",
    platform: str = "",
    duration_sec: Optional[float] = None,
    beat_count: int = 6,
) -> dict:
    """The material plan: lyric-browsable beats, searches with real links, the
    asset list and a non-technical step-by-step recipe. Deterministic."""
    lang = _lang(language)
    spec = spec or {}
    req = spec.get("requirements") or {}

    # ---- what are we promoting -------------------------------------------------
    brand = (spec.get("public_name") or spec.get("name") or "").strip()
    subject = _norm_line(subject) or _norm_line(prompt) or brand or ("this song" if lang == "en" else "lagu ini")
    subject_line = subject
    if artist and artist.lower() not in subject.lower():
        subject_line = f"{subject} — {artist}" if lang == "en" else f"{subject} oleh {artist}"

    # ---- style -----------------------------------------------------------------
    style_key = (style or "auto").strip().lower()
    if style_key not in STYLE_BY_KEY:
        hay = f"{prompt} {subject} {artist} {lyrics[:600]}".lower()
        if any(w in hay for w in ("song", "lagu", "music", "musik", "single", "album", "lyric", "lirik", "chorus")):
            style_key = "movie_edit" if any(w in hay for w in ("movie", "film", "scene", "adegan")) or lyrics.strip() else "lyric_video"
        elif any(w in hay for w in ("product", "produk", "app", "skincare", "service", "jasa", "shop", "toko")):
            style_key = "product_demo"
        else:
            style_key = "broll"
    if lyrics.strip() and style_key == "broll":
        style_key = "movie_edit"
    style_meta = STYLE_BY_KEY[style_key]

    # ---- mood ------------------------------------------------------------------
    vibe_key = (vibe or "auto").strip().lower()
    if vibe_key not in MOOD_BY_KEY:
        vibe_key = "auto"

    # ---- beats from the lyrics (the lyric browser) ------------------------------
    lines = clean_lyric_lines(lyrics)
    repeats = _count_repeats(lines)
    max_beats = int(_clamp(beat_count or 6, 3, 12))
    beats: List[dict] = []
    lyric_map: List[dict] = []
    song_mood = _song_mood(lines) if lines else "chill"
    pool = GENERIC_BY_STYLE.get(style_key) or GENERIC_MOTIFS
    used_motifs: List[str] = []

    def pick_motif(line: str) -> dict:
        """Content first, then rotation: a motif already used by an earlier beat
        gives way to the next unused one in the style's pool (and the pool cycles
        without ever repeating back-to-back)."""
        found = motif_for_line(line)
        if found is not None and found["key"] not in used_motifs:
            used_motifs.append(found["key"])
            return found
        for key in pool:
            if key not in used_motifs:
                used_motifs.append(key)
                return MOTIF_BY_KEY[key]
        # Pool exhausted (more beats than motifs): start a new cycle, but never
        # hand two consecutive beats the same shot idea.
        previous = used_motifs[-1] if used_motifs else None
        for key in pool + ([found["key"]] if found is not None else []):
            if key != previous:
                used_motifs.append(key)
                return MOTIF_BY_KEY[key]
        used_motifs.append(pool[0])
        return MOTIF_BY_KEY[pool[0]]

    if lines:
        # One beat per DISTINCT lyric line (a chorus repeats in the song, not in
        # the edit): score them, keep the strongest, and always let the opening
        # line in so the edit starts where the song starts.
        first_index: Dict[str, int] = {}
        for i, line in enumerate(lines):
            first_index.setdefault(line.lower(), i)
        ranked = sorted(first_index.values(), key=lambda i: (-beat_score(lines[i], repeats), i))
        order = list(ranked[:max_beats])
        if 0 not in order and order:
            order[-1] = 0
        chosen = sorted(set(order))
        for pos, line_idx in enumerate(chosen, start=1):
            line = lines[line_idx]
            beats.append(_make_beat(pos, line, subject_line, lang, vibe_key, style_key,
                                    pick_motif(line), repeat=repeats.get(line.lower(), 1),
                                    song_mood=song_mood))
        beat_of_line: Dict[str, str] = {lines[i].lower(): f"b{pos}" for pos, i in enumerate(chosen, start=1)}
        for idx, line in enumerate(lines):
            lyric_map.append({"line": line, "beat_id": beat_of_line.get(line.lower()),
                              "repeat": repeats.get(line.lower(), 1)})
    else:
        for pos in range(1, max(4, min(max_beats, 6)) + 1):
            essence = _generic_essence(lang, subject_line, pos - 1, style_key)
            # empty content on purpose: placeholder beats must not keyword-match
            # their own wording ("show the problem" is not a stage motif).
            beats.append(_make_beat(pos, essence, subject_line, lang, vibe_key, style_key,
                                    pick_motif(""), repeat=1, generic=True, song_mood=song_mood))

    # ---- pack-level searches ----------------------------------------------------
    song_q = f"{subject} {artist}".strip() or subject
    pack_searches = [
        {"kind": "audio", "query": f"{song_q} official audio", "url": search_url(f"{song_q} official audio"),
         "note_en": "The audio you cut to. Use the campaign's own file or the official upload.",
         "note_id": "Audio yang kamu potong. Pakai file dari kampanye atau unggahan resmi."},
        {"kind": "edit_ref", "query": f"{song_q} edit {('film' if style_key == 'movie_edit' else 'tiktok')}",
         "url": search_url(f"{song_q} edit {'film' if style_key == 'movie_edit' else 'tiktok'}"),
         "note_en": "See how other editors already cut this song — copy the RHYTHM, never the footage.",
         "note_id": "Lihat bagaimana editor lain memotong lagu ini — tiru IRAMANYA, jangan ambil footagenya."},
    ]

    # ---- pacing -----------------------------------------------------------------
    mood_meta = MOOD_BY_KEY.get(vibe_key) or MOOD_BY_KEY["auto"]
    pace = float(mood_meta.get("pace") or 1.6)
    if vibe_key == "auto":
        pace = float((MOOD_BY_KEY.get(song_mood) or {}).get("pace") or 1.6)
        mood_label = song_mood
    else:
        mood_label = vibe_key
    pace_hint = (_PACE_WORDS[lang].get(pace) or _PACE_WORDS[lang][1.6])
    for b in beats:
        b["shot"] = _shot_text(b, lang, pace, pace_hint)

    duration = float(duration_sec or req.get("max_duration_sec") or 30)
    duration = _clamp(duration, 8, 180)
    clip_need = int(_clamp(max(round(duration / pace), len(beats)), MIN_CLIPS, MAX_CLIPS))

    asset_plan = _asset_plan(lang, style_key, len(beats), clip_need, duration, subject_line)
    steps = _steps(lang, style_key, subject_line, artist, duration, pace, clip_need, len(beats), platform)
    watch_outs = _watch_outs(lang, style_key, req)

    summary = {
        "en": (f"{clip_need} short clips (~{pace:.1f}s each) cut to “{subject_line}”, "
               f"about {int(duration)}s total, one clip per lyric beat below."),
        "id": (f"{clip_need} klip pendek (~{pace:.1f}s per klip) dipotong ke “{subject_line}”, "
               f"total sekitar {int(duration)} detik, satu klip per bait lirik di bawah."),
    }[lang]

    pack = {
        "version": MATERIAL_VERSION,
        "source": "rules",
        "campaign_id": spec.get("campaign_id"),
        "campaign_name": brand or None,
        "language": lang,
        "subject": subject_line,
        "artist": artist.strip(),
        "style": style_key,
        "style_label": _pick(style_meta, lang),
        "style_hint": _pick(style_meta, lang + "_hint") or str(style_meta.get("en_hint") or ""),
        "vibe": vibe_key,
        "vibe_label": _pick(MOOD_BY_KEY.get(vibe_key) or MOOD_BY_KEY["auto"], lang),
        "mood": mood_label,
        "pace_sec": round(pace, 2),
        "pace_hint": pace_hint,
        "duration_sec": round(duration, 1),
        "clip_count": clip_need,
        "summary": summary,
        "beats": beats,
        "lyric_map": lyric_map,
        "searches": pack_searches,
        "asset_plan": asset_plan,
        "steps": steps,
        "watch_outs": watch_outs,
        "notes": [],
    }
    if not beats:
        pack["notes"].append("No lyrics supplied — the beats below are generic placeholders.")
    if not lyrics.strip():
        pack["notes"].append({
            "en": "Paste the lyrics to make the beats exact: each line then gets its own scene idea and link.",
            "id": "Tempel liriknya supaya baitnya tepat: tiap baris dapat ide adegan dan link sendiri.",
        }[lang])
    pack["material_md"] = build_material_md(pack, spec)
    return pack


def _make_beat(pos: int, line: str, subject: str, lang: str, vibe: str, style: str,
               motif: dict, repeat: int = 1, generic: bool = False, song_mood: str = "chill") -> dict:
    line_mood = mood_for_line(line)
    if vibe and vibe != "auto":
        mood = vibe
    elif line_mood != "auto":
        mood = line_mood
    else:
        mood = song_mood
    visual = _pick(motif, lang) or _pick(motif, "en")
    return {
        "id": f"b{pos}",
        "index": pos,
        "line": _short_essence(line) if not generic else line,
        "line_full": line,
        "essence": _essence(line, lang, mood, generic, pos, motif["key"]),
        "repeat": repeat,
        "is_chorus": repeat > 1,
        "mood": mood,
        "motif": motif["key"],
        "visual": visual,
        "pace_sec": None,
        "shot": "",
        "why": BEAT_TEMPLATES[lang]["why"],
        "searches": _query_kinds(style, motif, lang),
        "ai_scene": None,
        "results": [],
    }


def _essence(line: str, lang: str, mood: str, generic: bool = False,
             pos: int = 1, motif_key: str = "") -> str:
    """One plain sentence about what the line MEANS on screen."""
    if generic:
        return line
    low = (line or "").lower()
    if re.search(r"\b(why|kenapa|mengapa)\b", low):
        return {"en": "A question with no answer — hold the shot a beat longer here.",
                "id": "Pertanyaan tanpa jawaban — tahan shotnya sedikit lebih lama di sini."}[lang]
    if re.search(r"\b(never|no|won't|not|jangan|tak|tidak)\b", low):
        return {"en": "Refusal or a hard boundary — show the thing being walked away from.",
                "id": "Penolakan atau batas keras — tunjukkan hal yang ditinggalkan."}[lang]
    if re.search(r"\b(you|kamu|kau)\b", low) and re.search(r"\b(i|me|my|aku|ku)\b", low):
        return {"en": "Two people talking — this is dialogue, so cut between two faces.",
                "id": "Dua orang berbicara — ini dialog, jadi potong bergantian antara dua wajah."}[lang]
    return _essence_variant(mood, pos, motif_key, lang)


def _shot_text(beat: dict, lang: str, pace: float, pace_hint: str) -> str:
    text = BEAT_TEMPLATES[lang]["shot"].replace("{visual}", beat["visual"]).replace("{pace_s}", f"{pace:.1f}")
    beat["pace_sec"] = round(pace, 2)
    return f"{text} ({pace_hint})"


def _asset_plan(lang: str, style: str, beat_count: int, clip_need: int, duration: float,
                subject: str) -> List[dict]:
    extra = {
        "movie_edit": [
            {"en": "candidate film/scene clips (2 per beat, keep the best one)", "id": "klip adegan film kandidat (2 per bait, simpan yang terbaik)", "count": beat_count * 2},
            {"en": "one strong hook clip for the first 2 seconds", "id": "satu klip pembuka yang kuat untuk 2 detik pertama", "count": 1},
        ],
        "lyric_video": [
            {"en": "moving backdrops (search links below — no copyright issues)", "id": "latar bergerak (link di bawah — bebas masalah hak cipta)", "count": max(3, beat_count)},
            {"en": "the song's official audio + cover art", "id": "audio resmi lagu + artwork", "count": 2},
        ],
        "story": [
            {"en": "your own footage: 3-5 scenes on camera or of your day", "id": "footage sendiri: 3-5 adegan di depan kamera atau harimu", "count": max(3, beat_count)},
            {"en": "b-roll cutaways to cover the jumps", "id": "b-roll potongan untuk menutup lompatan", "count": max(3, beat_count)},
        ],
        "product_demo": [
            {"en": "product shots: in hand, in use, close-up detail", "id": "shot produk: di tangan, sedang dipakai, detail dekat", "count": max(6, beat_count * 2)},
            {"en": "result/proof shot (before → after)", "id": "shot hasil/bukti (sebelum → sesudah)", "count": 2},
        ],
        "broll": [
            {"en": "mood b-roll clips", "id": "klip b-roll suasana", "count": clip_need},
            {"en": "one wide establishing shot", "id": "satu shot lebar pembuka", "count": 1},
        ],
    }[style]
    plan = [{"item": _pick(x, lang), "count": x["count"]} for x in extra]
    plan.append({
        "item": {"en": f"total on-screen clips for ~{int(duration)}s (≈{clip_need} cuts)", "id": f"total klip untuk ~{int(duration)} detik (≈{clip_need} potongan)"}[lang],
        "count": clip_need,
    })
    return plan


def _steps(lang: str, style: str, subject: str, artist: str, duration: float, pace: float,
           clip_need: int, beat_count: int, platform: str) -> List[dict]:
    """The non-technical recipe. Every step says WHAT to do, HOW LONG it takes and
    WHAT comes out of it — no marketing jargon, no editing jargon."""
    p = platform or ("TikTok / Reels / Shorts" if lang == "en" else "TikTok / Reels / Shorts")
    if lang == "id":
        common = [
            {"title": "Siapkan lagunya (5 menit)", "detail": f"Simpan file audio “{subject}” di folder kerja kamu. Tandai di menit berapa bagian reff mulai — itu bagian yang paling sering diputar orang.", "minutes": 5},
            {"title": "Kumpulkan bahan (20-30 menit)", "detail": f"Buka link di tiap bait di bawah. Untuk setiap bait ambil 2 klip kandidat, jadi sekitar {beat_count * 2} klip. Unduh atau simpan link-nya — belum perlu dipotong.", "minutes": 25},
            {"title": f"Pilih {beat_count} klip kunci + 1 pembuka", "detail": f"Dari kandidat itu pilih satu per bait ({beat_count} klip) plus satu klip pembuka. Total potongan di video akhir sekitar {clip_need}. Bandingkan: mana yang dalam 1 detik saja sudah kelihatan menarik? Sisanya sisihkan — jangan dipaksa masuk.", "minutes": 10},
            {"title": "Potong mengikuti lirik (30-45 menit)", "detail": f"Susun klip sesuai urutan bait. Panjang tiap klip sekitar {pace:.1f} detik ({'cepat mengikuti beat' if pace <= 1.2 else 'santai, biarkan tiap shot terlihat'}). Ganti klip tepat saat liriknya berganti baris.", "minutes": 40},
            {"title": "Rapikan pembuka 2 detik", "detail": "Dua detik pertama menentukan orang lanjut nonton atau tidak. Taruh gambar paling kuat di sana, bukan intro/judul panjang. Subtitle besar, potongan pendek, tempo naik.", "minutes": 10},
            {"title": "Isi judul + caption (5 menit)", "detail": "Judul: 8 kata, menyebut intinya. Caption: 1 baris lirik yang paling nendang, lalu 1 ajakan (simpan/bagikan/komen), lalu tag kampanye. Jangan 15 tag.", "minutes": 5},
            {"title": "Cek aturan kampanye (2 menit)", "detail": "Cek daftar 'Jangan' di bawah sebelum unggah: panjang klip, ukuran layar, klaim yang dilarang, tag wajib.", "minutes": 2},
            {"title": "Unggah ke " + p + " (10 menit)", "detail": "Unggah di jam ramai (12:00-13:00 atau 19:00-21:00 waktu setempat). Setelah 24 jam catat: berapa view, berapa orang nonton sampai habis. Angka ini yang dipakai memperbaiki klip berikutnya.", "minutes": 10},
        ]
    else:
        common = [
            {"title": "Get the song ready (5 min)", "detail": f"Save the audio of “{subject}” in one folder. Mark where the chorus starts — that is the part people replay, so your strongest clips go there.", "minutes": 5},
            {"title": "Collect the material (20-30 min)", "detail": f"Open the links under each beat. Grab 2 candidate clips per beat — around {beat_count * 2} in all. Download them or just save the links; no cutting yet.", "minutes": 25},
            {"title": f"Pick {beat_count} key clips + 1 opener", "detail": f"From the candidates pick one per beat ({beat_count} clips) plus one opener. The finished video ends up with roughly {clip_need} cuts. Ask which one reads as interesting in the first second — set the rest aside, do not force them in.", "minutes": 10},
            {"title": "Cut it to the lyrics (30-45 min)", "detail": f"Lay the clips in lyric order. Each clip runs about {pace:.1f}s ({'fast, on the beat' if pace <= 1.2 else 'unhurried, let each shot be seen'}). Change the clip exactly when the lyric line changes.", "minutes": 40},
            {"title": "Fix the first 2 seconds", "detail": "Two seconds decide whether anyone keeps watching. Put the strongest image there — no intro card. Big subtitle, short cut, rising tempo.", "minutes": 10},
            {"title": "Write title + caption (5 min)", "detail": "Title: 8 words, says the point. Caption: the one lyric line that hits hardest, then one call to action (save / share / comment), then the campaign tags. Never 15 tags.", "minutes": 5},
            {"title": "Check the campaign rules (2 min)", "detail": "Run the Do-not list below before uploading: clip length, aspect ratio, banned claims, required tags.", "minutes": 2},
            {"title": f"Post to {p} (10 min)", "detail": "Post at 12:00-13:00 or 19:00-21:00 local time. After 24 hours write down views and how many watched to the end — those two numbers tell you what to change next.", "minutes": 10},
        ]
    if style == "product_demo":
        common.insert(1, {
            "title": {"en": "Film the product yourself (20 min)", "id": "Rekam produknya sendiri (20 menit)"}[lang],
            "detail": {"en": "One shot per step: in hand, in use, the result. Window light is enough — shoot 3 takes of each and keep the cleanest.",
                       "id": "Satu shot per langkah: di tangan, sedang dipakai, hasilnya. Cukup cahaya jendela — rekam 3 kali tiap shot dan simpan yang paling bersih."}[lang],
            "minutes": 20,
        })
    if style == "lyric_video":
        common.insert(3, {
            "title": {"en": "Type the lyric lines on screen (25 min)", "id": "Tulis baris liriknya di layar (25 menit)"}[lang],
            "detail": {"en": "One line at a time, big font, in sync with the vocal. Highlight 2-3 words per line instead of the whole sentence.",
                       "id": "Satu baris sekali, huruf besar, sinkron dengan vokalnya. Tebalkan 2-3 kata per baris, bukan seluruh kalimat."}[lang],
            "minutes": 25,
        })
    for i, step in enumerate(common, start=1):
        step["n"] = i
    return common


def _watch_outs(lang: str, style: str, req: dict) -> List[dict]:
    if lang == "id":
        base = [
            {"level": "warn", "text": "Footage film/b-roll dari YouTube BUKAN milikmu. Untuk klip komersial pakai bahan berlisensi, klip resmi pendek sebagai referensi/komentar, atau stok bebas royalti — link di bawah sudah ditandai. Kalau kampanye melarang footage pihak ketiga, ikuti aturan kampanye, bukan link."},
            {"level": "info", "text": "Ambil IRAMA-nya, bukan footagenya: putar referensi buat belajar tempo potongannya, lalu pakai bahanmu sendiri."},
            {"level": "info", "text": "Tanpa watermark aplikasi lain, dan jangan pakai musik/tag kompetitor."},
        ]
        if style in ("movie_edit", "lyric_video"):
            base.append({"level": "info", "text": "Ambil momen pendek (1-2 detik) sebagai penyokong lirik, bukan adegan panjang — sekaligus lebih enak dilihat dan lebih aman."})
    else:
        base = [
            {"level": "warn", "text": "Film and b-roll footage you find on YouTube is NOT yours. For a paid campaign use licensed material, short official clips as reference/commentary, or royalty-free stock — the links below are labelled. If the campaign bans third-party footage, the campaign rule wins, not the link."},
            {"level": "info", "text": "Copy the RHYTHM, not the footage: watch references to learn the cut tempo, then shoot/use your own material."},
            {"level": "info", "text": "No watermark from another app, no competitor music or tags."},
        ]
        if style in ("movie_edit", "lyric_video"):
            base.append({"level": "info", "text": "Use short 1-2 second moments as lyric support rather than long scenes — it reads better and it is safer."})
    for rule in (req.get("negative_rules") or [])[:4]:
        base.append({"level": "warn", "text": f"Campaign rule: {rule}"})
    min_d = req.get("min_duration_sec")
    max_d = req.get("max_duration_sec")
    if min_d or max_d:
        base.append({"level": "info", "text": (
            f"Clip length must stay between {int(min_d or 0)}s and {int(max_d or 0)}s."
            if lang == "en" else f"Panjang klip wajib antara {int(min_d or 0)}s dan {int(max_d or 0)}s."
        )})
    return base


# ---------------------------------------------------------------- markdown

def build_material_md(pack: dict, spec: Optional[dict] = None) -> str:
    """MATERIAL.md — the hand-off document (links are clickable in any editor)."""
    out: List[str] = []
    out.append(f"# Material plan — {pack.get('subject') or 'campaign'}")
    out.append("")
    out.append(f"- Style: **{pack.get('style_label')}** — {pack.get('style_hint')}")
    out.append(f"- Feel: **{pack.get('vibe_label')}** · cut rhythm: ~{pack.get('pace_sec')}s per shot ({pack.get('pace_hint')})")
    out.append(f"- Target length: ~{int(pack.get('duration_sec') or 30)}s · ≈{pack.get('clip_count')} cuts")
    out.append(f"- {pack.get('summary')}")
    out.append("")
    if pack.get("notes"):
        out.append("## Notes")
        for n in pack["notes"]:
            out.append(f"- {n}")
        out.append("")

    out.append("## Step by step")
    out.append("")
    for step in pack.get("steps") or []:
        out.append(f"{step['n']}. **{step['title']}** — {step['detail']}")
    out.append("")

    out.append("## What you need")
    out.append("")
    out.append("| Material | How many |")
    out.append("| --- | --- |")
    for item in pack.get("asset_plan") or []:
        out.append(f"| {item.get('item')} | {item.get('count')} |")
    out.append("")

    out.append("## Beats — the song browsed by lyric")
    out.append("")
    for beat in pack.get("beats") or []:
        flag = " (chorus)" if beat.get("is_chorus") else ""
        out.append(f"### {beat['id']} · {beat.get('mood')} · {beat.get('motif')}{flag}")
        out.append("")
        out.append(f"- **Lyric:** {beat.get('line_full') or beat.get('line')}")
        if beat.get("essence"):
            out.append(f"- **What it says:** {beat['essence']}")
        out.append(f"- **Shot:** {beat.get('shot')}")
        if beat.get("ai_scene"):
            scene = beat["ai_scene"]
            out.append(f"- **Scene idea:** {scene.get('film')} — {scene.get('scene')} _(why: {scene.get('why')})_")
        out.append("- **Search for the material:**")
        for s in beat.get("searches") or []:
            label = (KIND_LABEL.get(s.get("kind")) or {}).get(pack.get("language") or "en", s.get("kind"))
            out.append(f"  - [{label}]({s.get('url')}) — query: `{s.get('query')}`")
        for r in beat.get("results") or []:
            out.append(f"  - ▶️ [{r.get('title')}]({r.get('url')}) — {r.get('channel') or ''} {r.get('duration_label') or ''}".rstrip())
        out.append("")

    if pack.get("lyric_map"):
        out.append("## Lyric map (which line feeds which beat)")
        out.append("")
        for row in pack["lyric_map"]:
            tag = row.get("beat_id") or "—"
            extra = " ×%d" % row["repeat"] if (row.get("repeat") or 1) > 1 else ""
            out.append(f"- `{tag}`{extra} {row.get('line')}")
        out.append("")

    out.append("## More searches")
    out.append("")
    for s in pack.get("searches") or []:
        note = s.get("note_" + (pack.get("language") or "en")) or ""
        out.append(f"- [{s.get('query')}]({s.get('url')}) — {note}")
    out.append("")

    out.append("## Watch out")
    out.append("")
    for w in pack.get("watch_outs") or []:
        icon = "⚠️" if w.get("level") == "warn" else "ℹ️"
        out.append(f"- {icon} {w.get('text')}")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- optional LLM

def material_prompt(pack: dict, spec: Optional[dict] = None, language: str = "en") -> str:
    """Prompt for the OPTIONAL scene-picker: name real, well-known film scenes
    that match each lyric beat. The model is advisory only — the caller builds
    the YouTube search from whatever it returns."""
    lang_name = "Bahasa Indonesia" if _lang(language) == "id" else "English"
    beat_lines = "\n".join(
        f"{b['id']}|{b.get('motif')}|{b.get('mood')}|{b.get('line_full') or b.get('line')}"
        for b in pack.get("beats") or []
    )
    return (
        "You are a film-literate music-video editor. For each lyric beat below, name ONE real, "
        "well-known movie or series scene that visually matches the line (a scene a viewer can "
        "find on YouTube). Prefer scenes that are famous enough to exist as a clip upload.\n"
        f"Write in {lang_name}. Never invent a film that does not exist.\n\n"
        f"Song / subject: {pack.get('subject')}\n"
        f"Style: {pack.get('style')} · feel: {pack.get('vibe')}\n"
        f"Beats (id|motif|mood|lyric):\n{beat_lines}\n\n"
        'Return ONLY JSON: {"scenes": [{"id": "b1", "film": "Film title (year)", "scene": "which scene, one line", '
        '"search_query": "the exact YouTube search that finds that scene clip", "why": "one line tying it to the lyric"}]}'
    )


def apply_llm_material(pack: dict, parsed: dict, model: str) -> int:
    """Merge the scene-picker answer into the pack. Returns beats touched."""
    applied = 0
    by_id = {b["id"]: b for b in pack.get("beats") or []}
    for entry in parsed.get("scenes") or []:
        if not isinstance(entry, dict):
            continue
        beat = by_id.get(str(entry.get("id")))
        if beat is None:
            continue
        film = str(entry.get("film") or "").strip()[:120]
        scene = str(entry.get("scene") or "").strip()[:240]
        if not (film or scene):
            continue
        beat["ai_scene"] = {
            "film": film,
            "scene": scene,
            "why": str(entry.get("why") or "").strip()[:240],
        }
        query = str(entry.get("search_query") or "").strip()[:160] or f"{film} {scene} scene".strip()
        if query:
            beat.setdefault("searches", []).insert(0, {
                "kind": "ai_scene", "query": query, "url": search_url(query), "ai": True,
            })
        applied += 1
    if applied:
        pack["source"] = "rules+llm"
        pack["model"] = model
        note = ("Scene ideas came from the AI pass — check that the film and scene really exist before you cut."
                if pack.get("language") != "id" else
                "Ide adegan datang dari pass AI — pastikan film dan adegannya benar-benar ada sebelum memotong.")
        pack.setdefault("notes", []).append(note)
    pack["material_md"] = build_material_md(pack)
    return applied
