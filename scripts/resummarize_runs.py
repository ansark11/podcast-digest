"""
Re-summarize existing runs/ transcripts with the current build_summary_prompt,
without re-transcribing, to A/B test a prompt change against the exact same
source material.

Each batch gets a --tag naming the prompt version, written as a "[tag] " title
prefix. Prefixed rather than suffixed so the marker survives filename/display
truncation on long titles. Untagged artifacts are the originals; any title
already starting with "[" is a previous batch and is skipped as a source, so
you never summarize a summary's sibling twice.

Pair a run with its counterpart by guid (shared across every version of the
same episode), not by title — truncation can still hide the marker in a
filename listing.

Usage:
  python scripts/resummarize_runs.py --tag v3
  python scripts/resummarize_runs.py --tag grounding-precision-naming
"""

import argparse
import json

import podcast_digest as pd


def main(tag):
    run_files = sorted(pd.RUNS_DIR.glob("*.json"))
    originals = [f for f in run_files if not json.loads(f.read_text())["episode_title"].startswith("[")]

    if not originals:
        print(f"No untagged run artifacts found in {pd.RUNS_DIR}.")
        return

    print(f"Re-summarizing {len(originals)} episode(s) with tag [{tag}]")
    for run_path in originals:
        run = json.loads(run_path.read_text())
        print(f"Re-summarizing: {run['episode_title']}")

        prompt = pd.build_summary_prompt(run["show"], run["episode_title"], run["transcript"])
        summary = pd.summarize_transcript(prompt)

        episode = {
            "title": f"[{tag}] {run['episode_title']}",
            "guid": run["guid"],
            "published": run["published"],
            "audio_url": run["audio_url"],
        }
        pd.save_episode_artifact(
            run["show"], episode, run["transcript"], prompt, summary, run["transcription_source"]
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="Label for this prompt version, e.g. v3")
    args = parser.parse_args()
    main(args.tag)
