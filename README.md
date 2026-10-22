# Ten Minute Voice

A short daily voice lesson with a coach that listens. Three tabs:

- **Today**: one lesson a day, fixed when you start it and done in order: warm-up (body, breath, sound), a quick review of an old focus when one is due, the drill for your current focus, read a page aloud, shadow a narrator. Every seventh lesson is a check-in on the same page.
- **Practice**: free play. Any exercise you've been given, any page, any book to shadow, enunciation passages, or an uploaded file. Graded, but doesn't move the curriculum.
- **Journey**: your path of focuses (current, in review, up next, learned), every past lesson read-only with its recordings and notes, streak and score charts.

The curriculum: each review's lessons become focuses (a habit the queue already has gets flagged again and moves up). One focus is current at a time; pass its drill in lessons on 3 different days and it moves to review, returning after 3, 7 and 21 days; pass those and it's learned.

Audio: public-domain LibriVox recordings. Text: Project Gutenberg. Sentence timings were made by transcribing each recording with Whisper (word timestamps) and aligning it to the Gutenberg text.

## Accounts and coach feedback

The app runs at https://voice-daily.exe.xyz/ (exe.dev VM `voice-daily`, systemd unit `voice`). `server.py` serves the page, signs people in with exe.dev login, and keeps each person's recordings (`~/voice-data/audio/<user>/`) and settings (SQLite `~/voice-data/voice.db`) on the VM. The GitHub Pages copy shows a "moved" page that lets people download recordings stored in that browser.

Each take is reviewed automatically:
1. ffmpeg converts it to mono MP3.
2. faster-whisper adds word timestamps.
3. Praat (parselmouth) measures each word's pitch in semitones, pitch movement, loudness, how quiet the ending is, and the pause before it.
4. The coach model gets the passage, the marked words, those measurements and the person's last few reviews. It returns scores, priorities, a drill and timestamped notes.

Coach models are tried in order: Gemini 4 Argon through OpenRouter (`OPENROUTER_API_KEY`), Gemini 4 Argon direct from Google, Gemini 3.8 Flash and Gemini 3.6 Flash, then Claude Opus 5.5. The Gemini models listen to the audio and treat the measurements as supporting evidence. Claude can't take audio, so it works from the measurements alone, through the exe.dev LLM gateway with no key needed. The Gemini key goes in `.env` on the VM as `GEMINI_API_KEY`; it is not committed.

Deploy: `rsync -a --exclude .git ./ voice-daily.exe.xyz:voice-daily/ && ssh voice-daily.exe.xyz sudo systemctl restart voice`
Python deps on the VM (`~/venv`): `faster-whisper anthropic praat-parselmouth`.
