"""Ten Minute Voice server: serves the app, keeps each person's recordings and
progress, and has a voice coach write timestamped feedback on each take.

Identity comes from the exe.dev proxy (X-ExeDev-UserID / X-ExeDev-Email).

Coach pipeline: faster-whisper word timestamps -> Praat (parselmouth) pitch and
loudness per word -> an LLM reads the passage, transcript and measurements.
With GEMINI_API_KEY set, Gemini listens to the audio and gets the measurements as
supporting evidence, trying GEMINI_MODELS in order (quota or overload -> next one).
If none answer, or there's no key, Claude reads the measurements instead (it can't
take audio). COACH=claude skips Gemini.

  GEMINI_MODELS    default gemini-4-argon,gemini-3.8-flash,gemini-3.6-flash
  CLAUDE_MODEL     default claude-opus-5-5
  ANTHROPIC_BASE_URL  default https://llm.int.exe.xyz (exe.dev gateway, no key needed)
  DATA_DIR         default ~/voice-data
  PORT             default 8000
  DAILY_LIMIT      feedback runs per person per day, default 30
"""
import base64, json, mimetypes, os, queue, re, sqlite3, subprocess, threading, time, traceback, urllib.error, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote, unquote

APP = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(APP, ".env")) if os.path.exists(os.path.join(APP, ".env")) else []:
    k, _, v = line.strip().partition("=")
    if k and not k.startswith("#"): os.environ.setdefault(k, v.strip().strip('"\''))

DATA = os.path.expanduser(os.environ.get("DATA_DIR", "~/voice-data"))
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-4-argon,gemini-3.8-flash,gemini-3.6-flash").split(",") if m.strip()]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
USE_GEMINI = bool(os.environ.get("GEMINI_API_KEY")) and os.environ.get("COACH", "") != "claude"
MODEL = GEMINI_MODELS[0] if USE_GEMINI else CLAUDE_MODEL
def coach_ready(): return True
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "30"))
STATIC = {"/": "index.html", "/index.html": "index.html", "/books.json": "books.json"}
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

def q(sql, args=(), one=False):
    with _db_lock:
        cur = db.execute(sql, args); db.commit()
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows

def take_json(r):
    d = {k: r[k] for k in ("id", "kind", "label", "ts", "secs", "status", "error", "model")}
    d["feedback"] = json.loads(r["feedback"]) if r["feedback"] else None
    d["text"] = r["text"]; d["marks"] = json.loads(r["marks"] or "[]")
    return d

def audio_path(r, ext="mp3"): return os.path.join(DATA, "audio", r["user_id"], r["id"] + "." + ext)

# ---------- analysis pipeline ----------
jobs = queue.Queue()
_whisper = None

def transcribe(path):
    """Word-level timestamps, or None if faster-whisper isn't installed."""
    global _whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    if _whisper is None:
        _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "small.en"), device="cpu", compute_type="int8")
    import numpy as np  # decode with ffmpeg ourselves; faster-whisper's PyAV path is version-fragile
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    segs, _ = _whisper.transcribe(np.frombuffer(pcm, np.float32), word_timestamps=True, vad_filter=False, beam_size=1)
    return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2), "p": round(w.probability, 2)} for s in segs for w in (s.words or [])]

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
    "summary": {"type": "string", "description": "2-4 sentences: overall impression of this read, in plain second person."},
    "scores": _obj({k: {"type": "integer", "description": "1 (weak) to 5 (excellent)"} for k in
                    ("pitch_variation", "stress", "pace_and_pauses", "articulation", "endings", "fluency")}),
    "wpm": {"type": "integer", "description": "Speaking rate in words per minute, excluding long silences."},
    "strengths": {"type": "array", "items": {"type": "string"}},
    "priorities": {"type": "array", "items": {"type": "string"}, "description": "The 2-3 changes that would most improve the next read, most important first."},
    "moments": {"type": "array", "description": "Specific moments worth hearing again, in time order. Aim for 5-12. Include good moments too.",
                "items": _obj({
                    "start": {"type": "number", "description": "Seconds from the start of the recording."},
                    "end": {"type": "number"},
                    "quote": {"type": "string", "description": "The words spoken in this span."},
                    "kind": {"type": "string", "enum": ["pitch", "stress", "pace", "pause", "articulation", "ending", "fluency", "breath", "good"]},
                    "note": {"type": "string", "description": "What happened, concretely."},
                    "try": {"type": "string", "description": "Exactly how to say it next time (empty for good moments)."}})},
    "progress": {"type": "string", "description": "How this compares with the earlier sessions listed, or empty if none."},
    "drill": {"type": "string", "description": "One specific 1-2 minute exercise for tomorrow, aimed at the top priority."},
})

COACH = """You are an expert voice and speech coach (think: a top dialect coach and public-speaking trainer). \
You are listening to a recording of your student reading a passage aloud as part of a 10-minute daily practice routine \
(warm-up, read aloud with marked stress words, shadow a professional narrator, enunciation drills). \
Their goals: more pitch movement and expressiveness, landing the important words, controlled pace with purposeful pauses, \
crisp consonants and fully finished word endings, and a confident, resonant voice.

Be specific and honest, like a good coach: name what actually happened, quote the words, and say exactly what to do differently. \
Skip generic advice. Praise only what is genuinely good. For each moment, give start and end in seconds from the start of the recording."""

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

def build_prompt(r, words, history, pros=None, listening=False):
    parts = [COACH, "\n\n", LISTEN if listening else MEASURED, "\n\nRecording: %s, %s, %.0f seconds." % (r["kind"], r["label"] or "", r["secs"] or 0)]
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
        parts.append("\n\nTheir earlier sessions (oldest first), for the progress note:\n" + "\n".join(
            "- %s: %s Scores %s. Priorities then: %s" % (time.strftime("%b %d", time.localtime(h["ts"] / 1000)), h["summary"], json.dumps(h["scores"]), "; ".join(h["priorities"]))
            for h in history))
    return "".join(parts)

_claude = None
def call_claude(prompt):
    global _claude
    import anthropic
    if _claude is None:
        _claude = anthropic.Anthropic(base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://llm.int.exe.xyz"),
                                      api_key=os.environ.get("ANTHROPIC_API_KEY", "implicit"), max_retries=3, timeout=600,
                                      default_headers={"anthropic-version": "2023-06-01"})  # SDK 2.x sends a date the gateway rejects
    with _claude.messages.stream(
        model=CLAUDE_MODEL, max_tokens=32000,
        thinking={"type": "adaptive"}, output_config={"effort": "high", "format": {"type": "json_schema", "schema": FEEDBACK_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal": raise RuntimeError("Claude declined to review this take.")
    if msg.stop_reason == "max_tokens": raise RuntimeError("Claude's review ran too long and was cut off.")
    return json.loads(next(b.text for b in msg.content if b.type == "text"))

class Unavailable(Exception): pass

def call_gemini(model, prompt, audio_b64):
    key = os.environ.get("GEMINI_API_KEY")
    body = {
        "contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}}, {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": FEEDBACK_SCHEMA,
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

def snap(fb, words, dur):
    """Keep moment times inside the recording and sorted."""
    ms = []
    for m in fb.get("moments", []):
        s = max(0.0, min(float(m.get("start", 0)), dur)); e = max(s, min(float(m.get("end", s)), dur))
        m["start"], m["end"] = round(s, 1), round(e, 1); ms.append(m)
    fb["moments"] = sorted(ms, key=lambda m: m["start"])
    return fb

def analyze(take_id):
    r = q("SELECT * FROM takes WHERE id=?", (take_id,), one=True)
    if not r: return
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
        hist = [json.loads(h["feedback"]) | {"ts": h["ts"]} for h in q(
            "SELECT feedback, ts FROM takes WHERE user_id=? AND status='done' AND ts<? ORDER BY ts DESC LIMIT 4", (r["user_id"], r["ts"]))][::-1]
        pros = None
        if words:
            try: pros = prosody(mp3, words)
            except Exception: traceback.print_exc()
        fb = used = None
        if USE_GEMINI:
            with open(mp3, "rb") as f: b64 = base64.b64encode(f.read()).decode()
            prompt = build_prompt(r, words, hist, pros, listening=True)
            for m in GEMINI_MODELS:
                try: fb, used = call_gemini(m, prompt, b64), m; break
                except Unavailable as e: print("gemini %s unavailable: %s" % (m, e), flush=True)
        if fb is None:
            if not words: raise RuntimeError("Couldn't transcribe this take (is anything audible?).")
            fb, used = call_claude(build_prompt(r, words, hist, pros)), CLAUDE_MODEL
        fb = snap(fb, words, r["secs"] or 1e9)
        q("UPDATE takes SET status='done', feedback=?, model=?, analyzed=? WHERE id=?", (json.dumps(fb), used, int(time.time()), take_id))
    except Exception as e:
        traceback.print_exc()
        q("UPDATE takes SET status='error', error=? WHERE id=?", (str(e)[:500], take_id))

def worker():
    while True:
        tid = jobs.get()
        try: analyze(tid)
        finally: jobs.task_done()

# ---------- http ----------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "voice"

    def log_message(self, fmt, *a): pass

    def user(self):
        uid = self.headers.get("X-ExeDev-UserID")
        if not uid: return None
        email = self.headers.get("X-ExeDev-Email", "")
        q("INSERT INTO users(id,email,created) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email", (uid, email, int(time.time())))
        return {"id": uid, "email": email}

    def send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "no-cache")
        for k, v in (headers or {}).items(): self.send_header(k, v)
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
            if p in ("/", "/index.html") and not self.headers.get("X-ExeDev-UserID"):
                return self.send(302, b"", "text/plain", {"Location": "/__exe.dev/login?redirect=" + quote(self.path)})
            return self.send_file(os.path.join(APP, STATIC.get(p, p.lstrip("/"))),
                                  cache="public, max-age=86400" if p.startswith("/audio/") else "no-cache")
        if not p.startswith("/api/"): return self.send(404, {"error": "not found"})
        me = self.user()
        if not me: return self.send(401, {"error": "login required", "login": "/__exe.dev/login?redirect=/"})
        if p == "/api/me":
            return self.send(200, {**me, "coach": coach_ready(), "model": MODEL})
        if p == "/api/state":
            r = q("SELECT json FROM state WHERE user_id=?", (me["id"],), one=True)
            return self.send(200, json.loads(r["json"]) if r else {})
        if p == "/api/takes":
            return self.send(200, [take_json(r) for r in q("SELECT * FROM takes WHERE user_id=? ORDER BY ts DESC", (me["id"],))])
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

    def do_POST(self):
        me = self.user()
        if not me: return self.send(401, {"error": "login required"})
        p = urlparse(self.path).path
        if p == "/api/takes":
            # body: raw audio. meta (kind, label, text, marks) in X-Take-Meta header as JSON.
            try: meta = json.loads(unquote(self.headers.get("X-Take-Meta") or "{}"))
            except Exception: meta = {}
            try: raw = self.body()
            except ValueError: return self.send(413, {"error": "recording too large"})
            if len(raw) < 1000: return self.send(400, {"error": "empty recording"})
            tid = "%s-%d-%s" % (re.sub(r"\W", "", meta.get("kind", "daily"))[:10] or "daily", int(time.time() * 1000), uuid.uuid4().hex[:6])
            row = {"id": tid, "user_id": me["id"]}
            os.makedirs(os.path.dirname(audio_path(row)), exist_ok=True)
            mime = (self.headers.get("Content-Type") or "audio/webm").split(";")[0]
            ext = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mpeg": "mp3"}.get(mime, "bin")
            orig = audio_path(row, "orig." + ext)
            with open(orig, "wb") as f: f.write(raw)
            # mono mp3: plays everywhere (incl. iOS), seeks cleanly, small enough to send inline
            cp = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", orig, "-ac", "1", "-ar", "24000", "-b:a", "64k", audio_path(row)], capture_output=True)
            if cp.returncode: return self.send(400, {"error": "could not decode audio: " + cp.stderr.decode()[:200]})
            try: secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", audio_path(row)], capture_output=True, text=True).stdout.strip())
            except ValueError: secs = float(meta.get("secs") or 0)
            ts = int(meta.get("ts") or time.time() * 1000)
            q("INSERT INTO takes(id,user_id,kind,label,ts,secs,text,marks,mime,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (tid, me["id"], meta.get("kind", "daily"), meta.get("label", ""), ts, round(secs, 2), meta.get("text", ""),
               json.dumps(meta.get("marks", [])), mime, "queued"))
            self.queue_if_allowed(me, tid)
            return self.send(200, take_json(q("SELECT * FROM takes WHERE id=?", (tid,), one=True)))
        m = re.fullmatch(r"/api/takes/([\w:.-]+)/analyze", p)
        if m:
            r = q("SELECT * FROM takes WHERE id=? AND user_id=?", (m[1], me["id"]), one=True)
            if not r: return self.send(404, {"error": "not found"})
            if r["status"] in ("queued", "processing"): return self.send(200, take_json(r))
            q("UPDATE takes SET status='queued', error=NULL WHERE id=?", (r["id"],))
            self.queue_if_allowed(me, r["id"])
            return self.send(200, take_json(q("SELECT * FROM takes WHERE id=?", (r["id"],), one=True)))
        self.send(404, {"error": "not found"})

    def queue_if_allowed(self, me, tid):
        n = q("SELECT COUNT(*) c FROM takes WHERE user_id=? AND analyzed>?", (me["id"], int(time.time()) - 86400), one=True)["c"]
        if n >= DAILY_LIMIT:
            q("UPDATE takes SET status='error', error=? WHERE id=?", ("Daily feedback limit reached (%d). Try again tomorrow." % DAILY_LIMIT, tid))
        else:
            jobs.put(tid)

    def do_DELETE(self):
        me = self.user()
        if not me: return self.send(401, {"error": "login required"})
        m = re.fullmatch(r"/api/takes/([\w:.-]+)", urlparse(self.path).path)
        if not m: return self.send(404, {"error": "not found"})
        r = q("SELECT * FROM takes WHERE id=? AND user_id=?", (m[1], me["id"]), one=True)
        if not r: return self.send(404, {"error": "not found"})
        d = os.path.dirname(audio_path(r))
        for f in os.listdir(d):
            if f.startswith(r["id"] + "."): os.remove(os.path.join(d, f))
        q("DELETE FROM takes WHERE id=?", (r["id"],))
        self.send(200, {"ok": True})

if __name__ == "__main__":
    # resume anything interrupted by a restart (and takes recorded before a key was set)
    for r in q("SELECT id FROM takes WHERE status IN ('queued','processing')" + (" OR status='nokey'" if coach_ready() else "") + " ORDER BY ts"):
        jobs.put(r["id"])
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print("voice server on :%d, coach: %s" % (port, " -> ".join((GEMINI_MODELS if USE_GEMINI else []) + [CLAUDE_MODEL])), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
