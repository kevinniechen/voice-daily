# Ad playbook: Ten Minute Voice

The aim of the first $500–1,000: find **one audience and one message** where people start, record, and come back the next day. Speak won Korea with one sharp audience first; we do the same before spending more.

## 1. Tag every link
Each ad and post links to the site with tags, so `/stats` shows which one works:

| Where | Link |
|---|---|
| TikTok ad, "confident at work" | `https://voice-daily.exe.xyz/?utm_source=tiktok&utm_campaign=work` |
| TikTok ad, "clearer English" | `https://voice-daily.exe.xyz/?utm_source=tiktok&utm_campaign=clarity` |
| Meta (Instagram) ad, "confident at work" | `https://voice-daily.exe.xyz/?utm_source=meta&utm_campaign=work` |
| Meta ad, "public speaking" | `https://voice-daily.exe.xyz/?utm_source=meta&utm_campaign=stage` |
| Your own TikTok / Reels / LinkedIn posts | `…/?utm_source=kevin&utm_campaign=organic` |
| Friends and the 8VC network | `…/?utm_source=network` |

Shared voice reports already carry `?ref=…` automatically.

Results: **https://voice-daily.exe.xyz/stats** (only your exe.dev login can open it). It shows, per source: started, recorded, finished a lesson, and came back another day.

## 2. Three angles to test
Each is one 15–20 second vertical video. The hook (the first two seconds) matters most.

**A. Confident at work** (professionals, 25–45, US)
- Hook (on screen): *"My voice dies at the end of every sentence in meetings."*
- Show: you reading a line, then the coach's note appearing ("your air runs out before 'story'; ribs collapse"), tapping the quote to hear it, the fix, then reading it again, better.
- End card: *"A voice coach that listens. 6 minutes a day. Free first lesson."*

**B. Clearer English** (non-native speakers, US/UK/Canada, plus India and Southeast Asia later; this was Speak's market)
- Hook: *"People keep asking me to repeat myself."*
- Show: the articulation note ("'wronged me' came out as 'wanted me'"), the Say These lines, and the report card.
- End card: *"Be understood the first time."*

**C. Public speaking** (founders, students, toasts and pitches)
- Hook: *"I recorded my pitch and an AI voice coach tore it apart."*
- Show: the moment-by-moment notes with timestamps, then the 🔥 streak.

**Assets you have:**
- `video/demo-ad-1080x1920.mp4`: a 23-second silent product demo. Use it as B-roll under a voiceover, or as-is for a "how it works" ad.
- Report card images: generated in the app ("Share your voice report").
- **Best creative:** you, filmed on your phone, with an iPhone screen recording of a real lesson (Control Center → Screen Recording, mic on). Real voice, real feedback, real before-and-after. Real people outperform polished product videos on TikTok.

## 3. The first test
- **Budget:** $20–30 a day per angle, 2–3 angles, for 5–7 days, about $500 total. Use the new-account credits: Google's 2× credit and TikTok's spend match.
- **Platforms:** TikTok first (cheapest attention, voice-native), then Meta (Instagram Reels).
- **Targeting:** broad, letting the algorithm find people, with only country and age set. The creative does the targeting.
- **Optimise for:** link clicks at first. Once there are about 50 starts, switch to conversion on a "recorded" event if you add the TikTok and Meta pixels (I can add these when you're ready).

**How to read `/stats` after 5–7 days:**

| Metric | Good | Bad |
|---|---|---|
| Started → recorded | above 40% | under 20%: the first screen or the consent loses them |
| Started → finished a lesson | above 25% | |
| Came back another day | above 15% | the real signal: under 5% means the habit isn't there yet |

Kill angles below these lines. Put the budget behind the one that clears them.

**Cost check:** each new person who does a full lesson costs you about 15–20¢ in model calls, and `/stats` shows the count. Your OpenRouter balance was about $12 at last check; top it up before running ads.

## 4. Once the iOS app is live
- **Apple Search Ads** (searches like "voice coach", "public speaking", "speech practice"): the highest-intent installs. Start with Search Match on, $20 a day.
- Switch the TikTok and Meta ads to app-install campaigns. This needs their SDKs or an attribution service; I'll add it at that point.
- Use `video/app-preview-886x1920.mp4` as the App Store preview video (6.9" iPhone, 23 seconds).

## 5. Free channels, in parallel
- Post your own 30-day series ("Day 1 of fixing my voice with an AI coach"), using the app's notes and your recordings. Speak grows heavily through creators; you can be the first one.
- Pay 5–10 micro-creators $50–150 each for one honest post, giving each their own link (`?utm_source=creator&utm_campaign=<name>`).
- Post on Product Hunt, Show HN, and r/PublicSpeaking or r/EnglishLearning (follow each subreddit's rules on self-promotion).
