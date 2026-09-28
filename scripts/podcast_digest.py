"""
Podcast Digest Pipeline
------------------------
Checks a fixed list of podcast RSS feeds for new episodes. If a feed
publishes a usable Podcasting 2.0 <podcast:transcript> tag, that's used
directly; otherwise audio is downloaded and transcribed with OpenAI's
gpt-transcribe. Each transcript is summarized with GPT-5.6 Luna, and
one digest email covers everything new. Designed to run on a schedule
via GitHub Actions (see .github/workflows/podcast-digest.yml).

Each processed episode produces two committed files, one per episode:
  summaries/<show>/<date>_<slug>.json   structured summary + episode metadata
                                        (what the web app reads)
  transcripts/<show>/<date>_<slug>.txt  the source transcript
Re-processing an episode overwrites its files (matched by guid), so there
is only ever one summary per episode. Experimental re-summaries for evals
live separately under evals/ (see scripts/evals/).

Required environment variables (set as GitHub Actions secrets):
  OPENAI_API_KEY       - OpenAI API key (transcription and summarization)
  GMAIL_ADDRESS        - Gmail address to send from
  GMAIL_APP_PASSWORD   - Gmail app password (not your normal password)
  DIGEST_TO_EMAIL      - Where the digest should be sent
"""

import os
import re
import json
import time
import argparse
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
SUMMARIES_DIR = BASE_DIR / "summaries"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

TRANSCRIPTION_MAX_BYTES = 24 * 1024 * 1024  # stay safely under the 25MB API limit
CHUNK_MS = 10 * 60 * 1000                   # split long episodes into 10-minute chunks
MAX_NEW_EPISODES_PER_SHOW = 3               # safety cap per run, per show
MAX_TRANSCRIPT_CHARS = 400_000              # well under Luna's ~1M token context window
MIN_FEED_TRANSCRIPT_CHARS = 500             # below this, treat a feed transcript as bogus/placeholder
TRANSCRIPTION_MODEL = "gpt-transcribe"
SUMMARY_MODEL = "gpt-5.6-luna"
# Bump whenever build_summary_prompt() or SUMMARY_SCHEMA changes, so every
# summary records which prompt produced it. v5 = v4's guidance blocks plus
# structured (JSON) output.
PROMPT_VERSION = "v5"
SUMMARY_FILE_SCHEMA_VERSION = 1
SUPPORTED_FEED_TRANSCRIPT_TYPES = {"text/plain", "text/vtt", "application/srt", "text/srt"}

# Built lazily so tasks that make no API calls (metadata refresh, eval
# reports) don't need an OpenAI key.
_openai_client = None


def get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def slugify(text, max_len=60):
    text = re.sub(r"['\u2019]", "", text)  # "Lenny's" -> "lennys", not "lenny-s"
    text = re.sub(r"[^\w\s]", " ", text)   # dashes/punctuation separate words before ASCII folding
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len].rstrip("-") or "untitled"


def to_iso_date(published):
    """RSS dates are RFC 2822 strings; store ISO 8601 so the app can sort them."""
    try:
        return parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def parse_duration(raw):
    """itunes:duration is either seconds ("3600") or H:MM:SS / MM:SS."""
    if not raw:
        return None
    try:
        parts = [int(p) for p in str(raw).strip().split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds or None


def _entry_metadata(entry, show_artwork_url):
    """Display metadata for the web app: artwork, show notes, duration, link."""
    image = entry.get("image")
    artwork_url = image.get("href") if isinstance(image, dict) else None
    content = entry.get("content") or []
    description_html = (content[0].get("value") if content else None) or entry.get("summary") or ""
    return {
        "episode_url": entry.get("link"),
        "artwork_url": artwork_url or show_artwork_url,
        "description_html": description_html,
        "duration_seconds": parse_duration(entry.get("itunes_duration")),
    }


def _show_artwork(feed):
    image = feed.feed.get("image")
    return image.get("href") if isinstance(image, dict) else None


def get_latest_episodes(rss_url, limit=MAX_NEW_EPISODES_PER_SHOW):
    """Pull the most recent episodes from a show's RSS feed."""
    feed = feedparser.parse(rss_url)
    show_artwork_url = _show_artwork(feed)
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
                **_entry_metadata(entry, show_artwork_url),
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
        result = get_openai_client().audio.transcriptions.create(**kwargs)
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
    return f"""You're summarizing a podcast episode for a personal digest (an email and a
reading app). The reader is relying on this instead of listening, so it has to be both complete and
accurate about what was actually said.

Show: {show_name}
Episode: {episode_title}

Transcript:
{transcript}

Return these fields:
- overview: a 2-3 sentence overview of the episode.
- takeaways: the key takeaways. {COVERAGE_GUIDANCE}
  Give each takeaway a short heading (2-6 words naming the topic) and the
  takeaway itself as the detail.
- quotes: notable quotes, word for word as said in the episode, with the
  speaker's name. Use null for the speaker only if it's genuinely unclear.
  Empty list if nothing stands out.
- resources: books, tools, products, shows, articles, and other resources
  mentioned, each with its kind. Empty list if none.

{GROUNDING_GUIDANCE}

{PRECISION_GUIDANCE}

{NAMING_GUIDANCE}

Write plain text in every field: no markdown, asterisks, or bullet characters.

Aim for roughly 400-800 words overall — this is a digest someone reads in a couple
of minutes, not a transcript. If the episode covers a lot of ground, still cover
all of it but tighten each takeaway. Don't drop topics to hit the range, and don't
pad to reach it."""


RESOURCE_KINDS = ["book", "tool", "product", "company", "show", "article", "person", "other"]

# Enforced by OpenAI structured outputs (strict mode), so the response always
# parses and always has every field the web app expects.
SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "takeaways", "quotes", "resources"],
    "properties": {
        "overview": {"type": "string"},
        "takeaways": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["heading", "detail"],
                "properties": {"heading": {"type": "string"}, "detail": {"type": "string"}},
            },
        },
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "speaker"],
                "properties": {"text": {"type": "string"}, "speaker": {"type": ["string", "null"]}},
            },
        },
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "kind"],
                "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": RESOURCE_KINDS}},
            },
        },
    },
}


def summarize_transcript(prompt):
    """Return the summary as a dict matching SUMMARY_SCHEMA."""
    response = get_openai_client().chat.completions.create(
        model=SUMMARY_MODEL,
        max_completion_tokens=4000,
        reasoning_effort="low",  # this is synthesis, not multi-step reasoning — "medium" (the
                                  # default) burns the entire token budget on internal reasoning
                                  # and returns empty content on open-ended prompts
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "episode_summary", "strict": True, "schema": SUMMARY_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(
            f"Empty summary from {SUMMARY_MODEL} (finish_reason={response.choices[0].finish_reason}, "
            f"reasoning_tokens={response.usage.completion_tokens_details.reasoning_tokens})"
        )
    return json.loads(content)


def render_summary_text(summary):
    """Plain-text version of a structured summary, for the email and the eval judge."""
    lines = [summary["overview"], ""]
    for t in summary["takeaways"]:
        lines.append(f"- {t['heading']}: {t['detail']}" if t.get("heading") else f"- {t['detail']}")
    if summary["quotes"]:
        lines += ["", "Notable quotes:"]
        for q in summary["quotes"]:
            lines.append(f"- \u201c{q['text']}\u201d" + (f" ({q['speaker']})" if q.get("speaker") else ""))
    if summary["resources"]:
        lines += ["", "Resources mentioned: " + ", ".join(r["name"] for r in summary["resources"])]
    return "\n".join(lines)


def find_summary_path(show_slug, guid):
    """Existing summary file for this episode, if any — so a re-run overwrites it."""
    show_dir = SUMMARIES_DIR / show_slug
    if not show_dir.exists():
        return None
    for path in show_dir.glob("*.json"):
        if load_json(path, {}).get("guid") == guid:
            return path
    return None


def episode_file_stem(episode):
    published = to_iso_date(episode.get("published", ""))
    date = published[:10] if published else time.strftime("%Y-%m-%d")
    return f"{date}_{slugify(episode['title'])}"


def build_summary_record(show_name, episode, summary, transcription_source, transcript_path,
                         prompt_version=PROMPT_VERSION, generated_at=None):
    return {
        "schema_version": SUMMARY_FILE_SCHEMA_VERSION,
        "guid": episode["guid"],
        "show": show_name,
        "show_slug": slugify(show_name),
        "episode_title": episode["title"],
        "published": to_iso_date(episode.get("published", "")),
        "audio_url": episode.get("audio_url"),
        "episode_url": episode.get("episode_url"),
        "artwork_url": episode.get("artwork_url"),
        "description_html": episode.get("description_html"),
        "duration_seconds": episode.get("duration_seconds"),
        "transcript_path": transcript_path,
        "transcription_source": transcription_source,
        "summary_model": SUMMARY_MODEL,
        "prompt_version": prompt_version,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
    }


def save_episode_outputs(show_name, episode, transcript, summary, transcription_source):
    """Write the episode's summary and transcript, replacing any earlier version."""
    show_slug = slugify(show_name)
    summary_path = find_summary_path(show_slug, episode["guid"]) or (
        SUMMARIES_DIR / show_slug / f"{episode_file_stem(episode)}.json"
    )
    transcript_path = TRANSCRIPTS_DIR / show_slug / f"{summary_path.stem}.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(transcript)

    record = build_summary_record(
        show_name, episode, summary, transcription_source,
        str(transcript_path.relative_to(BASE_DIR)),
    )
    save_json(summary_path, record)
    print(f"  Saved {summary_path.relative_to(BASE_DIR)}")
    return record


METADATA_FIELDS = ("episode_url", "artwork_url", "description_html", "duration_seconds")


def refresh_metadata(shows):
    """Fill in artwork, show notes, duration and link for any summary missing them.

    Runs every pipeline run (one feed fetch per show, no API cost), so older
    summaries, or ones written while the feed lacked a field, catch up.
    """
    for show in shows:
        show_dir = SUMMARIES_DIR / slugify(show["name"])
        paths = sorted(show_dir.glob("*.json")) if show_dir.exists() else []
        stale = [p for p in paths if any(not load_json(p, {}).get(f) for f in METADATA_FIELDS)]
        if not stale:
            continue

        feed = feedparser.parse(show["rss_url"])
        show_artwork_url = _show_artwork(feed)
        by_guid = {e.get("id", e.get("link")): e for e in feed.entries}
        updated = 0
        for path in stale:
            record = load_json(path, {})
            entry = by_guid.get(record.get("guid"))
            if entry is None:
                continue
            fresh = _entry_metadata(entry, show_artwork_url)
            changed = False
            for field in METADATA_FIELDS:
                if not record.get(field) and fresh.get(field):
                    record[field] = fresh[field]
                    changed = True
            if changed:
                save_json(path, record)
                updated += 1
        print(f"Metadata: updated {updated} of {len(stale)} incomplete summary file(s) for {show['name']}")


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
    """Transcribe + summarize one episode, saving its summary and transcript. Returns the summary dict."""
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
    save_episode_outputs(show_name, episode, transcript, summary, transcription_source)
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
                link = f"\n{ep['episode_url']}" if ep.get("episode_url") else ""
                digest_sections.append(f"=== {name}: {ep['title']} ==={link}\n\n{render_summary_text(summary)}")
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

    try:
        refresh_metadata(shows)
    except Exception as e:
        print(f"Metadata refresh failed (summaries are unaffected): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Podcast digest pipeline")
    parser.add_argument("--refresh-metadata-only", action="store_true",
                        help="Only fill in missing artwork/show notes/duration on existing summaries")
    args = parser.parse_args()
    if args.refresh_metadata_only:
        refresh_metadata(load_json(SHOWS_FILE, []))
    else:
        main()
