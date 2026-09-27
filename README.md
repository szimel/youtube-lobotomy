# Productivity Feed

A local YouTube client that only shows videos worth watching.

Instead of opening YouTube and getting whatever its recommendation engine wants
to sell you that day, this app reads your home feed, judges every candidate
against rules *you* write, and keeps only what passes. Videos play inside the
page, which still counts as a normal view, so your real recommendations keep
improving from what you actually watch here.

It runs on your machine, stores everything in `data/`, and talks to exactly two
external services: YouTube (through your own exported cookies) and
[Jev](https://typesafe.ai) for the curation decisions.

## What it does

- **Refresh feed** — pulls your YouTube home feed, fetches transcripts, and asks
  Jev about every candidate. The current feed is *replaced*, never appended.
- **Search** — same pipeline over a search instead of your home feed.
- **Watch later** — videos you keep, with a one-click "trust this creator".
- **Curation rules** — the whole rulebook lives in one dialog and is yours to
  edit: a viewer profile, "must be true" questions, red flags, an optional
  rating scale, and how much transcript Jev gets to read.
- **Test a video** — paste any URL and see exactly how the rules judge it,
  without touching the feed.
- **Filtered out** — every candidate from the last run with the rule that
  decided it, so an empty feed is explainable rather than mysterious.

## Setup

Requires Python 3.11+ and Node (only as yt-dlp's JavaScript runtime).

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# then put your Jev API key in .env
.\run.ps1
```

`run.ps1` checks the virtualenv, the dependencies, `.env` and `data/cookies.txt`
before starting, and opens the browser once the server answers. It prints what is
missing and stops rather than starting an app that cannot work.

## Keeping YouTube signed in

This is the part that bites. Everything authenticated — your home feed, your
watch history — comes from `data/cookies.txt`.

YouTube rotates account cookies whenever a tab is open on youtube.com, so a jar
exported from your everyday browser is stale almost immediately, even though it
still looks well-formed. It then either fails or, worse, quietly behaves as if
you were signed out.

1. Open a **private/incognito** window and sign in to YouTube there.
2. Export cookies for youtube.com in **Netscape format** — the
   [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   extension is the usual choice.
3. Save it as `data/cookies.txt`.
4. **Close that incognito window and never browse YouTube in it again.** It
   exists only to hold a session that nobody rotates.

The file is git-ignored. yt-dlp rewrites it as it runs, which is what keeps the
session alive.

When it does go stale, the app says so in those words and repeats these steps
instead of reporting a generic network error. The check for a signed-in session
is explicit: a jar with no `SAPISID` cookie is rejected, because YouTube answers
search and video lookups anyway — as a signed-out stranger — and the app would
happily curate the wrong feed.

## How curation works

Jev is a **decision model, not a chat model**. It answers yes/no questions and
places things on scales; it cannot write a sentence, so the line under each video
is a factual summary of its answers ("Passed every rule: Teaches something real
0.91"), not prose invented for you.

Each rule you write becomes one question. A video is approved when every "must be
true" passes, no red flag fires, and — if you enabled the rating — it clears the
minimum. Everything else is decided in plain Python, so the verdict is auditable.

Two properties of Jev shape the defaults:

- **Its numbers are compressed.** A video that obviously teaches something real
  can still score 0.45 on a 0–1 question, and the score scale tops out well below
  its nominal maximum. That is why rules are yes/no questions with thresholds and
  why the rating scale is off by default; measured against a chat model on the
  same 12 videos, the yes/no rules agreed 10/12 while a raw score threshold agreed
  3/12.
- **It reads literally and has a hard context limit.** Transcripts are capped at
  20,000 tokens (Jev's `state` budget is 32k and the profile and description
  share it), and transcripts shorter than 1,500 tokens are dropped entirely —
  they say less than the title does. Captions are cached under
  `data/transcripts/`, so a repeat refresh does not pay for them twice.

Every candidate gets a transcript, including ones that end up rejected. A
clickbait title on a genuinely good video is a confident rejection on the title
alone; describing it with a transcript is what rescues it. In testing, swapping
in clickbait titles rejected 4/4 approved videos on titles alone, while the same
videos with transcripts still approved 3/4.

### Testing rules before saving them

**Preview against last run** in the Curation rules dialog re-judges the videos
from the previous run under your edited rules and shows what would change.

- Editing a **threshold or a name** asks Jev nothing new — those never reach it —
  so the answer is instant and free.
- **Adding, removing or rewording a rule** does change the questions, so Jev is
  asked again, against the cached transcripts. That costs roughly $0.002 for 20
  videos.

Nothing is saved until you press *Save rules*.

## Cost

Jev bills **input only, at $0.042 per million tokens**; output is free. A typical
20-video refresh reads about 170k tokens, so it costs well under a cent, and the
exact figure is reported after every run.

## What the page remembers

- **Watched videos** are recorded server-side (30 seconds counts) so a refresh
  stops offering them again. A *search* still shows them, because you asked.
- **Trusted creators** bypass the rules entirely and appear in the feed whatever
  Jev would have said.
- **Filtered out** keeps the last run's candidates with their answers, which is
  what makes the preview free.

## Data files

Everything lives in `data/`, and all of it is plain JSON you can read or delete.

| File | What it holds |
| --- | --- |
| `feed.json` | The current feed. Replaced on every refresh and search. |
| `watch_later.json` | Videos you kept. |
| `watched.json` | What you actually watched here, newest first. |
| `settings.json` | Candidate limits and the whole Jev rulebook. |
| `trusted_creators.json` | Channels that skip the filter. |
| `last_run.json` | The previous run: every candidate, its answers, and its verdict. |
| `runs.json` | The last 50 runs — counts, tokens and cost. |
| `transcripts/` | Cached captions, one file per video. |
| `cookies.txt` | Your YouTube session. Git-ignored. |

## Development

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests   # 131 tests
node --check static/script.js
```

The tests cover the rule plumbing, the API, and the page/script contract
(`tests/test_frontend.py` fails if the script selects an element the template no
longer defines). Background jobs are exercised through `/api/progress`, and no
test talks to the network.

Long operations — refresh, search, a rule preview that needs new questions — run
in a worker thread and report progress through `GET /api/progress`, which is why
the page can say *"Fetching transcripts — 4 of 20 · Some Title"* instead of
sitting on a silent request for half a minute. Only one job runs at a time; a
second request gets `409`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "Add an exported YouTube cookies.txt file" | No `data/cookies.txt`. Follow *Keeping YouTube signed in*. |
| "YouTube rejected the exported cookies" | The session rotated or expired. Re-export from a fresh incognito window. |
| "has no signed-in session cookie (SAPISID)" | The jar came from a signed-out browser. Sign in first, then export. |
| Feed comes back with 1–2 videos | Open **Filtered out** and read the verdicts. The rules may simply be tight; **Preview against last run** shows what a looser threshold would let through. |
| "N videos could not be judged" | Jev calls failed (usually transient). Those videos are not rejections — refresh again. |
| "N judged on title alone" | Captions were rate limited or the video has none. This is normal for some videos, and it is reported rather than hidden. |
| Refresh returns 409 | A job is already running. Wait for it to finish. |
