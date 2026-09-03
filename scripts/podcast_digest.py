"""
Podcast Digest Pipeline
------------------------
Checks a fixed list of podcast RSS feeds for new episodes, transcribes
new audio with the OpenAI Whisper API, summarizes each transcript with
GPT-5.6 Luna, and emails a digest. Designed to run on a schedule via
GitHub Actions (see .github/workflows/podcast-digest.yml).

Each processed episode's transcript, exact prompt, and raw summary are
saved to runs/<timestamp>_<title>.json for review and eval purposes.

Required environment variables (set as GitHub Actions secrets):
  OPENAI_API_KEY       - OpenAI API key (for Whisper transcription and summarization)
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

WHISPER_MAX_BYTES = 24 * 1024 * 1024  # stay safely under the 25MB API limit
CHUNK_MS = 10 * 60 * 1000             # split long episodes into 10-minute chunks
MAX_NEW_EPISODES_PER_SHOW = 3         # safety cap per run, per show
SUMMARY_MODEL = "gpt-5.6-luna"

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
            })
    return episodes


def download_audio(url, dest_path):
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def _transcribe_chunk(file_path):
    with open(file_path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
        )
    return result.text


def transcribe_audio(file_path):
    """Transcribe audio with Whisper, splitting into chunks if the file is too large."""
    size = os.path.getsize(file_path)
    if size <= WHISPER_MAX_BYTES:
        return _transcribe_chunk(file_path)

    audio = AudioSegment.from_file(file_path)
    chunks = make_chunks(audio, CHUNK_MS)
    transcript_parts = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, chunk in enumerate(chunks):
            chunk_path = os.path.join(tmp_dir, f"chunk_{i}.mp3")
            chunk.export(chunk_path, format="mp3")
            transcript_parts.append(_transcribe_chunk(chunk_path))
    return " ".join(transcript_parts)


def build_summary_prompt(show_name, episode_title, transcript):
    return f"""You're summarizing a podcast episode for a personal digest email.

Show: {show_name}
Episode: {episode_title}

Transcript:
{transcript[:100000]}

Write:
1. A 2-3 sentence overview
2. 5-8 bullet-point key takeaways
3. Any notable quotes or resources mentioned (if any)

Keep it concise and scannable. Plain text, no markdown headers."""


def summarize_transcript(prompt):
    response = openai_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_completion_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def save_episode_artifact(show_name, episode, transcript, prompt, summary):
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
        "transcription_model": "whisper-1",
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
                with tempfile.TemporaryDirectory() as tmp_dir:
                    audio_path = os.path.join(tmp_dir, "episode.mp3")
                    download_audio(ep["audio_url"], audio_path)
                    transcript = transcribe_audio(audio_path)
                    prompt = build_summary_prompt(name, ep["title"], transcript)
                    summary = summarize_transcript(prompt)

                save_episode_artifact(name, ep, transcript, prompt, summary)
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
