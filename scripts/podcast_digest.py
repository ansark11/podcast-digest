"""
Podcast Digest Pipeline
------------------------
Checks a fixed list of podcast RSS feeds for new episodes. If a feed
publishes a usable Podcasting 2.0 <podcast:transcript> tag, that's used
directly; otherwise audio is downloaded and transcribed with OpenAI's
gpt-transcribe. Each transcript is summarized with GPT-5.6 Luna, and
one digest email covers everything new. Designed to run on a schedule
via GitHub Actions (see .github/workflows/podcast-digest.yml).

Each processed episode's transcript, exact prompt, and raw summary are
saved to runs/<timestamp>_<title>.json for review and eval purposes.

Required environment variables (set as GitHub Actions secrets):
  OPENAI_API_KEY       - OpenAI API key (transcription and summarization)
  GMAIL_ADDRESS        - Gmail address to send from
  GMAIL_APP_PASSWORD   - Gmail app password (not your normal password)
  DIGEST_TO_EMAIL      - Where the digest should be sent
"""

import os
import json
import time
import smtplib
import tempfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI
from pydub import AudioSegment
from pydub.utils import make_chunks

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
SHOWS_FILE = BASE_DIR / "config" / "shows.json"
STATE_FILE = BASE_DIR / "state" / "seen_episodes.json"
RUNS_DIR = BASE_DIR / "runs"

TRANSCRIPTION_MAX_BYTES = 24 * 1024 * 1024  # stay safely under the 25MB API limit
CHUNK_MS = 10 * 60 * 1000                   # split long episodes into 10-minute chunks
MAX_NEW_EPISODES_PER_SHOW = 3               # safety cap per run, per show
MAX_TRANSCRIPT_CHARS = 400_000              # well under Luna's ~1M token context window
MIN_FEED_TRANSCRIPT_CHARS = 500             # below this, treat a feed transcript as bogus/placeholder
TRANSCRIPTION_MODEL = "gpt-transcribe"
SUMMARY_MODEL = "gpt-5.6-luna"
SUPPORTED_FEED_TRANSCRIPT_TYPES = {"text/plain", "text/vtt", "application/srt", "text/srt"}

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_latest_episodes(rss_url, limit=MAX_NEW_EPISODES_PER_SHOW):
    """Pull the most recent episodes from a show's RSS feed."""
    feed = feedparser.parse(rss_url)
    episodes = []
    for entry in feed.entries[:limit]:
        audio_url = None
        for link in entry.get("links", []):
            if link.get("type", "").startswith("audio"):
                audio_url = link["href"]
                break
        if not audio_url and entry.get("enclosures"):
            audio_url = entry.enclosures[0].get("href")
        if audio_url:
            episodes.append({
                "guid": entry.get("id", entry.get("link")),
                "title": entry.get("title", "Untitled episode"),
                "audio_url": audio_url,
                "published": entry.get("published", ""),
                "feed_transcript_tag": entry.get("podcast_transcript"),
            })
    return episodes


def download_audio(url, dest_path):
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def _strip_caption_timing(text):
    """Reduce VTT/SRT caption text to plain prose by dropping cue numbers and timestamps."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return " ".join(lines)


def fetch_feed_transcript(transcript_tag):
    """Use a podcast's own published <podcast:transcript>, if present and fetchable.

    Returns None (falling back to ASR) if there's no tag, the format isn't one
    we handle, the fetch fails, or the content looks too short to be real.
    """
    if not transcript_tag:
        return None
    url = transcript_tag.get("url")
    fmt = transcript_tag.get("type", "")
    if not url or fmt not in SUPPORTED_FEED_TRANSCRIPT_TYPES:
        return None

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    text = resp.text
    if fmt in ("text/vtt", "application/srt", "text/srt"):
        text = _strip_caption_timing(text)
    text = text.strip()

    if len(text) < MIN_FEED_TRANSCRIPT_CHARS:
        return None
    return text


def _transcribe_chunk(file_path, context=None):
    with open(file_path, "rb") as f:
        kwargs = {"model": TRANSCRIPTION_MODEL, "file": f}
        if context:
            kwargs["prompt"] = context
        result = openai_client.audio.transcriptions.create(**kwargs)
    return result.text


def transcribe_audio(file_path, context=None):
    """Transcribe audio, splitting into chunks if the file is too large.

    `context` is passed as the model's prompt hint (show/episode name) to
    improve accuracy on proper nouns without a second transcription pass.
    """
    size = os.path.getsize(file_path)
    if size <= TRANSCRIPTION_MAX_BYTES:
        return _transcribe_chunk(file_path, context=context)

    audio = AudioSegment.from_file(file_path)
    chunks = make_chunks(audio, CHUNK_MS)
    transcript_parts = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, chunk in enumerate(chunks):
            chunk_path = os.path.join(tmp_dir, f"chunk_{i}.mp3")
            chunk.export(chunk_path, format="mp3")
            transcript_parts.append(_transcribe_chunk(chunk_path, context=context))
    return " ".join(transcript_parts)


# The guidance blocks below are composed into one prompt by
# build_summary_prompt(). They're kept separate so a change to one concern can
# be diffed, and so a score movement in the eval harness can be attributed to
# a specific block. Each targets a failure mode the judge scores for, but they
# describe how to write — deliberately not how output gets graded, since the
# cheapest way to score well on "faithfulness" is to hedge everything into mush.

COVERAGE_GUIDANCE = """Cover every major topic or question the episode actually
   discusses. Do not compress a wide-ranging conversation down to a fixed number
   of bullets — a dense episode covering many distinct topics should get more
   bullets than a narrow, single-topic one. Keep each bullet to 1-2 sentences,
   but do not drop a distinct topic just to keep the list short."""

GROUNDING_GUIDANCE = """Include only what the episode actually contains. Do not add
advice, industry context, definitions, or conclusions the speakers did not state
themselves, even where they would be accurate or useful. If the episode doesn't
cover something, leave it out rather than filling the gap."""

PRECISION_GUIDANCE = """Represent claims the way they were actually made:
- Keep the speaker's level of certainty. If someone said something might work, or
  that they weren't sure, don't restate it as settled fact.
- Don't misattribute. Where it's genuinely ambiguous who said something, or where
  the host and guest disagree, make the speaker clear — but don't add names where
  the attribution is already obvious from context.
- Keep qualifiers attached to what they modify. Don't drop a caveat that changes
  what a statement means.

This is about representing the conversation precisely, not about hedging your own
writing. Write plainly and directly — don't pad the summary with "reportedly" or
"seemingly" to play it safe."""

NAMING_GUIDANCE = """For names, companies, and technical terms, follow the episode
title's spelling for anything that appears there — the title comes from the
publisher and is authoritative. The transcript is machine-generated and can
mishear names, so use it only for terms the title doesn't cover. If someone is
only ever referred to by first name, use just the first name — don't supply a
surname."""


def build_summary_prompt(show_name, episode_title, transcript):
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        print(f"  Warning: transcript is {len(transcript)} chars, truncating to {MAX_TRANSCRIPT_CHARS} for summarization")
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]

    # Guidance sits after the transcript on purpose: with ~20k tokens of
    # transcript, instructions placed before it are likelier to be lost.
    return f"""You're summarizing a podcast episode for a personal digest email. The
reader is relying on this instead of listening, so it has to be both complete and
accurate about what was actually said.

Show: {show_name}
Episode: {episode_title}

Transcript:
{transcript}

Write:
1. A 2-3 sentence overview
2. Bullet-point key takeaways. {COVERAGE_GUIDANCE}
3. Any notable quotes or resources mentioned (if any)

{GROUNDING_GUIDANCE}

{PRECISION_GUIDANCE}

{NAMING_GUIDANCE}

Keep bullets scannable. Plain text, no markdown headers.

Aim for roughly 400-800 words overall — this is a digest someone reads in a couple
of minutes, not a transcript. If the episode covers a lot of ground, still cover
all of it but tighten each bullet. Don't drop topics to hit the range, and don't
pad to reach it."""


def summarize_transcript(prompt):
    response = openai_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_completion_tokens=4000,
        reasoning_effort="low",  # this is synthesis, not multi-step reasoning — "medium" (the
                                  # default) burns the entire token budget on internal reasoning
                                  # and returns empty content on open-ended prompts
        messages=[{"role": "user", "content": prompt}],
    )
    summary = response.choices[0].message.content
    if not summary:
        raise RuntimeError(
            f"Empty summary from {SUMMARY_MODEL} (finish_reason={response.choices[0].finish_reason}, "
            f"reasoning_tokens={response.usage.completion_tokens_details.reasoning_tokens})"
        )
    return summary


def save_episode_artifact(show_name, episode, transcript, prompt, summary, transcription_source):
    """Persist everything needed to review or eval this episode's summary later."""
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in episode["title"])[:80]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = RUNS_DIR / f"{timestamp}_{safe_title}.json"
    save_json(path, {
        "show": show_name,
        "episode_title": episode["title"],
        "guid": episode["guid"],
        "published": episode["published"],
        "audio_url": episode["audio_url"],
        "transcription_source": transcription_source,
        "summary_model": SUMMARY_MODEL,
        "prompt": prompt,
        "transcript": transcript,
        "summary": summary,
        "processed_at": timestamp,
    })
    print(f"  Saved run artifact: {path.relative_to(BASE_DIR)}")


def send_digest_email(digest_sections):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["DIGEST_TO_EMAIL"]

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"Podcast digest — {time.strftime('%Y-%m-%d')}"

    body = "\n\n".join(digest_sections)
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)


def process_episode(show_name, episode):
    """Transcribe + summarize one episode, saving a runs/ artifact. Returns the summary text."""
    transcript = fetch_feed_transcript(episode.get("feed_transcript_tag"))
    if transcript:
        print("  Using transcript published in the feed (skipped audio transcription)")
        transcription_source = "feed"
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = os.path.join(tmp_dir, "episode.mp3")
            download_audio(episode["audio_url"], audio_path)
            transcript = transcribe_audio(audio_path, context=f"Podcast: {show_name}. Episode: {episode['title']}.")
        transcription_source = TRANSCRIPTION_MODEL

    prompt = build_summary_prompt(show_name, episode["title"], transcript)
    summary = summarize_transcript(prompt)
    save_episode_artifact(show_name, episode, transcript, prompt, summary, transcription_source)
    return summary


def main():
    shows = load_json(SHOWS_FILE, [])
    if not shows:
        print("No shows configured in config/shows.json — nothing to do.")
        return

    seen = load_json(STATE_FILE, {})
    digest_sections = []

    for show in shows:
        name = show["name"]
        rss_url = show["rss_url"]
        seen_guids = set(seen.get(name, []))

        episodes = get_latest_episodes(rss_url)
        new_episodes = [e for e in episodes if e["guid"] not in seen_guids]

        for ep in new_episodes:
            print(f"Processing new episode: {name} — {ep['title']}")
            try:
                summary = process_episode(name, ep)
                digest_sections.append(f"=== {name}: {ep['title']} ===\n{summary}")
                seen_guids.add(ep["guid"])
            except Exception as e:
                # Don't let one bad episode kill the whole run
                print(f"Failed to process {name} — {ep['title']}: {e}")

        seen[name] = list(seen_guids)

    if digest_sections:
        send_digest_email(digest_sections)
        print(f"Sent digest with {len(digest_sections)} episode(s).")
    else:
        print("No new episodes found.")

    save_json(STATE_FILE, seen)


if __name__ == "__main__":
    main()
