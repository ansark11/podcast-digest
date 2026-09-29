"""
Backfill: summarize past episodes into summaries/ (and transcripts/),
including ones older than the weekly run's lookback window. Use it to
populate the app with older episodes, or to give the eval tooling more
source material.

Usage:
  python scripts/backfill.py --count 12
  python scripts/backfill.py --count 5 --show "Lenny's Podcast"
"""

import argparse

import podcast_digest as pd


def backfill(show_name_filter, count):
    shows = pd.load_json(pd.SHOWS_FILE, [])

    for show in shows:
        name = show["name"]
        if show_name_filter and name != show_name_filter:
            continue

        # Each episode's summary file is written as soon as it's done, so an
        # interrupted run loses nothing completed — re-running the same
        # command just picks up where it left off.
        done = pd.summarized_guids(name)
        episodes = pd.get_latest_episodes(show["rss_url"], limit=count + len(done) + 5)
        candidates = [e for e in episodes if e["guid"] not in done][:count]

        print(f"Backfilling {len(candidates)} episode(s) for {name}")
        for ep in candidates:
            print(f"Processing: {name} — {ep['title']}")
            try:
                pd.process_episode(name, ep)
            except Exception as e:
                print(f"  Failed: {e}")

    pd.refresh_metadata(shows)
    print("Backfill complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=12, help="Episodes to backfill per show")
    parser.add_argument("--show", default=None, help="Only backfill this show (default: all configured shows)")
    args = parser.parse_args()
    backfill(args.show, args.count)
