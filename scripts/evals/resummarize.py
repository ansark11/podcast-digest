"""
Re-summarize episodes with the current build_summary_prompt, without
re-transcribing, to A/B test a prompt change against the exact same source
material.

Sources are the committed production files: every summaries/<show>/*.json
and its transcript under transcripts/. Output goes ONLY to evals/runs/<tag>/,
never to summaries/, so an experiment can't change what the app shows. To
make a take the official one, re-run the production pipeline for it (or copy
it over deliberately).

Pair a take with its counterparts by filename: the same episode has the same
filename in every evals/runs/<tag>/ folder.

Usage:
  python scripts/evals/resummarize.py --tag v6
  python scripts/evals/resummarize.py --tag v6 --show "Lenny's Podcast"
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import podcast_digest as pd  # noqa: E402

EVAL_RUNS_DIR = pd.BASE_DIR / "evals" / "runs"


def main(tag, show_filter):
    summary_files = sorted(pd.SUMMARIES_DIR.glob("*/*.json"))
    if not summary_files:
        print(f"No summaries found in {pd.SUMMARIES_DIR}.")
        return

    out_dir = EVAL_RUNS_DIR / tag
    for summary_path in summary_files:
        record = json.loads(summary_path.read_text())
        if show_filter and record["show"] != show_filter:
            continue
        out_path = out_dir / summary_path.name
        if out_path.exists():
            print(f"Skipping (already in {tag}): {record['episode_title']}")
            continue

        transcript = (pd.BASE_DIR / record["transcript_path"]).read_text()
        print(f"Re-summarizing [{tag}]: {record['episode_title']}")
        prompt = pd.build_summary_prompt(record["show"], record["episode_title"], transcript)
        try:
            summary = pd.summarize_transcript(prompt)
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        pd.save_json(out_path, {
            "show": record["show"],
            "episode_title": record["episode_title"],
            "guid": record["guid"],
            "published": record["published"],
            "audio_url": record["audio_url"],
            "transcription_source": record["transcription_source"],
            "summary_model": pd.SUMMARY_MODEL,
            "prompt_version": tag,
            "prompt": prompt,
            "transcript": transcript,
            # The judge grades plain text, so every eval run carries the same
            # rendering the digest email uses, plus the structured original.
            "summary": pd.render_summary_text(summary),
            "summary_structured": summary,
            "processed_at": time.strftime("%Y%m%d_%H%M%S"),
        })
        print(f"  Saved {out_path.relative_to(pd.BASE_DIR)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="Label for this prompt version, e.g. v6")
    parser.add_argument("--show", default=None, help="Only this show (default: all)")
    args = parser.parse_args()
    main(args.tag, args.show)
