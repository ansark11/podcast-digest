"""
Re-summarize existing runs/ transcripts with the current build_summary_prompt,
without re-transcribing, to A/B test a prompt change against the exact same
source material. Writes new artifacts (title prefixed "[reprompt]" — prefixed
rather than suffixed so the marker survives filename/display truncation on
long titles) rather than overwriting the originals. Pair originals with their
reprompt counterpart by guid (shared across both), not by title, since
truncation can still make the marker invisible in a filename listing.

Usage: python scripts/resummarize_runs.py
"""

import json

import podcast_digest as pd


def main():
    run_files = sorted(pd.RUNS_DIR.glob("*.json"))
    originals = [f for f in run_files if "[reprompt]" not in f.stem]

    if not originals:
        print(f"No run artifacts found in {pd.RUNS_DIR}.")
        return

    for run_path in originals:
        run = json.loads(run_path.read_text())
        print(f"Re-summarizing: {run['episode_title']}")

        prompt = pd.build_summary_prompt(run["show"], run["episode_title"], run["transcript"])
        summary = pd.summarize_transcript(prompt)

        episode = {
            "title": f"[reprompt] {run['episode_title']}",
            "guid": run["guid"],
            "published": run["published"],
            "audio_url": run["audio_url"],
        }
        pd.save_episode_artifact(
            run["show"], episode, run["transcript"], prompt, summary, run["transcription_source"]
        )


if __name__ == "__main__":
    main()
