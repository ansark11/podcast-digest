# Podcast Digest

Daily pipeline that checks a fixed list of podcasts for new episodes,
transcribes them (using the feed's own published transcript when one is
freely available, otherwise OpenAI's gpt-transcribe), summarizes them
into key points (GPT-5.6 Luna), and emails you a digest. Runs
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
Make sure your filled-in `config/shows.json` is committed — it's the one
file `.gitignore` doesn't need to touch (unlike API keys, it's not a secret).

### 6. Test it
Go to the **Actions** tab in your repo → "Podcast Digest" workflow →
**Run workflow** to trigger it manually and confirm it works before
waiting for the schedule.

## Reviewing what the pipeline actually did
Every processed episode writes a JSON file to `runs/` (gitignored locally)
containing the full transcript, the exact prompt sent to the model, and
the raw summary it returned. On GitHub Actions, the same files are
uploaded as a downloadable workflow artifact (Actions tab → the run →
Artifacts) since the runner's filesystem doesn't persist between runs.

## Evaluating summary quality
```bash
# Backfill past episodes into runs/ to get eval data without waiting
# for new episodes (real transcription/summarization cost applies):
python scripts/backfill_runs.py --count 12

# Automated LLM-as-judge pass. JUDGE_MODEL must stay a different model
# from the summarizer — a judge grading its own family inflates scores.
# The judge reports *what* is wrong (hallucinations, distortions,
# missed topics, flagged terms) and how severe each item is; the 1-5
# scores are then derived from that evidence in code, not assigned by
# the model. Writes evals/<name>.json.
python scripts/eval_runs.py judge

# Walk through judged episodes interactively and record your own
# 1-5 score + notes, to calibrate against the judge.
python scripts/eval_runs.py review

# Print (and save to evals/report.md) a table comparing judge vs.
# human scores across every evaluated episode.
python scripts/eval_runs.py report
```
`evals/` is gitignored, same as `runs/` — this is local review tooling,
not part of the production digest pipeline.

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
4. Each transcript is summarized with GPT-5.6 Luna; if anything new
   was found, one digest email is sent covering all of it
5. `state/seen_episodes.json` is updated and committed back to the repo,
   so the same episode is never processed twice

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
