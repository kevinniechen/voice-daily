"""Ten Minute Voice server: serves the app, keeps each person's recordings and
progress, and has a voice coach write timestamped feedback on each take.

Identity comes from the exe.dev proxy (X-ExeDev-UserID / X-ExeDev-Email).

Coach pipeline: faster-whisper word timestamps -> Praat (parselmouth) pitch and
loudness per word -> an LLM reads the passage, transcript and measurements.
Gemini listens to the audio and gets the measurements as supporting evidence. Models are
tried in order (no key, quota or overload -> next): OPENROUTER_MODELS via OpenRouter, then
GEMINI_MODELS direct from Google. If none answer, Claude reads the measurements instead
(it can't take audio). COACH=claude skips Gemini.

  OPENROUTER_API_KEY, OPENROUTER_MODELS  default google/gemini-4-argon
  GEMINI_API_KEY, GEMINI_MODELS          default gemini-4-argon,gemini-3.8-flash,gemini-3.6-flash
  CLAUDE_MODEL     default claude-opus-5-5
  ANTHROPIC_BASE_URL  default https://llm.int.exe.xyz (exe.dev gateway, no key needed)
  DATA_DIR         default ~/voice-data
  PORT             default 8000
  DAILY_LIMIT      reviews + drill grades per person per day, default 60
"""
import base64, hashlib, json, secrets, shutil, mimetypes, os, queue, re, sqlite3, subprocess, threading, time, traceback, urllib.error, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote, unquote

APP = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(APP, ".env")) if os.path.exists(os.path.join(APP, ".env")) else []:
    k, _, v = line.strip().partition("=")
    if k and not k.startswith("#"): os.environ.setdefault(k, v.strip().strip('"\''))

DATA = os.path.expanduser(os.environ.get("DATA_DIR", "~/voice-data"))
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-4-argon,gemini-3.8-flash,gemini-3.6-flash").split(",") if m.strip()]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
OPENROUTER_MODELS = [m.strip() for m in os.environ.get("OPENROUTER_MODELS", "google/gemini-4-argon").split(",") if m.strip()]
LISTENERS = [] if os.environ.get("COACH") == "claude" else (
    [("openrouter", m) for m in OPENROUTER_MODELS if os.environ.get("OPENROUTER_API_KEY")] +
    [("gemini", m) for m in GEMINI_MODELS if os.environ.get("GEMINI_API_KEY")])
MODEL = LISTENERS[0][1] if LISTENERS else CLAUDE_MODEL
def coach_ready(): return True
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "60"))
IMPORT_ORIGINS = {"https://kevinniechen.github.io"}
GUEST_COOKIE = "vd_guest"   # the device's session cookie (guests and Apple sign-ins)
APPLE_CLIENT_IDS = [x for x in os.environ.get("APPLE_CLIENT_IDS", "com.kevchn.tenminutevoice").split(",") if x]
GUESTS_PER_IP_DAY = int(os.environ.get("GUESTS_PER_IP_DAY", "10"))
GUEST_REVIEWS_DAY = int(os.environ.get("GUEST_REVIEWS_DAY", "300"))  # all guests together, to protect the model budget
IMPORT_TOKENS = {}  # token -> (user_id, expires)
STATIC = {"/": "index.html", "/index.html": "index.html", "/books.json": "books.json", "/manifest.webmanifest": "manifest.webmanifest",
          "/privacy": "privacy.html", "/terms": "terms.html",
          "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png", "/apple-touch-icon.png": "apple-touch-icon.png"}
mimetypes.add_type("application/manifest+json", ".webmanifest")
os.makedirs(os.path.join(DATA, "audio"), exist_ok=True)

# ---------- db ----------
_db_lock = threading.Lock()
db = sqlite3.connect(os.path.join(DATA, "voice.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS state(user_id TEXT PRIMARY KEY, json TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS takes(
  id TEXT PRIMARY KEY, user_id TEXT, kind TEXT, label TEXT, ts INTEGER, secs REAL,
  text TEXT, marks TEXT, mime TEXT, status TEXT, feedback TEXT, words TEXT, error TEXT, model TEXT, analyzed INTEGER);
CREATE INDEX IF NOT EXISTS takes_user ON takes(user_id, ts);
""")
for ddl in (  # added after launch; each is a no-op once applied. Tables first, so a fresh database works too.
        """CREATE TABLE drills(id TEXT PRIMARY KEY, user_id TEXT, title TEXT, area TEXT, lesson TEXT, from_take TEXT,
             created INTEGER, due INTEGER, interval INTEGER DEFAULT 0, streak INTEGER DEFAULT 0, status TEXT DEFAULT 'active')""",
        """CREATE TABLE lessons(id TEXT PRIMARY KEY, user_id TEXT, date TEXT, n INTEGER, kind TEXT, focus_id TEXT, review_id TEXT,
             page INTEGER, status TEXT DEFAULT 'active', created INTEGER, completed INTEGER)""",
        "ALTER TABLE takes ADD COLUMN plan TEXT",                     # next-session plan (no longer written)
        "ALTER TABLE takes ADD COLUMN drill_id TEXT",                 # set on exercise attempts
        "ALTER TABLE takes ADD COLUMN filed INTEGER",                 # 1 once its lessons are in the curriculum
        "ALTER TABLE takes ADD COLUMN lesson_id TEXT",                # set on recordings made inside a lesson
        "ALTER TABLE takes ADD COLUMN step TEXT",                     # which lesson step: review, drill, read, shadow
        "ALTER TABLE drills ADD COLUMN stage TEXT DEFAULT 'queued'",  # queued -> current -> review -> learned
        "ALTER TABLE drills ADD COLUMN passes INTEGER DEFAULT 0",     # lesson days passed while current
        "ALTER TABLE drills ADD COLUMN last_pass TEXT",
        "ALTER TABLE drills ADD COLUMN level INTEGER DEFAULT 0",      # review passes
        "ALTER TABLE drills ADD COLUMN flags INTEGER DEFAULT 1",      # how many reviews raised this habit
        "ALTER TABLE users ADD COLUMN guest_hash TEXT",               # guests: sha256 of their device cookie
        "ALTER TABLE users ADD COLUMN ip TEXT",
        "CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id TEXT, created INTEGER)",
        "ALTER TABLE users ADD COLUMN src TEXT"):                       # where they came from: utm tags, referrer (first touch)  # device sign-ins (guests, Apple)
    try: db.execute(ddl)
    except sqlite3.OperationalError: pass

def q(sql, args=(), one=False):
    with _db_lock:
        cur = db.execute(sql, args); db.commit()
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows

def take_json(r):
    d = {k: r[k] for k in ("id", "kind", "label", "ts", "secs", "status", "error", "model")}
    d["feedback"] = json.loads(r["feedback"]) if r["feedback"] else None
    d["text"] = r["text"]; d["marks"] = json.loads(r["marks"] or "[]")
    d["plan"] = json.loads(r["plan"]) if r["plan"] else None
    d["drill_id"] = r["drill_id"]; d["lesson_id"] = r["lesson_id"]; d["step"] = r["step"]
    return d

def audio_path(r, ext="mp3"): return os.path.join(DATA, "audio", r["user_id"], r["id"] + "." + ext)

# ---------- analysis pipeline ----------
jobs = queue.Queue()
_whisper = None

_whisper_lock = threading.Lock()
def whisper():
    global _whisper
    try: from faster_whisper import WhisperModel
    except ImportError: return None
    if _whisper is None: _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "small.en"), device="cpu", compute_type="int8")
    return _whisper

def pcm_of(path):
    """16 kHz mono floats, decoded with ffmpeg (faster-whisper's PyAV path is version-fragile). Works on a growing file too."""
    import numpy as np
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True).stdout
    return np.frombuffer(out[: len(out) // 4 * 4], np.float32)

def transcribe_pcm(pcm, offset=0.0, prompt=None):
    m = whisper()
    if m is None or len(pcm) < 1600: return [] if m else None
    with _whisper_lock:
        segs, _ = m.transcribe(pcm, word_timestamps=True, vad_filter=False, beam_size=1, initial_prompt=prompt or None)
        segs = list(segs)
    return [{"w": w.word.strip(), "s": round(w.start + offset, 2), "e": round(w.end + offset, 2), "p": round(w.probability, 2)} for s in segs for w in (s.words or [])]

def transcribe(path):
    """Word-level timestamps, or None if faster-whisper isn't installed."""
    if whisper() is None: return None
    return transcribe_pcm(pcm_of(path))

# ---------- streaming: transcribe while they're still recording, so Stop only waits for the last few seconds ----------
STREAMS = {}           # stream id -> {user, path, words, done (seconds transcribed), lock, busy, created}
STREAM_TAIL = 2.0      # keep the newest seconds open: a word may still be in progress

def last_pause(pcm, lo, hi, min_gap=0.3):
    """Middle of the latest pause of at least min_gap seconds between lo and hi (seconds), or None."""
    import numpy as np
    fr = 320   # 20 ms frames at 16 kHz
    a, b = int(lo * 16000) // fr, int(hi * 16000) // fr
    if b - a < 10: return None
    frames = pcm[a * fr: b * fr].reshape(-1, fr)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    quiet = rms < max(0.004, 0.12 * float(np.percentile(rms, 90)))
    need, run = int(min_gap / 0.02), 0
    for i in range(len(quiet) - 1, -1, -1):
        run = run + 1 if quiet[i] else 0
        if run >= need:
            j = i + run   # walk to the end of this quiet stretch
            while j < len(quiet) and quiet[j]: j += 1
            return (a + (i + j) / 2) * 0.02
    return None

def stream_catch_up(st, final=False):
    """Transcribe whole phrases as they finish: cut only at pauses, so no word is ever split between pieces."""
    with st["lock"]:
        try: pcm = pcm_of(st["path"])
        except Exception: return
        avail = len(pcm) / 16000
        if final: cut = avail
        else:
            if avail - STREAM_TAIL - st["done"] < 5: return
            cut = last_pause(pcm, st["done"] + 1, avail - 0.5)
            if cut is None: return   # no pause yet: wait for one
        if cut - st["done"] < 0.3: return
        context = " ".join(w["w"] for w in st["words"][-40:])
        new = transcribe_pcm(pcm[int(st["done"] * 16000): int(cut * 16000)], st["done"], context)
        if new is None: st["broken"] = True; return
        st["words"] += new; st["done"] = cut

def stream_kick(st):
    if st["busy"]: return
    st["busy"] = True
    def run():
        try: stream_catch_up(st)
        except Exception: traceback.print_exc()
        finally: st["busy"] = False
    threading.Thread(target=run, daemon=True).start()

def prosody(path, words):
    """Per-word pitch and loudness from Praat, plus whole-take stats. None if parselmouth is missing."""
    try:
        import numpy as np, parselmouth
    except ImportError:
        return None
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    snd = parselmouth.Sound(np.frombuffer(pcm, np.float32).astype(np.float64), sampling_frequency=16000)
    pitch = snd.to_pitch_ac(time_step=0.01, pitch_floor=60, pitch_ceiling=500)
    f0, ft = pitch.selected_array["frequency"], pitch.xs()
    inten = snd.to_intensity(time_step=0.01, minimum_pitch=75); db, it = inten.values[0], inten.xs()
    voiced = f0[f0 > 0]
    if len(voiced) < 20: return None
    ref = float(np.median(voiced)); st = lambda hz: 12 * np.log2(hz / ref)
    out, prev_end = [], None
    for w in words:
        fm = (ft >= w["s"]) & (ft <= w["e"]) & (f0 > 0); im = (it >= w["s"]) & (it <= w["e"])
        sv = st(f0[fm]) if fm.any() else None
        peak = float(db[im].max()) if im.any() else None
        tail = it >= w["e"] - max(0.06, (w["e"] - w["s"]) * 0.25)
        tail_db = float(db[im & tail].mean()) if (im & tail).any() else None
        out.append({**w, "gap": round(w["s"] - prev_end, 2) if prev_end is not None else None,
                    "st": round(float(np.median(sv)), 1) if sv is not None else None,
                    "move": round(float(sv.max() - sv.min()), 1) if sv is not None and len(sv) > 2 else None,
                    "db": round(peak, 1) if peak is not None else None,
                    "tail": round(tail_db - peak, 1) if peak is not None and tail_db is not None else None})
        prev_end = w["e"]
    sv = st(voiced); talk = sum(w["e"] - w["s"] for w in words)
    gaps = [o["gap"] for o in out if o["gap"] and o["gap"] > 0.25]
    return {"words": out, "stats": {
        "median_pitch_hz": round(ref), "pitch_range_semitones_p10_p90": round(float(np.percentile(sv, 90) - np.percentile(sv, 10)), 1),
        "pitch_sd_semitones": round(float(sv.std()), 1), "loudness_sd_db": round(float(db[db > db.max() - 40].std()), 1),
        "words": len(words), "wpm_overall": round(len(words) / max(words[-1]["e"] - words[0]["s"], 1) * 60) if words else None,
        "articulation_rate_wpm": round(len(words) / max(talk, 1) * 60), "pauses_over_250ms": len(gaps),
        "mean_pause_s": round(sum(gaps) / len(gaps), 2) if gaps else 0}}

def mmss(t): t = max(0, t); return "%02d:%04.1f" % (int(t // 60), t % 60)

def _obj(props, req=None): return {"type": "object", "properties": props, "required": req or list(props), "additionalProperties": False}
FEEDBACK_SCHEMA = _obj({
    "summary": {"type": "string", "description": "2-4 sentences: overall impression of this read, in plain second person. Link examples as [words](start-end)."},
    "scores": _obj({k: {"type": "integer", "description": "1 (weak) to 5 (excellent)"} for k in
                    ("pitch_variation", "stress", "pace_and_pauses", "articulation", "endings", "fluency")}),
    "wpm": {"type": "integer", "description": "Speaking rate in words per minute, excluding long silences."},
    "strengths": {"type": "array", "items": {"type": "string"}, "description": "Each with at least one linked example: [words](start-end)."},
    "priorities": {"type": "array", "items": {"type": "string"}, "description": "One line each: the 2-3 lesson titles' headline changes, most important first."},
    "moments": {"type": "array", "description": "Specific moments worth hearing again, in time order. Aim for 5-12. Include good moments too.",
                "items": _obj({
                    "start": {"type": "number", "description": "Seconds from the start of the recording."},
                    "end": {"type": "number"},
                    "quote": {"type": "string", "description": "The words spoken in this span."},
                    "kind": {"type": "string", "enum": ["pitch", "stress", "pace", "pause", "articulation", "ending", "fluency", "breath", "good"]},
                    "note": {"type": "string", "description": "What you hear and what's causing it physically."},
                    "try": {"type": "string", "description": "How to fix it physically next time - a body or breath cue, not just 'say it slower' (empty for good moments)."}})},
    "progress": {"type": "string", "description": "How this compares with the earlier sessions listed, or empty if none."},
    "lessons": {"type": "array", "description": "The 2-3 most important things to work on, as a voice teacher would teach them, most important first.",
                "items": _obj({
                    "title": {"type": "string", "description": "Short, plain-language name, e.g. 'Bring the sound forward out of your throat'."},
                    "area": {"type": "string", "enum": ["breath", "alignment", "tension", "resonance", "onset", "articulation", "pitch", "thought"]},
                    "hear": {"type": "string", "description": "What you hear, with linked examples [words](start-end)."},
                    "why": {"type": "string", "description": "The physical or mental cause underneath it, in plain language."},
                    "exercise": {"type": "string", "description": "Name of the exercise."},
                    "steps": {"type": "array", "items": {"type": "string"}, "description": "How to do it, step by step, with body cues."},
                    "dose": {"type": "string", "description": "How long and how often, e.g. '2 minutes before each read, daily this week'."},
                    "feel": {"type": "string", "description": "What it feels or sounds like when it's working."}})},
    "drill": {"type": "string", "description": "Tomorrow's 2-minute warm-up plan, built from the lessons' exercises."},
})

COACH = """You are a seasoned voice teacher - trained in the Linklater, Fitzmaurice and Lessac traditions, with years of \
coaching actors, audiobook narrators and executives one-on-one. Your student records a passage aloud as part of a 6-minute \
daily routine (warm-up, read aloud with marked stress words, shadow a professional narrator, enunciation drills). \
Their goals: a confident, resonant, expressive voice; landing the important words; purposeful pacing; clean, finished words.

Coach like you would in the room, not like a speech-analytics tool. Anyone can count words per minute; your value is hearing \
WHY the voice does what it does and knowing the exercise that fixes it. For every problem, diagnose the physical cause \
underneath it:
- breath: shallow or high-chest breathing, running out of air before the phrase ends, no support under the final words, \
held breath, audible gasps
- alignment: collapsed chest or forward head (often heard as a pressed, small or thin sound), locked knees, slumped reading posture
- tension: tight jaw (muffled vowels, little mouth space), tongue-root or neck tension, a pushed or squeezed tone, a high larynx
- resonance and placement: sound swallowed at the back of the throat, stuck in the nose, or forward "in the mask"; \
thin vs. rich; dull vs. bright
- onset and phonation: hard glottal attacks, breathiness, vocal fry at phrase ends, pitch dropping out of support
- articulators: lazy lips, tongue tip not reaching the ridge, jaw doing the tongue's job, final consonants unreleased
- thought and intention: reading words instead of thinking the thoughts, no picture behind the image, \
pausing for breath instead of for meaning, rushing the connective tissue between big moments
Then prescribe the work: name a specific exercise (e.g. lip trills on a slide, straw phonation, "hng-ah" drops, humming into \
"mm-hmm" for forward placement, rib-swing breathing, the cork or "bone prop" drill, consonant "plosive pops", \
whispered intention reads, standing wall alignment, semi-supine breathing, yawn-sigh, tongue-out reading, \
reading one thought per breath, "word-painting" an image before you say it) and say exactly how to do it, \
how long and how often, and what they should feel when it's working. Give physical cues a real coach would give: \
where to feel the vibration, what to release, how to stand, where the breath should go, what to imagine.

The measurements you are given are your private evidence, like a teacher glancing at a spectrogram. \
Don't lead with them or turn feedback into numbers; mention one only when it truly helps the student hear the problem.

Be specific and honest. Praise only what is genuinely good. For each moment, give start and end in seconds from the start of \
the recording.

The student can click to hear anything you point to, so link every example. Whenever any text you write (summary, strengths, \
priorities, progress, notes, tips) quotes or refers to something specific in THIS recording, write it as [words as spoken](start-end) \
with times in seconds, e.g. "you dropped endings ([made a my mind](41.2-42.0), [love th'old man](63.5-64.3))" or \
"the pause before [Hearken!](38.9-39.6) was excellent". Put the words as you actually heard them inside the brackets. \
Every quoted phrase or word from this recording gets a link; never refer to a moment without one. \
Don't link things from earlier sessions, since those are different recordings."""

LISTEN = """Listen to the audio itself - pitch contour, stress, rhythm, pauses, breath, resonance, articulation, character - not just the words. \
Below the audio you also get a machine transcript with Praat measurements for every word, to help you place times precisely \
and to back up what you hear with numbers where useful:
- st: median pitch in semitones relative to the speaker's own median; move: pitch movement within the word in semitones.
- db: peak loudness; tail: loudness of the word's last quarter relative to its peak; gap: silence before the word; p: transcriber confidence.
These are machine measurements and can mislead (e.g. a soft but clearly released final consonant can show a low tail). \
Where they disagree with what you hear, trust your ears."""

MEASURED = """You cannot hear the recording. Instead you get a machine transcript plus acoustic measurements for every word, \
taken with Praat. Read them the way a coach reads a pitch trace:
- st: the word's median pitch in semitones relative to the speaker's own median (0 = their usual pitch; +3 = noticeably higher).
- move: pitch movement within the word in semitones. Stressed, expressive words usually move 3+; flat reading sits under 2.
- db: the word's peak loudness. Compare with neighbours; stressed words are usually a few dB louder.
- tail: loudness of the word's last quarter relative to its peak. Around -6 is normal; -15 or lower often means a swallowed or dropped ending.
- gap: silence before the word in seconds. Pauses at commas and full stops are good; pauses mid-phrase, or none at a full stop, are not.
- p: the transcriber's confidence. Low confidence on a common word often means it was mumbled or under-articulated.
Compare the transcript with the passage to find skipped, added or misread words.
Base every judgement on this evidence and cite the numbers when useful (e.g. "nervous moved only 1.2 semitones"). \
You cannot judge voice quality, resonance or breathiness directly; do not claim to have heard them. \
Score articulation and endings from the tail, confidence and misread evidence."""

def when(ts, now_ts):
    """How an earlier session reads in the prompt: a redo the same day says so, so the coach compares within the day."""
    hrs = (now_ts - ts) / 3.6e6
    return ("%.0f minutes earlier today" % (hrs * 60) if hrs < 1 else
            "%.0f hours earlier today" % hrs if hrs < 12 else time.strftime("%b %d", time.localtime(ts / 1000)))

GOALS = {"work": "sound confident and clear at work: meetings, calls, interviews",
         "stage": "present and speak in public with presence",
         "clarity": "be understood easily: clearer English, crisper consonants, steadier pace",
         "perform": "read aloud and perform: audiobooks, acting, storytelling"}
def user_goal(uid):
    st = q("SELECT json FROM state WHERE user_id=?", (uid,), one=True)
    g = json.loads(st["json"]).get("goal") if st else None
    return GOALS.get(g)

def build_prompt(r, words, history, pros=None, listening=False):
    parts = [COACH, "\n\n", LISTEN if listening else MEASURED, "\n\nRecording: %s, %s, %.0f seconds." % (r["kind"], r["label"] or "", r["secs"] or 0)]
    goal = user_goal(r["user_id"])
    if goal: parts.append("\n\nWhat they want their voice to do: " + goal + ". Prioritise what matters most for that, and say why it matters for it.")
    if r["text"]:
        parts.append("\n\nThe passage they were reading:\n\"\"\"\n%s\n\"\"\"" % r["text"])
    marks = json.loads(r["marks"] or "[]")
    if marks:
        parts.append("\n\nWords they marked to stress (with bigger pitch movement): " + ", ".join(marks))
    if pros:
        parts.append("\n\nWhole-take measurements: " + json.dumps(pros["stats"]))
        parts.append("\n\nPer word (start-end seconds | word | st | move | db | tail | gap | p):\n" + "\n".join(
            "%.2f-%.2f | %s | %s | %s | %s | %s | %s | %s" % (w["s"], w["e"], w["w"], w["st"], w["move"], w["db"], w["tail"], w["gap"], w.get("p"))
            for w in pros["words"]))
    elif words:
        lines, cur, t0 = [], [], None
        for w in words:
            if t0 is None: t0 = w["s"]
            cur.append(w["w"])
            if len(cur) >= 12 or w["w"].endswith((".", "?", "!")):
                lines.append("[%s] %s" % (mmss(t0), " ".join(cur))); cur, t0 = [], None
        if cur: lines.append("[%s] %s" % (mmss(t0), " ".join(cur)))
        parts.append("\n\nTranscript with start times (MM:SS.s):\n" + "\n".join(lines))
    if history:
        parts.append("\n\nTheir earlier sessions (oldest first), for the progress note. One from earlier today means this is a redo: "
                     "say what changed since that attempt first, then the longer trend.\n" + "\n".join(
            "- %s: %s Scores %s. Priorities then: %s" % (when(h["ts"], r["ts"]), h["summary"], json.dumps(h["scores"]), "; ".join(h["priorities"]))
            for h in history))
        plan = history[-1].get("_plan")
        if plan:
            parts.append("\n\nAfter that last session you set them this focus for today, which they practised in the warm-up: "
                         "reading focus: %s; warm-up: %s. Say in the progress note whether it is working, citing examples."
                         % (plan.get("read_focus", ""), ", ".join(w["name"] for w in plan.get("warmup", []))))
    return "".join(parts)

_claude = None
def call_claude(prompt, schema=None, effort="high"):
    global _claude
    import anthropic
    if _claude is None:
        _claude = anthropic.Anthropic(base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://llm.int.exe.xyz"),
                                      api_key=os.environ.get("ANTHROPIC_API_KEY", "implicit"), max_retries=3, timeout=600,
                                      default_headers={"anthropic-version": "2023-06-01"})  # SDK 2.x sends a date the gateway rejects
    with _claude.messages.stream(
        model=CLAUDE_MODEL, max_tokens=32000,
        thinking={"type": "adaptive"}, output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema or FEEDBACK_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal": raise RuntimeError("Claude declined to review this take.")
    if msg.stop_reason == "max_tokens": raise RuntimeError("Claude's review ran too long and was cut off.")
    return json.loads(next(b.text for b in msg.content if b.type == "text"))

class Unavailable(Exception): pass

BAD_TEXT = re.compile(r"\bjson\b|standard format|just kidding|as an ai\b|output format|<\|", re.I)
def sane(out, schema):
    """Reject well-formed but broken answers (missing fields, empty notes, format chatter) before anyone sees them."""
    if not isinstance(out, dict): return False
    for k in schema.get("required", []):
        if k not in out: return False
    texts = []
    def walk(v):
        if isinstance(v, str): texts.append(v)
        elif isinstance(v, dict): [walk(x) for x in v.values()]
        elif isinstance(v, list): [walk(x) for x in v]
    walk(out)
    if any(BAD_TEXT.search(t) for t in texts): return False
    main = out.get("note") if "grade" in schema.get("properties", {}) else out.get("summary")
    return isinstance(main, str) and len(main.strip()) >= 30

def ask_listeners(prompt, audios, schema=None):
    """Try each listening model in order; retry a broken answer once before moving on."""
    schema = schema or FEEDBACK_SCHEMA
    for via, m in LISTENERS:
        for attempt in range(2):
            try: out = (call_openrouter if via == "openrouter" else call_gemini)(m, prompt, audios, schema)
            except Unavailable as e: print("%s %s unavailable: %s" % (via, m, e), flush=True); break
            except (ValueError, KeyError) as e: print("%s %s unreadable answer: %s" % (via, m, e), flush=True); continue
            if sane(out, schema): return out, m.split("/")[-1]
            print("%s %s gave a broken answer (attempt %d): %s" % (via, m, attempt + 1, json.dumps(out)[:300]), flush=True)
    return None, None

def call_openrouter(model, prompt, audio_b64, schema=None):
    body = {
        "model": model, "max_tokens": 32000, "reasoning": {"effort": "high"},
        # only providers that honour response_format (others let the model ramble) and that don't keep or train on the data
        "provider": {"require_parameters": True, "data_collection": "deny"},
        "messages": [{"role": "user", "content": [
            *[{"type": "input_audio", "input_audio": {"data": a, "format": "mp3"}} for a in ([audio_b64] if isinstance(audio_b64, str) else audio_b64)],
            {"type": "text", "text": prompt}]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "feedback", "strict": True, "schema": schema or FEEDBACK_SCHEMA}},
    }
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
        "HTTP-Referer": "https://voice-daily.exe.xyz/", "X-Title": "Ten Minute Voice"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp: out = json.load(resp)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        if e.code in (402, 404, 408, 429) or e.code >= 500: raise Unavailable("%s %s" % (e.code, msg))  # no credit / busy: next model
        raise RuntimeError("OpenRouter %s: %s" % (e.code, msg))
    if out.get("error"): raise Unavailable(str(out["error"])[:300])
    text = (out.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if not text.strip(): raise Unavailable("OpenRouter returned no text")
    return json.loads(text[text.find("{"):text.rfind("}") + 1])

def call_gemini(model, prompt, audio_b64, schema=None):
    key = os.environ.get("GEMINI_API_KEY")
    body = {
        "contents": [{"role": "user", "parts": [*[{"inline_data": {"mime_type": "audio/mp3", "data": a}} for a in ([audio_b64] if isinstance(audio_b64, str) else audio_b64)], {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema or FEEDBACK_SCHEMA,
                             "thinkingConfig": {"thinkingLevel": os.environ.get("GEMINI_THINKING", "high")}},
    }
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                out = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code == 429 or e.code == 404: raise Unavailable("%s %s" % (e.code, msg))  # no quota / no access: next model
            if e.code >= 500 and attempt == 0: time.sleep(8); continue
            if e.code >= 500: raise Unavailable("%s %s" % (e.code, msg))
            raise RuntimeError("Gemini %s: %s" % (e.code, msg))
    cand = (out.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    if not text: raise RuntimeError("Gemini returned no text (%s)" % cand.get("finishReason"))
    return json.loads(text)

CITE = re.compile(r"\[([^\]]+)\]\(\s*(\d+(?:\.\d+)?)\s*(?:[-\u2013]\s*(\d+(?:\.\d+)?))?\s*s?\s*\)")

_norm = lambda w: re.sub(r"[^a-z0-9']", "", w.lower())
def find_span(quote, words):
    """Where in the transcript was this quote said? Best-matching run of words, or None."""
    qt = [x for x in map(_norm, quote.split()) if x]
    if not qt or not words: return None
    ws = [_norm(w["w"]) for w in words]
    best, at = 0, None
    for i in range(len(ws)):
        hit = sum(1 for j, x in enumerate(qt) if i + j < len(ws) and ws[i + j] == x)
        if hit > best: best, at = hit, i
    if best < max(1, (len(qt) + 1) // 2): return None
    end = min(at + len(qt), len(words)) - 1
    return words[at]["s"], words[end]["e"]

def link_quotes(text, words):
    """Turn quotes the model marked without times ({{like this}}) into [like this](start-end) using the transcript."""
    def one(m):
        q = m[1].strip().strip(",.;:!?").strip()
        sp = find_span(q, words)
        return "[%s](%.1f-%.1f)" % (q, sp[0], sp[1]) if sp else "\u201c%s\u201d" % q
    return re.sub(r"\{\{([^{}]+)\}\}", one, text or "")

def snap(fb, words, dur):
    """Keep moment and [words](start-end) link times inside the recording; sort moments."""
    def fix(text):
        text = link_quotes(text, words)
        def one(m):
            s0 = max(0.0, min(float(m[2]), dur)); e0 = max(s0, min(float(m[3] or s0 + 1.5), dur))
            return "[%s](%.1f-%.1f)" % (m[1], s0, e0)
        return CITE.sub(one, text) if isinstance(text, str) else text
    for k in ("summary", "progress", "drill"): fb[k] = fix(fb.get(k, ""))
    for k in ("strengths", "priorities"): fb[k] = [fix(x) for x in fb.get(k, [])]
    for m in fb.get("moments", []): m["note"], m["try"] = fix(m.get("note", "")), fix(m.get("try", ""))
    for l in fb.get("lessons", []):
        for k in ("hear", "why", "feel"): l[k] = fix(l.get(k, ""))
    ms = []
    for m in fb.get("moments", []):
        s = max(0.0, min(float(m.get("start", 0)), dur)); e = max(s, min(float(m.get("end", s)), dur))
        m["start"], m["end"] = round(s, 1), round(e, 1); ms.append(m)
    fb["moments"] = sorted(ms, key=lambda m: m["start"])
    return fb

def analyze(take_id):
    r = q("SELECT * FROM takes WHERE id=?", (take_id,), one=True)
    if not r or r["status"] not in ("queued", "processing", "nokey"): return  # cancelled or already reviewed
    if not coach_ready():
        q("UPDATE takes SET status='nokey' WHERE id=?", (take_id,)); return
    q("UPDATE takes SET status='processing', error=NULL WHERE id=?", (take_id,))
    try:
        mp3 = audio_path(r)
        words = json.loads(r["words"]) if r["words"] else None
        if words is None:
            try: words = transcribe(mp3)
            except Exception: traceback.print_exc(); words = None
            if words is not None: q("UPDATE takes SET words=? WHERE id=?", (json.dumps(words), take_id))
        if r["kind"] in GRADERS: return GRADERS[r["kind"]](r, mp3, words)
        hist = [json.loads(h["feedback"]) | {"ts": h["ts"], "_plan": json.loads(h["plan"]) if h["plan"] else None} for h in q(
            "SELECT feedback, plan, ts FROM takes WHERE user_id=? AND status='done' AND kind IN ('daily','weekly','upload') AND ts<? ORDER BY ts DESC LIMIT 4", (r["user_id"], r["ts"]))][::-1]
        pros = None
        if words:
            try: pros = prosody(mp3, words)
            except Exception: traceback.print_exc()
        fb = used = None
        if LISTENERS:
            with open(mp3, "rb") as f: b64 = base64.b64encode(f.read()).decode()
            prompt = build_prompt(r, words, hist, pros, listening=True)
            fb, used = ask_listeners(prompt, b64)
        if fb is None:
            if not words: raise RuntimeError("Couldn't transcribe this take (is anything audible?).")
            fb, used = call_claude(build_prompt(r, words, hist, pros)), CLAUDE_MODEL
        fb = snap(fb, words, r["secs"] or 1e9)
        q("UPDATE takes SET status='done', feedback=?, model=?, analyzed=? WHERE id=?", (json.dumps(fb), used, int(time.time()), take_id))
    except Exception as e:
        traceback.print_exc()
        q("UPDATE takes SET status='error', error=? WHERE id=?", (str(e)[:500], take_id))
        return
    if r["kind"] in READ_KINDS: file_drills(take_id)

# ---------- drills: an exercise from a lesson, practised on its own and repeated on a schedule ----------
GRADE_SCHEMA = _obj({
    "grade": {"type": "string", "enum": ["got_it", "almost", "not_yet"]},
    "note": {"type": "string", "description": "1-2 sentences on what you heard, with linked examples [words](start-end)."},
    "tip": {"type": "string", "description": "One sentence: the single physical adjustment for the next try. Don't repeat the note. Empty if got_it."},
})
DAY = 86400
# The curriculum: each drill is a focus. One is "current" at a time; pass it in lessons on PASS_DAYS different days and it
# moves to review, coming back at REVIEW_GAPS days; pass every review and it's learned.
PASS_DAYS = 3
REVIEW_GAPS = [3, 7, 21]

def drill_json(d, attempts=None):
    out = {k: d[k] for k in ("id", "title", "area", "created", "due", "stage", "passes", "level", "flags", "from_take")} | {"lesson": json.loads(d["lesson"])}
    if attempts is not None: out["attempts"] = attempts
    return out

def word_table(pros):
    return "\n".join("%.2f-%.2f | %s | %s | %s | %s | %s | %s | %s" % (w["s"], w["e"], w["w"], w["st"], w["move"], w["db"], w["tail"], w["gap"], w.get("p"))
                     for w in pros["words"])

GRADE_INTRO = ("You are the student's voice teacher. Listen and grade this one attempt the way a teacher would in the room: "
               "got_it = the target skill is clearly there; almost = moving in the right direction but not consistent; "
               "not_yet = the old habit is still in charge. Be brief and specific, name the physical cause when you can, "
               "and link what you mention in the student's recording as [words as spoken](start-end) with times in seconds.")

def quick_grade(r, mp3, words, task, past, ref_mp3=None):
    """Short graded feedback on a focused attempt (drill, shadowing, enunciation). ref_mp3: the narrator, for shadowing."""
    pros = ref_pros = None
    if words:
        try: pros = prosody(mp3, words)
        except Exception: traceback.print_exc()
    goal = user_goal(r["user_id"])
    prompt = "\n\n".join(filter(None, [GRADE_INTRO, task, ("Their goal: " + goal + ".") if goal else "",
        "Their earlier attempts (oldest first):\n" + "\n".join("- %s: %s. %s" % (
            time.strftime("%b %d", time.localtime(a["ts"] / 1000)), a["grade"], CITE.sub(lambda m: m[1], a["note"])) for a in past) if past else "",
        ("Student recording" + (" (the second audio)" if ref_mp3 else "") + ": transcript with per-word measurements "
         "(start-end s | word | st | move | db | tail | gap | p), as supporting evidence; trust your ears:\n" + word_table(pros)) if pros else ""]))
    audios = []
    for f in ([ref_mp3] if ref_mp3 else []) + [mp3]:
        with open(f, "rb") as fh: audios.append(base64.b64encode(fh.read()).decode())
    fb = used = None
    fb, used = ask_listeners(prompt, audios, GRADE_SCHEMA)
    if fb is None:
        if not pros: raise RuntimeError("Couldn't hear this attempt (is anything audible?).")
        if ref_mp3:
            try:
                rw = transcribe(ref_mp3); ref_pros = prosody(ref_mp3, rw) if rw else None
            except Exception: traceback.print_exc()
            if ref_pros: prompt += "\n\nNarrator recording (the first audio), same measurements:\n" + word_table(ref_pros)
        fb, used = call_claude("You can't hear the audio; work from the measurements.\n\n" + prompt, schema=GRADE_SCHEMA, effort="medium"), CLAUDE_MODEL
    dur = r["secs"] or 1e9
    for k in ("note", "tip"):
        fb[k] = link_quotes(fb.get(k, ""), words)
        fb[k] = CITE.sub(lambda m: "[%s](%.1f-%.1f)" % (m[1], max(0, min(float(m[2]), dur)), max(0, min(float(m[3] or float(m[2]) + 1.5), dur))), fb.get(k, ""))
    q("UPDATE takes SET status='done', feedback=?, model=?, analyzed=? WHERE id=?", (json.dumps(fb), used, int(time.time()), r["id"]))
    return fb

def past_attempts(r, where, args):
    return [json.loads(a["feedback"]) | {"ts": a["ts"]} for a in q(
        "SELECT feedback, ts FROM takes WHERE " + where + " AND status='done' AND ts<? ORDER BY ts DESC LIMIT 3", args + (r["ts"],))][::-1]

def grade_drill(r, mp3, words):
    d = q("SELECT * FROM drills WHERE id=?", (r["drill_id"],), one=True)
    if not d: raise RuntimeError("This drill no longer exists.")
    lesson = json.loads(d["lesson"])
    task = ("They just recorded themselves doing a drill you gave them, on its own, right after reading your instructions. "
            "Judge only the skill this drill targets.\n\n"
            "The drill: %s (%s)\nThe problem it fixes: %s\nWhy it happens: %s\nExercise: %s\nSteps:\n%s\nWhen it's working: %s" % (
            d["title"], d["area"], CITE.sub(lambda m: m[1], lesson.get("hear", "")), lesson.get("why", ""), lesson.get("exercise", ""),
            "\n".join("- " + x for x in lesson.get("steps", [])), lesson.get("feel", "")) +
            ("\nThe lines they were given to say:\n" + "\n".join("- " + x for x in lesson["lines"]) if lesson.get("lines") else ""))
    fb = quick_grade(r, mp3, words, task, past_attempts(r, "drill_id=?", (d["id"],)))
    if r["lesson_id"]: progress(d, r, fb["grade"])

def grade_enun(r, mp3, words):
    task = ("This is their enunciation exercise: read the passage once slowly with exaggerated articulation "
            "(full lip rounding, crisp consonants, open vowels), then once at normal speed. Judge articulation only: "
            "consonant precision, finished final consonants, clear vowels, how actively lips, tongue tip and jaw work, "
            "and whether the clarity survives in the normal-speed pass. Grade on the normal-speed pass.\n\nThe passage (%s):\n\"\"\"%s\"\"\"" % (r["label"], r["text"]))
    quick_grade(r, mp3, words, task, past_attempts(r, "user_id=? AND kind='enun'", (r["user_id"],)))

BOOKS = None
def book(bid):
    global BOOKS
    if BOOKS is None:
        with open(os.path.join(APP, "books.json")) as f: BOOKS = {b["id"]: b for b in json.load(f)}
    return BOOKS.get(bid)

def grade_shadow(r, mp3, words):
    """Cut the narrator's audio for the sentences they shadowed, so the coach hears both side by side."""
    meta = json.loads(r["marks"] or "{}") if (r["marks"] or "").startswith("{") else {}
    b, sents = book(meta.get("book")), meta.get("sentences") or []
    sents = [x for x in sents if x.get("you_at") is not None][:20]
    if not b or not sents: raise RuntimeError("No sentences were shadowed while recording.")
    ref = audio_path(r, "narrator.mp3")
    parts = "".join("[0:a]atrim=start=%.2f:end=%.2f,asetpts=PTS-STARTPTS[a%d];" % (x["ns"], x["ne"], i) for i, x in enumerate(sents))
    graph = parts + "".join("[a%d]" % i for i in range(len(sents))) + "concat=n=%d:v=0:a=1[out]" % len(sents)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(APP, b["audio"]), "-filter_complex", graph, "-map", "[out]",
                    "-ac", "1", "-ar", "24000", "-b:a", "64k", ref], check=True, capture_output=True)
    lines, at = [], 0.0
    for i, x in enumerate(sents):
        d = x["ne"] - x["ns"]
        lines.append("%d. \"%s\" - narrator: %.1f-%.1fs in the first audio; student's repeat: %.1f-%.1fs in the second audio" % (
            i + 1, x["text"], at, at + d, x["you_at"], x.get("you_end") or x["you_at"] + d * 1.2 + 0.6))
        at += d
    task = ("This is their shadowing exercise. They hear one sentence from a professional narrator, then say it straight back, "
            "copying the narrator's pitch shape, stress, pauses, pace and energy. You get two audios: FIRST, the narrator's sentences "
            "back to back; SECOND, the student's recording. In the student's recording, compare only their repeat windows listed below; "
            "between them the narrator may be faintly audible through their speakers - ignore that. For each sentence, notice how "
            "closely they matched the narrator's delivery, then grade how well they copied it overall. In the note, compare concretely "
            "(e.g. 'he lifts \"never\" and pauses before \"night\"; you stayed level and ran through [never at night](12.1-13.0)'). "
            "Link times in the student's recording only.\n\nSentences:\n" + "\n".join(lines))
    quick_grade(r, mp3, words, task, past_attempts(r, "user_id=? AND kind='shadow'", (r["user_id"],)), ref_mp3=ref)

GRADERS = {"drill": grade_drill, "enun": grade_enun, "shadow": grade_shadow}
READ_KINDS = ("daily", "weekly", "upload")  # full reviews; the rest get a quick grade

FILE_SCHEMA = _obj({"same_as": {"type": "array", "items": {"type": "integer"},
    "description": "One entry per new lesson, in order: the number of the existing focus it works on, or -1 if it's a new habit."}})

LINES_SCHEMA = _obj({"lines": {"type": "array", "items": {"type": "string"},
    "description": "3-5 short sentences (under ~20 words each) for the student to say while doing this exercise."}})

def drill_lines(did):
    """Give a focus the words to practise it on: lines from the passage the coach heard, or original ones."""
    d = q("SELECT * FROM drills WHERE id=?", (did,), one=True)
    if not d: return
    l = json.loads(d["lesson"])
    if l.get("lines"): return
    src = q("SELECT text FROM takes WHERE id=?", (d["from_take"],), one=True)
    text = (src["text"] if src else "") or ""
    try:
        lines = call_claude("A voice teacher gave a student this exercise. Choose the words they will say while doing it: 3-5 short "
            "sentences that exercise exactly this habit (e.g. for breath through endings: sentences that end on an important word; "
            "for consonants: lines dense with the sounds they fumble). " +
            ("Take them from the passage they read, quoting exactly; you may cut a long sentence at a natural break. Put any lines "
             "the exercise steps mention first." if text.strip() else "Write original lines in plain modern English.") +
            "\n\nExercise: %s\nThe habit: %s\nSteps:\n%s" % (l.get("exercise", ""), plain_text(l.get("hear", "")), "\n".join("- " + x for x in l.get("steps", []))) +
            ("\n\nThe passage:\n\"\"\"%s\"\"\"" % text[:4000] if text.strip() else ""),
            schema=LINES_SCHEMA, effort="low")["lines"]
    except Exception:
        traceback.print_exc(); return
    l["lines"] = [x.strip() for x in lines if x.strip()][:5]
    q("UPDATE drills SET lesson=? WHERE id=?", (json.dumps(l), did))

def plain_text(t): return CITE.sub(lambda m: m[1], t or "")

def make_drill(t, n, lesson):
    did = "%s-L%d" % (t["id"], n)
    if not q("SELECT 1 FROM drills WHERE id=?", (did,), one=True):
        q("INSERT INTO drills(id,user_id,title,area,lesson,from_take,created,due,stage) VALUES(?,?,?,?,?,?,?,?,'queued')",
          (did, t["user_id"], lesson.get("title", ""), lesson.get("area", ""), json.dumps(lesson), t["id"], int(time.time()), int(time.time())))
        drill_lines(did)
    return did

def file_drills(take_id):
    """Add each new habit from a review to the curriculum queue. A habit the queue already has gets flagged again,
    which moves it up: the queue is ordered by how often reviews raise it."""
    t = q("SELECT * FROM takes WHERE id=?", (take_id,), one=True)
    lessons = (json.loads(t["feedback"]) if t and t["feedback"] else {}).get("lessons") or []
    if not lessons: return q("UPDATE takes SET filed=1 WHERE id=?", (take_id,))
    have = q("SELECT id, title, lesson FROM drills WHERE user_id=? AND stage IN ('queued','current') ORDER BY created", (t["user_id"],))
    same = [-1] * len(lessons)
    if have:
        try:
            same = call_claude("A voice student's current focuses (habits they're working on) and the lessons from their newest review. "
                "For each new lesson, say which existing focus works on the same habit (even if worded differently or using another exercise), "
                "or -1 if it's a different habit.\n\nExisting focuses:\n" + "\n".join(
                    "%d. %s: %s" % (i, d["title"], json.loads(d["lesson"]).get("exercise", "")) for i, d in enumerate(have)) +
                "\n\nNew lessons:\n" + "\n".join("- %s: %s" % (l.get("title", ""), l.get("exercise", "")) for l in lessons),
                schema=FILE_SCHEMA, effort="low")["same_as"]
        except Exception: traceback.print_exc()
    for n, l in enumerate(lessons):
        k = same[n] if n < len(same) else -1
        if 0 <= k < len(have): q("UPDATE drills SET flags=flags+1 WHERE id=?", (have[k]["id"],))
        else: make_drill(t, n, l)
    q("UPDATE takes SET filed=1 WHERE id=?", (take_id,))

def progress(d, r, grade):
    """Move a focus along the curriculum. Only lesson attempts count; free practice is just logged."""
    les = q("SELECT * FROM lessons WHERE id=?", (r["lesson_id"],), one=True)
    if not les: return
    now = int(time.time())
    if r["step"] == "drill" and d["stage"] == "current" and grade == "got_it" and d["last_pass"] != les["date"]:
        passes = d["passes"] + 1
        if passes >= PASS_DAYS:
            q("UPDATE drills SET passes=?, last_pass=?, stage='review', level=0, due=? WHERE id=?", (passes, les["date"], now + REVIEW_GAPS[0] * DAY - 4 * 3600, d["id"]))
        else:
            q("UPDATE drills SET passes=?, last_pass=? WHERE id=?", (passes, les["date"], d["id"]))
    elif r["step"] == "review" and d["stage"] == "review":
        if grade == "got_it":
            level = d["level"] + 1
            if level >= len(REVIEW_GAPS): q("UPDATE drills SET level=?, stage='learned' WHERE id=?", (level, d["id"]))
            else: q("UPDATE drills SET level=?, due=? WHERE id=?", (level, now + REVIEW_GAPS[level] * DAY - 4 * 3600, d["id"]))
        else:
            q("UPDATE drills SET due=? WHERE id=?", (now + DAY - 4 * 3600, d["id"]))

# ---------- lessons: one a day, fixed when started ----------
def lesson_json(l):
    f = q("SELECT * FROM drills WHERE id=?", (l["focus_id"],), one=True) if l["focus_id"] else None
    rv = q("SELECT * FROM drills WHERE id=?", (l["review_id"],), one=True) if l["review_id"] else None
    takes = [take_json(t) for t in q("SELECT * FROM takes WHERE lesson_id=? ORDER BY ts", (l["id"],))]
    return {k: l[k] for k in ("id", "date", "n", "kind", "page", "status", "created", "completed")} | {
        "focus": drill_json(f) if f else None, "review": drill_json(rv) if rv else None, "takes": takes}

def start_lesson(uid, date, page):
    l = q("SELECT * FROM lessons WHERE user_id=? AND date=?", (uid, date), one=True)
    if l: return l
    n = q("SELECT COUNT(*) c FROM lessons WHERE user_id=?", (uid,), one=True)["c"] + 1
    cur = q("SELECT * FROM drills WHERE user_id=? AND stage='current'", (uid,), one=True)
    if not cur:  # next focus: the habit reviews raise most, then the oldest
        cur = q("SELECT * FROM drills WHERE user_id=? AND stage='queued' ORDER BY flags DESC, created LIMIT 1", (uid,), one=True)
        if cur: q("UPDATE drills SET stage='current', passes=0 WHERE id=?", (cur["id"],))
    rv = q("SELECT * FROM drills WHERE user_id=? AND stage='review' AND due<=? ORDER BY due LIMIT 1", (uid, int(time.time())), one=True)
    kind = "checkin" if n % 7 == 0 else "daily"
    lid = "lesson-%s-%d" % (uid[:8], int(time.time() * 1000))
    q("INSERT INTO lessons(id,user_id,date,n,kind,focus_id,review_id,page,created) VALUES(?,?,?,?,?,?,?,?,?)",
      (lid, uid, date, n, kind, cur["id"] if cur else None, rv["id"] if rv else None, 0 if kind == "checkin" else int(page or 0), int(time.time())))
    return q("SELECT * FROM lessons WHERE id=?", (lid,), one=True)

def worker():
    while True:
        tid = jobs.get()
        try:
            if isinstance(tid, tuple): {"file": file_drills, "lines": drill_lines}[tid[0]](tid[1])
            else: analyze(tid)
        finally: jobs.task_done()

_apple_keys = {"at": 0, "set": None}
def verify_apple(token, nonce):
    """Check an Apple identity token: signed by Apple's current keys, for our app, unexpired, with our nonce."""
    import jwt
    if not token: raise ValueError("no token")
    if time.time() - _apple_keys["at"] > 3600:
        with urllib.request.urlopen("https://appleid.apple.com/auth/keys", timeout=10) as r: _apple_keys["set"] = json.load(r)
        _apple_keys["at"] = time.time()
    kid = jwt.get_unverified_header(token).get("kid")
    jwk = next((k for k in _apple_keys["set"]["keys"] if k["kid"] == kid), None)
    if not jwk: raise ValueError("unknown signing key")
    claims = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["RS256"], audience=APPLE_CLIENT_IDS, issuer="https://appleid.apple.com")
    if nonce and claims.get("nonce") not in (nonce, hashlib.sha256(nonce.encode()).hexdigest()): raise ValueError("nonce mismatch")
    return claims

def merge_user(src, dst):
    """A guest signed in: move their recordings, focuses, lessons and practice days into the account."""
    if src == dst: return
    for t in ("takes", "drills", "lessons"): q("UPDATE %s SET user_id=? WHERE user_id=?" % t, (dst, src))
    q("UPDATE users SET src=COALESCE(src,(SELECT src FROM users WHERE id=?)), created=MIN(created,(SELECT created FROM users WHERE id=?)) WHERE id=?", (src, src, dst))
    q("DELETE FROM sessions WHERE user_id=?", (src,))
    a, b = os.path.join(DATA, "audio", src), os.path.join(DATA, "audio", dst)
    if os.path.isdir(a):
        os.makedirs(b, exist_ok=True)
        for f in os.listdir(a): os.replace(os.path.join(a, f), os.path.join(b, f))
        os.rmdir(a)
    gs, us = [q("SELECT json FROM state WHERE user_id=?", (u,), one=True) for u in (src, dst)]
    if gs:
        g, u = json.loads(gs["json"]), json.loads(us["json"]) if us else {}
        days = {k: sorted(set(u.get("days", {}).get(k, [])) | set(v)) for k, v in g.get("days", {}).items()}
        merged = {**g, **u, "days": {**u.get("days", {}), **days}}
        q("INSERT INTO state(user_id,json,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET json=excluded.json, updated=excluded.updated",
          (dst, json.dumps(merged), int(time.time())))
        q("DELETE FROM state WHERE user_id=?", (src,))
    # one lesson per day: if both had one today, keep the account's
    for l in q("SELECT l.id FROM lessons l WHERE l.user_id=? AND EXISTS (SELECT 1 FROM lessons k WHERE k.user_id=? AND k.date=l.date AND k.created<l.created)", (dst, dst)):
        q("UPDATE takes SET lesson_id=NULL, step=NULL WHERE lesson_id=?", (l["id"],)); q("DELETE FROM lessons WHERE id=?", (l["id"],))
    q("DELETE FROM users WHERE id=?", (src,))

# ---------- http ----------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "voice"

    def log_message(self, fmt, *a): pass

    # Who's asking: a signed-in exe.dev user (headers set by the exe.dev proxy, which strips any a client sends),
    # or a guest identified by a random device cookie. Guests can do everything; signing in keeps progress across
    # devices, and whatever the guest did on this device moves into the account.
    def guest_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == GUEST_COOKIE and re.fullmatch(r"[A-Za-z0-9_-]{20,100}", v): return v
        return None

    def user(self):
        uid, tok = self.headers.get("X-ExeDev-UserID"), self.guest_token()
        if uid:
            email = self.headers.get("X-ExeDev-Email", "")
            q("INSERT INTO users(id,email,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email", (uid, email, int(time.time())))
            if tok:
                th = hashlib.sha256(tok.encode()).hexdigest()
                g = q("SELECT user_id id FROM sessions WHERE token_hash=? AND user_id LIKE 'guest-%'", (th,), one=True) or \
                    q("SELECT id FROM users WHERE guest_hash=?", (th,), one=True)
                if g: merge_user(g["id"], uid)
                q("DELETE FROM sessions WHERE token_hash=?", (th,))
                self._cookie = GUEST_COOKIE + "=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            return {"id": uid, "email": email, "guest": False}
        if tok:
            th = hashlib.sha256(tok.encode()).hexdigest()
            sess = q("SELECT u.id, u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (th,), one=True)
            if sess: return {"id": sess["id"], "email": sess["email"] or "", "guest": sess["id"].startswith("guest-")}
            g = q("SELECT id FROM users WHERE guest_hash=?", (th,), one=True)
            if g: return {"id": g["id"], "email": "", "guest": True}
        return None

    def start_session(self, uid):
        tok = secrets.token_urlsafe(32)
        q("INSERT INTO sessions(token_hash,user_id,created) VALUES(?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), uid, int(time.time())))
        self._cookie = GUEST_COOKIE + "=" + tok + "; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax"

    def apple_signin(self):
        """Sign in with Apple from the iOS app: verify Apple's signed identity token, then sign this device in.
        Whatever the guest on this device did moves into the account."""
        try: a = json.loads(self.body(1 << 16))
        except Exception: return self.send(400, {"error": "bad request"})
        try: claims = verify_apple(a.get("token", ""), a.get("nonce", ""))
        except Exception as e: return self.send(401, {"error": "Apple sign-in failed: %s" % e})
        uid = "apple-" + hashlib.sha256(claims["sub"].encode()).hexdigest()[:20]
        email = claims.get("email", "")
        q("INSERT INTO users(id,email,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET email=CASE WHEN excluded.email!='' THEN excluded.email ELSE users.email END",
          (uid, email, int(time.time())))
        cur = self.user()
        if cur and cur["guest"]: merge_user(cur["id"], uid)
        tok = self.guest_token()
        if tok: q("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
        self.start_session(uid)
        return self.send(200, {"id": uid, "email": email, "guest": False})

    def stats(self):
        """The ad-test funnel, by where people came from. Only the owner (OWNER_EMAIL, an exe.dev login) can see it."""
        owner = os.environ.get("OWNER_EMAIL", "kevin@8vc.com")
        if self.headers.get("X-ExeDev-Email") != owner:
            return self.send(302, b"", "text/plain", {"Location": "/__exe.dev/login?redirect=/stats"}) if not self.headers.get("X-ExeDev-Email") else self.send(403, "not yours", "text/plain")
        since = int(time.time()) - 30 * 86400
        rows = q("""SELECT u.id, u.created, u.src,
            (SELECT COUNT(*) FROM takes t WHERE t.user_id=u.id) recs,
            (SELECT COUNT(*) FROM lessons l WHERE l.user_id=u.id AND l.status='done') lessons,
            (SELECT COUNT(DISTINCT date(t.ts/1000,'unixepoch')) FROM takes t WHERE t.user_id=u.id) days
            FROM users u WHERE u.created>? ORDER BY u.created DESC""", (since,))
        by = {}
        for r in rows:
            sv = json.loads(r["src"]) if r["src"] else {}
            key = sv.get("utm_source") or (("ref: " + sv["ref"]) if sv.get("ref") else None) or (sv.get("referrer") and "link: " + sv["referrer"]) or "direct"
            key += (" / " + sv["utm_campaign"]) if sv.get("utm_campaign") else ""
            b = by.setdefault(key, [0, 0, 0, 0])
            b[0] += 1; b[1] += r["recs"] > 0; b[2] += r["lessons"] > 0; b[3] += r["days"] > 1
        pct = lambda a, n: "%d <small>(%d%%)</small>" % (a, round(100 * a / n)) if n else "0"
        trs = "".join("<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (k.replace("<", "&lt;"), v[0], pct(v[1], v[0]), pct(v[2], v[0]), pct(v[3], v[0]))
                      for k, v in sorted(by.items(), key=lambda kv: -kv[1][0]))
        tot = [sum(v[i] for v in by.values()) for i in range(4)]
        cost = q("SELECT COUNT(*) c FROM takes WHERE analyzed>?", (since,), one=True)["c"]
        html = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Stats · Ten Minute Voice</title>
<style>body{font:15px/1.5 system-ui;margin:0;padding:24px 16px;background:#f6f7f9;color:#16202c}main{max-width:760px;margin:auto}table{border-collapse:collapse;width:100%%;background:#fff;border:1px solid #dfe3e9;border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;text-align:right;border-bottom:1px solid #eef0f3;font-variant-numeric:tabular-nums}th:first-child,td:first-child{text-align:left}th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#5f6b7a}small{color:#5f6b7a}p{color:#5f6b7a}</style>
<main><h1>Last 30 days</h1><p>New people by where they came from. Tag ad links like <code>?utm_source=tiktok&amp;utm_campaign=confident-calls</code>.</p>
<table><tr><th>Source</th><th>Started</th><th>Recorded</th><th>Finished a lesson</th><th>Came back another day</th></tr>%s
<tr><td><b>All</b></td><td><b>%d</b></td><td><b>%s</b></td><td><b>%s</b></td><td><b>%s</b></td></tr></table>
<p>%d coach reviews and grades in this period (roughly 3–15¢ each).</p></main>""" % (trs, tot[0], pct(tot[1], tot[0]), pct(tot[2], tot[0]), pct(tot[3], tot[0]), cost)
        return self.send(200, html, "text/html; charset=utf-8")

    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[-1].strip()  # the proxy appends the real one; earlier entries are client-supplied

    def new_guest(self, src=None):
        ip = self.client_ip()
        n = q("SELECT COUNT(*) c FROM users WHERE ip=? AND created>?", (ip, int(time.time()) - 86400), one=True)["c"]
        if n >= GUESTS_PER_IP_DAY: return None
        tok = secrets.token_urlsafe(32)
        gid = "guest-" + secrets.token_hex(8)
        q("INSERT INTO users(id,email,created,ip,src) VALUES(?,?,?,?,?)", (gid, "", int(time.time()), ip, json.dumps(src)[:1000] if src else None))
        self.start_session(gid)
        return {"id": gid, "email": "", "guest": True}

    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "no-cache")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        if getattr(self, "_cookie", None): self.send_header("Set-Cookie", self._cookie); self._cookie = None
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def send_file(self, path, ctype=None, cache="no-cache"):
        """Static file with Range support (needed for seeking audio)."""
        if not os.path.isfile(path): return self.send(404, {"error": "not found"})
        size = os.path.getsize(path); start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            if m[1]: start = int(m[1]); end = int(m[2]) if m[2] else size - 1
            else: start = size - int(m[2])
            end = min(end, size - 1)
            if start > end: return self.send(416, b"", headers={"Content-Range": "bytes */%d" % size})
        self.send_response(206 if m and (m[1] or m[2]) else 200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache)
        if m and (m[1] or m[2]): self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD": return
        with open(path, "rb") as f:
            f.seek(start); left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk: break
                self.wfile.write(chunk); left -= len(chunk)

    def body(self, limit=60 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit: raise ValueError("too large")
        return self.rfile.read(n)

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        u = urlparse(self.path); p = u.path
        if p in STATIC or (p.startswith("/audio/") and re.fullmatch(r"/audio/[a-z0-9_-]+\.mp3", p)):
            if p in ("/privacy", "/terms"):   # legal pages carry the contact address from the environment
                with open(os.path.join(APP, STATIC[p])) as f:
                    return self.send(200, f.read().replace("{{CONTACT}}", os.environ.get("CONTACT_EMAIL", "the contact address on our App Store page")), "text/html; charset=utf-8")
            return self.send_file(os.path.join(APP, STATIC.get(p, p.lstrip("/"))),
                                  cache="public, max-age=86400" if p.startswith("/audio/") else "no-cache")
        if p == "/stats": return self.stats()
        if not p.startswith("/api/"): return self.send(404, {"error": "not found"})
        if p == "/api/signin": return self.send(302, b"", "text/plain", {"Location": "/__exe.dev/login?redirect=/"})
        me = self.user()
        if not me: return self.send(401, {"error": "login required", "login": "/__exe.dev/login?redirect=/"})
        if p == "/api/me":
            return self.send(200, {**me, "coach": coach_ready(), "model": MODEL})
        if p == "/api/signin":  # exe.dev login, then back home; the guest cookie rides along so their progress moves over
            return self.send(302, b"", "text/plain", {"Location": "/__exe.dev/login?redirect=/"})
        if p == "/api/state":
            r = q("SELECT json FROM state WHERE user_id=?", (me["id"],), one=True)
            return self.send(200, json.loads(r["json"]) if r else {})
        if p == "/api/takes":
            return self.send(200, [take_json(r) for r in q("SELECT * FROM takes WHERE user_id=? ORDER BY ts DESC", (me["id"],))])
        if p == "/api/drills":
            att = {}
            for a in q("SELECT * FROM takes WHERE user_id=? AND kind='drill' ORDER BY ts", (me["id"],)): att.setdefault(a["drill_id"], []).append(take_json(a))
            return self.send(200, [drill_json(d, att.get(d["id"], [])) for d in q(
                "SELECT * FROM drills WHERE user_id=? ORDER BY CASE stage WHEN 'current' THEN 0 WHEN 'review' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END, flags DESC, created", (me["id"],))])
        if p == "/api/lessons":
            return self.send(200, [lesson_json(l) for l in q("SELECT * FROM lessons WHERE user_id=? ORDER BY n DESC", (me["id"],))])
        m = re.fullmatch(r"/api/takes/([\w:.-]+)(/audio)?", p)
        if m:
            r = q("SELECT * FROM takes WHERE id=? AND user_id=?", (m[1], me["id"]), one=True)
            if not r: return self.send(404, {"error": "not found"})
            return self.send_file(audio_path(r), "audio/mpeg") if m[2] else self.send(200, take_json(r))
        self.send(404, {"error": "not found"})

    def do_PUT(self):
        me = self.user()
        if not me: return self.send(401, {"error": "login required"})
        if urlparse(self.path).path != "/api/state": return self.send(404, {"error": "not found"})
        try: st = json.loads(self.body(2 << 20))
        except Exception: return self.send(400, {"error": "bad json"})
        q("INSERT INTO state(user_id,json,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET json=excluded.json, updated=excluded.updated",
          (me["id"], json.dumps(st), int(time.time())))
        self.send(200, {"ok": True})

    # ---- moving takes from the old GitHub Pages site ----
    # That site can't carry the exe.dev login, so the new site hands it a short-lived token
    # (POST /api/import-token), and it uploads to /api/import/* with the token instead.
    def cors(self):
        o = self.headers.get("Origin", "")
        return {"Access-Control-Allow-Origin": o, "Vary": "Origin"} if o in IMPORT_ORIGINS else {}

    def do_OPTIONS(self):
        self.send(204, b"", "text/plain", {**self.cors(), "Access-Control-Allow-Methods": "POST",
                  "Access-Control-Allow-Headers": "Content-Type, X-Take-Meta, X-Import-Token", "Access-Control-Max-Age": "600"})

    def import_user(self):
        t = IMPORT_TOKENS.get(self.headers.get("X-Import-Token", ""))
        return {"id": t[0]} if t and t[1] > time.time() else None

    def do_POST(self):
        p = urlparse(self.path).path
        if p.startswith("/api/import/"):
            me = self.import_user()
            if not me: return self.send(401, {"error": "import link expired; start again from the new site"}, headers=self.cors())
            if p == "/api/import/take": return self.save_take(me, extra=self.cors())
            if p == "/api/import/state":
                try: old = json.loads(self.body(2 << 20))
                except Exception: return self.send(400, {"error": "bad json"}, headers=self.cors())
                r = q("SELECT json FROM state WHERE user_id=?", (me["id"],), one=True)
                cur = json.loads(r["json"]) if r else {}
                merged = {**old, **cur}  # the account's settings win; practice days are combined
                days = {k: list(v) for k, v in (cur.get("days") or {}).items()}
                for k, v in (old.get("days") or {}).items(): days[k] = sorted(set(days.get(k, [])) | set(v))
                merged["days"] = days
                q("INSERT INTO state(user_id,json,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET json=excluded.json, updated=excluded.updated",
                  (me["id"], json.dumps(merged), int(time.time())))
                return self.send(200, {"ok": True}, headers=self.cors())
            return self.send(404, {"error": "not found"}, headers=self.cors())
        if p == "/api/apple": return self.apple_signin()
        if p == "/api/signout":
            tok = self.guest_token()
            if tok: q("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
            self._cookie = GUEST_COOKIE + "=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            return self.send(200, {"ok": True})
        if p == "/api/guest":
            try: src = {k: str(v)[:120] for k, v in (json.loads(self.body(1 << 12) or b"{}").get("src") or {}).items()
                        if k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "ref", "referrer")}
            except Exception: src = None
            me = self.user() or self.new_guest(src)
            return self.send(200, me) if me else self.send(429, {"error": "Too many new accounts from this network today. Try again tomorrow, or sign in."})
        me = self.user()
        if not me: return self.send(401, {"error": "login required"})
        if p == "/api/import-token":
            for k in [k for k, v in IMPORT_TOKENS.items() if v[1] < time.time()]: IMPORT_TOKENS.pop(k, None)
            tok = uuid.uuid4().hex + uuid.uuid4().hex
            IMPORT_TOKENS[tok] = (me["id"], time.time() + 1800)
            return self.send(200, {"token": tok})
        if p == "/api/takes": return self.save_take(me)
        if p == "/api/stream":
            for k in [k for k, v in STREAMS.items() if v["created"] < time.time() - 3600]:
                try: os.remove(STREAMS.pop(k)["path"])
                except OSError: pass
            sid = secrets.token_hex(12)
            os.makedirs(os.path.join(DATA, "streams"), exist_ok=True)
            STREAMS[sid] = {"user": me["id"], "path": os.path.join(DATA, "streams", sid + ".part"), "words": [], "done": 0.0,
                            "lock": threading.Lock(), "busy": False, "created": time.time()}
            open(STREAMS[sid]["path"], "wb").close()
            return self.send(200, {"id": sid})
        m = re.fullmatch(r"/api/stream/([0-9a-f]{24})", p)
        if m:
            st = STREAMS.get(m[1])
            if not st or st["user"] != me["id"]: return self.send(409, {"error": "unknown stream"})
            try: chunk = self.body(16 << 20)
            except ValueError: return self.send(413, {"error": "too large"})
            with open(st["path"], "ab") as f: f.write(chunk)
            stream_kick(st)
            return self.send(200, {"ok": True, "transcribed": round(st["done"], 1)})
        if p == "/api/lesson":  # today's lesson: made once per date, then fixed
            try: a = json.loads(self.body(1 << 16))
            except Exception: return self.send(400, {"error": "bad request"})
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.get("date", "")): return self.send(400, {"error": "bad date"})
            return self.send(200, lesson_json(start_lesson(me["id"], a["date"], a.get("page"))))
        m = re.fullmatch(r"/api/lesson/([\w.-]+)/done", p)
        if m:
            q("UPDATE lessons SET status='done', completed=? WHERE id=? AND user_id=? AND status='active'", (int(time.time()), m[1], me["id"]))
            l = q("SELECT * FROM lessons WHERE id=? AND user_id=?", (m[1], me["id"]), one=True)
            return self.send(200, lesson_json(l)) if l else self.send(404, {"error": "not found"})
        m = re.fullmatch(r"/api/takes/([\w:.-]+)/analyze", p)
        if m:
            r = q("SELECT * FROM takes WHERE id=? AND user_id=?", (m[1], me["id"]), one=True)
            if not r: return self.send(404, {"error": "not found"})
            if r["status"] in ("queued", "processing"): return self.send(200, take_json(r))
            q("UPDATE takes SET status='queued', error=NULL WHERE id=?", (r["id"],))
            self.queue_if_allowed(me, r["id"])
            return self.send(200, take_json(q("SELECT * FROM takes WHERE id=?", (r["id"],), one=True)))
        self.send(404, {"error": "not found"})

    def save_take(self, me, extra=None):
        """Body: raw audio. Meta (kind, label, ts, text, marks) in the X-Take-Meta header as URI-encoded JSON."""
        extra = extra or {}
        try: meta = json.loads(unquote(self.headers.get("X-Take-Meta") or "{}"))
        except Exception: meta = {}
        try: raw = self.body()
        except ValueError: return self.send(413, {"error": "recording too large"}, headers=extra)
        st = None
        sid = self.headers.get("X-Stream-Id")
        if sid:  # most of the recording already arrived while they spoke; this body is the last few seconds
            st = STREAMS.pop(sid, None)
            if not st or st["user"] != me["id"]: return self.send(409, {"error": "unknown stream"}, headers=extra)
            with open(st["path"], "ab") as f: f.write(raw)
            with open(st["path"], "rb") as f: raw = f.read()
        if len(raw) < 1000: return self.send(400, {"error": "empty recording"}, headers=extra)
        if meta.get("ts"):  # same take sent twice (re-import, retry): keep the first
            dup = q("SELECT * FROM takes WHERE user_id=? AND ts=?", (me["id"], int(meta["ts"])), one=True)
            if dup: return self.send(200, {**take_json(dup), "duplicate": True}, headers=extra)
        tid = "%s-%d-%s" % (re.sub(r"\W", "", meta.get("kind", "daily"))[:10] or "daily", int(time.time() * 1000), uuid.uuid4().hex[:6])
        row = {"id": tid, "user_id": me["id"]}
        os.makedirs(os.path.dirname(audio_path(row)), exist_ok=True)
        mime = (self.headers.get("Content-Type") or "audio/webm").split(";")[0]
        ext = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac", "audio/ogg": "ogg",
               "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/flac": "flac", "video/mp4": "mp4", "video/webm": "webm"}.get(mime, "bin")
        orig = audio_path(row, "orig." + ext)
        with open(orig, "wb") as f: f.write(raw)
        # mono mp3: plays everywhere (incl. iOS), seeks cleanly, small enough to send inline
        cp = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", orig, "-ac", "1", "-ar", "24000", "-b:a", "64k", audio_path(row)], capture_output=True)
        if cp.returncode:
            os.remove(orig); return self.send(400, {"error": "could not decode audio: " + cp.stderr.decode()[:200]}, headers=extra)
        try: secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", audio_path(row)], capture_output=True, text=True).stdout.strip())
        except ValueError: secs = float(meta.get("secs") or 0)
        ts = int(meta.get("ts") or time.time() * 1000)
        words = None
        if st:
            try: stream_catch_up(st, final=True); words = None if st.get("broken") else st["words"]
            except Exception: traceback.print_exc()
            try: os.remove(st["path"])
            except OSError: pass
        drill = q("SELECT id FROM drills WHERE id=? AND user_id=?", (meta.get("drill_id"), me["id"]), one=True) if meta.get("drill_id") else None
        les = q("SELECT id FROM lessons WHERE id=? AND user_id=? AND status='active'", (meta.get("lesson_id"), me["id"]), one=True) if meta.get("lesson_id") else None
        q("INSERT INTO takes(id,user_id,kind,label,ts,secs,text,marks,mime,status,drill_id,lesson_id,step,words) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (tid, me["id"], "drill" if drill else meta.get("kind", "daily"), meta.get("label", ""), ts, round(secs, 2), meta.get("text", ""),
           json.dumps(meta.get("shadow") if meta.get("kind") == "shadow" else meta.get("marks", [])), mime, "queued", drill["id"] if drill else None,
           les["id"] if les else None, meta.get("step") if les else None, json.dumps(words) if words is not None else None))
        self.queue_if_allowed(me, tid)
        return self.send(200, take_json(q("SELECT * FROM takes WHERE id=?", (tid,), one=True)), headers=extra)

    def queue_if_allowed(self, me, tid):
        day = int(time.time()) - 86400
        n = q("SELECT COUNT(*) c FROM takes WHERE user_id=? AND analyzed>?", (me["id"], day), one=True)["c"]
        g = me.get("guest") and q("SELECT COUNT(*) c FROM takes WHERE user_id LIKE 'guest-%' AND analyzed>?", (day,), one=True)["c"]
        if n >= DAILY_LIMIT:
            q("UPDATE takes SET status='error', error=? WHERE id=?", ("Daily feedback limit reached (%d). Try again tomorrow." % DAILY_LIMIT, tid))
        elif g and g >= GUEST_REVIEWS_DAY:
            q("UPDATE takes SET status='error', error=? WHERE id=?", ("The coach is very busy today. Sign in to keep going, or try again tomorrow.", tid))
        else:
            jobs.put(tid)

    def do_DELETE(self):
        me = self.user()
        if not me: return self.send(401, {"error": "login required"})
        if urlparse(self.path).path == "/api/account":   # everything: recordings, notes, focuses, lessons, settings
            for t in ("takes", "drills", "lessons", "state", "sessions"): q("DELETE FROM %s WHERE user_id=?" % t, (me["id"],))
            q("DELETE FROM users WHERE id=?", (me["id"],))
            shutil.rmtree(os.path.join(DATA, "audio", me["id"]), ignore_errors=True)
            self._cookie = GUEST_COOKIE + "=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/api/drills/([\w:.-]+)", urlparse(self.path).path)
        if m:  # removes the drill and its practice recordings
            for t in q("SELECT * FROM takes WHERE drill_id=? AND user_id=?", (m[1], me["id"])): self.remove_take(t)
            q("DELETE FROM drills WHERE id=? AND user_id=?", (m[1], me["id"]))
            return self.send(200, {"ok": True})
        m = re.fullmatch(r"/api/takes/([\w:.-]+)", urlparse(self.path).path)
        if not m: return self.send(404, {"error": "not found"})
        r = q("SELECT * FROM takes WHERE id=? AND user_id=?", (m[1], me["id"]), one=True)
        if not r: return self.send(404, {"error": "not found"})
        self.remove_take(r)
        self.send(200, {"ok": True})

    def remove_take(self, r):
        d = os.path.dirname(audio_path(r))
        for f in os.listdir(d) if os.path.isdir(d) else []:
            if f.startswith(r["id"] + "."): os.remove(os.path.join(d, f))
        q("DELETE FROM takes WHERE id=?", (r["id"],))

if __name__ == "__main__":
    # resume anything interrupted by a restart (and takes recorded before a key was set)
    # focuses made before practice lines existed
    for r in q("SELECT id FROM drills WHERE lesson NOT LIKE '%\"lines\"%'"):
        jobs.put(("lines", r["id"]))
    # reviews from before the library existed: file their exercises (oldest first, so later ones dedupe against them)
    for r in q("SELECT id FROM takes WHERE status='done' AND filed IS NULL AND kind IN ('daily','weekly','upload') ORDER BY ts"):
        jobs.put(("file", r["id"]))
    for r in q("SELECT id FROM takes WHERE status IN ('queued','processing')" + (" OR status='nokey'" if coach_ready() else "") + " ORDER BY ts"):
        jobs.put(r["id"])
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print("voice server on :%d, coach: %s" % (port, " -> ".join(["%s (%s)" % (m, via) for via, m in LISTENERS] + [CLAUDE_MODEL])), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
