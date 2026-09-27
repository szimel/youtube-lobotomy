# Productivity Feed

A local YouTube client that only shows videos worth watching.

Instead of opening YouTube and getting whatever its recommendation engine wants
to sell you that day, this app reads your home feed, judges every candidate
against rules *you* write, and keeps only what passes. Videos play inside the
page, and playback starts from YouTube's own play button — the only kind its
documentation counts as a view — so your real recommendations keep improving
from what you actually watch here.

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

**Export from a machine on the same connection as the app.** yt-dlp's
documentation is blunt that this kind of authentication "requires cookies from a
browser with the same IP address that you will be using with yt-dlp". Exporting
at home and running the app at home keeps one public IP and works; exporting on a
phone over mobile data and running the app on the server does not.

**Worth knowing:** yt-dlp's own caveat is that using an account with it "you run
the risk of it being banned (temporarily or permanently)", which is why they
suggest a throwaway account and a modest request rate. This app reads a home feed
and a handful of captions per refresh, which is far below the rate limits they
describe, but the warning is theirs, not mine, and it is about your Google
account rather than about this software.

## Running on a home server

### It does not need a browser

Nothing on the server side renders a page or drives one. The app's whole
interaction with YouTube is `cookies.txt` plus yt-dlp, and videos play in
whatever browser is *looking* at the page — your phone, your laptop, anything
with a browser on the same network. A headless Ubuntu box works exactly like the
Windows desktop.

Two things follow from that:

- `--cookies-from-browser` needs a browser profile on the machine running it,
  but `--cookies FILE` (what this app uses) does not. Export the jar wherever you
  have a browser, copy it to `data/cookies.txt`, and the server is happy.
- A CRLF jar exported on Windows works as-is on Linux. Python opens cookie files
  in text mode, which translates `\r\n` to `\n` before parsing, and yt-dlp
  rewrites the file with Unix line endings after its first run. (yt-dlp's FAQ
  suggests converting with `dos2unix` if you ever hit `HTTP Error 400` on a
  cookie file — harmless advice, just not necessary here.)

The one thing that *is* worth getting right is the public IP, as above: the
cookies must be exported from a browser on the same connection the server uses.

### With Docker

The repository builds a self-contained image. It needs a data directory and a
Jev key, and nothing else:

```yaml
services:
  productivity-feed:
    image: ghcr.io/YOUR_GITHUB_USER/youtube-lobotomy:latest
    container_name: productivity-feed
    restart: unless-stopped
    ports:
      - 8087:8080
    environment:
      JEV_API_KEY: ${JEV_API_KEY}
      PUID: 1000     # `id -u` on the host, so the mounted data dir is writable
      PGID: 1000     # `id -g`
    volumes:
      - ./data:/app/data
    networks:
      - caddy_net

networks:
  caddy_net:
    external: true
```

`compose.yaml` in this repository is that block, ready to copy. Then:

```bash
mkdir -p ./data
cp /path/to/cookies.txt ./data/cookies.txt
docker compose up -d
docker compose logs -f          # watch a refresh happen
```

There is no separate "install" step and no database. Everything the app writes —
the feed, the rulebook, the run history, the transcript cache, the cookies —
lives in that one mounted directory, so backing it up means copying `./data`.

**No registry yet?** Build it from a checkout, or point Compose straight at the
repository:

```yaml
    build: .                                                  # a local checkout
    build: https://github.com/YOUR_GITHUB_USER/youtube-lobotomy.git   # or the repo
```

Compose builds when the image is missing, so `docker compose up -d` works either
way, and `docker compose build --pull` updates it. (A git context is a shallow
clone of the pushed commits — uncommitted local edits are invisible to it.)

**Publishing your own image.** `.github/workflows/docker-publish.yml` runs the
tests, then builds `linux/amd64` and `linux/arm64` images and pushes them to
`ghcr.io/<your-account>/youtube-lobotomy` on every push to `main` and every `v*`
tag. Two things to know:

- GitHub makes a new package **private**. Pulling it anonymously — which is what
  a home server wants — needs one visit to the package's settings → *Danger
  Zone* → *Change visibility* → *Public*. It cannot be made private again.
- With no registry at all, `docker save youtube-lobotomy:dev | ssh server
  'docker load'` also works.

### Reaching it over Tailscale

A container port published as `8087:8080` is reachable on the LAN and over
Tailscale at `http://<tailscale-ip>:8087`. That is the simplest option and it
works. Two refinements worth considering:

- **Bind to the Tailscale address only** — replace the `ports:` entry with
  `- "100.x.y.z:8087:8080"` so the app is not also on the LAN.
- **Serve it over HTTPS** — keep the app on `127.0.0.1:8080` and run
  `tailscale serve 8080`, which gives
  `https://<machine>.<tailnet>.ts.net` with a real certificate. Beyond the nicer
  URL, this matters for playback: YouTube requires the embedding page to send a
  `Referer`, and an HTTPS public origin is the configuration its documentation
  describes. Tailscale's own advice is the same — keep the service on localhost
  and let Serve be the only way in.

If you put anything in front of the app that rewrites the `Host` header and does
*not* send `X-Forwarded-Host`, its writes will be refused with 403
`cross_site_request`. Declaring the public address fixes it:

```yaml
      PUBLIC_ORIGINS: https://feed.example.com
```

### Without Docker

`run.sh` is the Linux counterpart of `run.ps1`: it checks the virtualenv, the
dependencies, `.env` and the cookies before starting, and prints what is missing.
A `systemd` unit running `gunicorn --config docker/gunicorn.conf.py app:app`
works too — use **one worker**, because the refresh queue and the progress the
page polls live in the process's memory.

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
.venv\Scripts\python.exe -m unittest discover   # 142 tests
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
| Every button returns 403 `cross_site_request` | Something in front of the app rewrites the `Host` header without forwarding `X-Forwarded-Host`. Set `PUBLIC_ORIGINS` to the address in your browser's bar. |
| Refresh returns 409 | A job is already running. Wait for it to finish. |
| Container says `/app/data is not writable` | The mounted directory is owned by a different uid. Set `PUID`/`PGID` to `id -u`/`id -g` on the host. |
| `docker compose pull` says denied on a ghcr.io image | The package is still private. Package settings → *Danger Zone* → *Change visibility* → *Public*. |
| Videos load but will not play in the page | Some videos disallow embedding, and YouTube refuses the embed outright when the page sends no `Referer` — which is what happens if `Referrer-Policy` is set to suppress it. Use `strict-origin-when-cross-origin`. |
