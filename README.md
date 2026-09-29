# Podcast Digest

Weekly pipeline that checks a fixed list of podcasts for episodes from the
last two weeks, transcribes them (using the feed's own published transcript
when one is freely available, otherwise OpenAI's gpt-transcribe), summarizes
them into key points (GPT-5.6 Luna), and saves each summary to `summaries/`
for the web app. Runs weekly on your Mac via launchd (GitHub's runners are
blocked by Substack, so they can't fetch Lenny's Podcast).

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

### 3. Add your OpenAI API key
For transcription and summarization: platform.openai.com → API keys.
Put it in `.env` (`cp .env.example .env`; `.env` is gitignored).

### 4. Install the weekly job
```bash
scripts/scheduler/install.sh
```
This sets up a launchd job that runs every Friday at 9:00 local time. It runs
from its own clone of `main` in `~/Library/Application Support/podcast-digest`
(background jobs can't read `~/Documents`), so it never touches your working
copy. Re-run the installer after changing `.env`.

### 5. Commit `config/shows.json`
Make sure your filled-in `config/shows.json` is committed. Unlike API keys,
it's not a secret.

### 6. Test it
Run it once now instead of waiting for Friday (the installer prints the
exact command):
```bash
~/Library/Application\ Support/podcast-digest/venv/bin/python \
  ~/Library/Application\ Support/podcast-digest/repo/scripts/scheduler/weekly_run.py --force
```
Output goes to `~/Library/Logs/podcast-digest/run.log`.

## Project layout
```
config/shows.json          shows to follow (name + RSS feed)
summaries/<show>/*.json    one structured summary per episode (read by the web app)
transcripts/<show>/*.txt   the transcript each summary was made from
web/                       the reading app (Astro), deployed on Vercel
scripts/
  podcast_digest.py        production pipeline
  scheduler/               weekly launchd job for the Mac (install.sh, weekly_run.py)
  backfill.py              summarize older episodes into summaries/
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
push, including the pipeline's weekly commit, so new summaries appear on
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
1. launchd starts `scripts/scheduler/weekly_run.py` every Friday at 9:00.
   If the Mac is asleep then, it runs on wake; if it was off, at the next
   login; if it's offline, it retries hourly until it gets through. A macOS
   notification reports new summaries or errors
2. For each show, it takes the RSS feed's episodes from the last 14 days
   (`LOOKBACK_DAYS`) and skips any that already have a file in
   `summaries/`. An episode that failed has no file, so the next run
   retries it while it's still in the window. For anything older, use
   `scripts/backfill.py`
3. If the feed publishes a usable `<podcast:transcript>` for an episode,
   that's used directly. Otherwise the audio is downloaded and
   transcribed with gpt-transcribe (chunked automatically if over the
   25MB API limit), using the show/episode name as context to improve
   accuracy on names and terms
4. Each transcript is summarized with GPT-5.6 Luna into structured JSON.
   The summary goes to `summaries/` and the transcript to `transcripts/`
5. `summaries/` and `transcripts/` are committed and pushed to `main`, so
   every summary is kept permanently. That push makes Vercel rebuild the
   web app, so new summaries appear without a manual deploy

## Costs
gpt-transcribe is about $0.0045/minute of audio (only charged when a
show doesn't already publish a usable transcript). A handful of
~45-minute episodes per week runs a couple dollars a month — transcription
is ~97% of total spend. GPT-5.6 Luna summarization ($0.20/$1.20 per million
input/output tokens) is under a cent per episode.

## Local testing
```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your key — .env is gitignored
python scripts/podcast_digest.py
```
