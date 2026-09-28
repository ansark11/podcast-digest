# Podcast Digest

Daily pipeline that checks a fixed list of podcasts for new episodes,
transcribes them (using the feed's own published transcript when one is
freely available, otherwise OpenAI's gpt-transcribe), summarizes them
into key points (GPT-5.6 Luna), saves each summary to `summaries/`, and
emails you a digest. Runs
automatically on GitHub Actions — no server or laptop needs to stay on.

**Note:** this only works for shows with a public RSS feed. Spotify
Originals/exclusives don't publish one, so they can't be pulled this way.

## Setup

### 1. Find your shows' RSS feeds
Most podcasts have a public feed even if you listen on Spotify. Easiest
ways to find it:
- Search "[show name] RSS feed" — many podcast sites link it directly
- Use a lookup tool like [Podcast Index](https://podcastindex.org) or
  [Listen Notes](https://www.listennotes.com) and search the show

### 2. Configure your shows
Copy `config/shows.example.json` to `config/shows.json` and fill in your
shows:
```json
[
  { "name": "Show Name", "rss_url": "https://actual-feed-url.com/rss" }
]
```

### 3. Get your API keys
- **OpenAI API key** (for transcription and summarization): platform.openai.com → API keys
- **Gmail app password** (for sending the email): Google Account →
  Security → 2-Step Verification → App passwords. Generate one for "Mail".
  (Your normal Gmail password won't work here — Google requires an app
  password for programmatic SMTP access.)

### 4. Add secrets to your GitHub repo
Push this project to a new GitHub repo, then go to:
**Settings → Secrets and variables → Actions → New repository secret**

Add each of these:
| Secret name | Value |
|---|---|
| `OPENAI_API_KEY` | your OpenAI key |
| `GMAIL_ADDRESS` | the Gmail address to send from |
| `GMAIL_APP_PASSWORD` | the app password from step 3 |
| `DIGEST_TO_EMAIL` | where the digest should be sent (can be the same address) |

### 5. Commit `config/shows.json`
Make sure your filled-in `config/shows.json` is committed. Unlike API keys,
it's not a secret.

### 6. Test it
Go to the **Actions** tab in your repo → "Podcast Digest" workflow →
**Run workflow** to trigger it manually and confirm it works before
waiting for the schedule.

## Project layout
```
config/shows.json          shows to follow (name + RSS feed)
state/seen_episodes.json   episodes already processed
summaries/<show>/*.json    one structured summary per episode (read by the web app)
transcripts/<show>/*.txt   the transcript each summary was made from
web/                       the reading app (Astro), deployed on Vercel
scripts/
  podcast_digest.py        production pipeline (run daily by GitHub Actions)
  backfill.py              summarize older episodes into summaries/, no email
  evals/                   eval tooling, never writes to summaries/
evals/                     gitignored: experimental takes, judge scores, reports
```

## Web app
`web/` is an Astro site that turns every file in `summaries/` into a page:
a library (search, unread markers, continue reading, saved) and a reading
view per episode. It follows the device's light/dark setting. Reading
progress and saves are stored in the browser, so they don't sync between
devices.

Vercel deploys it (project Root Directory: `web`) and rebuilds on every
push, including the pipeline's daily commit, so new summaries appear on
their own. To run it locally:
```bash
cd web
npm install
npm run dev      # http://localhost:4321
```

## Summary files
Each episode gets exactly one file in `summaries/`, matched by the episode's
guid, so re-processing an episode replaces its file instead of adding another.
A summary file holds:
- episode metadata: show, title, publish date, audio link, episode link,
  artwork, show notes (`description_html`), duration
- `prompt_version` and `summary_model`, so you always know what produced it
- `summary`: `overview`, `takeaways` (heading + detail), `quotes`
  (text + speaker) and `resources` (name + kind)

The model returns the summary as JSON (OpenAI structured outputs), so every
file has the same shape. Artwork, show notes and duration come from the RSS
feed. If any are missing, the next pipeline run fills them in. To do that
without processing new episodes:
```bash
python scripts/podcast_digest.py --refresh-metadata-only
```

## Evaluating summary quality
Eval work stays in `evals/` (gitignored) and never touches `summaries/`:
```
evals/runs/<tag>/*.json      one experimental take per folder (full prompt + transcript + summary)
evals/results/<tag>/*.json   judge and human scores for each run
evals/reports/report.md      comparison table
```
Folders starting with `_` (like `evals/results/_archive/`) are ignored.

```bash
# Re-summarize every episode in summaries/ with the current prompt, as a new
# take. Uses the committed transcripts, so there's no transcription cost:
python scripts/evals/resummarize.py --tag v6

# Automated LLM-as-judge pass. JUDGE_MODEL must stay a different model
# from the summarizer — a judge grading its own family inflates scores.
# The judge reports *what* is wrong (hallucinations, distortions,
# missed topics, flagged terms) and how severe each item is; the 1-5
# scores are then derived from that evidence in code, not assigned by
# the model.
python scripts/evals/eval_runs.py judge

# Walk through judged episodes interactively and record your own
# 1-5 score + notes, to calibrate against the judge.
python scripts/evals/eval_runs.py review

# Print (and save to evals/reports/report.md) a table comparing judge vs.
# human scores across every evaluated take.
python scripts/evals/eval_runs.py report
```
When you change the prompt, bump `PROMPT_VERSION` in `podcast_digest.py`.
A take only becomes the official summary when the production pipeline
produces it. Eval scripts never promote anything on their own.

## How it works
1. GitHub Actions runs `scripts/podcast_digest.py` on a cron schedule
   (default: 9am ET daily — edit the `cron` line in
   `.github/workflows/podcast-digest.yml` to change it)
2. For each show, it checks the RSS feed for episodes not yet in
   `state/seen_episodes.json`
3. If the feed publishes a usable `<podcast:transcript>` for an episode,
   that's used directly. Otherwise the audio is downloaded and
   transcribed with gpt-transcribe (chunked automatically if over the
   25MB API limit), using the show/episode name as context to improve
   accuracy on names and terms
4. Each transcript is summarized with GPT-5.6 Luna into structured JSON.
   The summary goes to `summaries/` and the transcript to `transcripts/`
5. If anything new was found, one digest email is sent covering all of it
6. `state/seen_episodes.json`, `summaries/` and `transcripts/` are committed
   back to the repo, so the same episode is never processed twice and every
   summary is kept permanently

## Costs
gpt-transcribe is about $0.0045/minute of audio (only charged when a
show doesn't already publish a usable transcript). A handful of
~45-minute episodes per week runs a couple dollars a month — transcription
is ~97% of total spend. GPT-5.6 Luna summarization ($0.20/$1.20 per million
input/output tokens) is under a cent per episode.

## Local testing
```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys/password — .env is gitignored
python scripts/podcast_digest.py
```
